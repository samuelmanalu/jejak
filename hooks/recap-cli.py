#!/usr/bin/env python3
"""
Day-recap data collector.

Gathers everything that happened in a time window from the three local sources:
  1. Neo4j     — Memory / Session / Project nodes written by the Jejak hooks
  2. MySQL     — prompt_logs rows written by prompt-logger.py
  3. git       — commits authored in the window across local repo roots

Prints a compact plain-text report (or --json) for a model to synthesize into a
narrative recap. Never raises on a dead source: each section degrades to a
"[unavailable]" line so a recap still works with partial data.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
import jejak_common as jejak  # noqa: E402

DEFAULT_ROOTS = os.environ.get("JEJAK_RECAP_ROOTS", "~/projects:~/dev:~/src:~/code:~/repos:~/IdeaProjects:~/.claude").split(":")
PROMPT_CLIP = 220
MAX_PROMPTS_PER_PROJECT = 12
MAX_MEMORIES_PER_PROJECT = 25


# --- window -----------------------------------------------------------------

def resolve_window(args):
    if args.since:
        start = datetime.fromisoformat(args.since)
        end = datetime.fromisoformat(args.until) if args.until else datetime.now()
        return start, end, "custom"
    base = datetime.fromisoformat(args.date).replace(hour=0, minute=0, second=0, microsecond=0) \
        if args.date else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if args.yesterday:
        base -= timedelta(days=1)
    days = args.days or 1
    start = base - timedelta(days=days - 1)
    end = base + timedelta(days=1) if (args.date or args.yesterday) else datetime.now()
    label = "today" if days == 1 and not args.date and not args.yesterday else f"{days}d"
    return start, end, label


# --- sources ----------------------------------------------------------------

def _to_utc(dt):
    """Neo4j datetime() stores server-UTC; the window is local wall-clock."""
    return dt + (datetime.utcnow() - datetime.now())


def _to_local(iso):
    """Render a UTC ISO string from Neo4j back into local wall-clock HH:MM."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso[:19]) - (datetime.utcnow() - datetime.now())
        return d.strftime("%H:%M")
    except Exception:
        return iso[:16]


def collect_knowledge(start, end):
    from neo4j import GraphDatabase
    cfg = jejak.load_config("neo4j")
    try:
        driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]),
                                      notifications_min_severity="OFF")
    except TypeError:  # older driver without notification filtering
        driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    us, ue = _to_utc(start).isoformat(), _to_utc(end).isoformat()
    out = {"projects": {}, "sessions": [], "prompt_memories": 0}
    try:
        with driver.session() as s:
            rows = list(s.run(
                """
                MATCH (m:Memory)
                WHERE m.created_at >= datetime($start) AND m.created_at < datetime($end)
                  AND coalesce(m.superseded, false) = false
                  AND m.type <> 'prompt'
                OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
                OPTIONAL MATCH (m)-[:RELATES_TO]-(o:Memory) WHERE o.type <> 'prompt'
                RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                       m.source_file AS source_file, m.relevance_score AS score,
                       toString(m.created_at) AS created_at,
                       collect(DISTINCT t.name) AS topics,
                       coalesce(head(collect(DISTINCT p.name)), 'unassigned') AS project,
                       count(DISTINCT o) AS links
                """,
                start=us, end=ue,
            ))
            for r in sorted(rows, key=lambda r: r["created_at"] or ""):
                out["projects"].setdefault(r["project"], []).append({
                    "id": (r["id"] or "")[:8],
                    "type": r["type"],
                    "content": r["content"],
                    "source_file": r["source_file"],
                    "score": round(r["score"], 1) if r["score"] else None,
                    "links": r["links"],
                    "at": _to_local(r["created_at"]),
                    "topics": [t for t in r["topics"] if t],
                })
            out["prompt_memories"] = s.run(
                """
                MATCH (m:Memory {type: 'prompt'})
                WHERE m.created_at >= datetime($start) AND m.created_at < datetime($end)
                RETURN count(m) AS n
                """, start=us, end=ue).single()["n"]
            sess = list(s.run(
                """
                MATCH (s:Session)
                WHERE s.last_active >= datetime($start) AND s.last_active < datetime($end)
                OPTIONAL MATCH (m:Memory)-[:IN_SESSION]->(s)
                  WHERE m.type <> 'prompt' AND m.created_at >= datetime($start)
                    AND m.created_at < datetime($end)
                OPTIONAL MATCH (m2:Memory)-[:IN_SESSION]->(s)
                  WHERE m2.created_at >= datetime($start) AND m2.created_at < datetime($end)
                OPTIONAL MATCH (m2)-[:IN_PROJECT]->(p:Project)
                RETURN s.session_id AS id, toString(s.last_active) AS last_active,
                       s.machine_name AS machine, count(DISTINCT m) AS memories,
                       collect(DISTINCT p.name) AS projects
                """, start=us, end=ue))
            out["sessions"] = [
                {"id": (r["id"] or "")[:8], "last_active": _to_local(r["last_active"]),
                 "machine": r["machine"], "memories": r["memories"],
                 "projects": [p for p in r["projects"] if p]}
                for r in sorted(sess, key=lambda r: r["last_active"] or "")
            ]
    finally:
        driver.close()
    return out


def collect_prompts(start, end):
    import mysql.connector
    cfg = jejak.load_config("mysql")
    conn = mysql.connector.connect(**cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id, cwd, prompt, tags, created_at FROM prompt_logs "
            "WHERE created_at >= %s AND created_at < %s ORDER BY created_at ASC",
            (start, end),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    by_project = {}
    for session_id, cwd, prompt, tags, created_at in rows:
        name = jejak.extract_project_name(cwd)
        entry = by_project.setdefault(name, {"cwds": set(), "tags": {}, "prompts": [], "total": 0})
        entry["cwds"].add(cwd)
        entry["total"] += 1
        try:
            for t in json.loads(tags):
                entry["tags"][t] = entry["tags"].get(t, 0) + 1
        except Exception:
            pass
        entry["prompts"].append({
            "at": created_at.strftime("%H:%M") if created_at else "",
            "session": (session_id or "")[:8],
            "text": " ".join((prompt or "").split())[:PROMPT_CLIP],
        })
    for entry in by_project.values():
        entry["cwds"] = sorted(entry["cwds"])
    return by_project


def git_repos(roots):
    found = []
    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        if os.path.isdir(os.path.join(root, ".git")):
            found.append(root)
        try:
            out = subprocess.run(
                ["find", root, "-maxdepth", "5", "-name", ".git", "-type", "d"],
                capture_output=True, text=True, timeout=60,
            ).stdout
        except Exception:
            continue
        for line in out.splitlines():
            if line.endswith("/.git"):
                found.append(line[: -len("/.git")])
    return sorted(set(found))


def collect_git(start, end, roots, author):
    repos, results = git_repos(roots), []
    since, until = start.isoformat(), end.isoformat()
    for repo in repos:
        try:
            log = subprocess.run(
                ["git", "-C", repo, "log", "--all", "--no-merges",
                 f"--since={since}", f"--until={until}", f"--author={author}",
                 "--date=format:%H:%M", "--pretty=%h|%ad|%d|%s", "--shortstat"],
                capture_output=True, text=True, timeout=20,
            ).stdout.strip()
            if not log:
                continue
            commits, pending = [], None
            for line in log.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "|" in line and line.count("|") >= 3:
                    if pending:
                        commits.append(pending)
                    sha, at, refs, subject = line.split("|", 3)
                    pending = {"sha": sha, "at": at, "refs": refs.strip(), "subject": subject, "stat": ""}
                elif pending is not None:
                    pending["stat"] = line
            if pending:
                commits.append(pending)
            branch = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True, timeout=10).stdout.strip()
            dirty = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=15).stdout.strip()
            results.append({
                "repo": repo.replace(os.path.expanduser("~"), "~"),
                "branch": branch,
                "dirty_files": len(dirty.splitlines()) if dirty else 0,
                "commits": commits,
            })
        except Exception:
            continue
    return results


# --- rendering --------------------------------------------------------------

def render(data):
    L = []
    w = data["window"]
    L.append(f"# RECAP WINDOW: {w['start']} -> {w['end']}  ({w['label']})")

    L.append("\n## JEJAK (memories saved in window)")
    k = data["knowledge"]
    if isinstance(k, str):
        L.append(f"[unavailable] {k}")
    else:
        total = sum(len(v) for v in k["projects"].values())
        L.append(f"total: {total} knowledge memories across {len(k['projects'])} project(s); "
                 f"{k.get('prompt_memories', 0)} prompt nodes; {len(k['sessions'])} active session(s)")
        for se in k["sessions"]:
            L.append(f"    session {se['id']} last active {se['last_active']} on {se['machine']} "
                     f"- {se['memories']} knowledge item(s) - {', '.join(se['projects']) or 'no project'}")
        for proj, mems in sorted(k["projects"].items(), key=lambda x: -len(x[1])):
            L.append(f"\n### {proj} ({len(mems)})")
            for m in mems[:MAX_MEMORIES_PER_PROJECT]:
                src = f"  [file: {m['source_file']}]" if m.get("source_file") else ""
                tp = f"  {{{', '.join(m['topics'])}}}" if m["topics"] else ""
                score = f"  <score {m['score']}, {m['links']} link(s)>" if m.get("score") else ""
                L.append(f"- {m['at']} [{m['type']}] ({m['id']}) {m['content']}{src}{tp}{score}")
            if len(mems) > MAX_MEMORIES_PER_PROJECT:
                L.append(f"- ... {len(mems) - MAX_MEMORIES_PER_PROJECT} more (use /jejak map)")

    L.append("\n## PROMPTS (what was asked, by project)")
    p = data["prompts"]
    if isinstance(p, str):
        L.append(f"[unavailable] {p}")
    elif not p:
        L.append("none")
    else:
        L.append(f"total: {sum(v['total'] for v in p.values())} prompts across {len(p)} project(s)")
        for proj, e in sorted(p.items(), key=lambda x: -x[1]["total"]):
            tags = ", ".join(f"{t}:{c}" for t, c in sorted(e["tags"].items(), key=lambda x: -x[1])[:6])
            L.append(f"\n### {proj} ({e['total']} prompts) tags[{tags}]")
            L.append(f"    dirs: {', '.join(d.replace(os.path.expanduser('~'), '~') for d in e['cwds'])}")
            step = max(1, len(e["prompts"]) // MAX_PROMPTS_PER_PROJECT)
            for pr in e["prompts"][::step][:MAX_PROMPTS_PER_PROJECT]:
                L.append(f"- {pr['at']} ({pr['session']}) {pr['text']}")

    L.append("\n## GIT ACTIVITY")
    g = data["git"]
    if isinstance(g, str):
        L.append(f"[unavailable] {g}")
    elif not g:
        L.append("no commits authored in window")
    else:
        L.append(f"total: {sum(len(r['commits']) for r in g)} commit(s) in {len(g)} repo(s)")
        for r in g:
            L.append(f"\n### {r['repo']}  (on {r['branch']}, {r['dirty_files']} uncommitted file(s))")
            for c in r["commits"]:
                stat = f"    [{c['stat']}]" if c["stat"] else ""
                refs = f" {c['refs']}" if c["refs"] else ""
                L.append(f"- {c['at']} {c['sha']}{refs} {c['subject']}{stat}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Collect a day's work from Jejak, prompt log, and git.")
    ap.add_argument("--date", help="Recap this calendar date (YYYY-MM-DD)")
    ap.add_argument("--yesterday", action="store_true")
    ap.add_argument("--days", type=int, help="Number of days back to include (default 1)")
    ap.add_argument("--since", help="Explicit ISO start timestamp")
    ap.add_argument("--until", help="Explicit ISO end timestamp")
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS, help="Git roots to scan")
    ap.add_argument("--author", default=None, help="Git author filter (default: git config user.email)")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    start, end, label = resolve_window(args)
    author = args.author or subprocess.run(
        ["git", "config", "--global", "user.email"], capture_output=True, text=True
    ).stdout.strip() or os.environ.get("USER", "")

    data = {"window": {"start": start.isoformat(sep=" ", timespec="minutes"),
                       "end": end.isoformat(sep=" ", timespec="minutes"),
                       "label": label, "git_author": author}}
    for key, fn in (("knowledge", lambda: collect_knowledge(start, end)),
                    ("prompts", lambda: collect_prompts(start, end)),
                    ("git", lambda: [] if args.no_git else collect_git(start, end, args.roots, author))):
        try:
            data[key] = fn()
        except Exception as e:
            data[key] = f"{type(e).__name__}: {e}"
            jejak.log_error(f"recap-cli.py:{key}", e)

    print(json.dumps(data, indent=1, default=str) if args.json else render(data))


main()

#!/usr/bin/env python3
"""
Jejak sync — move knowledge between machines.

    jejak-sync export [-o FILE] [--include-prompts] [--since YYYY-MM-DD] [--project DIR]
    jejak-sync import FILE [--dry-run]
    jejak-sync inspect FILE

    jejak-sync backup  [-o FILE]          full snapshot: moving laptops
    jejak-sync restore FILE [--dry-run]

Two modes, deliberately separate:

  export/import  — knowledge only, for syncing two machines you both use.
                   Prompts excluded, small, merge-oriented.
  backup/restore — everything: all memories INCLUDING prompts, the MySQL
                   prompt log, and your Jejak config. For moving to a new
                   laptop or keeping a real backup. Restore is still a
                   merge, so it is safe to run onto a machine in use.

Neither carries db-config.json. Credentials are yours to move by hand.

Design notes, because they matter for correctness:

  * memory_id is a UUID and is the merge key. Import is idempotent and
    order-independent: re-importing the same bundle changes nothing.
  * RELATES_TO is NOT exported. It is derived entirely from shared topics
    (see jejak.py), and on a real graph it outnumbers the memories ~150:1.
    It is rebuilt locally after import.
  * relevance_score is NOT exported. It is derived from hits, spread and
    connection count, all of which change when two graphs merge. It is
    recomputed locally with the canonical formula in jejak_common.
  * Project paths are absolute and machine-specific. They are written as
    ~/-relative on export and expanded to the local HOME on import, so a
    different username or layout does not fork the Project nodes.
  * Conflicts resolve by updated_at (newest wins). `superseded` is
    monotonic: once an entry is superseded anywhere, it stays superseded.
"""
import argparse
import gzip
import io
import json
import os
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
import jejak_common as jejak  # noqa: E402
from neo4j import GraphDatabase  # noqa: E402

FORMAT_VERSION = 1
HOME = os.path.expanduser("~")
BEGIN_MARK = "<!-- JEJAK:BEGIN"
END_MARK = "<!-- JEJAK:END -->"

# Derived or purely local; never carried between machines.
#   relevance_score/score_updated_at/promoted_at/level - recomputed on import
#   last_accessed_at - local recency telemetry. The session hooks bump it on
#     every surfaced memory, so syncing it churns the git repo with diffs that
#     carry no knowledge. hit_count IS synced (it only moves on a real re-save)
#     and merges as max(), so "this proved useful" survives across machines.
DERIVED_PROPS = {"relevance_score", "score_updated_at", "promoted_at", "level",
                 "last_accessed_at"}

# Serialized as ISO strings in the bundle; MUST be coerced back to Neo4j
# temporals on import, or duration.between() in the scoring query fails.
DATETIME_PROPS = ("created_at", "updated_at", "superseded_at")


def driver():
    cfg = jejak.load_config("neo4j")
    return GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))


def portable(path):
    """/Users/sam/dev/x -> ~/dev/x"""
    if path == HOME:
        return "~"
    if path.startswith(HOME + os.sep):
        return "~" + path[len(HOME):]
    return path


def localize(path):
    """~/dev/x -> /Users/whoever/dev/x"""
    return os.path.expanduser(path) if path.startswith("~") else path


def iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def cmd_export(args):
    out = args.output or f"jejak-{jejak.machine_name()}-{datetime.now():%Y%m%d-%H%M%S}.jsonl.gz"

    where = ["coalesce(m.type,'') <> ''"]
    params = {}
    if not args.include_prompts:
        where.append("m.type <> 'prompt'")
    if args.since:
        where.append("m.created_at >= datetime($since)")
        params["since"] = f"{args.since}T00:00:00Z"
    if args.project:
        where.append("EXISTS { MATCH (m)-[:IN_PROJECT]->(p:Project {path:$ppath}) }")
        params["ppath"] = os.path.abspath(os.path.expanduser(args.project))

    q = f"""
        MATCH (m:Memory)
        WHERE {' AND '.join(where)}
        OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
        OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
        OPTIONAL MATCH (m)-[:IN_SESSION]->(s:Session)
        RETURN m,
               collect(DISTINCT t.name) AS topics,
               collect(DISTINCT p.path) AS projects,
               collect(DISTINCT s.session_id) AS sessions
    """

    drv = driver()
    n = 0
    by_type = {}
    try:
        with drv.session() as session, gzip.open(out, "wt", encoding="utf-8") as fh:
            rows = list(session.run(q, **params))
            manifest = {
                "kind": "manifest",
                "format_version": FORMAT_VERSION,
                "source_machine": jejak.machine_name(),
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "memory_count": len(rows),
                "includes_prompts": bool(args.include_prompts),
                "note": "RELATES_TO and relevance_score are derived and rebuilt on import",
            }
            fh.write(json.dumps(manifest) + "\n")

            for r in rows:
                m = dict(r["m"])
                props = {k: iso(v) for k, v in m.items() if k not in DERIVED_PROPS}
                # Defence in depth: content was redacted on write, redact again on export.
                if props.get("content"):
                    props["content"] = jejak.redact(props["content"])
                if props.get("source_file"):
                    props["source_file"] = portable(props["source_file"])
                rec = {
                    "kind": "memory",
                    "props": props,
                    # collect() order is arbitrary; sort so output is reproducible
                    "topics": sorted(t for t in r["topics"] if t),
                    "projects": sorted(portable(p) for p in r["projects"] if p),
                    "sessions": sorted(s for s in r["sessions"] if s),
                }
                fh.write(json.dumps(rec) + "\n")
                n += 1
                by_type[props.get("type", "?")] = by_type.get(props.get("type", "?"), 0) + 1
    finally:
        drv.close()

    size = os.path.getsize(out)
    print(f"Exported {n} memories -> {out}  ({size/1024:.0f} KB gzipped)")
    for t, c in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"    {t:16} {c}")
    print("\nTransfer it however you like (scp, git, USB), then on the other machine:")
    print(f"    python3 tools/jejak-sync.py import {os.path.basename(out)}")


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------

def read_bundle(path):
    manifest, records = None, []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("kind") == "manifest":
                manifest = rec
            elif rec.get("kind") == "memory":
                records.append(rec)
    if manifest is None:
        raise SystemExit(f"{path}: no manifest — not a Jejak bundle")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise SystemExit(f"{path}: format version {manifest.get('format_version')}, "
                         f"this tool speaks {FORMAT_VERSION}")
    return manifest, records


def cmd_inspect(args):
    manifest, records = read_bundle(args.file)
    print(f"source machine : {manifest['source_machine']}")
    print(f"exported at    : {manifest['exported_at']}")
    print(f"memories       : {len(records)}")
    by_type, projects = {}, set()
    for r in records:
        t = r["props"].get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1
        projects.update(r["projects"])
    for t, c in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"    {t:16} {c}")
    print(f"projects       : {len(projects)}")
    for p in sorted(projects)[:12]:
        print(f"    {p}")


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

def cmd_import(args):
    manifest, records = read_bundle(args.file)
    print(f"==> Bundle from {manifest['source_machine']} ({len(records)} memories)")

    # Honour tombstones here, not only in pull: a backup taken before a
    # `forget` still contains the deleted memory, and restoring it would
    # quietly resurrect knowledge someone deliberately removed.
    if os.path.exists(REMOTE_CFG):
        try:
            tombs = read_tombstones(json.load(open(REMOTE_CFG))["path"])
        except Exception:
            tombs = {}
        if tombs:
            before = len(records)
            records = [r for r in records if r["props"]["memory_id"] not in tombs]
            if before != len(records):
                print(f"    skipped {before - len(records)} tombstoned memory(ies) "
                      f"(deleted via `forget`, not resurrected)")

    drv = driver()
    try:
        with drv.session() as session:
            ids = [r["props"]["memory_id"] for r in records]
            existing = {}
            for i in range(0, len(ids), 500):
                for row in session.run(
                        "MATCH (m:Memory) WHERE m.memory_id IN $ids "
                        "RETURN m.memory_id AS id, toString(m.updated_at) AS u", ids=ids[i:i + 500]):
                    existing[row["id"]] = row["u"]

            new = [r for r in records if r["props"]["memory_id"] not in existing]
            updated, unchanged = [], []
            for r in records:
                mid = r["props"]["memory_id"]
                if mid not in existing:
                    continue
                incoming = str(r["props"].get("updated_at") or "")
                (updated if incoming > (existing[mid] or "") else unchanged).append(r)

            print(f"    new       : {len(new)}")
            print(f"    updated   : {len(updated)}  (incoming is newer)")
            print(f"    unchanged : {len(unchanged)}")

            if args.dry_run:
                print("\n--dry-run: nothing written.")
                for r in new[:5]:
                    print(f"    + [{r['props'].get('type')}] "
                          f"{(r['props'].get('content') or '')[:90]}")
                return

            todo = new + updated
            # Merged but never scored = an earlier import died before its derived
            # steps. Without this, every re-run reports "up to date" and they never run.
            unfinished = [r["id"] for r in session.run(
                "MATCH (m:Memory) WHERE m.type <> 'prompt' AND m.relevance_score IS NULL "
                "RETURN m.memory_id AS id")]
            if not todo and not unfinished:
                print("\nNothing to do — this machine is already up to date.")
                return
            if unfinished:
                print(f"    unfinished: {len(unfinished)}  (merged by an interrupted import; finishing)")

            for i in range(0, len(todo), 200):
                batch = [{
                    "props": r["props"],
                    "topics": r["topics"],
                    "projects": [localize(p) for p in r["projects"]],
                    "sessions": r["sessions"],
                } for r in todo[i:i + 200]]
                session.run("""
                    UNWIND $batch AS row
                    MERGE (m:Memory {memory_id: row.props.memory_id})
                    SET m += row.props,
                        m.superseded = coalesce(m.superseded, false)
                                       OR coalesce(row.props.superseded, false)
                    // ISO strings -> real temporals, else scoring's
                    // duration.between() blows up on a String.
                    SET m.created_at       = CASE WHEN row.props.created_at       IS NULL THEN m.created_at       ELSE datetime(row.props.created_at)       END,
                        m.updated_at       = CASE WHEN row.props.updated_at       IS NULL THEN m.updated_at       ELSE datetime(row.props.updated_at)       END,
                        m.superseded_at    = CASE WHEN row.props.superseded_at    IS NULL THEN m.superseded_at    ELSE datetime(row.props.superseded_at)    END,
                        // local-only: never arrives in a bundle, seed it for new nodes.
                        // Convert from row, not m.created_at: Cypher 25 evaluates every
                        // SET item before assigning, so m.created_at is still the String.
                        m.last_accessed_at = coalesce(m.last_accessed_at, datetime(row.props.created_at),
                                                      m.created_at, datetime()),
                        // usefulness is monotonic across machines
                        m.hit_count        = CASE WHEN coalesce(row.props.hit_count,0) > coalesce(m.hit_count,0)
                                                  THEN row.props.hit_count ELSE coalesce(m.hit_count,1) END
                    WITH m, row
                    CALL (m, row) {
                      UNWIND row.topics AS tname
                      MERGE (t:Topic {name: tname})
                      MERGE (m)-[:ABOUT]->(t)
                    }
                    CALL (m, row) {
                      UNWIND row.projects AS ppath
                      MERGE (p:Project {path: ppath})
                      MERGE (m)-[:IN_PROJECT]->(p)
                    }
                    CALL (m, row) {
                      UNWIND row.sessions AS sid
                      MERGE (s:Session {session_id: sid})
                      MERGE (m)-[:IN_SESSION]->(s)
                    }
                """, batch=batch)
                print(f"    merged {min(i+200, len(todo))}/{len(todo)}")

            # Projects arrive as bare paths; name them the way the hooks do.
            unnamed = [r["path"] for r in session.run(
                "MATCH (p:Project) WHERE p.name IS NULL RETURN p.path AS path")]
            if unnamed:
                session.run("""
                    UNWIND $rows AS row
                    MATCH (p:Project {path: row.path}) SET p.name = row.name
                """, rows=[{"path": p, "name": jejak.extract_project_name(p)} for p in unnamed])

            print("==> Rebuilding RELATES_TO (derived from shared topics)")
            touched = list({r["props"]["memory_id"] for r in todo} | set(unfinished))
            for i in range(0, len(touched), 100):
                session.run("""
                    UNWIND $ids AS mid
                    MATCH (m:Memory {memory_id: mid})
                    WHERE m.type <> 'prompt'
                    MATCH (m)-[:ABOUT]->(t:Topic)<-[:ABOUT]-(other:Memory)
                    WHERE other.memory_id <> m.memory_id
                      AND other.type <> 'prompt'
                      AND t.name <> $catchall
                      AND NOT exists((m)-[:RELATES_TO]-(other))
                    WITH m, other, count(t) AS shared
                    WHERE shared >= 1
                    MERGE (m)-[:RELATES_TO {strength: shared}]->(other)
                """, ids=touched[i:i + 100], catchall=jejak.CATCHALL_TOPIC)

            print("==> Recalculating relevance scores")
            jejak.recalculate_all_scores(session)

            total = session.run("MATCH (m:Memory) RETURN count(m) AS c").single()["c"]
            print(f"\nDone. Graph now holds {total} memories.")
    finally:
        drv.close()



# --------------------------------------------------------------------------
# backup / restore  (full snapshot, for moving machines)
# --------------------------------------------------------------------------

CLAUDE_HOME = os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
BACKUP_VERSION = 1


def mysql_conn():
    import mysql.connector
    cfg = jejak.load_config("mysql")
    return mysql.connector.connect(host=cfg["host"], user=cfg["user"],
                                   password=cfg["password"], database=cfg["database"])


def dump_prompt_logs(path):
    """MySQL prompt log -> gzipped JSONL. Drops the auto-increment id (machine
    specific) and makes cwd portable. Content is redacted again on the way out."""
    try:
        conn = mysql_conn()
    except Exception as e:
        print(f"    prompt log: skipped ({e})")
        return 0
    n = 0
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT session_id, cwd, prompt, tags, machine_name, created_at FROM prompt_logs")
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for row in cur:
                row["cwd"] = portable(row["cwd"] or "")
                row["prompt"] = jejak.redact(row["prompt"] or "")
                row["created_at"] = iso(row["created_at"])
                if not isinstance(row["tags"], str):
                    row["tags"] = json.dumps(row["tags"])
                fh.write(json.dumps(row) + "\n")
                n += 1
        cur.close()
    finally:
        conn.close()
    return n


def load_prompt_logs(path, dry_run=False):
    """Merge prompt-log rows back in, keyed on (machine_name, session_id,
    created_at) so re-running a restore never duplicates."""
    if not os.path.exists(path):
        return 0, 0
    rows = [json.loads(l) for l in gzip.open(path, "rt", encoding="utf-8")]
    try:
        conn = mysql_conn()
    except Exception as e:
        print(f"    prompt log: skipped ({e})")
        return 0, 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT machine_name, session_id, created_at FROM prompt_logs")
        have = {(m, s, iso(c)) for m, s, c in cur.fetchall()}
        todo = [r for r in rows
                if (r["machine_name"], r["session_id"], r["created_at"]) not in have]
        if dry_run or not todo:
            cur.close()
            return len(todo), len(rows) - len(todo)
        cur.executemany(
            "INSERT INTO prompt_logs (session_id, cwd, prompt, tags, machine_name, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            [(r["session_id"], localize(r["cwd"]), r["prompt"], r["tags"],
              r["machine_name"], r["created_at"].replace("T", " ")[:19]) for r in todo])
        conn.commit()
        cur.close()
        return len(todo), len(rows) - len(todo)
    finally:
        conn.close()


def cmd_backup(args):
    out = args.output or f"jejak-backup-{jejak.machine_name()}-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    out = os.path.abspath(out)

    with tempfile.TemporaryDirectory() as tmp:
        graph = os.path.join(tmp, "graph.jsonl.gz")
        print("==> Knowledge graph (all memories, prompts included)")
        cmd_export(argparse.Namespace(output=graph, include_prompts=True,
                                      since=None, project=None))

        print("==> Prompt log")
        plog = os.path.join(tmp, "prompt_logs.jsonl.gz")
        n_logs = dump_prompt_logs(plog)
        print(f"    {n_logs} rows")

        print("==> Config")
        cfgdir = os.path.join(tmp, "config")
        os.makedirs(cfgdir)
        kept = []
        md = os.path.join(CLAUDE_HOME, "CLAUDE.md")
        if os.path.exists(md):
            text = open(md).read()
            if BEGIN_MARK in text and END_MARK in text:
                block = text[text.index(BEGIN_MARK):text.index(END_MARK) + len(END_MARK)]
                open(os.path.join(cfgdir, "CLAUDE.md.jejak-block.md"), "w").write(block)
                kept.append("CLAUDE.md Jejak block")
        st = os.path.join(CLAUDE_HOME, "settings.json")
        if os.path.exists(st):
            try:
                hooks = json.load(open(st)).get("hooks", {})
                jejak_hooks = {}
                for ev, groups in hooks.items():
                    keep = []
                    for g in groups:
                        hs = [h for h in g.get("hooks", []) if "jejak" in h.get("command", "")
                              or "prompt-logger" in h.get("command", "")]
                        if hs:
                            ng = {"hooks": hs}
                            if g.get("matcher"):
                                ng["matcher"] = g["matcher"]
                            keep.append(ng)
                    if keep:
                        jejak_hooks[ev] = keep
                if jejak_hooks:
                    json.dump({"hooks": jejak_hooks},
                              open(os.path.join(cfgdir, "settings.hooks.json"), "w"), indent=2)
                    kept.append("settings.json hook registrations")
            except Exception as e:
                print(f"    settings.json: skipped ({e})")
        print("    " + (", ".join(kept) if kept else "nothing found"))

        manifest = {
            "kind": "jejak-backup",
            "backup_version": BACKUP_VERSION,
            "source_machine": jejak.machine_name(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "prompt_log_rows": n_logs,
            "contains_credentials": False,
            "note": "db-config.json is NOT included - move credentials yourself",
        }
        json.dump(manifest, open(os.path.join(tmp, "manifest.json"), "w"), indent=2)

        with tarfile.open(out, "w:gz") as tar:
            for name in sorted(os.listdir(tmp)):
                tar.add(os.path.join(tmp, name), arcname=name)

    print(f"\nBackup written: {out}  ({os.path.getsize(out)/1024:.0f} KB)")
    print("\nOn the new machine:")
    print("    git clone https://github.com/samuelmanalu/jejak.git && cd jejak && ./install.sh")
    print("    # edit ~/.claude/hooks/db-config.json, re-run ./install.sh")
    print(f"    python3 tools/jejak-sync.py restore {os.path.basename(out)}")
    print("\nThis file is your knowledge in plaintext. Move it like a database dump.")


def cmd_restore(args):
    path = os.path.abspath(args.file)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith(("/", "..")) or ".." in member.name.split("/"):
                    raise SystemExit(f"refusing unsafe path in archive: {member.name}")
            tar.extractall(tmp)

        mpath = os.path.join(tmp, "manifest.json")
        if not os.path.exists(mpath):
            raise SystemExit(f"{path}: no manifest.json - not a Jejak backup")
        manifest = json.load(open(mpath))
        if manifest.get("backup_version") != BACKUP_VERSION:
            raise SystemExit(f"backup version {manifest.get('backup_version')}, "
                             f"this tool speaks {BACKUP_VERSION}")
        print(f"==> Backup from {manifest['source_machine']} ({manifest['created_at']})")

        graph = os.path.join(tmp, "graph.jsonl.gz")
        if os.path.exists(graph):
            cmd_import(argparse.Namespace(file=graph, dry_run=args.dry_run))

        print("\n==> Prompt log")
        added, skipped = load_prompt_logs(os.path.join(tmp, "prompt_logs.jsonl.gz"),
                                          dry_run=args.dry_run)
        verb = "would insert" if args.dry_run else "inserted"
        print(f"    {verb} {added}, already present {skipped}")

        cfgdir = os.path.join(tmp, "config")
        if os.path.isdir(cfgdir) and os.listdir(cfgdir):
            print("\n==> Config in this backup (apply with ./install.sh, not restored automatically):")
            for f in sorted(os.listdir(cfgdir)):
                print(f"    {f}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")



# --------------------------------------------------------------------------
# git-backed sync  (a private repo as the transport, instead of a server)
# --------------------------------------------------------------------------

REMOTE_CFG = os.path.join(CLAUDE_HOME, "hooks", "jejak-remote.json")
DEFAULT_CLONE = os.path.join(CLAUDE_HOME, "jejak-knowledge")
SHARD_WIDTH = 2  # memory_id[:2] -> 256 shards
TOMBSTONES = "tombstones.jsonl"


def read_tombstones(repo):
    """id -> tombstone record. Append-only: a deletion recorded anywhere stays
    recorded, so a machine that has not pulled yet cannot resurrect it."""
    path = os.path.join(repo, TOMBSTONES)
    if not os.path.exists(path):
        return {}
    out = {}
    for lineno, line in enumerate(open(path), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            out[rec["memory_id"]] = rec
        except (json.JSONDecodeError, KeyError) as e:
            raise SystemExit(f"{TOMBSTONES}:{lineno}: corrupt tombstone ({e})")
    return out


def write_tombstones(repo, tombs):
    """Deterministic and additive - never truncates another machine's entries."""
    path = os.path.join(repo, TOMBSTONES)
    merged = read_tombstones(repo)
    merged.update(tombs)
    with open(path, "w") as fh:
        for mid in sorted(merged):
            fh.write(json.dumps(merged[mid], sort_keys=True) + "\n")
    return merged


def apply_tombstones(session, tombs):
    """Remove tombstoned memories from the local graph. Returns how many went."""
    if not tombs:
        return 0
    ids = list(tombs)
    gone = 0
    for i in range(0, len(ids), 500):
        r = session.run("MATCH (m:Memory) WHERE m.memory_id IN $ids "
                        "WITH m, count(m) AS _ DETACH DELETE m RETURN count(_) AS c",
                        ids=ids[i:i + 500]).single()
        gone += (r["c"] if r else 0)
    return gone


def git(args, cwd, check=True, quiet=False):
    import subprocess
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    if not quiet and r.stdout.strip():
        print("    " + r.stdout.strip().replace("\n", "\n    "))
    return r


def remote_config():
    if not os.path.exists(REMOTE_CFG):
        raise SystemExit("No knowledge remote configured. Run:\n"
                         "    python3 tools/jejak-sync.py remote init <git-url>")
    return json.load(open(REMOTE_CFG))


def assert_private(url):
    """Refuse to sync knowledge into a public GitHub repo.

    A developer's graph is full of employer-internal detail. Making that
    public is not a mistake you get to undo, so this is a hard gate.
    """
    import re
    import subprocess
    import urllib.request
    m = re.search(r"github\.com[:/]+([^/]+)/([^/.]+)", url)
    if not m:
        print("    ! Not a GitHub URL - cannot verify it is private. You are on your own.")
        return
    owner, repo = m.group(1), m.group(2)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        try:
            token = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                   text=True).stdout.strip() or None
        except FileNotFoundError:
            token = None
    if not token:
        # Unattended path (the SessionEnd autosync hook has no env token):
        # ask git for the very credential it already uses to push. Nothing new
        # is stored, and the gate keeps verifying live rather than on trust.
        try:
            r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                               capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                if line.startswith("password="):
                    token = line.split("=", 1)[1].strip() or None
        except Exception:
            token = None
    req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}",
                                 headers={"Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception as e:
        raise SystemExit(f"    ! Could not verify {owner}/{repo} is private ({e}).\n"
                         f"      Refusing to push knowledge to a repo I cannot check.\n"
                         f"      Set GITHUB_TOKEN, or use --allow-unverified if you are certain.")
    if not data.get("private", True):
        raise SystemExit(
            f"\n    REFUSING: {owner}/{repo} is PUBLIC.\n"
            f"    Your graph contains employer-internal detail. Make the repo private:\n"
            f"        gh repo edit {owner}/{repo} --visibility private\n")
    print(f"    verified {owner}/{repo} is private")


def shard_for(memory_id):
    return (memory_id[:SHARD_WIDTH] or "00").lower()


def shard_digest(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_shards(repo, records):
    """Deterministic layout: sorted records, sorted keys, one shard per
    memory_id prefix. A new memory touches one small file, so git diffs stay
    readable and two machines usually edit different shards."""
    kdir = os.path.join(repo, "knowledge")
    os.makedirs(kdir, exist_ok=True)
    buckets = {}
    for r in records:
        buckets.setdefault(shard_for(r["props"]["memory_id"]), []).append(r)
    for name in os.listdir(kdir):
        if name.endswith(".jsonl") and name[:-6] not in buckets:
            os.remove(os.path.join(kdir, name))
    for shard, recs in buckets.items():
        recs.sort(key=lambda r: r["props"]["memory_id"])
        with open(os.path.join(kdir, f"{shard}.jsonl"), "w") as fh:
            for r in recs:
                # Records from an older engine carry these unsorted; normalise
                # here too, or machines rewrite each other's shards every push.
                for k in ("topics", "projects", "sessions"):
                    if isinstance(r.get(k), list):
                        r[k] = sorted(r[k])
                fh.write(json.dumps(r, sort_keys=True) + "\n")
    kdir_files = sorted(f for f in os.listdir(kdir) if f.endswith(".jsonl"))
    json.dump({"format_version": FORMAT_VERSION, "shard_width": SHARD_WIDTH,
               "memory_count": len(records),
               "shards": {f: {"sha256": shard_digest(os.path.join(kdir, f)),
                              "records": sum(1 for l in open(os.path.join(kdir, f)) if l.strip())}
                          for f in kdir_files}},
              open(os.path.join(repo, "manifest.json"), "w"), indent=2, sort_keys=True)
    return len(buckets)


def read_shards(repo, strict=True):
    """Read every shard. A corrupt line is named, not tracebacked, and in
    strict mode it stops the run - a backup that silently drops records is
    worse than one that refuses to load."""
    kdir = os.path.join(repo, "knowledge")
    if not os.path.isdir(kdir):
        return []
    out, bad = [], []
    for name in sorted(os.listdir(kdir)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(kdir, name)) as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as e:
                    bad.append(f"knowledge/{name}:{lineno}: {e.msg}")
    if bad:
        msg = "Corrupt shard data:\n    " + "\n    ".join(bad[:10])
        if len(bad) > 10:
            msg += f"\n    ... and {len(bad)-10} more"
        if strict:
            raise SystemExit(msg + "\n\n  Run: jejak-sync verify   (then restore from git history)")
        print("    ! " + msg)
    return out


def fetch_records(include_prompts=False):
    """Same shape export writes, without going through a file."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as t:
        f = os.path.join(t, "g.jsonl.gz")
        cmd_export(argparse.Namespace(output=f, include_prompts=include_prompts,
                                      since=None, project=None))
        _, recs = read_bundle(f)
    return recs


def cmd_remote(args):
    if args.action == "init":
        path = os.path.abspath(os.path.expanduser(args.path or DEFAULT_CLONE))
        if not args.allow_unverified:
            assert_private(args.url)
        if os.path.isdir(os.path.join(path, ".git")):
            print(f"==> Reusing existing clone at {path}")
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            print(f"==> Cloning {args.url} -> {path}")
            import subprocess
            r = subprocess.run(["git", "clone", args.url, path],
                               capture_output=True, text=True)
            if r.returncode != 0:      # empty repo: init and wire the remote
                os.makedirs(path, exist_ok=True)
                git(["init", "-q", "-b", "main"], path)
                git(["remote", "add", "origin", args.url], path, check=False, quiet=True)
        # The clone inherits the global git identity. On a work machine that is
        # usually the employer address, which silently attributes every
        # knowledge commit to the wrong GitHub account. Pin it here.
        import subprocess as _sp
        local_email = _sp.run(["git", "config", "--local", "user.email"],
                              cwd=path, capture_output=True, text=True).stdout.strip()
        if args.email:
            git(["config", "user.email", args.email], path, quiet=True)
            git(["config", "user.name", args.name or args.email.split("@")[0]], path, quiet=True)
            print(f"    commit identity pinned to {args.email}")
        elif not local_email:
            inherited = _sp.run(["git", "config", "user.email"], cwd=path,
                                capture_output=True, text=True).stdout.strip()
            print(f"    ! commit identity is inherited: {inherited or '(unset)'}")
            print(f"      GitHub credits commits by email. If that is not the account")
            print(f"      you want credited, set it now:")
            print(f"        git -C {path} config user.email you@example.com")

        readme = os.path.join(path, "README.md")
        if not os.path.exists(readme):
            open(readme, "w").write(
                "# Jejak knowledge\n\nSynced by `jejak-sync`. **Keep this repository private** - "
                "it contains a developer's working knowledge, including employer-internal detail.\n")
        json.dump({"url": args.url, "path": path}, open(REMOTE_CFG, "w"), indent=2)
        os.chmod(REMOTE_CFG, 0o600)
        print(f"==> Remote configured. Now run:  jejak-sync push")
    elif args.action == "show":
        cfg = remote_config()
        print(f"  url  : {cfg['url']}")
        print(f"  clone: {cfg['path']}")


def cmd_push(args):
    cfg = remote_config()
    repo = cfg["path"]
    if not args.allow_unverified:
        assert_private(cfg["url"])

    # Enforce deletions recorded anywhere BEFORE collecting, otherwise this
    # machine re-adds what another machine deleted and the tombstone is moot.
    tombs = read_tombstones(repo)
    if tombs:
        drv = driver()
        try:
            with drv.session() as session:
                gone = apply_tombstones(session, tombs)
        finally:
            drv.close()
        print(f"==> Tombstones: {len(tombs)} recorded"
              + (f", removed {gone} still present locally" if gone else ""))

    print("==> Collecting knowledge")
    records = fetch_records(include_prompts=args.include_prompts)
    records = [r for r in records if r["props"]["memory_id"] not in tombs]
    # Deletion only ever happens through tombstones, so a repo record missing
    # from the local graph is one this machine has not pulled yet (e.g. merged
    # in by an earlier push's retry). Keep it, or this push erases another
    # machine's knowledge from the repo.
    local_ids = {r["props"]["memory_id"] for r in records}
    kept = [r for r in read_shards(repo, strict=False)
            if r["props"]["memory_id"] not in local_ids
            and r["props"]["memory_id"] not in tombs]
    records += kept
    n_shards = write_shards(repo, records)
    write_tombstones(repo, tombs)
    print(f"    {len(records)} memories across {n_shards} shards"
          + (f" ({len(kept)} kept from the repo, not yet pulled here)" if kept else ""))

    print("==> Committing")
    git(["add", "-A"], repo, quiet=True)
    status = git(["status", "--porcelain"], repo, quiet=True)

    # Commits made elsewhere in this tool (forget, a conflict reconcile) leave a
    # clean tree with unpushed work. Returning early here once made `forget`
    # report success while the deletion never left the machine.
    git(["fetch", "-q", "origin"], repo, check=False, quiet=True)
    ahead = git(["rev-list", "--count", "origin/main..HEAD"], repo, check=False, quiet=True)
    try:
        unpushed = int((ahead.stdout or "0").strip() or 0)
    except ValueError:
        unpushed = 0

    if not status.stdout.strip() and unpushed == 0:
        print("    nothing changed")
        return
    if status.stdout.strip():
        changed = len(status.stdout.strip().splitlines())
        subject = os.environ.get("JEJAK_COMMIT_SUBJECT") or \
            f"knowledge: {len(records)} memories from {jejak.machine_name()}"
        git(["commit", "-q", "-m", subject,
             "-m", f"{len(records)} memories from {jejak.machine_name()}"], repo, quiet=True)
        print(f"    {changed} shard file(s) changed")
        unpushed += 1
    if unpushed:
        print(f"    {unpushed} commit(s) to push")
    if args.no_push:
        print("    --no-push: committed locally only")
        return

    # A plain push fails the moment another machine has pushed, which would
    # leave this machine's knowledge committed locally and never stored.
    # Converge and retry instead.
    print("==> Pushing")
    import subprocess
    for attempt in range(1, 4):
        r = subprocess.run(["git", "push", "origin", "HEAD:main"],
                           cwd=repo, capture_output=True, text=True)
        if r.returncode == 0:
            print("    pushed")
            return
        if "rejected" not in r.stderr and "fetch first" not in r.stderr:
            raise SystemExit(f"    push failed:\n{r.stderr.strip()}")
        print(f"    remote moved; merging and retrying ({attempt}/3)")
        git(["pull", "-q", "--no-rebase", "--no-edit", "origin", "main"], repo, check=False, quiet=True)
        # A conflicted shard is resolved by re-deriving it from the union of
        # both sides' records, newest updated_at winning per memory.
        st = git(["diff", "--name-only", "--diff-filter=U"], repo, quiet=True)
        if st.stdout.strip():
            conflicted = st.stdout.strip().splitlines()
            print(f"    resolving {len(conflicted)} conflicted shard(s) from the merged graph")
            merged = read_shards(repo, strict=False)
            by_id = {}
            for rec in merged:
                mid = rec["props"]["memory_id"]
                prev = by_id.get(mid)
                if prev is None or str(rec["props"].get("updated_at") or "") > str(prev["props"].get("updated_at") or ""):
                    by_id[mid] = rec
            write_shards(repo, list(by_id.values()))
            git(["add", "-A"], repo, quiet=True)
            git(["commit", "-q", "--no-edit", "-m", "merge: reconcile shards"], repo, check=False, quiet=True)
    raise SystemExit("    push still rejected after 3 attempts - resolve by hand in " + repo)


def cmd_pull(args):
    cfg = remote_config()
    repo = cfg["path"]
    print("==> Fetching")
    git(["pull", "-q", "--no-rebase", "origin", "main"], repo, check=False, quiet=True)
    tombs = read_tombstones(repo)
    records = read_shards(repo)
    if tombs:
        before = len(records)
        records = [r for r in records if r["props"]["memory_id"] not in tombs]
        if args.dry_run:
            drv = driver()
            try:
                with drv.session() as session:
                    n = session.run("MATCH (m:Memory) WHERE m.memory_id IN $ids "
                                    "RETURN count(m) AS c", ids=list(tombs)).single()["c"]
            finally:
                drv.close()
            print(f"    tombstones: {len(tombs)} recorded, would delete {n} local memory(ies)")
        else:
            drv = driver()
            try:
                with drv.session() as session:
                    gone = apply_tombstones(session, tombs)
            finally:
                drv.close()
            print(f"    tombstones: {len(tombs)} recorded, deleted {gone} local memory(ies)")
        if before != len(records):
            print(f"    skipped {before - len(records)} tombstoned record(s) still in shards")
    if not records:
        print("    remote holds no knowledge yet")
        return
    print(f"    {len(records)} memories in the repo")
    with tempfile.TemporaryDirectory() as t:
        bundle = os.path.join(t, "pulled.jsonl.gz")
        with gzip.open(bundle, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "manifest", "format_version": FORMAT_VERSION,
                                 "source_machine": "git-remote",
                                 "exported_at": datetime.now(timezone.utc).isoformat(),
                                 "memory_count": len(records)}) + "\n")
            for r in records:
                fh.write(json.dumps(r) + "\n")
        cmd_import(argparse.Namespace(file=bundle, dry_run=args.dry_run))


def cmd_forget(args):
    """Delete a memory everywhere: locally now, and on every other machine at
    its next pull. This is the one destructive operation in the tool."""
    cfg = remote_config()
    repo = cfg["path"]
    drv = driver()
    try:
        with drv.session() as session:
            found = []
            for prefix in args.memory_id:
                rows = session.run(
                    "MATCH (m:Memory) WHERE m.memory_id STARTS WITH $p "
                    "RETURN m.memory_id AS id, m.type AS t, substring(m.content,0,120) AS c",
                    p=prefix).data()
                if not rows:
                    print(f"  no memory matches {prefix}")
                elif len(rows) > 1 and len(prefix) < 36:
                    print(f"  {prefix} is ambiguous ({len(rows)} matches) - use a longer id")
                else:
                    found.extend(rows)
            if not found:
                raise SystemExit("  nothing to forget")

            print(f"\n  About to permanently delete {len(found)} memory(ies):")
            for r in found:
                print(f"    [{r['t']}] {r['id'][:8]}  {r['c']}")
            print("\n  They will be deleted from this machine now, and from every other")
            print("  machine at its next pull. Content stays recoverable in git history.")
            if not args.yes:
                if input("\n  Type 'forget' to confirm: ").strip() != "forget":
                    raise SystemExit("  aborted")

            now = datetime.now(timezone.utc).isoformat()
            tombs = {r["id"]: {"memory_id": r["id"], "deleted_at": now,
                               "machine": jejak.machine_name(),
                               "type": r["t"],
                               "reason": args.reason or ""} for r in found}
            gone = apply_tombstones(session, tombs)
    finally:
        drv.close()

    all_tombs = write_tombstones(repo, tombs)
    records = [r for r in read_shards(repo, strict=False)
               if r["props"]["memory_id"] not in all_tombs]
    write_shards(repo, records)
    print(f"\n  deleted {gone} locally; {len(all_tombs)} tombstone(s) recorded")
    git(["add", "-A"], repo, quiet=True)
    git(["commit", "-q", "-m", f"forget {len(tombs)} memory(ies) from {jejak.machine_name()}"],
        repo, check=False, quiet=True)
    if not args.no_push:
        cmd_push(argparse.Namespace(include_prompts=False, no_push=False,
                                    allow_unverified=args.allow_unverified))


def cmd_verify(args):
    """Is the repo trustworthy as a store? Checks the manifest against what is
    actually on disk, so silent truncation or tampering is caught."""
    cfg = remote_config()
    repo = cfg["path"]
    kdir = os.path.join(repo, "knowledge")
    mpath = os.path.join(repo, "manifest.json")
    problems = []

    if not os.path.exists(mpath):
        raise SystemExit(f"  no manifest.json in {repo} - push once to create it")
    manifest = json.load(open(mpath))
    recorded = manifest.get("shards")
    if not recorded:
        print("  manifest has no checksums (written by an older version)")
        print("  run: jejak-sync push   to upgrade it")
        return

    on_disk = sorted(f for f in os.listdir(kdir) if f.endswith(".jsonl")) if os.path.isdir(kdir) else []
    for name in sorted(set(recorded) | set(on_disk)):
        if name not in on_disk:
            problems.append(f"MISSING  {name}")
            continue
        if name not in recorded:
            problems.append(f"UNTRACKED {name}")
            continue
        path = os.path.join(kdir, name)
        if shard_digest(path) != recorded[name]["sha256"]:
            got = sum(1 for l in open(path) if l.strip())
            problems.append(f"CHANGED  {name}  (manifest {recorded[name]['records']} records, file has {got})")

    records = read_shards(repo, strict=False)
    ids = [r["props"]["memory_id"] for r in records]
    dupes = len(ids) - len(set(ids))

    tombs = read_tombstones(repo)
    leaked = [r["props"]["memory_id"] for r in records
              if r["props"]["memory_id"] in tombs]
    if leaked:
        problems.append(f"RESURRECTED {len(leaked)} tombstoned memory(ies) present in shards")
    print(f"  shards        : {len(on_disk)}")
    print(f"  tombstones    : {len(tombs)}")
    print(f"  records       : {len(records)}  (manifest says {manifest.get('memory_count')})")
    print(f"  duplicate ids : {dupes}")
    if problems:
        print(f"\n  {len(problems)} PROBLEM(S):")
        for p in problems[:20]:
            print(f"    {p}")
        print("\n  A checksum mismatch after your own push is normal (push rewrites the")
        print("  manifest). A mismatch you did not cause means the file changed under you:")
        print(f"    git -C {repo} status && git -C {repo} checkout -- knowledge/")
        raise SystemExit(1)
    print("\n  OK - every shard matches its checksum.")


def cmd_sync(args):
    """pull -> merge -> push: the two machines converge."""
    cmd_pull(argparse.Namespace(dry_run=False))
    print()
    cmd_push(argparse.Namespace(include_prompts=False, no_push=False,
                                allow_unverified=args.allow_unverified))



# --------------------------------------------------------------------------
# daemon: periodic background sync
# --------------------------------------------------------------------------

DAEMON_CFG = os.path.join(CLAUDE_HOME, "hooks", "jejak-daemon.json")
PLIST_LABEL = "tech.jejak.sync"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{PLIST_LABEL}.plist")

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
  </array>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{path}</string></dict>
</dict>
</plist>
"""


def cmd_daemon(args):
    import shutil
    import subprocess
    daemon_script = os.path.join(CLAUDE_HOME, "hooks", "jejak-daemon.py")
    log = os.path.join(CLAUDE_HOME, "logs", "jejak-daemon.log")

    if args.action == "install":
        if not os.path.exists(daemon_script):
            raise SystemExit(f"  {daemon_script} not found - run ./install.sh first")
        remote_config()  # fails loudly if no remote is configured
        interval = max(60, args.interval)
        cfg = {"interval_seconds": interval,
               "model": args.model,
               "summarize": not args.no_summarize,
               "max_summary_items": 25}
        json.dump(cfg, open(DAEMON_CFG, "w"), indent=2)

        if sys.platform != "darwin":
            print(f"  Config written to {DAEMON_CFG}.")
            print(f"  launchd is macOS-only; schedule it yourself, e.g. crontab:")
            print(f"    */{max(1, interval // 60)} * * * * python3 {daemon_script}")
            return

        os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
        os.makedirs(os.path.dirname(log), exist_ok=True)
        claude_dir = os.path.dirname(shutil.which("claude") or "")
        path = ":".join(x for x in [claude_dir, "/usr/local/bin", "/usr/bin", "/bin",
                                    "/usr/sbin", "/sbin"] if x)
        open(PLIST_PATH, "w").write(PLIST.format(
            label=PLIST_LABEL, python=sys.executable, script=daemon_script,
            interval=interval, log=log, path=path))
        subprocess.run(["launchctl", "unload", PLIST_PATH],
                       capture_output=True, text=True)
        r = subprocess.run(["launchctl", "load", PLIST_PATH],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"  launchctl load failed: {r.stderr.strip()}")
        print(f"  Daemon installed: every {interval}s ({interval//60} min)")
        print(f"    summariser : {'off' if args.no_summarize else args.model}")
        print(f"    plist      : {PLIST_PATH}")
        print(f"    log        : {log}")
        print(f"\n  It only commits when there is new knowledge; an idle tick is silent.")

    elif args.action == "uninstall":
        if sys.platform == "darwin" and os.path.exists(PLIST_PATH):
            subprocess.run(["launchctl", "unload", PLIST_PATH],
                           capture_output=True, text=True)
            os.remove(PLIST_PATH)
            print("  Daemon removed.")
        else:
            print("  No launchd job installed.")

    elif args.action == "status":
        cfg = json.load(open(DAEMON_CFG)) if os.path.exists(DAEMON_CFG) else None
        print(f"  config    : {cfg or '(none)'}")
        if sys.platform == "darwin":
            r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
            row = [l for l in r.stdout.splitlines() if PLIST_LABEL in l]
            print(f"  launchd   : {row[0] if row else 'not loaded'}")
        print(f"  plist     : {PLIST_PATH if os.path.exists(PLIST_PATH) else '(none)'}")
        try:
            print(f"  unsynced  : ", end="")
            sys.path.insert(0, os.path.join(CLAUDE_HOME, "hooks"))
            import importlib.util
            spec = importlib.util.spec_from_file_location("jd", daemon_script)
            jd = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(jd)
            items, _ = jd.unsynced()
            print(f"{len(items)} memory(ies)")
        except Exception as e:
            print(f"(could not check: {e})")

    elif args.action == "run":
        r = subprocess.run([sys.executable, daemon_script, "--verbose"])
        sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser(description="Move Jejak knowledge between machines")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="Write a portable knowledge bundle")
    e.add_argument("-o", "--output")
    e.add_argument("--include-prompts", action="store_true",
                   help="Include raw prompt nodes (noisy, usually not wanted)")
    e.add_argument("--since", help="Only memories created on/after YYYY-MM-DD")
    e.add_argument("--project", help="Only memories for one project directory")
    e.set_defaults(func=cmd_export)

    i = sub.add_parser("import", help="Merge a bundle into this machine's graph")
    i.add_argument("file")
    i.add_argument("--dry-run", action="store_true", help="Report the plan, write nothing")
    i.set_defaults(func=cmd_import)

    b = sub.add_parser("backup", help="Full snapshot: knowledge + prompt log + config")
    b.add_argument("-o", "--output")
    b.set_defaults(func=cmd_backup)

    r = sub.add_parser("restore", help="Restore a full backup onto this machine")
    r.add_argument("file")
    r.add_argument("--dry-run", action="store_true", help="Report the plan, write nothing")
    r.set_defaults(func=cmd_restore)

    rm = sub.add_parser("remote", help="Configure a private git repo as the sync transport")
    rm.add_argument("action", choices=["init", "show"])
    rm.add_argument("url", nargs="?")
    rm.add_argument("--path", help=f"Where to clone (default {DEFAULT_CLONE})")
    rm.add_argument("--email", help="Git commit email for the knowledge repo "
                                    "(pins repo-local user.email; avoids inheriting a work address)")
    rm.add_argument("--name", help="Git commit name for the knowledge repo")
    rm.add_argument("--allow-unverified", action="store_true",
                    help="Skip the private-repo check (you had better be sure)")
    rm.set_defaults(func=cmd_remote)

    ps = sub.add_parser("push", help="Export knowledge into the git remote")
    ps.add_argument("--include-prompts", action="store_true")
    ps.add_argument("--no-push", action="store_true", help="Commit locally, do not push")
    ps.add_argument("--allow-unverified", action="store_true")
    ps.set_defaults(func=cmd_push)

    pl = sub.add_parser("pull", help="Merge knowledge from the git remote")
    pl.add_argument("--dry-run", action="store_true")
    pl.set_defaults(func=cmd_pull)

    sy = sub.add_parser("sync", help="pull then push - converge with the remote")
    sy.add_argument("--allow-unverified", action="store_true")
    sy.set_defaults(func=cmd_sync)

    fg = sub.add_parser("forget", help="Delete memories everywhere (records a tombstone)")
    fg.add_argument("memory_id", nargs="+", help="Memory id or unique prefix")
    fg.add_argument("--reason", help="Why, for the audit trail")
    fg.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    fg.add_argument("--no-push", action="store_true")
    fg.add_argument("--allow-unverified", action="store_true")
    fg.set_defaults(func=cmd_forget)

    dm = sub.add_parser("daemon", help="Periodic background sync (install/uninstall/status/run)")
    dm.add_argument("action", choices=["install", "uninstall", "status", "run"])
    dm.add_argument("--interval", type=int, default=900,
                    help="Seconds between checks (default 900 = 15 min, minimum 60)")
    dm.add_argument("--model", default="haiku", help="Model for commit summaries")
    dm.add_argument("--no-summarize", action="store_true",
                    help="Skip the model; use a deterministic commit message")
    dm.set_defaults(func=cmd_daemon)

    vf = sub.add_parser("verify", help="Check the knowledge repo against its checksums")
    vf.set_defaults(func=cmd_verify)

    n = sub.add_parser("inspect", help="Describe a bundle without importing it")
    n.add_argument("file")
    n.set_defaults(func=cmd_inspect)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

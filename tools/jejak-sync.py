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
import socket
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

# Derived locally, never carried between machines.
DERIVED_PROPS = {"relevance_score", "score_updated_at", "promoted_at", "level"}

# Serialized as ISO strings in the bundle; MUST be coerced back to Neo4j
# temporals on import, or duration.between() in the scoring query fails.
DATETIME_PROPS = ("created_at", "updated_at", "last_accessed_at", "superseded_at")


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
    out = args.output or f"jejak-{socket.gethostname()}-{datetime.now():%Y%m%d-%H%M%S}.jsonl.gz"

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
                    "topics": [t for t in r["topics"] if t],
                    "projects": [portable(p) for p in r["projects"] if p],
                    "sessions": [s for s in r["sessions"] if s],
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
            if not todo:
                print("\nNothing to do — this machine is already up to date.")
                return

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
                        m.last_accessed_at = CASE WHEN row.props.last_accessed_at IS NULL THEN m.last_accessed_at ELSE datetime(row.props.last_accessed_at) END,
                        m.superseded_at    = CASE WHEN row.props.superseded_at    IS NULL THEN m.superseded_at    ELSE datetime(row.props.superseded_at)    END
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

            print("==> Rebuilding RELATES_TO (derived from shared topics)")
            touched = [r["props"]["memory_id"] for r in todo]
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
    out = args.output or f"jejak-backup-{socket.gethostname()}-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
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

    n = sub.add_parser("inspect", help="Describe a bundle without importing it")
    n.add_argument("file")
    n.set_defaults(func=cmd_inspect)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

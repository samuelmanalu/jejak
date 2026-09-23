#!/usr/bin/env python3
"""
Jejak sync daemon — periodically push new knowledge to the private git repo.

Runs on a configurable interval (launchd on macOS, cron elsewhere).

Division of labour, deliberately:
  * DETECTION is deterministic — a set difference between the graph and the
    repo. Whether knowledge is unsynced is a fact, not a judgement, and
    putting a model in that path would make a reliability-critical check
    slow, costly and non-deterministic.
  * A SMALL MODEL (Haiku, via the same `claude -p` path jejak-extract uses)
    writes the commit message, turning "knowledge: 2380 memories" into
    "3 learnings on Dropwizard 5 migration, 1 oracle-gateway decision".
    That is genuine judgement, and if it fails the sync still happens with a
    deterministic message.

Nothing here blocks: a failed run is logged and retried on the next tick.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
import jejak_common as jejak  # noqa: E402

CLAUDE_HOME = os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
HOOKS = os.path.join(CLAUDE_HOME, "hooks")
REMOTE_CFG = os.path.join(HOOKS, "jejak-remote.json")
DAEMON_CFG = os.path.join(HOOKS, "jejak-daemon.json")
LOCK = os.path.join(CLAUDE_HOME, "logs", "jejak-daemon.lock")
SYNC = os.path.join(HOOKS, "jejak-sync.py")

DEFAULTS = {"interval_seconds": 900, "model": "haiku", "summarize": True,
            "max_summary_items": 25}


def config():
    cfg = dict(DEFAULTS)
    if os.path.exists(DAEMON_CFG):
        try:
            cfg.update(json.load(open(DAEMON_CFG)))
        except Exception as e:
            jejak.log_error("jejak-daemon/config", e)
    return cfg


class Lock:
    """Overlapping runs would fight over the git index. Stale locks (a killed
    run) expire, so a crash never wedges the daemon permanently."""
    def __init__(self, path, stale_after=1800):
        self.path, self.stale_after, self.held = path, stale_after, False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            age = time.time() - os.path.getmtime(self.path)
            if age < self.stale_after:
                return self
            os.remove(self.path)
        with open(self.path, "w") as fh:
            fh.write(str(os.getpid()))
        self.held = True
        return self

    def __exit__(self, *exc):
        if self.held and os.path.exists(self.path):
            os.remove(self.path)


def unsynced():
    """(records, repo_path) for knowledge present in the graph but not the repo."""
    cfg = json.load(open(REMOTE_CFG))
    repo = cfg["path"]
    kdir = os.path.join(repo, "knowledge")
    in_repo = set()
    if os.path.isdir(kdir):
        for name in os.listdir(kdir):
            if not name.endswith(".jsonl"):
                continue
            for line in open(os.path.join(kdir, name)):
                if line.strip():
                    try:
                        in_repo.add(json.loads(line)["props"]["memory_id"])
                    except Exception:
                        pass  # verify/read_shards reports corruption properly

    from neo4j import GraphDatabase
    n4 = jejak.load_config("neo4j")
    driver = GraphDatabase.driver(n4["uri"], auth=(n4["user"], n4["password"]))
    try:
        with driver.session() as s:
            rows = s.run("""
                MATCH (m:Memory)
                WHERE m.type <> 'prompt' AND coalesce(m.superseded,false) = false
                RETURN m.memory_id AS id, m.type AS type,
                       substring(m.content, 0, 160) AS content
            """).data()
    finally:
        driver.close()
    return [r for r in rows if r["id"] not in in_repo], repo


def summarize(items, model):
    """One line describing what is new. Best-effort: never blocks the sync."""
    listing = "\n".join(f"- [{it['type']}] {it['content']}" for it in items)
    prompt = (
        "Summarise these new engineering-knowledge entries as ONE git commit "
        "subject line, max 72 characters. Describe the substance (topics, "
        "services, decisions), not the count. No quotes, no trailing period, "
        "no preamble - output only the line.\n\n" + listing)
    env = dict(os.environ)
    env["JEJAK_EXTRACTING"] = "1"  # never let this re-enter the hooks
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=90, env=env)
        line = (r.stdout or "").strip().splitlines()
        line = line[0].strip().strip('"').strip() if line else ""
        if 3 < len(line) <= 120:
            return line
    except Exception as e:
        jejak.log_error("jejak-daemon/summarize", e)
    return None


def run_once(verbose=False):
    if not os.path.exists(REMOTE_CFG):
        if verbose:
            print("no knowledge remote configured; nothing to do")
        return 0
    cfg = config()
    with Lock(LOCK) as lock:
        if not lock.held:
            if verbose:
                print("another run is in progress")
            return 0

        items, repo = unsynced()
        if not items:
            if verbose:
                print("up to date - nothing new to sync")
            return 0
        if verbose:
            print(f"{len(items)} unsynced memory(ies)")

        subject = None
        if cfg["summarize"]:
            subject = summarize(items[: cfg["max_summary_items"]], cfg["model"])
        if not subject:
            kinds = {}
            for it in items:
                kinds[it["type"]] = kinds.get(it["type"], 0) + 1
            subject = "knowledge: " + ", ".join(
                f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
        if verbose:
            print(f"commit subject: {subject}")

        env = dict(os.environ)
        env["JEJAK_COMMIT_SUBJECT"] = subject
        r = subprocess.run(["python3", SYNC, "push"], capture_output=True,
                           text=True, timeout=600, env=env)
        tail = (r.stdout or r.stderr or "").strip().splitlines()
        jejak.log_error("jejak-daemon/info",
                        f"rc={r.returncode} synced={len(items)} :: {subject} :: "
                        + (tail[-1] if tail else ""))
        if verbose:
            print((r.stdout or r.stderr or "").strip())
        return r.returncode


def main():
    if os.environ.get("JEJAK_EXTRACTING"):
        return
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    try:
        sys.exit(run_once(verbose=verbose))
    except Exception as e:
        jejak.log_error("jejak-daemon/run", e)
        if verbose:
            raise
        sys.exit(1)


if __name__ == "__main__":
    main()

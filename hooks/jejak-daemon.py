#!/usr/bin/env python3
"""
Jejak sync daemon — periodically pull other machines' knowledge from the
private git repo, then push this machine's new knowledge to it.

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


def boot_time():
    """Epoch seconds of the last boot, or 0 when it cannot be read."""
    try:
        if sys.platform == "darwin":  # "{ sec = 1727160000, usec = 0 } ..."
            out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                                 capture_output=True, text=True, timeout=2).stdout
            return float(out.split("sec =")[1].split(",")[0])
        for line in open("/proc/stat"):
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, IndexError, ValueError, subprocess.SubprocessError):
        pass
    return 0


class Lock:
    """Overlapping runs would fight over the git index. Stale locks (a killed
    run) expire, so a crash never wedges the daemon permanently."""
    def __init__(self, path, stale_after=1800):
        self.path, self.stale_after, self.held = path, stale_after, False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            age = time.time() - os.path.getmtime(self.path)
            # A run killed by shutdown or `launchctl unload` leaves its lock
            # behind; without the PID check the first tick after a reboot skips.
            # PIDs restart at boot, so a pre-boot lock is stale even if its PID is taken.
            if (age < self.stale_after and self._holder_alive()
                    and os.path.getmtime(self.path) > boot_time()):
                return self
            os.remove(self.path)
        with open(self.path, "w") as fh:
            fh.write(str(os.getpid()))
        self.held = True
        return self

    def _holder_alive(self):
        try:
            os.kill(int(open(self.path).read().strip()), 0)
        except ProcessLookupError:
            return False
        except (ValueError, OSError):
            return True  # unreadable or not ours to signal: trust the age check
        return True

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


def wait_for_neo4j(timeout=180):
    """Neo4j takes a while to boot; at login this tick can beat it."""
    from neo4j import GraphDatabase
    n4 = jejak.load_config("neo4j")
    deadline = time.time() + timeout
    while True:
        try:
            with GraphDatabase.driver(n4["uri"], auth=(n4["user"], n4["password"])) as d:
                d.verify_connectivity()
            return True
        except Exception:
            if time.time() >= deadline:
                return False
            time.sleep(5)


def unpushed(repo):
    """Local commits that never reached origin/main."""
    r = subprocess.run(["git", "rev-list", "--count", "origin/main..HEAD"],
                       cwd=repo, capture_output=True, text=True)
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return 0


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


def pull(verbose=False):
    """Bring other machines' knowledge into the local graph. Idempotent and
    tombstone-aware, so running it every tick is safe. A failure is logged and
    never blocks the push: backing up this machine matters more."""
    try:
        r = subprocess.run(["python3", SYNC, "pull"], capture_output=True,
                           text=True, timeout=600)
    except Exception as e:
        jejak.log_error("jejak-daemon/pull", e)
        return
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        tail = (r.stderr or out).strip().splitlines()
        jejak.log_error("jejak-daemon/pull", f"rc={r.returncode} :: " + (tail[-1] if tail else ""))
    elif "already up to date" not in out:
        # Only a pull that changed something is worth a log line.
        summary = [l.strip() for l in out.splitlines()
                   if l.strip().startswith(("new ", "updated ", "unfinished:", "tombstones:"))]
        jejak.log_error("jejak-daemon/info", "pulled :: " + "; ".join(summary))
    if verbose:
        print(out or (r.stderr or "").strip())


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

        # RunAtLoad fires at login, alongside Neo4j's own launch agent.
        if not wait_for_neo4j():
            jejak.log_error("jejak-daemon/run", "Neo4j not reachable after 180s; retrying next tick")
            return 1
        pull(verbose)
        items, repo = unsynced()
        if not items:
            # Committed but never pushed (a push that died on the network, or
            # --no-push): the graph matches the working tree, so only git knows.
            if not unpushed(repo):
                if verbose:
                    print("up to date - nothing new to sync")
                return 0
            items = []
        if verbose:
            print(f"{len(items)} unsynced memory(ies)")

        subject = None
        if not items:
            subject = "knowledge: push unpushed commits"  # push reuses the existing commit
        elif cfg["summarize"]:
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

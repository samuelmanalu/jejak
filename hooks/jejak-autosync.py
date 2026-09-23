#!/usr/bin/env python3
"""
SessionEnd hook: push this session's knowledge to the private git remote.

Manual sync is forgotten sync, and knowledge that only exists on one laptop
is not stored. This runs detached so it never delays session exit, and it is
a no-op unless `jejak-sync remote init` has been run.

Failures are logged, never raised: losing a push is recoverable (the next one
catches up), blocking the user's session is not.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
import jejak_common as jejak  # noqa: E402

CLAUDE_HOME = os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
REMOTE_CFG = os.path.join(CLAUDE_HOME, "hooks", "jejak-remote.json")
# Installed layout puts the tool beside the hooks; a git checkout keeps it in tools/.
CANDIDATES = [
    os.path.join(CLAUDE_HOME, "hooks", "jejak-sync.py"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "jejak-sync.py"),
]


def sync_tool():
    for p in CANDIDATES:
        if os.path.exists(p):
            return os.path.abspath(p)
    return None


def run():
    if not os.path.exists(REMOTE_CFG):
        return  # no remote configured; nothing to do
    tool = sync_tool()
    if not tool:
        jejak.log_error("jejak-autosync/setup", "jejak-sync.py not found")
        return
    try:
        r = subprocess.run(["python3", tool, "push"],
                           capture_output=True, text=True, timeout=180)
        tail = (r.stdout or r.stderr or "").strip().splitlines()
        jejak.log_error("jejak-autosync/info",
                        f"rc={r.returncode} " + (tail[-1] if tail else ""))
    except subprocess.TimeoutExpired:
        jejak.log_error("jejak-autosync/timeout", "push exceeded 180s")
    except Exception as e:
        jejak.log_error("jejak-autosync/push", e)


def main():
    if os.environ.get("JEJAK_EXTRACTING"):
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        run()
        return
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        subprocess.Popen(
            ["python3", os.path.abspath(__file__), "--worker"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        jejak.log_error("jejak-autosync/spawn", e)
    print("{}")


main()

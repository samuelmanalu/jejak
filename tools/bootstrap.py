#!/usr/bin/env python3
"""
Jejak bootstrapper — wires Jejak into an existing Claude Code setup.

  * merges the Jejak hooks into ~/.claude/settings.json without disturbing
    hooks you already have
  * inserts (or refreshes) the Jejak block in ~/.claude/CLAUDE.md between
    <!-- JEJAK:BEGIN --> / <!-- JEJAK:END --> markers

Both operations are idempotent and back up the file they touch first.
"""
import json
import os
import shutil
import sys
from datetime import datetime

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))

BEGIN = "<!-- JEJAK:BEGIN"
END = "<!-- JEJAK:END -->"


def backup(path):
    if os.path.exists(path):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = f"{path}.pre-jejak.{stamp}"
        shutil.copy2(path, dst)
        return dst
    return None


def hook_key(entry, event, matcher):
    """Identity of a hook: the event, the matcher, and the command it runs."""
    return (event, matcher or "", entry.get("command", ""))


def merge_settings():
    example = os.path.join(SRC, "config", "settings.example.json")
    target = os.path.join(DEST, "settings.json")

    with open(example) as f:
        want = json.load(f)["hooks"]

    if os.path.exists(target):
        with open(target) as f:
            try:
                settings = json.load(f)
            except json.JSONDecodeError as e:
                print(f"    !! {target} is not valid JSON ({e}). Not touching it.")
                print("       Merge config/settings.example.json by hand.")
                return False
    else:
        settings = {}

    settings.setdefault("hooks", {})
    existing = settings["hooks"]

    # Everything already registered, so we never add a duplicate command.
    present = set()
    for event, groups in existing.items():
        for group in groups:
            for h in group.get("hooks", []):
                present.add(hook_key(h, event, group.get("matcher")))

    added = 0
    for event, groups in want.items():
        existing.setdefault(event, [])
        for group in groups:
            matcher = group.get("matcher")
            new_hooks = [h for h in group.get("hooks", [])
                         if hook_key(h, event, matcher) not in present]
            if not new_hooks:
                continue
            # Reuse a group with the same matcher if there is one.
            slot = next((g for g in existing[event]
                         if (g.get("matcher") or "") == (matcher or "")), None)
            if slot is None:
                slot = {"hooks": []}
                if matcher:
                    slot["matcher"] = matcher
                existing[event].append(slot)
            slot.setdefault("hooks", []).extend(new_hooks)
            for h in new_hooks:
                present.add(hook_key(h, event, matcher))
                added += 1

    if added == 0:
        print("    settings.json: all Jejak hooks already registered, unchanged")
        return True

    bak = backup(target)
    os.makedirs(DEST, exist_ok=True)
    with open(target, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print(f"    settings.json: registered {added} Jejak hook(s)"
          + (f" (backup: {os.path.basename(bak)})" if bak else ""))
    return True


def merge_claude_md():
    snippet_path = os.path.join(SRC, "config", "CLAUDE.md.snippet")
    target = os.path.join(DEST, "CLAUDE.md")

    with open(snippet_path) as f:
        snippet = f.read().strip()

    current = ""
    if os.path.exists(target):
        with open(target) as f:
            current = f.read()

    if BEGIN in current and END in current:
        start = current.index(BEGIN)
        stop = current.index(END) + len(END)
        if current[start:stop].strip() == snippet:
            print("    CLAUDE.md: Jejak block already current, unchanged")
            return True
        new = current[:start] + snippet + current[stop:]
        action = "refreshed the Jejak block"
    else:
        sep = "" if current == "" else ("\n" if current.endswith("\n") else "\n\n")
        new = current + sep + snippet + "\n"
        action = "appended the Jejak block"

    bak = backup(target)
    os.makedirs(DEST, exist_ok=True)
    with open(target, "w") as f:
        f.write(new)
    print(f"    CLAUDE.md: {action}"
          + (f" (backup: {os.path.basename(bak)})" if bak else ""))
    return True


def main():
    print(f"==> Wiring Jejak into {DEST}")
    ok = merge_settings()
    ok = merge_claude_md() and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Jejak — set up a NEW machine and pull your existing knowledge.
#
#   ./setup-machine.sh
#
# Does everything: prerequisites, credentials, install, schema, hook wiring,
# connects to the private knowledge repo, pulls, verifies, starts the daemon.
# Idempotent — safe to re-run if a step fails.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CLAUDE_HOME:-$HOME/.claude}"
CFG="$DEST/hooks/db-config.json"
SYNC="$SRC/tools/jejak-sync.py"

KNOWLEDGE_REPO="${JEJAK_KNOWLEDGE_REPO:-}"
COMMIT_EMAIL="${JEJAK_COMMIT_EMAIL:-}"
INTERVAL="${JEJAK_INTERVAL:-900}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
warn() { printf '    ! %s\n' "$1"; }
die()  { printf '\n\033[1mFAILED: %s\033[0m\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- prereqs ---
say "Checking prerequisites"
for bin in python3 git; do
  command -v "$bin" >/dev/null || die "$bin not found"
done
printf '    python3, git: ok\n'
command -v claude >/dev/null && printf '    claude CLI: ok\n' \
  || warn "claude CLI not on PATH - Jejak installs, but hooks and the daemon summariser need it"

python3 - <<'PY' || die "Python deps missing. Run: python3 -m pip install -r requirements.txt"
import importlib.util, sys
missing = [m for m in ("neo4j", "mysql.connector") if not importlib.util.find_spec(m)]
print("    python deps:", "ok" if not missing else "MISSING " + ", ".join(missing))
sys.exit(1 if missing else 0)
PY

# ------------------------------------------------------------ github auth ---
say "GitHub credential"
if printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null | grep -q '^password='; then
  printf '    a github.com credential is already stored: ok\n'
else
  warn "No stored github.com credential."
  printf '    Create a token at https://github.com/settings/tokens (scope: repo)\n'
  read -r -p "    Paste it now (input hidden, blank to skip): " -s TOKEN; echo
  if [ -n "${TOKEN:-}" ]; then
    read -r -p "    GitHub username: " GH_USER
    printf 'protocol=https\nhost=github.com\nusername=%s\npassword=%s\n\n' "$GH_USER" "$TOKEN" \
      | git credential-osxkeychain store 2>/dev/null \
      || printf 'protocol=https\nhost=github.com\nusername=%s\npassword=%s\n\n' "$GH_USER" "$TOKEN" \
         | git credential approve
    printf '    stored.\n'
  else
    warn "Skipped - the private-repo check will fail without it."
  fi
fi

# ----------------------------------------------------------- credentials ----
say "Database credentials"
if [ -f "$CFG" ] && ! grep -q CHANGE_ME "$CFG"; then
  printf '    %s already configured: ok\n' "$CFG"
else
  mkdir -p "$DEST/hooks"
  [ -f "$CFG" ] || cp "$SRC/config/db-config.example.json" "$CFG"
  printf '    THIS machine needs its OWN database passwords (never copy another machine'"'"'s file).\n'
  read -r -p "    Neo4j password: " -s NEO_PW; echo
  read -r -p "    MySQL password (root): " -s MY_PW; echo
  NEO_PW="$NEO_PW" MY_PW="$MY_PW" CFG="$CFG" python3 - <<'PY'
import json, os
p = os.environ["CFG"]
d = json.load(open(p))
d["neo4j"]["password"] = os.environ["NEO_PW"]
d["mysql"]["password"] = os.environ["MY_PW"]
json.dump(d, open(p, "w"), indent=2)
print("    written")
PY
  chmod 600 "$CFG"
fi

say "Checking the databases are reachable"
CFG="$CFG" python3 - <<'PY' || die "Cannot reach Neo4j and/or MySQL. Start them, then re-run."
import json, os, sys
c = json.load(open(os.environ["CFG"]))
ok = True
try:
    from neo4j import GraphDatabase
    d = GraphDatabase.driver(c["neo4j"]["uri"], auth=(c["neo4j"]["user"], c["neo4j"]["password"]))
    d.verify_connectivity(); d.close(); print("    neo4j: ok")
except Exception as e:
    print(f"    neo4j: FAILED ({e})"); ok = False
try:
    import mysql.connector
    m = mysql.connector.connect(host=c["mysql"]["host"], user=c["mysql"]["user"],
                                password=c["mysql"]["password"]); m.close(); print("    mysql: ok")
except Exception as e:
    print(f"    mysql: FAILED ({e})"); ok = False
sys.exit(0 if ok else 1)
PY

# --------------------------------------------------------------- install ----
say "Installing Jejak (code, schema, hooks, CLAUDE.md)"
"$SRC/install.sh"

# ---------------------------------------------------------- knowledge repo --
say "Connecting to your knowledge repo"
if [ -f "$DEST/hooks/jejak-remote.json" ]; then
  printf '    already configured: %s\n' "$(python3 -c "import json;print(json.load(open('$DEST/hooks/jejak-remote.json'))['url'])")"
else
  if [ -z "$KNOWLEDGE_REPO" ]; then
    read -r -p "    Private knowledge repo URL: " KNOWLEDGE_REPO
  fi
  [ -n "$KNOWLEDGE_REPO" ] || die "no knowledge repo URL given"
  if [ -z "$COMMIT_EMAIL" ]; then
    printf '    GitHub credits commits by EMAIL, not by token. Inherited here: %s\n' \
      "$(git config user.email || echo unset)"
    read -r -p "    Email to credit knowledge commits to: " COMMIT_EMAIL
  fi
  python3 "$SYNC" remote init "$KNOWLEDGE_REPO" ${COMMIT_EMAIL:+--email "$COMMIT_EMAIL"}
fi

say "Pulling your knowledge"
python3 "$SYNC" pull

say "Verifying"
python3 "$SYNC" verify

say "Starting the sync daemon"
python3 "$SYNC" daemon install --interval "$INTERVAL"

cat <<'NEXT'

Done. Restart Claude Code, then check:

    /jejak stats
    /jejak ask "why did we revert the sumatra-commons 6.0.0 bump"

If that answers from the graph, the migration worked end to end.

Note: this machine's prompt history starts empty - /recap will only cover
work done here. Knowledge (decisions, learnings, findings) is fully synced.
NEXT

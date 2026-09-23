#!/usr/bin/env bash
# Jejak installer — reproduces a complete working Jejak setup.
#
#   1. installs Python dependencies
#   2. copies hooks + skills into ~/.claude
#   3. seeds db-config.json (never overwrites an existing one)
#   4. applies the Neo4j + MySQL schema
#   5. registers the hooks in ~/.claude/settings.json
#   6. inserts the Jejak rules into ~/.claude/CLAUDE.md
#   7. verifies the install
#
# Idempotent: safe to re-run. Every file it rewrites is backed up first.
# Override the target with CLAUDE_HOME=/some/path ./install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CLAUDE_HOME:-$HOME/.claude}"
CFG="$DEST/hooks/db-config.json"

echo "==> Installing Jejak into $DEST"

# --- 1. dependencies ------------------------------------------------------
echo "==> Python dependencies"
python3 -m pip install --quiet --upgrade -r "$SRC/requirements.txt"

# --- 2. code --------------------------------------------------------------
mkdir -p "$DEST/hooks" "$DEST/skills/jejak" "$DEST/skills/recap" "$DEST/logs"
cp "$SRC"/hooks/*.py "$SRC"/hooks/*.html "$DEST/hooks/"
# jejak-sync lives beside the hooks once installed, so the SessionEnd
# autosync hook can find it without knowing where the checkout is.
cp "$SRC"/tools/jejak-sync.py "$DEST/hooks/"
cp "$SRC"/skills/jejak/SKILL.md "$DEST/skills/jejak/"
cp "$SRC"/skills/recap/SKILL.md "$DEST/skills/recap/"
echo "==> Copied hooks and skills"

# --- 3. credentials -------------------------------------------------------
if [ ! -f "$CFG" ]; then
  cp "$SRC/config/db-config.example.json" "$CFG"
  echo "==> Created $CFG"
  echo "    !! Passwords are CHANGE_ME — edit it, then re-run ./install.sh"
  SEEDED=1
else
  echo "==> Kept existing $CFG"
  SEEDED=0
fi
chmod 600 "$CFG"

# --- 4. schema ------------------------------------------------------------
if [ "$SEEDED" = "0" ] && ! grep -q 'CHANGE_ME' "$CFG"; then
  echo "==> Applying schema"
  python3 "$SRC/tools/apply_schema.py" || {
    echo "    !! Schema step failed. Apply by hand:"
    echo "       cypher-shell -u USER -p PASS -f $SRC/schema/neo4j.cypher"
    echo "       mysql -u USER -p < $SRC/schema/mysql.sql"
  }
else
  echo "==> Skipping schema (credentials not set yet)"
fi

# --- 5+6. hooks + CLAUDE.md ----------------------------------------------
python3 "$SRC/tools/bootstrap.py"

# --- 7. verify ------------------------------------------------------------
echo "==> Verifying"
if [ "$SEEDED" = "1" ] || grep -q 'CHANGE_ME' "$CFG"; then
  cat <<NEXT

Almost there. Two steps left:

  1. Edit $CFG with your Neo4j + MySQL credentials.
  2. Re-run ./install.sh — it will apply the schema and finish.

NEXT
  exit 0
fi

python3 "$DEST/hooks/jejak-cli.py" stats >/dev/null 2>&1 \
  && echo "    Jejak CLI reaches the database" \
  || { echo "    !! Jejak CLI cannot reach the database — check $CFG"; exit 1; }

cat <<NEXT

Jejak is installed.

  Restart Claude Code, then try:
     /jejak stats
     /jejak ask "what do we know about this project"

  Visualization:  python3 ~/.claude/hooks/serve-graph.py   -> http://127.0.0.1:8787

NEXT

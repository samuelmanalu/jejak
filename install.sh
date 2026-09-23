#!/usr/bin/env bash
# Jejak installer — copies hooks + skills into ~/.claude and seeds config.
# Idempotent: re-running overwrites code, never your db-config.json.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CLAUDE_HOME:-$HOME/.claude}"

echo "==> Installing Jejak into $DEST"
mkdir -p "$DEST/hooks" "$DEST/skills/jejak" "$DEST/skills/recap" "$DEST/logs"

cp "$SRC"/hooks/*.py "$SRC"/hooks/*.html "$DEST/hooks/"
cp "$SRC"/skills/jejak/SKILL.md "$DEST/skills/jejak/"
cp "$SRC"/skills/recap/SKILL.md "$DEST/skills/recap/"

if [ ! -f "$DEST/hooks/db-config.json" ]; then
  cp "$SRC/config/db-config.example.json" "$DEST/hooks/db-config.json"
  echo "    created $DEST/hooks/db-config.json  <-- EDIT THIS (passwords are CHANGE_ME)"
else
  echo "    kept existing $DEST/hooks/db-config.json"
fi
chmod 600 "$DEST/hooks/db-config.json"

echo "==> Python dependencies"
python3 -m pip install --quiet --upgrade -r "$SRC/requirements.txt"

cat <<'NEXT'

Installed. Three manual steps remain:

  1. Edit ~/.claude/hooks/db-config.json with your Neo4j + MySQL credentials.

  2. Create the schema:
       cypher-shell -u neo4j -p YOURPASS -f schema/neo4j.cypher
       mysql -u root -p < schema/mysql.sql

  3. Register the hooks: merge config/settings.example.json into
     ~/.claude/settings.json, and append config/CLAUDE.md.snippet to
     ~/.claude/CLAUDE.md.

Then restart Claude Code and run:  /jejak stats
NEXT

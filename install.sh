#!/usr/bin/env bash
# Jejak installer. Like MySQL: installing Jejak gives you a server, and every
# tool on every machine is a client that signs in to it.
#
#   ./install.sh                    server + client on this machine (personal setup)
#   ./install.sh --client <url>     client only, for a server that runs elsewhere
#   ./install.sh --migrate          server mode, and move an existing pre-server
#                                   graph into your personal space (backed up first)
#
# Server mode:
#   1. installs jejak-server into ~/.jejak/app (Python env via uv)
#   2. finds or starts Neo4j on 127.0.0.1 (existing install, Docker, or Homebrew)
#   3. creates the first admin (you) and starts the server on 127.0.0.1:7700
#      (launchd on macOS, a systemd user service on Linux)
# Both modes:
#   4. copies the hooks, CLI and skill into ~/.claude, wires them into Claude Code,
#      and adds a `jejak` command to ~/.local/bin
#   5. signs this machine in (server mode) or tells you how (client mode)
#
# Idempotent: safe to re-run; files it rewrites are backed up first.
# Secrets are never printed: passwords and tokens go to chmod-600 files.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CLAUDE_HOME:-$HOME/.claude}"
JHOME="${JEJAK_HOME:-$HOME/.jejak}"
APP="$JHOME/app"
SERVER_CFG="$JHOME/server.json"
PORT="${JEJAK_PORT:-7700}"
MODE=server
CLIENT_URL=""
MIGRATE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --client) MODE=client; CLIENT_URL="${2:?--client needs the server URL}"; shift 2 ;;
    --migrate) MIGRATE=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown option $1 (see --help)"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
die()  { printf '\n\033[1mFAILED: %s\033[0m\n' "$1" >&2; [ -n "${2:-}" ] && printf 'hint: %s\n' "$2" >&2; exit 1; }
rand() { python3 -c "import secrets; print(secrets.token_urlsafe(24))"; }

command -v python3 >/dev/null || die "python3 not found" "Install Python 3.9 or newer"
mkdir -p "$JHOME" && chmod 700 "$JHOME"

# ------------------------------------------------------------------ client ---
install_client() {
  say "Installing the Jejak client into $DEST"
  mkdir -p "$DEST/hooks" "$DEST/skills/jejak" "$DEST/skills/recap" "$DEST/logs" "$HOME/.local/bin"
  cp "$SRC"/hooks/*.py "$DEST/hooks/"
  cp "$SRC"/tools/jejak-sync.py "$DEST/hooks/"
  cp "$SRC"/skills/jejak/SKILL.md "$DEST/skills/jejak/"
  cp "$SRC"/skills/recap/SKILL.md "$DEST/skills/recap/"
  # Retired by the server; remove stale copies so nothing uses them.
  rm -f "$DEST/hooks/serve-graph.py" "$DEST/hooks/jejak-daemon.py" "$DEST/hooks/jejak-autosync.py"
  cat > "$HOME/.local/bin/jejak" <<SHIM
#!/bin/sh
exec python3 "$DEST/hooks/jejak-cli.py" "\$@"
SHIM
  chmod +x "$HOME/.local/bin/jejak"
  python3 "$SRC/tools/bootstrap.py"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "    note: add ~/.local/bin to PATH to use the 'jejak' command" ;; esac
}

if [ "$MODE" = client ]; then
  install_client
  cat <<NEXT

Jejak client installed. Sign in to $CLIENT_URL with the single-use invite code
your admin gave you:

    jejak login $CLIENT_URL --invite

(or with a username and password: jejak login $CLIENT_URL)
Then check everything with:  jejak status

NEXT
  exit 0
fi

# ------------------------------------------------------------------ server ---
command -v uv >/dev/null || die "uv not found (it manages the server's Python)" \
  "brew install uv   or   curl -LsSf https://astral.sh/uv/install.sh | sh"

say "Installing jejak-server into $APP"
mkdir -p "$APP/server" "$APP/hooks"
rsync -a --delete --exclude .venv --exclude __pycache__ "$SRC/server/" "$APP/server/"
cp "$SRC/hooks/jejak_common.py" "$SRC/hooks/jejak.html" "$APP/hooks/"
(cd "$APP/server" && uv sync --frozen --no-dev -q)
PY="$APP/server/.venv/bin/python"

write_server_cfg() {  # uri user password
  umask 077
  python3 - "$SERVER_CFG" "$1" "$2" "$3" "$PORT" <<'PY'
import json, sys
path, uri, user, pw, port = sys.argv[1:]
json.dump({"neo4j_uri": uri, "neo4j_user": user, "neo4j_password": pw,
           "host": "127.0.0.1", "port": int(port), "accept_prompts": True},
          open(path, "w"), indent=1)
PY
  chmod 600 "$SERVER_CFG"
}

say "Neo4j"
if [ -f "$SERVER_CFG" ]; then
  echo "    using the Neo4j in $SERVER_CFG"
elif [ -f "$DEST/hooks/db-config.json" ] && ! grep -q CHANGE_ME "$DEST/hooks/db-config.json"; then
  # An existing (pre-server) Jejak install: keep using its Neo4j.
  read -r URI USER_ PASS < <(python3 -c "import json;c=json.load(open('$DEST/hooks/db-config.json'))['neo4j'];print(c['uri'],c['user'],c['password'])")
  write_server_cfg "$URI" "$USER_" "$PASS"
  unset PASS
  echo "    using your existing Neo4j ($URI)"
elif command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  PW=$(rand)
  docker volume create jejak-neo4j-data >/dev/null
  docker run -d --name jejak-neo4j --restart unless-stopped -p 127.0.0.1:7687:7687 \
    -e NEO4J_AUTH="neo4j/$PW" -v jejak-neo4j-data:/data neo4j:5-community >/dev/null \
    || die "Could not start Neo4j in Docker" "Is port 7687 already in use? docker ps"
  write_server_cfg "bolt://127.0.0.1:7687" neo4j "$PW"
  unset PW
  echo "    started Neo4j in Docker (container jejak-neo4j, bound to 127.0.0.1)"
elif command -v brew >/dev/null; then
  brew list neo4j >/dev/null 2>&1 || brew install neo4j
  PW=$(rand)
  neo4j-admin dbms set-initial-password "$PW" >/dev/null 2>&1 \
    || die "Neo4j already has a password" "Put it in $SERVER_CFG (see server/jejak_server/config.py) and re-run"
  brew services start neo4j >/dev/null
  write_server_cfg "bolt://127.0.0.1:7687" neo4j "$PW"
  unset PW
  echo "    installed Neo4j with Homebrew (listens on localhost only by default)"
else
  die "No Neo4j found, and neither Docker nor Homebrew is available" \
      "Install Docker, or run Neo4j 5 yourself and describe it in $SERVER_CFG"
fi

export JEJAK_SERVER_CONFIG="$SERVER_CFG"
echo -n "    waiting for Neo4j"
for _ in $(seq 1 60); do
  (cd "$APP/server" && "$PY" -c "from jejak_server.config import driver; driver().verify_connectivity()" >/dev/null 2>&1) && break
  echo -n "."; sleep 2
done
echo
(cd "$APP/server" && "$PY" -c "from jejak_server.config import driver; driver().verify_connectivity()") \
  || die "Neo4j does not answer with the credentials in $SERVER_CFG"

ADMIN=$(python3 -c "import getpass,re; print(re.sub(r'[^a-z0-9._-]','-',getpass.getuser().lower()))")
TOKEN_FILE="$JHOME/.bootstrap-token"
say "Server admin '$ADMIN'"
(cd "$APP/server" && "$PY" -m jejak_server admin init --admin "$ADMIN" --token-file "$TOKEN_FILE" --token-name "$(hostname -s)")

UNSCOPED=$(cd "$APP/server" && "$PY" -c "
from jejak_server.config import driver
with driver().session() as s:
    print(s.run('MATCH (m:Memory) WHERE m.space_id IS NULL RETURN count(m) AS n').single()['n'])")
if [ "$UNSCOPED" != "0" ]; then
  if [ "$MIGRATE" = 1 ]; then
    say "Migrating $UNSCOPED existing memories into space '$ADMIN'"
    mkdir -p "$JHOME/backups" && chmod 700 "$JHOME/backups"
    BACKUP="$JHOME/backups/pre-server-$(date +%Y%m%d-%H%M%S).tar.gz"
    python3 "$SRC/tools/jejak-sync.py" backup -o "$BACKUP" || die "Backup failed; nothing was migrated"
    chmod 600 "$BACKUP"
    echo "    backup: $BACKUP"
    (cd "$APP/server" && "$PY" -m jejak_server admin migrate --owner "$ADMIN")
  else
    echo "    $UNSCOPED memories from before the server are not in a space yet."
    echo "    Back up and move them with:  ./install.sh --migrate"
  fi
fi

say "Server service on 127.0.0.1:$PORT"
if [ "$(uname)" = Darwin ]; then
  PLIST="$HOME/Library/LaunchAgents/tech.jejak.server.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>tech.jejak.server</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>jejak_server</string><string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$APP/server</string>
  <key>EnvironmentVariables</key><dict>
    <key>JEJAK_SERVER_CONFIG</key><string>$SERVER_CFG</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DEST/logs/jejak-server.log</string>
  <key>StandardErrorPath</key><string>$DEST/logs/jejak-server.log</string>
</dict></plist>
PL
  mkdir -p "$DEST/logs"
  launchctl bootout "gui/$(id -u)/tech.jejak.server" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
else
  UNIT="$HOME/.config/systemd/user/jejak-server.service"
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<UN
[Unit]
Description=Jejak server
After=network.target

[Service]
WorkingDirectory=$APP/server
Environment=JEJAK_SERVER_CONFIG=$SERVER_CFG
ExecStart=$PY -m jejak_server serve
Restart=on-failure

[Install]
WantedBy=default.target
UN
  systemctl --user daemon-reload
  systemctl --user enable --now jejak-server.service
fi
for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break; sleep 1; done
curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null || die "The server did not start" "tail $DEST/logs/jejak-server.log"
echo "    running"

install_client

say "Signing this machine in"
JEJAK_HOME="$JHOME" python3 "$DEST/hooks/jejak-cli.py" login "http://127.0.0.1:$PORT" --token-stdin < "$TOKEN_FILE"
rm -f "$TOKEN_FILE"

cat <<NEXT

Jejak is installed: a server on http://127.0.0.1:$PORT and this machine signed in.

  Check it:          jejak status
  Graph view:        http://127.0.0.1:$PORT   (set a password first: jejak password)
  Restart Claude Code, then:  /jejak stats

  Other MCP clients (Gemini, Cursor, ...):
      jejak token create gemini --file ~/.jejak/gemini.token
      then connect them to http://127.0.0.1:$PORT/mcp with "Authorization: Bearer <that token>"

  Share it with a team: run the server on a VPS with deploy/docker-compose.yml
  (HTTPS included), then: jejak user create <name> --space <team> --role editor

NEXT

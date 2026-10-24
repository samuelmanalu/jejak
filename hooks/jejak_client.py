"""
Jejak client: how the CLI and the Claude Code hooks reach a Jejak server.

Standard library only, so any machine with python3 can be a client.

    ~/.jejak/client.json   (chmod 600, folder 700)
        {"server": "https://jejak.example.com", "token": "jjk_...",
         "spaces": {"/Users/me/work": "team-a"}}      # optional, longest prefix wins
    ~/.jejak/outbox/       saves made while the server was unreachable; sent
                           automatically on the next jejak command

Environment overrides (handy for CI and agents): JEJAK_SERVER, JEJAK_TOKEN,
JEJAK_SPACE.
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jejak_common as jejak  # noqa: E402

HOME = os.path.expanduser(os.environ.get("JEJAK_HOME", "~/.jejak"))
CONFIG = os.path.join(HOME, "client.json")
OUTBOX = os.path.join(HOME, "outbox")
QUEUEABLE = {"save", "delete", "visibility", "restore"}


class ClientError(Exception):
    """Something the user (or their agent) can fix; `hint` says how."""

    def __init__(self, message, hint=None, status=None):
        super().__init__(message)
        self.hint, self.status = hint, status


class Unreachable(ClientError):
    pass


# --- config -------------------------------------------------------------------------

def load_config():
    cfg = {}
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            cfg = json.load(f)
    if os.environ.get("JEJAK_SERVER"):
        cfg["server"] = os.environ["JEJAK_SERVER"]
    if os.environ.get("JEJAK_TOKEN"):
        cfg["token"] = os.environ["JEJAK_TOKEN"]
    return cfg


def save_config(cfg):
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    os.chmod(HOME, 0o700)
    tmp = CONFIG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, CONFIG)
    os.chmod(CONFIG, 0o600)


def is_loopback_url(url):
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def check_server_url(url, insecure=False):
    """Tokens must not cross a network in clear text: http:// only for this machine."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ClientError(f"'{url}' is not a server URL.", "Use e.g. https://jejak.example.com or http://127.0.0.1:7700")
    if p.scheme == "http" and not is_loopback_url(url) and not insecure:
        raise ClientError("Refusing to send a token over plain http to another machine.",
                          "Use the server's https:// URL")
    return url.rstrip("/")


def space_for(directory, cfg=None):
    if os.environ.get("JEJAK_SPACE"):
        return os.environ["JEJAK_SPACE"]
    cfg = cfg if cfg is not None else load_config()
    best, slug = -1, None
    d = os.path.realpath(directory or os.getcwd())
    for prefix, s in (cfg.get("spaces") or {}).items():
        p = os.path.realpath(os.path.expanduser(prefix))
        if (d == p or d.startswith(p.rstrip("/") + "/")) and len(p) > best:
            best, slug = len(p), s
    return slug


def build_ctx(directory=None, session=None, space=None):
    """What the server needs to know about where the caller is working."""
    directory = os.path.abspath(directory) if directory else os.getcwd()
    key, name = jejak.project_identity(directory)
    return {"directory": directory, "project_key": key, "project_name": name,
            "machine": jejak.machine_name(),
            "session": session or os.environ.get("CLAUDE_SESSION_ID") or "manual",
            "space": space or space_for(directory)}


# --- HTTP -----------------------------------------------------------------------------

def request(method, path, body=None, timeout=30, auth=True, cfg=None, server=None):
    cfg = cfg if cfg is not None else load_config()
    server = server or cfg.get("server")
    if not server:
        raise ClientError("Not connected to a Jejak server.",
                          "jejak login <server-url>   (a local install uses http://127.0.0.1:7700)")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "jejak-client/1"}
    if auth:
        if not cfg.get("token"):
            raise ClientError("Not logged in.", f"jejak login {server}")
        headers["Authorization"] = "Bearer " + cfg["token"]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read() or b"{}")
        except ValueError:
            err = {}
        raise ClientError(err.get("error") or err.get("detail") or f"HTTP {e.code}",
                          err.get("hint"), e.code)
    except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise Unreachable(f"Cannot reach the Jejak server at {server} ({reason}).",
                          "Is it running? jejak status")


# --- outbox: no knowledge is lost while the server is unreachable ---------------------

def queue(command, args, ctx):
    os.makedirs(OUTBOX, mode=0o700, exist_ok=True)
    name = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    path = os.path.join(OUTBOX, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"command": command, "args": args, "ctx": ctx}, f)
    return path


def outbox_items():
    if not os.path.isdir(OUTBOX):
        return []
    return sorted(os.path.join(OUTBOX, n) for n in os.listdir(OUTBOX) if n.endswith(".json"))


def flush_outbox(timeout=5):
    """Send queued saves in order. Stops at the first unreachable error; an
    item the server rejects is moved to outbox/failed/ so it never blocks."""
    sent = 0
    for path in outbox_items():
        with open(path) as f:
            item = json.load(f)
        try:
            request("POST", f"/v1/run/{item['command']}", {"args": item["args"], "ctx": item["ctx"]},
                    timeout=timeout)
        except Unreachable:
            break
        except ClientError as e:
            failed = os.path.join(OUTBOX, "failed")
            os.makedirs(failed, mode=0o700, exist_ok=True)
            os.replace(path, os.path.join(failed, os.path.basename(path)))
            jejak.log_error("jejak outbox", e)
            continue
        os.remove(path)
        sent += 1
    return sent


def run(command, args, ctx, timeout=60):
    """Run a command on the server: (output, exit_code). Writes are queued if
    the server is unreachable."""
    if outbox_items():
        try:
            flush_outbox()
        except ClientError:
            pass
    try:
        r = request("POST", f"/v1/run/{command}", {"args": args, "ctx": ctx}, timeout=timeout)
    except Unreachable as e:
        if command in QUEUEABLE:
            queue(command, args, ctx)
            return (f"Queued [{command}]: {e} It will be sent automatically on the next jejak "
                    f"command (jejak status shows the queue)."), 0
        raise
    return r.get("output", ""), r.get("exit_code", 0)

"""
MCP over streamable HTTP at /mcp. Any MCP client connects with only the
server URL and a token:

    {"url": "https://jejak.example.com/mcp",
     "headers": {"Authorization": "Bearer jjk_..."}}

The tools are the same as the stdio server (hooks/jejak-mcp.py) and run the
same core commands with the caller's own spaces and role.
"""
import json
from datetime import date

import anyio
import time

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from . import auth, core, oauth
from .config import driver

TYPES = ("decision", "learning", "finding", "issue", "solution", "architecture", "convention", "context")
MAX_OUTPUT = 60_000

INSTRUCTIONS = """Jejak is the user's (and their team's) engineering knowledge graph. Rules:
- LOOK UP first: for any "why/how" question about a project, call jejak_ask or
  jejak_search before reading code. If Jejak answers it, don't re-derive it.
- CREATE: call jejak_check_duplicates first and judge the result by meaning.
  Same meaning -> don't save. Contradicts an entry -> ask the user which is
  current, then use jejak_update on the old one. Unique -> jejak_save.
  Always pass `directory` (the project's absolute path) and `source_file`
  when the knowledge is about a file.
- SPACES: knowledge lives in spaces (personal or team). Reads cover every space
  you belong to; saves go to your default space unless you pass `space`.
  Call jejak_whoami to see your spaces and roles. A viewer cannot save.
- UPDATE never edits in place: jejak_update saves a new version that
  supersedes the old one (the old one stays for audit).
- DELETE is soft and reversible (jejak_delete / jejak_restore). Only delete
  when the user asks or the entry is plainly wrong.
- VISIBILITY: entries are confidential by default. Pass visibility="public"
  only for knowledge anyone may read (e.g. about a public website), never for
  employer work, server addresses, security setup, credentials or personal data.
- Never save secrets, trivial file reads, or conversational filler."""

# Sign-in: a client needs only the URL. On its first call it gets 401 with a
# pointer to this server's OAuth metadata, opens the browser on our consent
# page, and receives short-lived tokens (see oauth.py). Personal access tokens
# (jjk_) keep working for scripts and clients without OAuth.
# Bearer auth is checked on every request, so the SDK's localhost-only Host
# check would only block real deployments behind a domain.
mcp = FastMCP(
    "jejak", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    auth_server_provider=oauth.JejakOAuthProvider(),
    auth=AuthSettings(
        issuer_url=oauth.public_url(),
        resource_server_url=oauth.resource_url(),
        validate_token_resource=False,   # every token this server accepts is issued for /mcp only
        # Not required_scopes: the SDK publishes it as the resource's scopes_supported and
        # clients then ask for only that. Every token carries jejak:read anyway.
        required_scopes=None,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=list(auth.SCOPES), default_scopes=oauth.DEFAULT_SCOPES),
        revocation_options=RevocationOptions(enabled=True),
    ),
)


def _principal(ctx: Context):
    request = ctx.request_context.request
    header = request.headers.get("authorization", "") if request else ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    with driver().session() as s:
        return auth.authenticate(s, token)


async def _run(ctx: Context, command, args, directory="", space=""):
    from .app import ApiError, run_command

    def go():
        try:
            p = _principal(ctx)
        except auth.AuthError as e:
            return f"[error] {e} Get a token with: jejak token create mcp"
        client_ctx = {"directory": directory or None, "space": space or None,
                      "machine": "mcp", "session": f"mcp-{p.username}-{date.today().isoformat()}"}
        try:
            r = run_command(p, command, args, client_ctx)
        except ApiError as e:
            return f"[error] {e.error}" + (f"\n[hint] {e.hint}" if e.hint else "")
        out = r["output"] or "(no output)"
        if r["exit_code"]:
            out = "[error] " + out
        return out if len(out) <= MAX_OUTPUT else out[:MAX_OUTPUT] + f"\n… truncated ({len(out)} chars)"

    return await anyio.to_thread.run_sync(go)


# ── who am I ────────────────────────────────────────────────────────────────

@mcp.tool()
async def jejak_whoami(ctx: Context) -> str:
    """Your username, your spaces and your role in each (owner, editor, viewer),
    and which space saves go to by default."""
    def go():
        try:
            p = _principal(ctx)
        except auth.AuthError as e:
            return f"[error] {e}"
        return json.dumps({"username": p.username, "default_space": p.slugs.get(p.default_space),
                           "spaces": {p.slugs[s]: r for s, r in p.roles.items()}}, indent=1)
    return await anyio.to_thread.run_sync(go)


# ── create ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def jejak_check_duplicates(ctx: Context, content: str, directory: str = "") -> str:
    """Show existing knowledge in this project that may duplicate or contradict
    `content`. Call before every jejak_save and judge the result by meaning."""
    return await _run(ctx, "check-duplicates", {"content": content}, directory)


@mcp.tool()
async def jejak_save(ctx: Context, type: str, content: str, directory: str = "", source_file: str = "",
                     visibility: str = "confidential", space: str = "") -> str:
    """Save a new knowledge entry. `type` is one of: decision, learning, finding,
    issue, solution, architecture, convention, context. `directory` is the
    project's absolute path; `source_file` links it to a file. `visibility` is
    confidential (default) or public. `space` is the space to save to (default:
    your default space). Secrets are redacted automatically. Run
    jejak_check_duplicates first."""
    if type not in TYPES:
        return f"invalid type '{type}'; use one of: {', '.join(TYPES)}"
    return await _run(ctx, "save", {"type": type, "content": content, "file": source_file or None,
                                    "visibility": visibility}, directory, space)


# ── read ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def jejak_ask(ctx: Context, question: str, directory: str = "") -> str:
    """Answer a question from stored knowledge: returns the matching entries and
    their connections for you to synthesize. Use before exploring code."""
    return await _run(ctx, "ask", {"question": question}, directory)


@mcp.tool()
async def jejak_search(ctx: Context, query: str) -> str:
    """Keyword search across all your spaces (newest 20 matches, any project)."""
    return await _run(ctx, "search", {"query": query})


@mcp.tool()
async def jejak_get(ctx: Context, memory_id: str) -> str:
    """One entry in full, whatever its status (active, superseded, deleted),
    with its supersede chain. `memory_id` may be the 8-character prefix."""
    return await _run(ctx, "get", {"memory_id": memory_id})


@mcp.tool()
async def jejak_links(ctx: Context, memory_id: str) -> str:
    """Why an entry matters: its topics, projects, sessions and related entries."""
    return await _run(ctx, "links", {"memory_id": memory_id})


@mcp.tool()
async def jejak_map(ctx: Context, directory: str) -> str:
    """Everything known about one project, grouped by type."""
    return await _run(ctx, "map", {}, directory)


@mcp.tool()
async def jejak_rank(ctx: Context, directory: str = "", all_projects: bool = False) -> str:
    """Entries ranked by relevance score, for this project or all projects."""
    return await _run(ctx, "rank", {"all": all_projects}, directory)


@mcp.tool()
async def jejak_topics(ctx: Context, directory: str) -> str:
    """Recurring topics in one project."""
    return await _run(ctx, "topics", {}, directory)


@mcp.tool()
async def jejak_session(ctx: Context, session_id: str) -> str:
    """Everything saved in one session."""
    return await _run(ctx, "session", {"session_id": session_id})


@mcp.tool()
async def jejak_stats(ctx: Context) -> str:
    """Counts across your spaces."""
    return await _run(ctx, "stats", {})


# ── update ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def jejak_update(ctx: Context, memory_id: str, content: str, directory: str = "",
                       source_file: str = "", visibility: str = "confidential") -> str:
    """Replace an entry with a corrected version. Saves `content` as a new entry
    of the SAME type, in the SAME space, that supersedes `memory_id`; the old one
    stays for audit but leaves active views. The new version is confidential
    unless `visibility` is public, because new content is not covered by the
    old entry's review."""
    raw = await _run(ctx, "get", {"memory_id": memory_id, "json": True})
    try:
        old = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if old["status"] != "active":
        return f"{old['memory_id'][:8]} is {old['status']}; only an active entry can be updated."

    def space_slug():
        with driver().session() as s:
            return auth.space_info(s, old["space_id"])["slug"]
    slug = await anyio.to_thread.run_sync(space_slug)
    target = directory or (old["project_paths"][0] if old["project_paths"] else "")
    args = {"type": old["type"], "content": content, "supersedes": old["memory_id"][:8],
            "visibility": visibility, "file": source_file or old.get("source_file")}
    out = await _run(ctx, "save", args, target or (old["projects"][0] if old["projects"] else ""), slug)
    if "SUPERSEDES:" not in out:
        out += "\n[warning] the old entry was NOT marked superseded; check with jejak_get."
    return out


@mcp.tool()
async def jejak_set_visibility(ctx: Context, memory_ids: list[str], visibility: str) -> str:
    """Mark entries public or confidential. Only public entries show in full in
    a space's public graph view; confidential ones appear there as anonymous
    nodes. Content is unchanged. Prompts always stay confidential."""
    if not memory_ids:
        return "give at least one memory id"
    return await _run(ctx, "visibility", {"visibility": visibility, "memory_ids": memory_ids})


# ── delete ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def jejak_delete(ctx: Context, memory_id: str, reason: str) -> str:
    """Soft-delete an entry: it leaves every active view, but stays in the graph
    and can be brought back with jejak_restore."""
    return await _run(ctx, "delete", {"memory_id": memory_id, "reason": reason})


@mcp.tool()
async def jejak_restore(ctx: Context, memory_id: str) -> str:
    """Bring a deleted entry back as a new active entry linked to the old one."""
    return await _run(ctx, "restore", {"memory_id": memory_id})


# Requests per client address per minute on the sign-in endpoints. Codes and
# tokens are 256-bit random, so this is about load and noise, not guessing.
# Per network (an IPv6 /64 is one network) and, so rotating addresses does not
# help, a server-wide ceiling per path.
LIMITS = {"/register": 10, "/token": 60, "/authorize": 30, "/revoke": 30}
GLOBAL_LIMITS = {"/register": 200, "/token": 3000, "/authorize": 600, "/revoke": 600}
_hits = {}


def _count(key, limit, now):
    recent = [t for t in _hits.get(key, []) if now - t < 60]
    recent.append(now)
    _hits[key] = recent
    return len(recent) > limit


def _limited(path, addr):
    limit = LIMITS.get(path)
    if not limit:
        return False
    now = time.time()
    if len(_hits) > 50_000:
        _hits.clear()
    over_net = _count((path, auth.net_of(addr)), limit, now)
    over_all = _count((path, "*"), GLOBAL_LIMITS[path], now)
    return over_net or over_all


def mcp_asgi():
    """MCP plus the OAuth endpoints, with rate limits on the sign-in endpoints."""
    mcp._session_manager = None   # a fresh session manager per app (it can run only once)
    inner = mcp.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"].rstrip("/") or "/"
            addr = (scope.get("client") or ("?",))[0]
            if _limited(path, addr):
                resp = JSONResponse({"error": "slow_down", "error_description": "Too many requests; wait a minute."},
                                    status_code=429, headers={"Retry-After": "60"})
                await resp(scope, receive, send)
                return
        await inner(scope, receive, send)

    return app

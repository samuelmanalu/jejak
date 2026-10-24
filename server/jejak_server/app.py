"""
The Jejak server: HTTP API (/v1), MCP (/mcp) and the graph page (/, /s/<space>).

Every request except /healthz, login, invite redemption and a space's public
graph needs `Authorization: Bearer jjk_...`. Errors are JSON
{"error": ..., "hint": ...}; the hint is the command that fixes it, because
most callers are AI agents that act on what they are told.
"""
import contextlib
import ipaddress
import os
import json
import threading

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import auth, core
from .config import driver, settings

HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "hooks", "jejak.html")


class ApiError(Exception):
    def __init__(self, status, error, hint=None):
        self.status, self.error, self.hint = status, error, hint


def _session():
    return driver().session()


def principal(request: Request):
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        raise ApiError(401, "Not logged in.", "jejak login <server-url>  (or send Authorization: Bearer <token>)")
    with _session() as s:
        try:
            return auth.authenticate(s, token)
        except auth.AuthError as e:
            raise ApiError(401, str(e), "jejak login <server-url> to get a new token")


def is_loopback(request: Request):
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        return False


def resolve_project(s, spaces, key=None, name=None, directory=None):
    """The stored project key for what the client sent.

    CLI clients send the git-remote key. MCP clients often only know a path, so
    a bare name is matched to an existing project of that name when there is
    exactly one in the caller's spaces."""
    if key and s.run("MATCH (p:Project {key: $k}) WHERE p.space_id IN $sp RETURN p LIMIT 1",
                     k=key, sp=spaces).peek():
        return key, name
    if not name and directory:
        name = directory.rstrip("/").rsplit("/", 1)[-1].lower()
    if name:
        rows = s.run("""
            MATCH (p:Project {name: $n}) WHERE p.space_id IN $sp
            RETURN DISTINCT p.key AS key LIMIT 2
        """, n=name.lower(), sp=spaces).data()
        if len(rows) == 1 and (not key or "/" not in rows[0]["key"]):
            return rows[0]["key"], name
    return key or (name.lower() if name else None), name


def build_ctx(s, p, c):
    """core.Ctx from the principal and the client's context block."""
    c = c or {}
    space = p.default_space
    if c.get("space"):
        try:
            space = p.space_by_slug(c["space"])
        except auth.AccessError as e:
            raise ApiError(404, str(e), "jejak space list")
    key, name = resolve_project(s, p.read_spaces, c.get("project_key"), c.get("project_name"),
                                c.get("directory"))
    return core.Ctx(username=p.username, read_spaces=p.read_spaces, write_spaces=p.write_spaces,
                    space=space, project_key=key, project_name=name or key,
                    directory=c.get("directory"), machine=(c.get("machine") or "unknown")[:80],
                    session=(c.get("session") or f"manual-{p.username}")[:120])


def run_command(p, command, args, client_ctx):
    """Run one CLI command for `p`. Returns {"output", "exit_code"}."""
    if command not in core.COMMANDS:
        raise ApiError(404, f"Unknown command '{command}'.",
                       "Commands: " + ", ".join(sorted(core.COMMANDS)))
    args = dict(args or {})
    with _session() as s:
        ctx = build_ctx(s, p, client_ctx)
        with s.begin_transaction() as tx:
            try:
                core.COMMANDS[command](tx, ctx, **args)
            except core.Forbidden as e:
                tx.rollback()
                raise ApiError(403, str(e), "Ask a space owner: jejak space add-member <space> "
                               f"{p.username} editor")
            except core.JejakError as e:
                tx.rollback()
                ctx.out(str(e))
                return {"output": ctx.text(), "exit_code": 1}
            except TypeError as e:
                tx.rollback()
                raise ApiError(400, f"Bad arguments for '{command}': {e}", "jejak " + command + " --help")
            tx.commit()
    return {"output": ctx.text(), "exit_code": 0}


# --- graph cache: rebuilt only when the knowledge changes --------------------------------

_graph_cache, _graph_lock = {}, threading.Lock()


def cached_graph(spaces, public_view):
    """The graph as encoded JSON bytes. Encoding 5 MB through FastAPI's
    jsonable_encoder took ~15 s, so the bytes themselves are cached."""
    key = (tuple(sorted(spaces)), public_view)
    with _session() as s:
        sig = core.graph_signature(s, list(spaces))
        with _graph_lock:
            hit = _graph_cache.get(key)
            if hit and hit[0] == sig:
                return hit[1]
        data = json.dumps(core.graph(s, list(spaces), public_view, settings()["edges_per_node"]),
                          separators=(",", ":")).encode()
    with _graph_lock:
        if len(_graph_cache) > 64:
            _graph_cache.clear()
        _graph_cache[key] = (sig, data)
    return data


# --- app ------------------------------------------------------------------------------------

def create_app():
    from .mcp_tools import mcp, mcp_asgi

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="Jejak", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ApiError)
    async def _api_error(_req, e: ApiError):
        body = {"error": e.error}
        if e.hint:
            body["hint"] = e.hint
        headers = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
        return JSONResponse(body, status_code=e.status, headers=headers)

    @app.exception_handler(ValueError)
    async def _value_error(_req, e: ValueError):
        return JSONResponse({"error": str(e)}, status_code=400)

    @app.middleware("http")
    async def _security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.get("/healthz")
    def healthz():
        with _session() as s:
            s.run("RETURN 1").consume()
        return {"ok": True, "service": "jejak"}

    # --- auth -------------------------------------------------------------------------------

    @app.post("/v1/auth/login")
    def login(request: Request, body: dict = Body(...)):
        with _session() as s:
            try:
                token = auth.login(s, body.get("username"), body.get("password"),
                                   body.get("token_name") or "login", request.client.host,
                                   body.get("days"))
            except auth.AuthError as e:
                raise ApiError(401, str(e))
        return {"token": token}

    @app.post("/v1/auth/invite")
    def redeem(request: Request, body: dict = Body(...)):
        with _session() as s:
            try:
                token = auth.redeem_invite(s, body.get("code"), body.get("token_name"), request.client.host)
            except auth.AuthError as e:
                raise ApiError(401, str(e))
        return {"token": token}

    @app.post("/v1/auth/logout")
    def logout(request: Request):
        p = principal(request)
        with _session() as s:
            auth.revoke_token(s, p.token_id, p.user_id)
        return {"ok": True}

    @app.post("/v1/auth/password")
    def change_password(request: Request, body: dict = Body(...)):
        p = principal(request)
        with _session() as s:
            has = s.run("MATCH (u:User {user_id: $u}) RETURN u.password_hash AS h", u=p.user_id).single()["h"]
            if has:
                try:
                    auth.verify_password(s, p.username, body.get("old_password"), request.client.host)
                except auth.AuthError:
                    raise ApiError(403, "Current password is wrong.")
            auth.set_password(s, p.username, body.get("new_password") or "")
        return {"ok": True}

    @app.get("/v1/me")
    def me(request: Request):
        p = principal(request)
        with _session() as s:
            spaces = [{**auth.space_info(s, sid), "role": role} for sid, role in p.roles.items()]
        return {"username": p.username, "server_admin": p.is_server_admin,
                "default_space": p.slugs.get(p.default_space),
                "spaces": sorted(({"slug": x["slug"], "name": x["name"], "role": x["role"],
                                   "public_graph": bool(x.get("public_graph")),
                                   "personal": bool(x.get("personal"))} for x in spaces),
                                 key=lambda x: x["slug"]),
                "accepts_prompts": settings()["accept_prompts"] and is_loopback(request)}

    # --- tokens -----------------------------------------------------------------------------

    @app.get("/v1/tokens")
    def tokens(request: Request):
        p = principal(request)
        with _session() as s:
            return {"tokens": auth.list_tokens(s, p.user_id)}

    @app.post("/v1/tokens")
    def new_token(request: Request, body: dict = Body(default={})):
        p = principal(request)
        with _session() as s:
            return {"token": auth.create_token(s, p.user_id, body.get("name") or "token", body.get("days"))}

    @app.delete("/v1/tokens/{token_id}")
    def revoke(token_id: str, request: Request):
        p = principal(request)
        if len(token_id) < 8:
            raise ApiError(400, "Give at least 8 characters of the token id.", "jejak token list")
        with _session() as s:
            n = auth.revoke_token(s, token_id, None if p.is_server_admin else p.user_id)
        if not n:
            raise ApiError(404, "No such token.", "jejak token list")
        return {"revoked": n}

    # --- knowledge --------------------------------------------------------------------------

    @app.post("/v1/run/{command}")
    def run(command: str, request: Request, body: dict = Body(default={})):
        p = principal(request)
        return run_command(p, command, body.get("args"), body.get("ctx"))

    @app.post("/v1/context/start")
    def context_start(request: Request, body: dict = Body(default={})):
        p = principal(request)
        with _session() as s:
            ctx = build_ctx(s, p, body.get("ctx"))
            return {"text": core.session_start_context(s, ctx)}

    @app.post("/v1/context/prompt")
    def context_prompt(request: Request, body: dict = Body(default={})):
        p = principal(request)
        with _session() as s:
            ctx = build_ctx(s, p, body.get("ctx"))
            return {"text": core.prompt_context(s, ctx, list(body.get("topics") or []))}

    @app.post("/v1/prompts")
    def save_prompt(request: Request, body: dict = Body(...)):
        """Raw prompts are personal: stored only by a server that opted in
        (accept_prompts) and only from a client on the same machine."""
        p = principal(request)
        if not (settings()["accept_prompts"] and is_loopback(request)):
            raise ApiError(403, "This server does not store prompts.",
                           "Nothing to do: prompts stay on your device.")
        with _session() as s:
            ctx = build_ctx(s, p, body.get("ctx"))
            with s.begin_transaction() as tx:
                core.save_memory(tx, ctx, "prompt", body.get("prompt") or "")
                tx.commit()
        return {"ok": True}

    @app.post("/v1/session-end")
    def end_session(request: Request, body: dict = Body(default={})):
        p = principal(request)
        with _session() as s:
            ctx = build_ctx(s, p, body.get("ctx"))
            with s.begin_transaction() as tx:
                mid = core.session_end(tx, ctx, body.get("session_id") or ctx.session)
                tx.commit()
        return {"summary_id": mid}

    # --- spaces and members -----------------------------------------------------------------

    def _owned(p, slug):
        try:
            sid = p.space_by_slug(slug)
        except auth.AccessError as e:
            raise ApiError(404, str(e), "jejak space list")
        if p.roles.get(sid) != "owner" and not p.is_server_admin:
            raise ApiError(403, f"Only an owner of '{slug}' can do this.", f"jejak space members {slug}")
        return sid

    @app.get("/v1/spaces")
    def spaces(request: Request):
        return {"spaces": me(request)["spaces"]}

    @app.post("/v1/spaces")
    def create_space(request: Request, body: dict = Body(...)):
        p = principal(request)
        with _session() as s:
            auth.create_space(s, body.get("slug"), body.get("name"), p.user_id)
        return {"slug": body.get("slug"), "role": "owner"}

    @app.patch("/v1/spaces/{slug}")
    def update_space(slug: str, request: Request, body: dict = Body(...)):
        p = principal(request)
        sid = _owned(p, slug)
        with _session() as s:
            if "public_graph" in body:
                auth.set_public_graph(s, sid, body["public_graph"])
            return auth.space_info(s, sid)

    @app.get("/v1/spaces/{slug}/members")
    def list_members(slug: str, request: Request):
        p = principal(request)
        try:
            sid = p.space_by_slug(slug)
        except auth.AccessError as e:
            raise ApiError(404, str(e), "jejak space list")
        with _session() as s:
            return {"space": slug, "members": auth.members(s, sid)}

    @app.put("/v1/spaces/{slug}/members/{username}")
    def put_member(slug: str, username: str, request: Request, body: dict = Body(...)):
        p = principal(request)
        sid = _owned(p, slug)
        with _session() as s:
            try:
                auth.set_member(s, sid, auth.user_id(s, username), body.get("role"))
            except ValueError as e:
                raise ApiError(400, str(e), "jejak user list  (server admins) or check the spelling")
        return {"space": slug, "username": username, "role": body.get("role")}

    @app.delete("/v1/spaces/{slug}/members/{username}")
    def delete_member(slug: str, username: str, request: Request):
        p = principal(request)
        sid = _owned(p, slug)
        with _session() as s:
            auth.remove_member(s, sid, auth.user_id(s, username))
        return {"space": slug, "removed": username}

    # --- users (server admins) --------------------------------------------------------------

    def _admin(request):
        p = principal(request)
        if not p.is_server_admin:
            raise ApiError(403, "Only a server admin can manage users.")
        return p

    @app.get("/v1/users")
    def users(request: Request):
        _admin(request)
        with _session() as s:
            return {"users": auth.list_users(s)}

    @app.post("/v1/users")
    def create_user(request: Request, body: dict = Body(...)):
        p = _admin(request)
        sid = _owned(p, body["space"]) if body.get("space") else None
        if (body.get("role") or "viewer") not in auth.ROLES:
            raise ApiError(400, f"Role must be one of: {', '.join(auth.ROLES)}")
        with _session() as s:
            uid, _ = auth.create_user(s, body.get("username"), None, bool(body.get("server_admin")))
            if sid:
                auth.set_member(s, sid, uid, body.get("role") or "viewer")
            code = auth.create_invite(s, uid, p.username, int(body.get("hours") or 24))
        return {"username": body.get("username"), "invite": code,
                "next": f"The user runs: jejak login <server-url> --invite  (and pastes the code; "
                        f"it works once, for {int(body.get('hours') or 24)} hours)"}

    @app.post("/v1/users/{username}/invite")
    def reinvite(username: str, request: Request, body: dict = Body(default={})):
        p = _admin(request)
        with _session() as s:
            code = auth.create_invite(s, auth.user_id(s, username), p.username, int(body.get("hours") or 24))
        return {"username": username, "invite": code}

    @app.post("/v1/users/{username}/{action}")
    def toggle_user(username: str, action: str, request: Request):
        p = _admin(request)
        if action not in ("disable", "enable"):
            raise ApiError(404, "Use disable or enable.")
        if username.lower() == p.username and action == "disable":
            raise ApiError(400, "You cannot disable yourself.")
        with _session() as s:
            auth.set_disabled(s, username, action == "disable")
        return {"username": username, "disabled": action == "disable"}

    # --- graph ------------------------------------------------------------------------------

    @app.get("/v1/graph")
    def my_graph(request: Request, space: str = None):
        p = principal(request)
        try:
            spaces = [p.space_by_slug(space)] if space else p.read_spaces
        except auth.AccessError as e:
            raise ApiError(404, str(e), "jejak space list")
        return Response(cached_graph(spaces, False), media_type="application/json")

    @app.get("/v1/public/{slug}/graph")
    def public_graph(slug: str):
        with _session() as s:
            sid = auth.public_space(s, slug)
        if not sid:
            raise ApiError(404, "No public graph here.")
        return Response(cached_graph([sid], True), media_type="application/json")

    @app.get("/")
    @app.get("/s/{slug}")
    def page(slug: str = None):
        return FileResponse(HTML, media_type="text/html")

    app.mount("/", mcp_asgi())  # /mcp; mounted last so the routes above win
    return app

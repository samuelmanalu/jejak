"""
OAuth 2.1 for MCP clients: the user adds only the server URL to their agent;
the agent opens a browser, the user signs in on THIS server and approves, and
the agent receives short-lived tokens it renews by itself.

The MCP SDK handles the protocol endpoints (/authorize, /token, /register,
/revoke, metadata) and enforces PKCE (S256), exact redirect-URI match and
expiry. This provider adds what the SDK leaves to us:

  * Redirect URIs are limited at registration: loopback (http://127.0.0.1,
    localhost, [::1], any port) and private-use app schemes (cursor://, ...),
    which only reach the user's own machine; https only for an allowlisted
    callback (oauth_redirect_allowlist). A phishing site therefore cannot
    register itself as the place a victim's code is sent.
  * Authorization codes: 32 random bytes, stored as sha256, valid 60 s,
    consumed atomically (two parallel redemptions cannot both succeed); a
    second use revokes everything issued from that code (RFC 9700).
  * Access tokens (jja_) live 1 h. Refresh tokens (jjr_) live 30 days and are
    rotated on every use; presenting an already-used refresh token revokes
    the whole grant, because it means the token was stolen.
  * Tokens are bound to this server's /mcp resource; a request for another
    resource is refused.
  * Every grant is visible and revocable: `jejak token list` / `revoke`.
"""
import json
import re
import secrets
import time
import urllib.parse
import uuid

import anyio
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
    RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import auth
from .config import driver, settings

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 60
REQUEST_TTL = 600
DEFAULT_SCOPES = ["jejak:read", "jejak:write"]
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
BLOCKED_SCHEMES = ("javascript", "data", "file", "vbscript", "about", "blob", "ftp", "ws", "wss")
MAX_CLIENTS = 5000


def public_url():
    return settings()["public_url"].rstrip("/")


def issuer():
    """Exactly the issuer string the metadata publishes (RFC 9207 clients compare it)."""
    from pydantic import AnyHttpUrl
    return str(AnyHttpUrl(public_url()))


def resource_url():
    return public_url() + "/mcp"


def redirect_allowed(uri):
    """Where an authorization code may be sent (see module docstring).

    App links are the dangerous case: some schemes are forwarders that hand
    the URL to a browser or an Office app (microsoft-edge:https://evil/…,
    x-safari-https://…, ms-word:ofe|u|https://…), which would deliver the code
    to a website. So only reverse-DNS schemes (RFC 8252 §7.1: com.example.app:)
    and the named coding-app schemes in oauth_app_schemes are accepted, and
    never one whose remainder looks like a web address."""
    p = urllib.parse.urlsplit(str(uri))
    scheme = (p.scheme or "").lower()
    if not scheme or p.fragment:
        return False
    if scheme == "http":
        return (p.hostname or "") in LOOPBACK_HOSTS
    if scheme == "https":
        return any(str(uri) == a or str(uri).startswith(a.rstrip("/") + "/")
                   for a in settings()["oauth_redirect_allowlist"])
    if not re.fullmatch(r"[a-z][a-z0-9+.-]*", scheme) or scheme in BLOCKED_SCHEMES:
        return False
    rest = str(uri)[len(scheme) + 1:]
    for _ in range(3):  # %2568 -> %68 -> h
        rest = urllib.parse.unquote(rest)
    rest = rest.lstrip("/").lower()
    if re.match(r"(https?|ftp|file|javascript|data)[:%]", rest) or "|" in rest or "://" in rest:
        return False
    if "." in scheme:
        return True
    # A named app scheme must point at that app's own sign-in callback, not at
    # whatever extension also listens on the scheme.
    required = settings()["oauth_app_schemes"].get(scheme)
    return required is not None and (p.netloc.lower() == required or rest.startswith(required + "/"))


def describe_redirect(uri):
    """Plain words for the consent page: where approval sends access."""
    p = urllib.parse.urlsplit(str(uri))
    if p.scheme == "http" and (p.hostname or "") in LOOPBACK_HOSTS:
        return "an app on this computer"
    if p.scheme == "https":
        return p.hostname
    return f"the app that opens {p.scheme}: links on this computer"


def is_loopback_redirect(uri):
    p = urllib.parse.urlsplit(str(uri))
    return p.scheme == "http" and (p.hostname or "") in LOOPBACK_HOSTS


def _now():
    return int(time.time())


def _db(fn, *a, **kw):
    """Run blocking Neo4j work off the event loop."""
    def go():
        with driver().session() as s:
            return fn(s, *a, **kw)
    return anyio.to_thread.run_sync(go)


class JejakOAuthProvider:
    # ── clients ──────────────────────────────────────────────────────────────

    async def get_client(self, client_id):
        def q(s):
            r = s.run("MATCH (c:OAuthClient {client_id: $id}) RETURN c.info AS info", id=client_id).single()
            return OAuthClientInformationFull.model_validate_json(r["info"]) if r else None
        return await _db(q)

    async def register_client(self, client_info):
        bad = [str(u) for u in (client_info.redirect_uris or []) if not redirect_allowed(u)]
        if not client_info.redirect_uris or bad:
            raise RegistrationError(
                "invalid_redirect_uri",
                "Redirect URIs must be on this computer (http://127.0.0.1 or localhost, any port), an "
                "app link (app-scheme://), or an https callback this server allows. Refused: " + ", ".join(bad))
        allowed = set(auth.SCOPES)
        requested = set((client_info.scope or " ".join(DEFAULT_SCOPES)).split())
        if not requested <= allowed:
            raise RegistrationError("invalid_client_metadata", f"Unknown scopes: {requested - allowed}")
        client_info.scope = " ".join(sorted(requested, key=auth.SCOPES.index))
        if client_info.client_name:
            client_info.client_name = "".join(ch for ch in client_info.client_name if ch.isprintable())[:80]

        def q(s):
            if s.run("MATCH (c:OAuthClient) RETURN count(c) AS n").single()["n"] >= MAX_CLIENTS:
                # Forget clients that never got (or no longer have) a grant, oldest first.
                # Never one that may be mid-sign-in (newer than a pending request lives).
                s.run("""
                    MATCH (c:OAuthClient)
                    WHERE NOT exists { (:Grant {client_id: c.client_id}) }
                      AND c.created_at < datetime() - duration({seconds: $age})
                    WITH c ORDER BY c.created_at LIMIT 500 DETACH DELETE c
                """, age=REQUEST_TTL * 2)
                if s.run("MATCH (c:OAuthClient) RETURN count(c) AS n").single()["n"] >= MAX_CLIENTS:
                    raise RegistrationError("invalid_client_metadata", "Too many registered apps; try later.")
            s.run("CREATE (:OAuthClient {client_id: $id, info: $info, created_at: datetime()})",
                  id=client_info.client_id, info=client_info.model_dump_json())
        await _db(q)

    # ── authorize: hand the browser to our sign-in and consent page ──────────

    async def authorize(self, client, params: AuthorizationParams):
        if params.resource and params.resource.rstrip("/") != resource_url():
            raise AuthorizeError("invalid_request", f"This server only issues tokens for {resource_url()}")
        scopes = params.scopes or DEFAULT_SCOPES
        if not set(scopes) <= set(auth.SCOPES):
            raise AuthorizeError("invalid_scope", f"Unknown scope; use: {' '.join(auth.SCOPES)}")
        req = secrets.token_urlsafe(32)
        data = {"client_id": client.client_id, "client_name": client.client_name or "an unnamed app",
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge, "state": params.state, "scopes": scopes,
                "resource": resource_url()}

        def q(s):
            s.run("""
                CREATE (:OAuthRequest {hash: $h, data: $data, expires_at: $exp, used: false,
                                       created_at: datetime()})
            """, h=auth.hash_token(req), data=json.dumps(data), exp=_now() + REQUEST_TTL)
            s.run("MATCH (r:OAuthRequest) WHERE r.expires_at < $now DETACH DELETE r", now=_now())
        await _db(q)
        return f"{public_url()}/oauth/consent?req={urllib.parse.quote(req)}"

    # ── authorization code ────────────────────────────────────────────────────

    async def load_authorization_code(self, client, code):
        def q(s):
            r = s.run("""
                MATCH (c:OAuthCode {hash: $h, client_id: $cid}) RETURN c.data AS data, c.used AS used
            """, h=auth.hash_token(code), cid=client.client_id).single()
            if not r:
                return None
            if r["used"]:
                # Replay: kill whatever this code produced (RFC 9700 §4.5.3.1 / OAuth 2.1).
                d = json.loads(r["data"])
                _revoke_grant(s, d["grant_id"])
                return None
            return r["data"]
        raw = await _db(q)
        if raw is None:
            return None
        d = json.loads(raw)
        return AuthorizationCode(code=code, client_id=d["client_id"], scopes=d["scopes"],
                                 expires_at=d["expires_at"], code_challenge=d["code_challenge"],
                                 redirect_uri=d["redirect_uri"],
                                 redirect_uri_provided_explicitly=d["redirect_uri_provided_explicitly"],
                                 resource=d["resource"], subject=d["user_id"])

    async def exchange_authorization_code(self, client, authorization_code):
        def q(s):
            # Lock first, then check: Neo4j re-reads `used` after the lock, so of two
            # parallel redemptions only one can see used = false.
            r = s.run("""
                MATCH (c:OAuthCode {hash: $h, client_id: $cid})
                SET c._lock = true
                WITH c WHERE c.used = false
                SET c.used = true, c.used_at = datetime()
                RETURN c.data AS data
            """, h=auth.hash_token(authorization_code.code), cid=client.client_id).single()
            if not r:
                raise TokenError("invalid_grant", "authorization code already used")
            d = json.loads(r["data"])
            return _issue(s, d["grant_id"], d["user_id"], d["scopes"])
        return await _db(q)

    # ── refresh ───────────────────────────────────────────────────────────────

    async def load_refresh_token(self, client, refresh_token):
        def q(s):
            r = s.run("""
                MATCH (t:Token {hash: $h, kind: 'refresh'})-[:IN_GRANT]->(g:Grant {client_id: $cid})
                RETURN t.used AS used, t.revoked AS revoked, g.revoked AS grant_revoked,
                       g.grant_id AS gid, t.scopes AS scopes, t.exp AS exp, g.user_id AS uid
            """, h=auth.hash_token(refresh_token), cid=client.client_id).single()
            if not r:
                return None
            if r["used"]:
                _revoke_grant(s, r["gid"])   # a rotated-out token came back: stolen
                return None
            if r["revoked"] or r["grant_revoked"]:
                return None
            return dict(r)
        r = await _db(q)
        if not r:
            return None
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=list(r["scopes"]),
                            expires_at=r["exp"], resource=resource_url(), subject=r["uid"])

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        def q(s):
            r = s.run("""
                MATCH (t:Token {hash: $h, kind: 'refresh'})-[:IN_GRANT]->(g:Grant {client_id: $cid})
                SET t._lock = true
                WITH t, g
                WHERE coalesce(t.used, false) = false AND coalesce(t.revoked, false) = false
                  AND coalesce(g.revoked, false) = false
                SET t.used = true, t.used_at = datetime()
                RETURN g.grant_id AS gid, g.user_id AS uid
            """, h=auth.hash_token(refresh_token.token), cid=client.client_id).single()
            if not r:
                raise TokenError("invalid_grant", "refresh token already used or revoked")
            return _issue(s, r["gid"], r["uid"], scopes)
        return await _db(q)

    # ── access tokens (for the SDK's bearer check on /mcp) ───────────────────

    async def load_access_token(self, token):
        def q(s):
            try:
                p = auth.authenticate(s, token)
            except auth.AuthError:
                return None
            exp = s.run("MATCH (t:Token {hash: $h}) RETURN t.exp AS exp", h=auth.hash_token(token)).single()
            return p, (exp["exp"] if exp else None)
        r = await _db(q)
        if not r:
            return None
        p, exp = r
        return AccessToken(token=token, client_id=p.client_id or "personal-token", scopes=list(p.scopes),
                           expires_at=exp, resource=resource_url(), subject=p.user_id)

    async def revoke_token(self, token):
        def q(s):
            r = s.run("MATCH (t:Token {hash: $h})-[:IN_GRANT]->(g:Grant) RETURN g.grant_id AS gid",
                      h=auth.hash_token(token.token)).single()
            if r:
                _revoke_grant(s, r["gid"])
        await _db(q)


def _issue(s, grant_id, user_id, scopes):
    """A new access + refresh token pair for a live grant."""
    if not s.run("""
        MATCH (g:Grant {grant_id: $g})-[:OF]->(u:User)
        WHERE coalesce(g.revoked, false) = false AND coalesce(u.disabled, false) = false
        RETURN g
    """, g=grant_id).peek():
        raise TokenError("invalid_grant", "this sign-in was revoked")
    access = auth.ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = auth.REFRESH_PREFIX + secrets.token_urlsafe(32)
    now = _now()
    for tok, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
        s.run("""
            MATCH (g:Grant {grant_id: $g}), (u:User {user_id: $u})
            CREATE (t:Token {token_id: $tid, hash: $h, kind: $kind, scopes: $scopes, exp: $exp,
                             expires_at: datetime({epochSeconds: $exp}), created_at: datetime(),
                             revoked: false, used: false})
            CREATE (t)-[:OF]->(u), (t)-[:IN_GRANT]->(g)
        """, g=grant_id, u=user_id, tid=str(uuid.uuid4()), h=auth.hash_token(tok), kind=kind,
             scopes=list(scopes), exp=now + ttl)
    # Old, expired tokens of this grant are no longer useful.
    s.run("""
        MATCH (t:Token)-[:IN_GRANT]->(:Grant {grant_id: $g}) WHERE t.exp < $cut DETACH DELETE t
    """, g=grant_id, cut=now - REFRESH_TTL)
    return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                      refresh_token=refresh, scope=" ".join(scopes))


def _revoke_grant(s, grant_id):
    s.run("""
        MATCH (g:Grant {grant_id: $g}) SET g.revoked = true, g.revoked_at = datetime()
        WITH g OPTIONAL MATCH (t:Token)-[:IN_GRANT]->(g) SET t.revoked = true
    """, g=grant_id)


# ── the consent step, called by the sign-in page ─────────────────────────────

class ConsentError(Exception):
    pass


def pending_request(s, req):
    r = s.run("""
        MATCH (r:OAuthRequest {hash: $h}) WHERE r.used = false AND r.expires_at > $now
        RETURN r.data AS data
    """, h=auth.hash_token(req or ""), now=_now()).single()
    if not r:
        raise ConsentError("This sign-in link has expired or was already used. Start again from your app.")
    return json.loads(r["data"])


def finish(s, req, user_id, approved_scopes):
    """Consume the request; on approval create the grant and a 60-second code.
    Returns the URL to send the browser back to."""
    r = s.run("""
        MATCH (r:OAuthRequest {hash: $h})
        SET r._lock = true
        WITH r WHERE r.used = false AND r.expires_at > $now
        SET r.used = true RETURN r.data AS data
    """, h=auth.hash_token(req or ""), now=_now()).single()
    if not r:
        raise ConsentError("This sign-in link has expired or was already used. Start again from your app.")
    d = json.loads(r["data"])
    if user_id is None:
        return construct_redirect_uri(d["redirect_uri"], error="access_denied",
                                      error_description="The user declined.", state=d["state"],
                                      iss=issuer())
    scopes = [sc for sc in d["scopes"] if sc in approved_scopes]
    if "jejak:read" not in scopes:
        scopes.insert(0, "jejak:read")
    grant_id = str(uuid.uuid4())
    s.run("""
        MATCH (u:User {user_id: $u})
        CREATE (g:Grant {grant_id: $g, client_id: $cid, client_name: $name, user_id: $u,
                         scopes: $scopes, redirect_host: $host, created_at: datetime(), revoked: false})
        CREATE (g)-[:OF]->(u)
    """, u=user_id, g=grant_id, cid=d["client_id"], name=d["client_name"], scopes=scopes,
         host=describe_redirect(d["redirect_uri"]))
    code = secrets.token_urlsafe(32)
    data = {**d, "user_id": user_id, "grant_id": grant_id, "scopes": scopes,
            "expires_at": time.time() + CODE_TTL}
    s.run("CREATE (:OAuthCode {hash: $h, client_id: $cid, data: $data, used: false, created_at: datetime()})",
          h=auth.hash_token(code), cid=d["client_id"], data=json.dumps(data))
    s.run("MATCH (c:OAuthCode) WHERE c.created_at < datetime() - duration({hours: 1}) DELETE c")
    # iss (RFC 9207) lets the client confirm which server answered.
    return construct_redirect_uri(d["redirect_uri"], code=code, state=d["state"], iss=issuer())

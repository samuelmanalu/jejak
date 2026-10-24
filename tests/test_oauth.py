"""
OAuth sign-in for MCP clients, attacked from every side we know of.
Needs a THROWAWAY Neo4j; see tests/test_server.py for how to start one.
"""
import base64
import hashlib
import os
import re
import secrets
import sys
import urllib.parse

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "server"))

NEO4J = os.environ.get("JEJAK_TEST_NEO4J_URI")
pytestmark = pytest.mark.skipif(not NEO4J, reason="set JEJAK_TEST_NEO4J_URI to a throwaway Neo4j")

REDIRECT = "http://127.0.0.1:33418/callback"
PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def env():
    os.environ.update(JEJAK_SERVER_CONFIG="/nonexistent", JEJAK_NEO4J_URI=NEO4J,
                      JEJAK_NEO4J_PASSWORD=os.environ["JEJAK_TEST_NEO4J_PASSWORD"])
    from fastapi.testclient import TestClient
    from jejak_server import auth, schema
    from jejak_server.app import create_app
    from jejak_server.config import driver

    sfx = secrets.token_hex(3)
    user = f"olivia-{sfx}"
    with driver().session() as s:
        schema.apply(s)
        uid, _ = auth.create_user(s, user, PASSWORD)
        newbie, _ = auth.create_user(s, f"newbie-{sfx}")
        invite = auth.create_invite(s, newbie, "test")
    with TestClient(create_app()) as c:
        yield c, user, invite, f"newbie-{sfx}"


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """Every test starts with a clean per-address budget (all tests share one address)."""
    from jejak_server import mcp_tools
    mcp_tools._hits.clear()


def register(c, redirect=REDIRECT, name="Test <b>App</b>"):
    return c.post("/register", json={"redirect_uris": [redirect], "client_name": name,
                                     "token_endpoint_auth_method": "none",
                                     "grant_types": ["authorization_code", "refresh_token"],
                                     "response_types": ["code"]})


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def start(c, client_id, scope="jejak:read jejak:write", resource=None):
    verifier, challenge = pkce()
    from jejak_server import oauth
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "st-123",
         "code_challenge": challenge, "code_challenge_method": "S256", "scope": scope,
         "resource": resource or oauth.resource_url()}
    r = c.get("/authorize", params=q, follow_redirects=False)
    return r, verifier


def consent_form(c, r):
    req = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["req"][0]
    page = c.get("/oauth/consent", params={"req": req})
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return req, csrf, page


def sign_in(c, user, client_id, scope="jejak:read jejak:write", extra=None):
    r, verifier = start(c, client_id, scope)
    req, csrf, _ = consent_form(c, r)
    form = {"req": req, "csrf": csrf, "username": user, "password": PASSWORD, "action": "allow",
            "scope": [s for s in scope.split() if s != "jejak:read"]}
    form.update(extra or {})
    done = c.post("/oauth/consent", data=form, follow_redirects=False)
    assert done.status_code == 303, done.text[:300]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(done.headers["location"]).query)
    return q, verifier


def token(c, client_id, **form):
    return c.post("/token", data={"client_id": client_id, **form})


def mcp(c, tok, method="tools/call", params=None):
    return c.post("/mcp", headers={"Authorization": f"Bearer {tok}",
                                   "Accept": "application/json, text/event-stream"},
                  json={"jsonrpc": "2.0", "id": 1, "method": method,
                        "params": params or {"name": "jejak_whoami", "arguments": {}}})


# --- discovery ------------------------------------------------------------------------

def test_unauthenticated_mcp_points_to_sign_in(env):
    c, *_ = env
    r = c.post("/mcp", json={})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert c.get("/.well-known/oauth-protected-resource/mcp").status_code == 200


# --- registration ---------------------------------------------------------------------

@pytest.mark.parametrize("uri", ["https://evil.example.com/cb", "http://evil.example.com/cb",
                                 "javascript:alert(1)", "data:text/html,x", "file:///etc/passwd",
                                 # app schemes that forward to a browser or Office (security review #1)
                                 "microsoft-edge:https://evil.com/cb", "x-safari-https://evil.com/cb",
                                 "googlechromes://evil.com/cb", "ms-word:ofe|u|https://evil.com/x",
                                 "jar:http://evil.com/x", "view-source:http://evil.com/",
                                 "com.evil.app:https://evil.com/cb", "myapp://cb", "http://127.evil.com/cb",
                                 "com.x:%68ttps://evil.com/cb", "cursor:%2568ttps://evil.com/cb",
                                 "cursor://anysphere/oauth", "vscode://vscode.git/clone?url=https://evil/r"])
def test_registration_refuses_redirects_off_the_users_machine(env, uri):
    c, *_ = env
    r = register(c, uri)
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("uri", ["http://localhost:9999/cb", "http://[::1]:7000/cb", "cursor://anysphere.cursor-retrieval/oauth/callback",
                                 "com.example.app:/oauth2redirect", "https://claude.ai/api/mcp/auth_callback"])
def test_registration_allows_local_app_and_allowlisted_callbacks(env, uri):
    c, *_ = env
    assert register(c, uri).status_code == 201


# --- the happy path, then each attack ------------------------------------------------------

def test_full_flow_then_code_replay_revokes_everything(env):
    c, user, *_ = env
    cid = register(c).json()["client_id"]
    q, verifier = sign_in(c, user, cid)
    assert q["state"] == ["st-123"] and "iss" in q
    code = q["code"][0]
    t = token(c, cid, grant_type="authorization_code", code=code, redirect_uri=REDIRECT,
              code_verifier=verifier)
    assert t.status_code == 200, t.text
    tok = t.json()
    assert tok["access_token"].startswith("jja_") and tok["refresh_token"].startswith("jjr_")
    assert user in mcp(c, tok["access_token"]).text

    # The refresh token is never accepted as an access token.
    assert mcp(c, tok["refresh_token"]).status_code == 401

    # A second use of the code fails AND revokes what the first use produced.
    again = token(c, cid, grant_type="authorization_code", code=code, redirect_uri=REDIRECT,
                  code_verifier=verifier)
    assert again.status_code == 400
    assert mcp(c, tok["access_token"]).status_code == 401


def test_wrong_pkce_verifier_and_wrong_client_fail(env):
    c, user, *_ = env
    cid = register(c).json()["client_id"]
    other = register(c).json()["client_id"]
    q, verifier = sign_in(c, user, cid)
    code = q["code"][0]
    assert token(c, cid, grant_type="authorization_code", code=code, redirect_uri=REDIRECT,
                 code_verifier=pkce()[0]).status_code == 400
    assert token(c, other, grant_type="authorization_code", code=code, redirect_uri=REDIRECT,
                 code_verifier=verifier).status_code == 400


def test_refresh_rotates_and_reuse_of_an_old_refresh_token_kills_the_grant(env):
    c, user, *_ = env
    cid = register(c).json()["client_id"]
    q, verifier = sign_in(c, user, cid)
    first = token(c, cid, grant_type="authorization_code", code=q["code"][0], redirect_uri=REDIRECT,
                  code_verifier=verifier).json()
    second = token(c, cid, grant_type="refresh_token", refresh_token=first["refresh_token"]).json()
    assert second["refresh_token"] != first["refresh_token"]
    assert user in mcp(c, second["access_token"]).text
    # Someone replays the first (already rotated) refresh token: the thief and the owner both lose access.
    stolen = token(c, cid, grant_type="refresh_token", refresh_token=first["refresh_token"])
    assert stolen.status_code == 400
    assert mcp(c, second["access_token"]).status_code == 401
    assert token(c, cid, grant_type="refresh_token", refresh_token=second["refresh_token"]).status_code == 400


def test_token_for_another_resource_is_refused(env):
    c, *_ = env
    cid = register(c).json()["client_id"]
    r, _ = start(c, cid, resource="https://other.example.com/mcp")
    assert "error=invalid_request" in r.headers.get("location", "") or r.status_code == 400


def test_consent_rejects_forged_posts(env):
    c, user, *_ = env
    cid = register(c).json()["client_id"]
    r, _ = start(c, cid)
    req, csrf, page = consent_form(c, r)
    assert "&lt;b&gt;App&lt;/b&gt;" in page.text and "<b>App</b>" not in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    cookie = c.cookies.get("jejak_consent")
    # A cross-site post has no Strict cookie.
    c.cookies.clear()
    forged = c.post("/oauth/consent", data={"req": req, "csrf": csrf, "username": user,
                                             "password": PASSWORD, "action": "allow"},
                    follow_redirects=False)
    assert forged.status_code == 403
    # Right cookie, wrong form value.
    c.cookies.set("jejak_consent", cookie, path="/oauth")
    forged = c.post("/oauth/consent", data={"req": req, "csrf": "x" * 43, "username": user,
                                             "password": PASSWORD, "action": "allow"},
                    follow_redirects=False)
    assert forged.status_code == 403


def test_wrong_password_and_declining(env):
    c, user, *_ = env
    cid = register(c).json()["client_id"]
    r, _ = start(c, cid)
    req, csrf, _ = consent_form(c, r)
    bad = c.post("/oauth/consent", data={"req": req, "csrf": csrf, "username": user,
                                          "password": "wrong password!", "action": "allow"},
                 follow_redirects=False)
    assert bad.status_code == 401 and "Wrong username or password" in bad.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', bad.text).group(1)
    no = c.post("/oauth/consent", data={"req": req, "csrf": csrf, "action": "deny"}, follow_redirects=False)
    assert no.status_code == 303 and "error=access_denied" in no.headers["location"]
    # The request is single-use.
    assert "expired or was already used" in c.get("/oauth/consent", params={"req": req}).text


def test_read_only_sign_in_cannot_write_or_manage(env):
    c, user, *_ = env
    cid = register(c).json()["client_id"]
    q, verifier = sign_in(c, user, cid, scope="jejak:read")
    tok = token(c, cid, grant_type="authorization_code", code=q["code"][0], redirect_uri=REDIRECT,
                code_verifier=verifier).json()
    assert tok["scope"] == "jejak:read"
    r = mcp(c, tok["access_token"], params={"name": "jejak_save", "arguments": {
        "type": "decision", "content": "read-only test", "directory": "/x/proj"}})
    assert "not write" in r.text
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    assert c.get("/v1/tokens", headers=h).status_code == 403


def test_first_sign_in_with_invite_sets_a_password_and_burns_the_invite(env):
    c, user, invite, newbie = env
    cid = register(c).json()["client_id"]
    r, verifier = start(c, cid)
    req, csrf, _ = consent_form(c, r)
    short = c.post("/oauth/consent", data={"req": req, "csrf": csrf, "invite": invite,
                                            "new_password": "short", "action": "allow"},
                   follow_redirects=False)
    assert short.status_code == 401 and "12 characters" in short.text   # invite NOT used up
    csrf = re.search(r'name="csrf" value="([^"]+)"', short.text).group(1)
    ok = c.post("/oauth/consent", data={"req": req, "csrf": csrf, "invite": invite,
                                         "new_password": "a long new password", "action": "allow"},
                follow_redirects=False)
    assert ok.status_code == 303 and "code=" in ok.headers["location"]
    r2, _ = start(c, cid)
    req2, csrf2, _ = consent_form(c, r2)
    reuse = c.post("/oauth/consent", data={"req": req2, "csrf": csrf2, "invite": invite,
                                            "new_password": "another long password", "action": "allow"},
                   follow_redirects=False)
    assert reuse.status_code == 401


def test_revoking_the_app_from_the_cli_cuts_it_off(env):
    c, user, *_ = env
    cid = register(c, name="Revoke Me").json()["client_id"]
    q, verifier = sign_in(c, user, cid)
    tok = token(c, cid, grant_type="authorization_code", code=q["code"][0], redirect_uri=REDIRECT,
                code_verifier=verifier).json()
    from jejak_server import auth
    from jejak_server.config import driver
    with driver().session() as s:
        uid = auth.user_id(s, user)
        app = next(t for t in auth.list_tokens(s, uid) if t["kind"] == "app" and t["name"] == "Revoke Me")
        assert auth.revoke_token(s, app["id"], uid) == 1
    assert mcp(c, tok["access_token"]).status_code == 401
    assert token(c, cid, grant_type="refresh_token", refresh_token=tok["refresh_token"]).status_code == 400


def test_sign_in_endpoints_are_rate_limited(env):
    c, *_ = env
    codes = [register(c).status_code for _ in range(12)]
    assert 429 in codes


def test_revoking_an_access_token_at_the_revoke_endpoint_works(env):
    c, user, *_ = env
    cid = register(c).json()["client_id"]
    q, verifier = sign_in(c, user, cid)
    tok = token(c, cid, grant_type="authorization_code", code=q["code"][0], redirect_uri=REDIRECT,
                code_verifier=verifier).json()
    r = c.post("/revoke", data={"client_id": cid, "client_secret": "", "token": tok["access_token"]})
    assert r.status_code == 200
    assert mcp(c, tok["access_token"]).status_code == 401


def test_parallel_redemptions_of_one_code_yield_one_token(env):
    """Two requests racing with the same code: exactly one gets tokens."""
    import anyio
    from concurrent.futures import ThreadPoolExecutor
    from jejak_server import oauth
    c, user, *_ = env
    cid = register(c).json()["client_id"]
    q, verifier = sign_in(c, user, cid)
    provider = oauth.JejakOAuthProvider()

    def redeem(_):
        async def go():
            client = await provider.get_client(cid)
            code = await provider.load_authorization_code(client, q["code"][0])
            if code is None:
                return False
            try:
                await provider.exchange_authorization_code(client, code)
                return True
            except Exception:
                return False
        return anyio.run(go)
    with ThreadPoolExecutor(8) as pool:
        wins = sum(pool.map(redeem, range(8)))
    assert wins == 1

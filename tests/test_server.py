"""
Jejak server tests.

    cd server && uv run --group dev pytest ../tests -q

Unit tests always run. The integration tests need a THROWAWAY Neo4j (they
create users and spaces with random names; never point this at real data):

    docker run -d --name jejak-test-neo4j -p 127.0.0.1:17687:7687 \
        -e NEO4J_AUTH=neo4j/testpassword123 neo4j:5-community
    JEJAK_TEST_NEO4J_URI=bolt://127.0.0.1:17687 JEJAK_TEST_NEO4J_PASSWORD=testpassword123 \
        uv run --group dev pytest ../tests -q
"""
import os
import re
import secrets
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "server"))

import jejak_common as jejak  # noqa: E402


# --- unit ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,key", [
    ("git@github.com:Org/Repo.git", "github.com/org/repo"),
    ("https://user:secret@github.com/Org/Repo", "github.com/org/repo"),
    ("ssh://git@gitlab.example.com:2222/a/b/c.git", "gitlab.example.com/a/b/c"),
    ("https://github.com/x/y.git/", "github.com/x/y"),
])
def test_normalize_remote_drops_credentials_and_ports(url, key):
    assert jejak.normalize_remote(url) == key


def test_jejak_tokens_and_invites_are_redacted():
    text = "token jjk_" + "a" * 43 + " and invite jji_" + "b" * 32
    out = jejak.redact(text)
    assert "jjk_" not in out and "jji_" not in out


def test_every_memory_query_is_scoped_to_spaces():
    """A query that finds memories by anything other than an exact, already
    resolved memory_id must filter on the caller's spaces."""
    src = open(os.path.join(ROOT, "server", "jejak_server", "core.py")).read()
    queries = re.findall(r'"""(.*?)"""', src, re.S)
    unscoped = []
    for q in queries:
        if not re.search(r"MATCH \((m|m1|a|other|n):Memory\)", q) and "MATCH (m:Memory)-" not in q:
            continue
        if any(s in q for s in ("$spaces", "$space", "{in_scope", "{PROJECT}", "memory_id: $")):
            continue
        unscoped.append(q.strip()[:120])
    assert not unscoped, f"unscoped Memory queries: {unscoped}"


def test_token_hashing():
    from jejak_server import auth
    t = auth.new_token()
    assert t.startswith("jjk_") and len(t) > 40
    assert auth.hash_token(t) == auth.hash_token(t) and auth.hash_token(t) != t


# --- integration ----------------------------------------------------------------------

NEO4J = os.environ.get("JEJAK_TEST_NEO4J_URI")
integration = pytest.mark.skipif(not NEO4J, reason="set JEJAK_TEST_NEO4J_URI to a throwaway Neo4j")


@pytest.fixture(scope="module")
def world():
    os.environ["JEJAK_SERVER_CONFIG"] = "/nonexistent"
    os.environ["JEJAK_NEO4J_URI"] = NEO4J
    os.environ["JEJAK_NEO4J_PASSWORD"] = os.environ["JEJAK_TEST_NEO4J_PASSWORD"]
    from fastapi.testclient import TestClient
    from jejak_server import auth, schema
    from jejak_server.app import create_app
    from jejak_server.config import driver

    sfx = secrets.token_hex(3)
    names = {k: f"{k}-{sfx}" for k in ("alice", "bob", "carol", "team")}
    tokens = {}
    with driver().session() as s:
        schema.apply(s)
        ids = {}
        for who in ("alice", "bob", "carol"):
            ids[who], _ = auth.create_user(s, names[who])
            tokens[who] = auth.create_token(s, ids[who], "test")
        team = auth.create_space(s, names["team"], owner_id=ids["alice"])
        auth.set_member(s, team, ids["bob"], "viewer")
        revoked = auth.create_token(s, ids["alice"], "to-revoke")
    with TestClient(create_app()) as c:
        yield c, names, tokens, revoked


def run(c, token, command, args, space=None, directory="/work/billing"):
    return c.post(f"/v1/run/{command}", headers={"Authorization": f"Bearer {token}"},
                  json={"args": args, "ctx": {"space": space, "directory": directory,
                                              "project_key": "example.com/acme/billing",
                                              "project_name": "billing"}})


@integration
def test_role_matrix_and_isolation(world):
    c, n, t, _ = world
    team = n["team"]
    r = run(c, t["alice"], "save", {"type": "decision", "content": f"Billing uses Postgres {team}"}, team)
    assert r.status_code == 200 and r.json()["exit_code"] == 0, r.text
    mid = re.search(r"id:([0-9a-f]{8})", r.json()["output"]).group(1)

    # bob (viewer) reads but cannot write or delete
    assert team in run(c, t["bob"], "search", {"query": team}).json()["output"]
    assert run(c, t["bob"], "save", {"type": "decision", "content": "x"}, team).status_code == 403
    assert run(c, t["bob"], "delete", {"memory_id": mid, "reason": "x"}).status_code == 403

    # carol (no membership) sees nothing and cannot target the space
    assert "No results" in run(c, t["carol"], "search", {"query": team}).json()["output"]
    assert run(c, t["carol"], "get", {"memory_id": mid}).json()["exit_code"] == 1
    assert run(c, t["carol"], "save", {"type": "decision", "content": "x"}, team).status_code == 404
    g = c.get(f"/v1/graph?space={team}", headers={"Authorization": f"Bearer {t['carol']}"})
    assert g.status_code == 404

    # carol's memory on the same topics is never linked to the team's
    r = run(c, t["carol"], "save", {"type": "decision", "content": f"Carol billing Postgres design {team}"})
    cid = re.search(r"id:([0-9a-f]{8})", r.json()["output"]).group(1)
    assert "No connections" in run(c, t["carol"], "links", {"memory_id": cid}).json()["output"]


@integration
def test_auth_failures(world):
    c, n, t, revoked = world
    assert c.get("/v1/me").status_code == 401
    assert c.get("/v1/me", headers={"Authorization": "Bearer jjk_nope"}).status_code == 401
    me = c.get("/v1/me", headers={"Authorization": f"Bearer {revoked}"})
    assert me.status_code == 200
    from jejak_server import auth
    from jejak_server.config import driver
    with driver().session() as s:
        auth.revoke_token(s, s.run("MATCH (t:Token {hash: $h}) RETURN t.token_id AS id",
                                   h=auth.hash_token(revoked)).single()["id"])
    assert c.get("/v1/me", headers={"Authorization": f"Bearer {revoked}"}).status_code == 401
    assert c.post("/mcp", json={}).status_code == 401
    body = c.get("/v1/me").json()
    assert "hint" in body  # agents get told what to do next


@integration
def test_public_graph_is_redacted(world):
    c, n, t, _ = world
    team = n["team"]
    secret = f"internal-host-{team}.corp"
    r = run(c, t["alice"], "save", {"type": "context", "content": f"Deploys go to {secret}"}, team)
    assert r.json()["exit_code"] == 0
    r = run(c, t["alice"], "save", {"type": "context", "content": f"Public fact {team}",
                                    "visibility": "public"}, team)
    assert r.json()["exit_code"] == 0
    assert c.get(f"/v1/public/{team}/graph").status_code == 404     # off by default
    h = {"Authorization": f"Bearer {t['alice']}"}
    assert c.patch(f"/v1/spaces/{team}", headers=h, json={"public_graph": True}).status_code == 200
    raw = c.get(f"/v1/public/{team}/graph").text
    assert secret not in raw
    assert f"Public fact {team}" in raw


@integration
def test_prompts_are_refused_by_default(world):
    c, n, t, _ = world
    r = c.post("/v1/prompts", headers={"Authorization": f"Bearer {t['alice']}"},
               json={"prompt": "hello", "ctx": {"project_key": "x"}})
    assert r.status_code == 403


@integration
def test_client_labels_cannot_inject_html(world):
    """Project names are chosen by clients and shown to every member of a space."""
    c, n, t, _ = world
    h = {"Authorization": f"Bearer {t['alice']}"}
    r = c.post("/v1/run/save", headers=h, json={
        "args": {"type": "context", "content": f"label test {n['team']}"},
        "ctx": {"space": n["team"], "project_key": '"><img src=x onerror=alert(1)>',
                "project_name": "<script>alert(1)</script>", "directory": "/x/<b>"}})
    assert r.json()["exit_code"] == 0
    raw = c.get(f"/v1/graph?space={n['team']}", headers=h).text
    assert "<" not in raw and ">" not in raw
    page = c.get("/")
    assert "connect-src 'self'" in page.headers["content-security-policy"]


@integration
def test_owner_can_only_invite_and_the_invited_user_decides(world):
    c, n, t, _ = world
    h = {"Authorization": f"Bearer {t['alice']}"}
    r = c.put(f"/v1/spaces/{n['team']}/members/{n['carol']}", headers=h, json={"role": "editor"})
    assert r.json()["status"] == "invited"
    hc = {"Authorization": f"Bearer {t['carol']}"}
    me = c.get("/v1/me", headers=hc).json()
    assert n["team"] not in [x["slug"] for x in me["spaces"]]
    assert [i["slug"] for i in me["invitations"]] == [n["team"]]
    assert "No results" in run(c, t["carol"], "search", {"query": f"Billing uses Postgres {n['team']}"}).json()["output"]
    assert c.post(f"/v1/spaces/{n['team']}/decline", headers=hc).status_code == 200
    assert c.get("/v1/me", headers=hc).json()["invitations"] == []


@integration
def test_the_last_owner_cannot_be_demoted(world):
    c, n, t, _ = world
    h = {"Authorization": f"Bearer {t['alice']}"}
    r = c.put(f"/v1/spaces/{n['team']}/members/{n['alice']}", headers=h, json={"role": "viewer"})
    assert r.status_code == 400 and "owner" in r.json()["error"]


@integration
def test_oversized_requests_and_content_are_refused(world):
    c, n, t, _ = world
    h = {"Authorization": f"Bearer {t['alice']}"}
    assert c.post("/v1/run/save", headers=h, content=b"x" * (2 * 1024 * 1024)).status_code == 413
    r = run(c, t["alice"], "save", {"type": "context", "content": "y" * 60_000}, n["team"])
    assert r.json()["exit_code"] == 1 and "Too long" in r.json()["output"]


@integration
def test_bogus_usernames_cost_nothing_and_leave_no_state(world):
    c, *_ = world
    from jejak_server import auth
    before = len(auth._FAILS)
    for i in range(5):
        r = c.post("/v1/auth/login", json={"username": "u" * 100_000 + str(i), "password": "x"})
        assert r.status_code == 401
    assert len(auth._FAILS) <= before + 1   # only the network counter


@integration
def test_password_change_signs_out_everything_else(world):
    c, n, t, _ = world
    from jejak_server import auth
    from jejak_server.config import driver
    with driver().session() as s:
        uid = auth.user_id(s, n["bob"])
        keep = auth.create_token(s, uid, "keep")
        other = auth.create_token(s, uid, "other")
    hk = {"Authorization": f"Bearer {keep}"}
    r = c.post("/v1/auth/password", headers=hk, json={"old_password": "", "new_password": "a brand new password"})
    assert r.status_code == 200, r.text
    assert c.get("/v1/me", headers=hk).status_code == 200
    assert c.get("/v1/me", headers={"Authorization": f"Bearer {other}"}).status_code == 401


@integration
def test_browser_tokens_are_read_only(world):
    c, n, t, _ = world
    from jejak_server import auth
    from jejak_server.config import driver
    with driver().session() as s:
        auth.set_password(s, n["carol"], "carols long password")
    tok = c.post("/v1/auth/login", json={"username": n["carol"], "password": "carols long password",
                                         "scopes": ["jejak:read"]}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert c.get("/v1/me", headers=h).json()["scopes"] == ["jejak:read"]
    assert c.get("/v1/tokens", headers=h).status_code == 403
    assert run(c, tok, "save", {"type": "context", "content": "x"}).status_code == 403

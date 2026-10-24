"""
Users, tokens, spaces and roles. Stored in the same Neo4j as the knowledge:

    (:User {user_id, username, password_hash, is_server_admin, disabled, default_space})
    (:Token {token_id, hash, name, created_at, last_used_at, expires_at, revoked})-[:OF]->(:User)
    (:Space {space_id, slug, name, public_graph, personal})
    (:User)-[:MEMBER_OF {role}]->(:Space)          role: owner | editor | viewer

Tokens look like `jjk_<43 url-safe chars>` (32 random bytes). Only their
sha256 is stored, so a database dump does not hand out working tokens.
Passwords are argon2 hashes.
"""
import hashlib
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

ROLES = ("viewer", "editor", "owner")
WRITE_ROLES = ("editor", "owner")
TOKEN_PREFIX = "jjk_"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}$")
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,38}$")

_hasher = PasswordHasher()


class AuthError(Exception):
    """401: who are you?"""


class AccessError(Exception):
    """403/404: you may not do this."""


@dataclass
class Principal:
    user_id: str
    username: str
    is_server_admin: bool
    default_space: str            # space_id
    roles: dict = field(default_factory=dict)   # space_id -> role
    slugs: dict = field(default_factory=dict)   # space_id -> slug
    token_id: str = None

    @property
    def read_spaces(self):
        return list(self.roles)

    @property
    def write_spaces(self):
        return [s for s, r in self.roles.items() if r in WRITE_ROLES]

    def space_by_slug(self, slug):
        for sid, s in self.slugs.items():
            if s == slug:
                return sid
        raise AccessError(f"No space '{slug}' (or you are not a member).")


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_token():
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_password(password):
    if len(password or "") < 10:
        raise ValueError("Passwords need at least 10 characters.")
    return _hasher.hash(password)


# --- login throttling (per username and per client address) -------------------------

_FAILS = {}
MAX_FAILS, WINDOW = 10, 15 * 60


def _throttled(key):
    now = time.time()
    hits = [t for t in _FAILS.get(key, []) if now - t < WINDOW]
    _FAILS[key] = hits
    return len(hits) >= MAX_FAILS


def _fail(key):
    _FAILS.setdefault(key, []).append(time.time())


# --- lookups ---------------------------------------------------------------------------

def _principal(session, user_id, token_id=None):
    rec = session.run("""
        MATCH (u:User {user_id: $uid}) WHERE coalesce(u.disabled, false) = false
        OPTIONAL MATCH (u)-[r:MEMBER_OF]->(s:Space)
        RETURN u {.*} AS u, collect({id: s.space_id, slug: s.slug, role: r.role}) AS spaces
    """, uid=user_id).single()
    if not rec:
        raise AuthError("This account is disabled or no longer exists.")
    u = rec["u"]
    spaces = [s for s in rec["spaces"] if s["id"]]
    return Principal(
        user_id=u["user_id"], username=u["username"],
        is_server_admin=bool(u.get("is_server_admin")),
        default_space=u.get("default_space"),
        roles={s["id"]: s["role"] for s in spaces},
        slugs={s["id"]: s["slug"] for s in spaces},
        token_id=token_id)


def authenticate(session, token):
    """The principal behind a bearer token, or AuthError."""
    if not token or not token.startswith(TOKEN_PREFIX):
        raise AuthError("Missing or malformed token.")
    rec = session.run("""
        MATCH (t:Token {hash: $hash})-[:OF]->(u:User)
        WHERE coalesce(t.revoked, false) = false
          AND (t.expires_at IS NULL OR t.expires_at > datetime())
        SET t.last_used_at = datetime()
        RETURN t.token_id AS token_id, u.user_id AS user_id
    """, hash=hash_token(token)).single()
    if not rec:
        raise AuthError("Invalid, expired or revoked token.")
    return _principal(session, rec["user_id"], rec["token_id"])


def login(session, username, password, token_name, client_addr="?", days=None):
    """Exchange a username and password for a new token (shown once)."""
    uid = verify_password(session, username, password, client_addr)
    return create_token(session, uid, token_name or "login", days)


def verify_password(session, username, password, client_addr="?"):
    """user_id if the password is right, else AuthError (throttled)."""
    keys = (f"user:{(username or '').lower()}", f"addr:{client_addr}")
    if any(_throttled(k) for k in keys):
        raise AuthError("Too many failed logins. Wait 15 minutes.")
    rec = session.run("""
        MATCH (u:User {username: $username}) WHERE coalesce(u.disabled, false) = false
        RETURN u.user_id AS user_id, u.password_hash AS hash
    """, username=(username or "").lower()).single()
    ok = False
    if rec and rec["hash"]:
        try:
            ok = _hasher.verify(rec["hash"], password or "")
        except (VerificationError, InvalidHashError):
            ok = False
    else:
        # Same work for unknown users, so timing does not reveal who exists.
        try:
            _hasher.verify(_hasher.hash("x" * 12), password or "")
        except VerificationError:
            pass
    if not ok:
        for k in keys:
            _fail(k)
        raise AuthError("Wrong username or password.")
    return rec["user_id"]


def create_token(session, user_id, name, days=None):
    token = new_token()
    session.run("""
        MATCH (u:User {user_id: $uid})
        CREATE (t:Token {token_id: $tid, hash: $hash, name: $name, created_at: datetime(),
                         revoked: false,
                         expires_at: CASE WHEN $days IS NULL THEN null
                                          ELSE datetime() + duration({days: $days}) END})
        CREATE (t)-[:OF]->(u)
    """, uid=user_id, tid=str(uuid.uuid4()), hash=hash_token(token), name=name, days=days)
    return token


INVITE_PREFIX = "jji_"


def create_invite(session, user_id, created_by, hours=24):
    """A single-use, short-lived code that a new user (or their AI agent) trades
    for a token with `jejak login <url> --invite`. Safer than handing out a
    long-lived token: if the code lands in a chat transcript, it is useless
    once redeemed or expired."""
    code = INVITE_PREFIX + secrets.token_urlsafe(24)
    session.run("""
        MATCH (u:User {user_id: $uid})
        CREATE (i:Invite {hash: $hash, created_at: datetime(), created_by: $by, used: false,
                          expires_at: datetime() + duration({hours: $hours})})-[:FOR]->(u)
    """, uid=user_id, hash=hash_token(code), by=created_by, hours=hours)
    return code


def redeem_invite(session, code, token_name, client_addr="?"):
    key = f"addr:{client_addr}"
    if _throttled(key):
        raise AuthError("Too many failed attempts. Wait 15 minutes.")
    rec = session.run("""
        MATCH (i:Invite {hash: $hash})-[:FOR]->(u:User)
        WHERE i.used = false AND i.expires_at > datetime() AND coalesce(u.disabled, false) = false
        SET i.used = true, i.used_at = datetime()
        RETURN u.user_id AS uid
    """, hash=hash_token(code or "")).single()
    if not rec:
        _fail(key)
        raise AuthError("Invite code is invalid, already used or expired. Ask an admin for a new one: "
                        "jejak user invite <username>")
    return create_token(session, rec["uid"], token_name or "invite")


def list_tokens(session, user_id):
    return [dict(r) for r in session.run("""
        MATCH (t:Token)-[:OF]->(:User {user_id: $uid})
        RETURN t.token_id AS token_id, t.name AS name, toString(t.created_at) AS created_at,
               toString(t.last_used_at) AS last_used_at, toString(t.expires_at) AS expires_at,
               coalesce(t.revoked, false) AS revoked
        ORDER BY t.created_at DESC
    """, uid=user_id)]


def revoke_token(session, token_id, user_id=None):
    """Revoke one token; limited to `user_id`'s tokens unless user_id is None (admin)."""
    rec = session.run("""
        MATCH (t:Token)-[:OF]->(u:User)
        WHERE t.token_id STARTS WITH $tid AND ($uid IS NULL OR u.user_id = $uid)
        SET t.revoked = true, t.revoked_at = datetime()
        RETURN count(t) AS n
    """, tid=token_id, uid=user_id).single()
    return rec["n"]


# --- users and spaces ---------------------------------------------------------------------

def create_space(session, slug, name=None, owner_id=None, personal=False):
    slug = (slug or "").lower()
    if not SLUG.match(slug):
        raise ValueError("Space names use 2-39 lowercase letters, digits and dashes.")
    if session.run("MATCH (s:Space {slug: $slug}) RETURN s", slug=slug).peek():
        raise ValueError(f"Space '{slug}' already exists.")
    sid = str(uuid.uuid4())
    session.run("""
        CREATE (s:Space {space_id: $sid, slug: $slug, name: $name, public_graph: false,
                         personal: $personal, created_at: datetime()})
    """, sid=sid, slug=slug, name=name or slug, personal=personal)
    if owner_id:
        set_member(session, sid, owner_id, "owner")
    return sid


def create_user(session, username, password=None, is_server_admin=False):
    """A user and their personal space (slug = username), which they own."""
    username = (username or "").lower()
    if not USERNAME.match(username):
        raise ValueError("Usernames use 2-39 lowercase letters, digits, dots, dashes, underscores.")
    if session.run("MATCH (u:User {username: $u}) RETURN u", u=username).peek():
        raise ValueError(f"User '{username}' already exists.")
    uid = str(uuid.uuid4())
    session.run("""
        CREATE (:User {user_id: $uid, username: $username, password_hash: $hash,
                       is_server_admin: $admin, disabled: false, created_at: datetime()})
    """, uid=uid, username=username, hash=hash_password(password) if password else None,
         admin=is_server_admin)
    slug = username.replace(".", "-").replace("_", "-")
    if session.run("MATCH (s:Space {slug: $slug}) RETURN s", slug=slug).peek():
        slug = f"{slug}-{uid[:6]}"
    sid = create_space(session, slug, f"{username} (personal)", uid, personal=True)
    session.run("MATCH (u:User {user_id: $uid}) SET u.default_space = $sid", uid=uid, sid=sid)
    return uid, sid


def set_password(session, username, password):
    n = session.run("MATCH (u:User {username: $u}) SET u.password_hash = $h RETURN count(u) AS n",
                    u=username.lower(), h=hash_password(password)).single()["n"]
    if not n:
        raise ValueError(f"No user '{username}'.")


def user_id(session, username):
    rec = session.run("MATCH (u:User {username: $u}) RETURN u.user_id AS id", u=(username or "").lower()).single()
    if not rec:
        raise ValueError(f"No user '{username}'.")
    return rec["id"]


def space_id(session, slug):
    rec = session.run("MATCH (s:Space {slug: $s}) RETURN s.space_id AS id", s=(slug or "").lower()).single()
    if not rec:
        raise ValueError(f"No space '{slug}'.")
    return rec["id"]


def set_member(session, sid, uid, role):
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
    session.run("""
        MATCH (u:User {user_id: $uid}), (s:Space {space_id: $sid})
        MERGE (u)-[r:MEMBER_OF]->(s) SET r.role = $role, r.since = coalesce(r.since, datetime())
    """, uid=uid, sid=sid, role=role)


def remove_member(session, sid, uid):
    owners = session.run("""
        MATCH (:User)-[r:MEMBER_OF {role: 'owner'}]->(:Space {space_id: $sid})
        RETURN collect(r) AS rs
    """, sid=sid).single()["rs"]
    is_owner = session.run("""
        MATCH (:User {user_id: $uid})-[r:MEMBER_OF {role: 'owner'}]->(:Space {space_id: $sid})
        RETURN count(r) AS n
    """, uid=uid, sid=sid).single()["n"]
    if is_owner and len(owners) <= 1:
        raise ValueError("A space needs at least one owner; add another owner first.")
    session.run("""
        MATCH (:User {user_id: $uid})-[r:MEMBER_OF]->(:Space {space_id: $sid}) DELETE r
    """, uid=uid, sid=sid)


def members(session, sid):
    return [dict(r) for r in session.run("""
        MATCH (u:User)-[r:MEMBER_OF]->(:Space {space_id: $sid})
        RETURN u.username AS username, r.role AS role ORDER BY u.username
    """, sid=sid)]


def space_info(session, sid):
    rec = session.run("MATCH (s:Space {space_id: $sid}) RETURN s {.*} AS s", sid=sid).single()
    if not rec:
        raise AccessError("No such space.")
    s = dict(rec["s"])
    s.pop("created_at", None)
    return s


def set_public_graph(session, sid, on):
    session.run("MATCH (s:Space {space_id: $sid}) SET s.public_graph = $on", sid=sid, on=bool(on))


def public_space(session, slug):
    """space_id of a space whose graph anyone may see (redacted), else None."""
    rec = session.run("""
        MATCH (s:Space {slug: $slug}) WHERE s.public_graph = true RETURN s.space_id AS id
    """, slug=(slug or "").lower()).single()
    return rec["id"] if rec else None


def list_users(session):
    return [dict(r) for r in session.run("""
        MATCH (u:User)
        OPTIONAL MATCH (u)-[r:MEMBER_OF]->(s:Space)
        WITH u, collect(s.slug + ':' + r.role) AS spaces
        RETURN u.username AS username, coalesce(u.is_server_admin, false) AS admin,
               coalesce(u.disabled, false) AS disabled, spaces ORDER BY u.username
    """)]


def set_disabled(session, username, disabled):
    n = session.run("MATCH (u:User {username: $u}) SET u.disabled = $d RETURN count(u) AS n",
                    u=username.lower(), d=bool(disabled)).single()["n"]
    if not n:
        raise ValueError(f"No user '{username}'.")

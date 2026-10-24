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
import ipaddress
import re
import threading
from collections import OrderedDict
import secrets
import time
import uuid
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

ROLES = ("viewer", "editor", "owner")
# What a token may do, on top of the user's role. Personal access tokens (jjk_)
# carry all three; an OAuth sign-in carries what the user approved.
SCOPES = ("jejak:read", "jejak:write", "jejak:admin")
ACCESS_PREFIX, REFRESH_PREFIX = "jja_", "jjr_"
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
    scopes: tuple = SCOPES
    client: str = None                          # OAuth client name, if any
    client_id: str = None                       # OAuth client id, if any

    @property
    def read_spaces(self):
        return list(self.roles) if "jejak:read" in self.scopes else []

    @property
    def write_spaces(self):
        if "jejak:write" not in self.scopes:
            return []
        return [s for s, r in self.roles.items() if r in WRITE_ROLES]

    @property
    def can_admin(self):
        """Manage members, users, tokens and public views (still subject to role)."""
        return "jejak:admin" in self.scopes

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
    if len(password or "") < 12:
        raise ValueError("Passwords need at least 12 characters.")
    if len(password) > 1024:
        raise ValueError("Passwords may be at most 1024 characters.")
    return _argon(_hasher.hash, password)


# --- login throttling ---------------------------------------------------------------
#
# In memory, bounded (oldest keys evicted), and keyed so it cannot be used to
# lock people out cheaply:
#   net:<addr or IPv6 /64>     every attempt counts, recorded BEFORE checking,
#                              so a parallel burst cannot slip past the limit
#   user-net:<user>|<net>      failures for one account from one network
#   user:<user>                failures for one account from everywhere (high)
# Password hashing (argon2, 64 MiB each) runs at most ARGON_SLOTS at a time.

WINDOW = 15 * 60
LIMITS = {"net": 30, "user-net": 10, "user": 100}
MAX_KEYS = 100_000
ARGON_SLOTS = 4
_FAILS = OrderedDict()
_LOCK = threading.Lock()
_ARGON = threading.BoundedSemaphore(ARGON_SLOTS)
_DUMMY_HASH = None


def net_of(addr):
    """The address, or its /64 for IPv6 (one host usually owns a whole /64)."""
    try:
        ip = ipaddress.ip_address((addr or "?").split("%")[0])
    except ValueError:
        return str(addr)[:64]
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _throttled(key):
    kind = key.split(":", 1)[0]
    now = time.time()
    with _LOCK:
        hits = [t for t in _FAILS.get(key, ()) if now - t < WINDOW]
        if hits:
            _FAILS[key] = hits
        else:
            _FAILS.pop(key, None)
        return len(hits) >= LIMITS.get(kind, 10)


def _fail(key):
    with _LOCK:
        _FAILS.setdefault(key, []).append(time.time())
        _FAILS.move_to_end(key)
        while len(_FAILS) > MAX_KEYS:
            _FAILS.popitem(last=False)


_GOOD = OrderedDict()   # (username, net) that signed in successfully


def _forget_one(key):
    with _LOCK:
        hits = _FAILS.get(key)
        if hits:
            hits.pop()
            if not hits:
                _FAILS.pop(key, None)


def _argon(fn, *a):
    if not _ARGON.acquire(timeout=10):
        raise AuthError("The server is busy. Try again in a moment.")
    try:
        return fn(*a)
    finally:
        _ARGON.release()


def _dummy_hash():
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _argon(_hasher.hash, secrets.token_urlsafe(16))
    return _DUMMY_HASH


# --- lookups ---------------------------------------------------------------------------

def _principal(session, user_id, token_id=None, scopes=SCOPES, client=None, client_id=None):
    rec = session.run("""
        MATCH (u:User {user_id: $uid}) WHERE coalesce(u.disabled, false) = false
        OPTIONAL MATCH (u)-[r:MEMBER_OF]->(s:Space) WHERE coalesce(r.pending, false) = false
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
        token_id=token_id, scopes=tuple(scopes), client=client, client_id=client_id)


def authenticate(session, token):
    """The principal behind a bearer token, or AuthError.

    Accepts personal access tokens (jjk_) and OAuth access tokens (jja_).
    Refresh tokens (jjr_) only work at the token endpoint, never here."""
    if not token or not token.startswith((TOKEN_PREFIX, ACCESS_PREFIX)):
        raise AuthError("Missing or malformed token.")
    rec = session.run("""
        MATCH (t:Token {hash: $hash})-[:OF]->(u:User)
        WHERE coalesce(t.revoked, false) = false
          AND coalesce(t.kind, 'pat') IN ['pat', 'access']
          AND (t.expires_at IS NULL OR t.expires_at > datetime())
        OPTIONAL MATCH (t)-[:IN_GRANT]->(g:Grant)
        WITH t, u, g
        WHERE g IS NULL OR coalesce(g.revoked, false) = false
        SET t.last_used_at = datetime()
        RETURN t.token_id AS token_id, u.user_id AS user_id, t.scopes AS scopes,
               g.client_name AS client, g.client_id AS client_id
    """, hash=hash_token(token)).single()
    if not rec:
        raise AuthError("Invalid, expired or revoked token.")
    # Only a token stored without scopes (a personal token from before scopes) gets all;
    # an empty list means none, never everything.
    scopes = SCOPES if rec["scopes"] is None else tuple(x for x in rec["scopes"] if x in SCOPES)
    return _principal(session, rec["user_id"], rec["token_id"], scopes, rec["client"], rec["client_id"])


def login(session, username, password, token_name, client_addr="?", days=None, scopes=None):
    """Exchange a username and password for a new token (shown once)."""
    uid = verify_password(session, username, password, client_addr)
    return create_token(session, uid, token_name or "login", days, scopes)


def verify_password(session, username, password, client_addr="?"):
    """user_id if the password is right, else AuthError (throttled)."""
    net = f"net:{net_of(client_addr)}"
    if _throttled(net):
        raise AuthError("Too many sign-in attempts from your network. Wait 15 minutes.")
    _fail(net)  # counted before checking, so parallel requests cannot all pass
    username = (username or "").lower()
    if not USERNAME.match(username) or len(password or "") > 1024:
        raise AuthError("Wrong username or password.")
    user_keys = [f"user-net:{username}|{net[4:]}"]
    # The account-wide limit (attacks spread over many networks) never blocks a
    # network this account has signed in from before, so it cannot be used to
    # lock the owner out.
    if (username, net) not in _GOOD:
        user_keys.append(f"user:{username}")
    if any(_throttled(k) for k in user_keys):
        raise AuthError("Too many failed sign-ins for this account. Wait 15 minutes.")
    rec = session.run("""
        MATCH (u:User {username: $username}) WHERE coalesce(u.disabled, false) = false
        RETURN u.user_id AS user_id, u.password_hash AS hash
    """, username=username).single()
    try:
        # Same work for unknown users, so timing does not reveal who exists.
        ok = _argon(_hasher.verify, rec["hash"] if rec and rec["hash"] else _dummy_hash(), password or "")
        ok = ok and bool(rec and rec["hash"])
    except (VerificationError, InvalidHashError):
        ok = False
    if not ok:
        for k in user_keys:
            _fail(k)
        raise AuthError("Wrong username or password.")
    _forget_one(net)   # successes do not use up a shared network's budget
    with _LOCK:
        _GOOD[(username, net)] = time.time()
        while len(_GOOD) > MAX_KEYS:
            _GOOD.popitem(last=False)
    return rec["user_id"]


def create_token(session, user_id, name, days=None, scopes=None):
    """scopes None = all (CLI and scripts); the browser asks for read only."""
    token = new_token()
    session.run("""
        MATCH (u:User {user_id: $uid})
        CREATE (t:Token {token_id: $tid, hash: $hash, name: $name, created_at: datetime(),
                         revoked: false, kind: 'pat', scopes: $scopes,
                         expires_at: CASE WHEN $days IS NULL THEN null
                                          ELSE datetime() + duration({days: $days}) END})
        CREATE (t)-[:OF]->(u)
    """, uid=user_id, tid=str(uuid.uuid4()), hash=hash_token(token), name=name, days=days,
         scopes=None if scopes is None else ["jejak:read"] + [x for x in scopes if x != "jejak:read"])
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
    return create_token(session, consume_invite(session, code, client_addr), token_name or "invite")


def consume_invite(session, code, client_addr="?"):
    """user_id for a valid invite code, which is used up; else AuthError (throttled)."""
    key = f"net:{net_of(client_addr)}"
    if _throttled(key):
        raise AuthError("Too many attempts from your network. Wait 15 minutes.")
    _fail(key)
    # Lock the node first, then check `used`: two parallel redemptions cannot both pass.
    rec = session.run("""
        MATCH (i:Invite {hash: $hash})-[:FOR]->(u:User)
        SET i._lock = true
        WITH i, u
        WHERE i.used = false AND i.expires_at > datetime() AND coalesce(u.disabled, false) = false
        SET i.used = true, i.used_at = datetime()
        RETURN u.user_id AS uid
    """, hash=hash_token(code or "")).single()
    if not rec:
        raise AuthError("Invite code is invalid, already used or expired. Ask an admin for a new one: "
                        "jejak user invite <username>")
    return rec["uid"]


def list_tokens(session, user_id):
    """Personal access tokens and connected apps (OAuth grants), newest first."""
    tokens = [dict(r) for r in session.run("""
        MATCH (t:Token)-[:OF]->(:User {user_id: $uid})
        WHERE coalesce(t.kind, 'pat') = 'pat'
        RETURN t.token_id AS id, 'token' AS kind, t.name AS name,
               toString(t.created_at) AS created_at, toString(t.last_used_at) AS last_used_at,
               toString(t.expires_at) AS expires_at, coalesce(t.revoked, false) AS revoked
        ORDER BY t.created_at DESC
    """, uid=user_id)]
    apps = [dict(r) for r in session.run("""
        MATCH (g:Grant)-[:OF]->(:User {user_id: $uid})
        OPTIONAL MATCH (t:Token {kind: 'access'})-[:IN_GRANT]->(g)
        WITH g, max(t.last_used_at) AS last
        RETURN g.grant_id AS id, 'app' AS kind, g.client_name AS name, g.scopes AS scopes,
               g.redirect_host AS sends_to, toString(g.created_at) AS created_at,
               toString(last) AS last_used_at, coalesce(g.revoked, false) AS revoked
        ORDER BY g.created_at DESC
    """, uid=user_id)]
    return tokens + apps


def revoke_token(session, token_id, user_id=None):
    """Revoke a personal token or a connected app (and every token it holds).
    Limited to `user_id`'s own unless user_id is None (server admin)."""
    n = session.run("""
        MATCH (t:Token)-[:OF]->(u:User)
        WHERE t.token_id STARTS WITH $tid AND coalesce(t.kind, 'pat') = 'pat'
          AND ($uid IS NULL OR u.user_id = $uid)
        SET t.revoked = true, t.revoked_at = datetime()
        RETURN count(t) AS n
    """, tid=token_id, uid=user_id).single()["n"]
    n += session.run("""
        MATCH (g:Grant)-[:OF]->(u:User)
        WHERE g.grant_id STARTS WITH $tid AND ($uid IS NULL OR u.user_id = $uid)
        SET g.revoked = true, g.revoked_at = datetime()
        WITH g OPTIONAL MATCH (t:Token)-[:IN_GRANT]->(g)
        SET t.revoked = true
        RETURN count(DISTINCT g) AS n
    """, tid=token_id, uid=user_id).single()["n"]
    return n


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


def _owner_count(session, sid):
    return session.run("""
        MATCH (:User)-[r:MEMBER_OF {role: 'owner'}]->(:Space {space_id: $sid})
        WHERE coalesce(r.pending, false) = false RETURN count(r) AS n
    """, sid=sid).single()["n"]


def _is_owner(session, sid, uid):
    return session.run("""
        MATCH (:User {user_id: $uid})-[r:MEMBER_OF {role: 'owner'}]->(:Space {space_id: $sid})
        WHERE coalesce(r.pending, false) = false RETURN count(r) AS n
    """, uid=uid, sid=sid).single()["n"] > 0


def set_member(session, sid, uid, role, invited_by=None):
    """Give `uid` a role in a space.

    A current member's role changes at once. Someone who is not a member yet
    is only INVITED when `invited_by` is set (a space owner acting through the
    API): the space stays invisible to them until they accept, so an owner
    cannot push content into another person's agent context. Server admins
    (admin CLI, user creation) add members directly."""
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
    current = session.run("""
        MATCH (:User {user_id: $uid})-[r:MEMBER_OF]->(:Space {space_id: $sid})
        RETURN r.role AS role, coalesce(r.pending, false) AS pending
    """, uid=uid, sid=sid).single()
    if current and not current["pending"] and current["role"] == "owner" and role != "owner" \
            and _owner_count(session, sid) <= 1:
        raise ValueError("A space needs at least one owner; make someone else owner first.")
    pending = bool(invited_by) and (current is None or current["pending"])
    session.run("""
        MATCH (u:User {user_id: $uid}), (s:Space {space_id: $sid})
        MERGE (u)-[r:MEMBER_OF]->(s)
        SET r.role = $role, r.since = coalesce(r.since, datetime()), r.pending = $pending,
            r.invited_by = CASE WHEN $pending THEN $by ELSE r.invited_by END
    """, uid=uid, sid=sid, role=role, pending=pending, by=invited_by)
    return "invited" if pending else "member"


def answer_invitation(session, uid, slug, accept):
    rec = session.run("""
        MATCH (:User {user_id: $uid})-[r:MEMBER_OF {pending: true}]->(s:Space {slug: $slug})
        WITH r, s
        CALL (r, s) { WITH r WHERE $accept SET r.pending = false, r.accepted_at = datetime() }
        CALL (r) { WITH r WHERE NOT $accept DELETE r }
        RETURN s.slug AS slug
    """, uid=uid, slug=(slug or "").lower(), accept=bool(accept)).single()
    if not rec:
        raise ValueError(f"No pending invitation to '{slug}'.")


def invitations(session, uid):
    return [dict(r) for r in session.run("""
        MATCH (:User {user_id: $uid})-[r:MEMBER_OF {pending: true}]->(s:Space)
        RETURN s.slug AS slug, r.role AS role, r.invited_by AS invited_by ORDER BY s.slug
    """, uid=uid)]


def remove_member(session, sid, uid):
    if _is_owner(session, sid, uid) and _owner_count(session, sid) <= 1:
        raise ValueError("A space needs at least one owner; add another owner first.")
    session.run("""
        MATCH (:User {user_id: $uid})-[r:MEMBER_OF]->(:Space {space_id: $sid}) DELETE r
    """, uid=uid, sid=sid)


def members(session, sid):
    return [dict(r) for r in session.run("""
        MATCH (u:User)-[r:MEMBER_OF]->(:Space {space_id: $sid})
        RETURN u.username AS username, r.role AS role, coalesce(r.pending, false) AS pending
        ORDER BY u.username
    """, sid=sid)]


def revoke_all_other(session, user_id, keep_token_id=None):
    """After a password change: every other token and connected app stops working."""
    session.run("""
        MATCH (t:Token)-[:OF]->(:User {user_id: $u})
        WHERE $keep IS NULL OR t.token_id <> $keep
        SET t.revoked = true, t.revoked_at = datetime()
    """, u=user_id, keep=keep_token_id)
    session.run("""
        MATCH (g:Grant)-[:OF]->(:User {user_id: $u}) SET g.revoked = true, g.revoked_at = datetime()
    """, u=user_id)


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

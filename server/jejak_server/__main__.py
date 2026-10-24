"""
jejak-server — run the server, or administer it from the machine it runs on
(like mysqladmin: talks to Neo4j directly, no token needed).

    python -m jejak_server serve
    python -m jejak_server admin init --admin alice [--token-file F]
    python -m jejak_server admin migrate --owner alice
    python -m jejak_server admin user create bob [--admin] [--space team --role editor]
    python -m jejak_server admin user invite bob [--hours 24]
    python -m jejak_server admin user list | disable bob | enable bob
    python -m jejak_server admin user password bob          (reads the password from stdin)
    python -m jejak_server admin token create alice --name laptop --file ~/.jejak/token
    python -m jejak_server admin token revoke <token-id-prefix>
    python -m jejak_server admin space create team --owner alice
    python -m jejak_server admin space member team bob editor | remove
    python -m jejak_server admin space public team on|off
    python -m jejak_server admin status

Secrets are never printed unless asked: tokens go to a chmod-600 file;
invite codes are printed because they are single-use and expire.
"""
import argparse
import getpass
import json
import os
import sys

from . import auth, schema
from .config import driver, settings


def _write_secret(path, value):
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    os.chmod(path, 0o600)


def _read_password():
    if sys.stdin.isatty():
        a = getpass.getpass("New password: ")
        if a != getpass.getpass("Again: "):
            raise SystemExit("Passwords differ.")
        return a
    return sys.stdin.readline().rstrip("\n")


def admin(args):
    with driver().session() as s:
        a = args.action
        if a == "init":
            left = schema.apply(s)
            existing = s.run("MATCH (u:User {username: $u}) RETURN u.user_id AS id",
                             u=args.admin.lower()).single()
            if existing:
                uid = existing["id"]
                print(f"Admin '{args.admin}' already exists; schema is up to date.")
            else:
                uid, _ = auth.create_user(s, args.admin, None, is_server_admin=True)
                print(f"Created server admin '{args.admin}' with personal space '{args.admin}'.")
            if args.token_file:
                _write_secret(args.token_file, auth.create_token(s, uid, args.token_name))
                print(f"Wrote a token to {args.token_file} (chmod 600).")
            if left:
                print(f"This database has {left} projects/sessions from before the server. "
                      f"Run: python -m jejak_server admin migrate --owner {args.admin}")
        elif a == "migrate":
            sid = s.run("MATCH (u:User {username: $u}) RETURN u.default_space AS sid",
                        u=args.owner.lower()).single()
            if not sid or not sid["sid"]:
                raise SystemExit(f"No user '{args.owner}'. Run: admin init --admin {args.owner}")
            s.close()
            schema.migrate(driver(), sid["sid"])
            print("Migration done. Nothing was deleted; memory and link counts are unchanged.")
        elif a == "status":
            print(json.dumps({**schema.counts(s), "users": len(auth.list_users(s)),
                              "spaces": s.run("MATCH (x:Space) RETURN count(x) AS n").single()["n"],
                              "accept_prompts": settings()["accept_prompts"]}, indent=1))
        elif a == "user":
            user_cmd(s, args)
        elif a == "token":
            if args.op == "create":
                uid = auth.user_id(s, args.name_or_id)
                token = auth.create_token(s, uid, args.label, args.days)
                if args.file:
                    _write_secret(args.file, token)
                    print(f"Wrote a token for {args.name_or_id} to {args.file} (chmod 600).")
                elif args.print_token:
                    print(token)
                else:
                    raise SystemExit("Give --file PATH (recommended) or --print to show the token.")
            elif args.op == "revoke":
                print(f"Revoked {auth.revoke_token(s, args.name_or_id)} token(s).")
            elif args.op == "list":
                print(json.dumps(auth.list_tokens(s, auth.user_id(s, args.name_or_id)), indent=1))
        elif a == "space":
            space_cmd(s, args)


def user_cmd(s, args):
    op = args.op
    if op == "list":
        print(json.dumps(auth.list_users(s), indent=1))
        return
    if not args.username:
        raise SystemExit("Give a username.")
    if op == "create":
        uid, _ = auth.create_user(s, args.username, None, args.admin)
        if args.space:
            auth.set_member(s, auth.space_id(s, args.space), uid, args.role)
        code = auth.create_invite(s, uid, "admin-cli", args.hours)
        print(f"Created '{args.username}'. Single-use invite code (valid {args.hours} h):\n{code}\n"
              f"They run: jejak login <server-url> --invite   and paste it.")
    elif op == "invite":
        code = auth.create_invite(s, auth.user_id(s, args.username), "admin-cli", args.hours)
        print(f"Single-use invite code for {args.username} (valid {args.hours} h):\n{code}")
    elif op == "password":
        auth.set_password(s, args.username, _read_password())
        print(f"Password set for {args.username}.")
    elif op in ("disable", "enable"):
        auth.set_disabled(s, args.username, op == "disable")
        print(f"{args.username} {op}d.")


def space_cmd(s, args):
    op = args.op
    if op == "list":
        print(json.dumps(s.run("""
            MATCH (x:Space) OPTIONAL MATCH (u:User)-[r:MEMBER_OF]->(x)
            RETURN x.slug AS slug, x.public_graph AS public_graph,
                   collect(u.username + ':' + r.role) AS members ORDER BY slug
        """).data(), indent=1))
    elif op == "create":
        owner = auth.user_id(s, args.owner) if args.owner else None
        auth.create_space(s, args.slug, args.title, owner)
        print(f"Created space '{args.slug}'" + (f" owned by {args.owner}." if owner else "."))
    elif op == "member":
        sid = auth.space_id(s, args.slug)
        if args.role == "remove":
            auth.remove_member(s, sid, auth.user_id(s, args.username))
            print(f"Removed {args.username} from {args.slug}.")
        else:
            auth.set_member(s, sid, auth.user_id(s, args.username), args.role)
            print(f"{args.username} is {args.role} in {args.slug}.")
    elif op == "public":
        auth.set_public_graph(s, auth.space_id(s, args.slug), args.state == "on")
        print(f"Public graph for {args.slug}: {args.state}.")


def main():
    ap = argparse.ArgumentParser(prog="jejak-server")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the HTTP + MCP server")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)

    ad = sub.add_parser("admin", help="administer users, spaces and the schema")
    asub = ad.add_subparsers(dest="action", required=True)
    i = asub.add_parser("init", help="apply the schema and create the first server admin")
    i.add_argument("--admin", required=True)
    i.add_argument("--token-file", help="also write a token for the admin here (chmod 600)")
    i.add_argument("--token-name", default="this-machine")
    m = asub.add_parser("migrate", help="move a pre-server graph into a user's personal space")
    m.add_argument("--owner", required=True)
    asub.add_parser("status")

    u = asub.add_parser("user")
    u.add_argument("op", choices=["create", "invite", "list", "password", "disable", "enable"])
    u.add_argument("username", nargs="?")
    u.add_argument("--admin", action="store_true", help="make a server admin")
    u.add_argument("--space", help="also add to this space")
    u.add_argument("--role", default="viewer", choices=auth.ROLES)
    u.add_argument("--hours", type=int, default=24, help="invite lifetime")

    t = asub.add_parser("token")
    t.add_argument("op", choices=["create", "revoke", "list"])
    t.add_argument("name_or_id", help="username (create, list) or token id prefix (revoke)")
    t.add_argument("--name", dest="label", default="token")
    t.add_argument("--days", type=int)
    t.add_argument("--file")
    t.add_argument("--print", dest="print_token", action="store_true")

    sp = asub.add_parser("space")
    spsub = sp.add_subparsers(dest="op", required=True)
    spsub.add_parser("list")
    c = spsub.add_parser("create")
    c.add_argument("slug")
    c.add_argument("--owner")
    c.add_argument("--title")
    mb = spsub.add_parser("member")
    mb.add_argument("slug")
    mb.add_argument("username")
    mb.add_argument("role", choices=list(auth.ROLES) + ["remove"])
    pb = spsub.add_parser("public")
    pb.add_argument("slug")
    pb.add_argument("state", choices=["on", "off"])

    args = ap.parse_args()
    if args.cmd == "serve":
        import uvicorn
        from .app import create_app
        s = settings()
        uvicorn.run(create_app(), host=args.host or s["host"], port=args.port or s["port"],
                    proxy_headers=True, forwarded_allow_ips=os.environ.get("JEJAK_TRUSTED_PROXIES", "127.0.0.1"),
                    log_level="warning", access_log=False)
    else:
        try:
            admin(args)
        except ValueError as e:
            raise SystemExit(str(e))


if __name__ == "__main__":
    main()

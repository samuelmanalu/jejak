#!/usr/bin/env python3
"""
Jejak CLI — a client of a Jejak server.

Knowledge:
  jejak save <type> <content> [-d DIR] [-f FILE] [--supersedes ID] [--visibility public] [--space S]
  jejak check-duplicates <content> [-d DIR]
  jejak ask <question> [-d DIR]        jejak search <query>
  jejak map [DIR]   jejak topics [DIR]   jejak rank [DIR] [-a]   jejak export [DIR] [-a]
  jejak links <id>  jejak get <id> [--json]   jejak session <id> [--json]   jejak stats
  jejak delete <id> -r <why>   jejak restore <id>   jejak visibility <public|confidential> <id>...

Connection and access:
  jejak login <url> [--invite | --token-stdin]    jejak logout    jejak whoami    jejak status
  jejak space list | create <slug> | use <slug> [DIR] | members <slug>
              | add-member <slug> <user> <viewer|editor|owner> | remove-member <slug> <user>
              | accept <slug> | decline <slug>   (invitations from space owners)
              | public <slug> on|off
  jejak token list | create <name> [--days N] [--file F] | revoke <id>
  jejak user list | create <name> [--space S --role R] [--admin] | invite <name> | disable|enable <name>
  jejak password

Types: decision, learning, finding, issue, solution, architecture, convention, context
Every error ends with a `hint:` line naming the command that fixes it.
"""
import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jejak_client as client  # noqa: E402
import jejak_common as jejak  # noqa: E402

VALID_TYPES = ["decision", "learning", "finding", "issue", "solution", "architecture", "convention",
               "context", "artifact"]


def fail(message, hint=None, code=1):
    print(f"error: {message}", file=sys.stderr)
    if hint:
        print(f"hint: {hint}", file=sys.stderr)
    sys.exit(code)


def emit(data, as_json):
    if as_json:
        print(json.dumps(data, indent=1))
    elif isinstance(data, str):
        print(data)
    else:
        for k, v in data.items():
            print(f"{k}: {v if not isinstance(v, (list, dict)) else json.dumps(v)}")


def _secret_from(prompt, env_name):
    """A secret from the environment, piped stdin, or a hidden prompt — never argv."""
    if os.environ.get(env_name):
        return os.environ[env_name].strip()
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    return getpass.getpass(prompt).strip()


# --- knowledge commands (run on the server) ---------------------------------------

def knowledge(args):
    a = vars(args).copy()
    command = a.pop("command")
    directory = a.pop("directory", None)
    space = a.pop("space", None)
    session = a.pop("session", None)
    for k in ("as_json", "func"):
        a.pop(k, None)
    if command == "save" and a.get("type") not in VALID_TYPES:
        fail(f"invalid type '{a.get('type')}'", "use one of: " + ", ".join(VALID_TYPES))
    ctx = client.build_ctx(directory, session, space)
    if command in ("search", "stats", "links", "get", "session", "visibility", "delete") and not directory:
        ctx.pop("project_key", None)  # not project-scoped: don't guess one from cwd
    output, code = client.run(command, a, ctx)
    if output:
        print(output)
    sys.exit(code)


# --- connection --------------------------------------------------------------------

def cmd_login(args):
    server = client.check_server_url(args.url, args.insecure)
    cfg = client.load_config()
    if args.invite:
        code = _secret_from("Invite code: ", "JEJAK_INVITE")
        token = client.request("POST", "/v1/auth/invite", {"code": code, "token_name": jejak.machine_name()},
                               auth=False, server=server)["token"]
    elif args.token_stdin:
        token = _secret_from("Token: ", "JEJAK_TOKEN")
    else:
        if not sys.stdin.isatty():
            fail("A password login needs a terminal.",
                 f"Let the user run it: ! jejak login {server}   (or use --invite / --token-stdin)")
        username = args.username or input("Username: ").strip()
        password = getpass.getpass("Password: ")
        token = client.request("POST", "/v1/auth/login",
                               {"username": username, "password": password,
                                "token_name": jejak.machine_name()},
                               auth=False, server=server)["token"]
    cfg.update({"server": server, "token": token, "insecure_for": server if args.insecure else None})
    cfg.pop("insecure", None)
    me = client.request("GET", "/v1/me", cfg=cfg)
    cfg["accepts_prompts"] = bool(me.get("accepts_prompts"))
    client.save_config(cfg)
    print(f"Logged in to {server} as {me['username']} (token saved in {client.CONFIG}, chmod 600).")
    print(f"Default space: {me['default_space']}. Spaces: "
          + ", ".join(f"{s['slug']} ({s['role']})" for s in me["spaces"]))


def cmd_logout(args):
    cfg = client.load_config()
    if cfg.get("token"):
        try:
            client.request("POST", "/v1/auth/logout", {}, cfg=cfg, timeout=5)
        except client.ClientError:
            pass
    cfg.pop("token", None)
    client.save_config(cfg)
    print("Logged out; the token was revoked on the server and removed here.")


def cmd_whoami(args):
    me = client.request("GET", "/v1/me")
    if args.as_json:
        emit(me, True)
        return
    print(f"{me['username']}{' (server admin)' if me['server_admin'] else ''} "
          f"on {client.load_config().get('server')}")
    print(f"Default space: {me['default_space']}")
    for s in me["spaces"]:
        flags = ", public graph" if s["public_graph"] else ""
        print(f"  {s['slug']:24s} {s['role']}{flags}")
    for inv in me.get("invitations") or []:
        print(f"  invited to {inv['slug']} as {inv['role']} by {inv['invited_by']}: "
              f"jejak space accept {inv['slug']}  (or decline)")


def cmd_status(args):
    """One look at everything an agent needs to know before using Jejak."""
    cfg = client.load_config()
    st = {"server": cfg.get("server"), "logged_in": bool(cfg.get("token")), "reachable": False,
          "user": None, "space_here": None, "project_here": None,
          "queued_offline": len(client.outbox_items())}
    key, _ = jejak.project_identity(os.getcwd())
    st["project_here"] = key
    hint = None
    if not st["server"]:
        hint = "jejak login <server-url>"
    else:
        try:
            client.request("GET", "/healthz", auth=False, timeout=5)
            st["reachable"] = True
            if st["logged_in"]:
                me = client.request("GET", "/v1/me", timeout=5)
                st["user"] = me["username"]
                st["space_here"] = client.space_for(os.getcwd(), cfg) or me["default_space"]
                role = {s["slug"]: s["role"] for s in me["spaces"]}.get(st["space_here"])
                st["role_here"] = role or "not a member"
                if not role:
                    hint = "jejak space list, then: jejak space use <slug>"
                elif role == "viewer":
                    hint = "You can read here but not save. Ask an owner for editor."
                if st["queued_offline"]:
                    st["sent_from_queue"] = client.flush_outbox()
                    st["queued_offline"] = len(client.outbox_items())
            else:
                hint = f"jejak login {st['server']}"
        except client.ClientError as e:
            hint = e.hint or str(e)
    st["ok"] = bool(st["reachable"] and st["user"] and hint is None)
    if hint:
        st["hint"] = hint
    emit(st, args.as_json)
    sys.exit(0 if st["ok"] else 1)


def cmd_password(args):
    if not sys.stdin.isatty():
        fail("Changing a password needs a terminal.", "Let the user run: ! jejak password")
    old = getpass.getpass("Current password (empty if you never set one): ")
    new = getpass.getpass("New password (12+ characters): ")
    if new != getpass.getpass("Again: "):
        fail("The new passwords differ.")
    client.request("POST", "/v1/auth/password", {"old_password": old, "new_password": new})
    print("Password changed.")


# --- spaces, tokens, users -------------------------------------------------------------

def cmd_space(args):
    op = args.op
    if op == "list":
        me = client.request("GET", "/v1/me")
        emit(me["spaces"], True) if args.as_json else cmd_whoami(args)
    elif op == "create":
        emit(client.request("POST", "/v1/spaces", {"slug": args.slug, "name": args.title}), args.as_json)
    elif op == "use":
        directory = os.path.realpath(os.path.abspath(args.directory or os.getcwd()))
        me = client.request("GET", "/v1/me")
        if args.slug not in {s["slug"] for s in me["spaces"]}:
            fail(f"You are not a member of '{args.slug}'.", "jejak space list")
        cfg = client.load_config()
        cfg.setdefault("spaces", {})[directory] = args.slug
        client.save_config(cfg)
        print(f"Knowledge saved under {directory} now goes to space '{args.slug}'.")
    elif op == "members":
        emit(client.request("GET", f"/v1/spaces/{args.slug}/members"), True)
    elif op == "add-member":
        emit(client.request("PUT", f"/v1/spaces/{args.slug}/members/{args.username}", {"role": args.role}),
             args.as_json)
    elif op == "remove-member":
        emit(client.request("DELETE", f"/v1/spaces/{args.slug}/members/{args.username}"), args.as_json)
    elif op in ("accept", "decline"):
        emit(client.request("POST", f"/v1/spaces/{args.slug}/{op}"), args.as_json)
    elif op == "public":
        r = client.request("PATCH", f"/v1/spaces/{args.slug}", {"public_graph": args.state == "on"})
        url = client.load_config().get("server", "") + f"/s/{args.slug}"
        print(f"Public graph for {args.slug}: {args.state}." + (f" Anyone can open {url} "
              "(only entries marked public show their text)." if r.get("public_graph") else ""))


def cmd_token(args):
    if args.op == "list":
        emit(client.request("GET", "/v1/tokens")["tokens"], True)
    elif args.op == "create":
        if not args.file:
            fail("Tokens are written to a file, never printed.",
                 f"jejak token create {args.name} --file ~/.jejak/{args.name}.token")
        token = client.request("POST", "/v1/tokens", {"name": args.name, "days": args.days})["token"]
        path = os.path.expanduser(args.file)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        os.chmod(path, 0o600)
        print(f"Token '{args.name}' written to {path} (chmod 600). Use it as "
              "'Authorization: Bearer <token>' for MCP clients; revoke with: jejak token revoke <id>")
    elif args.op == "revoke":
        emit(client.request("DELETE", f"/v1/tokens/{args.name}"), args.as_json)


def cmd_user(args):
    op = args.op
    if op == "list":
        emit(client.request("GET", "/v1/users")["users"], True)
    elif op == "create":
        r = client.request("POST", "/v1/users", {"username": args.name, "server_admin": args.admin,
                                                  "space": args.space, "role": args.role, "hours": args.hours})
        print(f"Created {r['username']}. Single-use invite code (valid {args.hours} h): {r['invite']}")
        print(f"They run: jejak login {client.load_config().get('server')} --invite   and paste the code.")
    elif op == "invite":
        r = client.request("POST", f"/v1/users/{args.name}/invite", {"hours": args.hours})
        print(f"Single-use invite code for {args.name} (valid {args.hours} h): {r['invite']}")
    elif op in ("disable", "enable"):
        emit(client.request("POST", f"/v1/users/{args.name}/{op}"), args.as_json)


# --- parser ----------------------------------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(prog="jejak", description="Jejak CLI (client of a Jejak server)")
    p.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command")

    def k(name, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(func=knowledge)
        return sp

    s = k("save", help="Save a knowledge entry")
    s.add_argument("type")
    s.add_argument("content", nargs="+")
    s.add_argument("-d", "--directory")
    s.add_argument("-s", "--session")
    s.add_argument("-f", "--file")
    s.add_argument("--supersedes")
    s.add_argument("--visibility", choices=jejak.VISIBILITIES)
    s.add_argument("--space", help="space to save to (default: this directory's space)")
    m = k("map", help="Knowledge map for a project")
    m.add_argument("directory", nargs="?")
    se = k("session", help="All knowledge from a session")
    se.add_argument("session_id")
    se.add_argument("--json", action="store_true")
    q = k("search", help="Search across all your spaces")
    q.add_argument("query", nargs="+")
    t = k("topics", help="Topic clusters for a project")
    t.add_argument("directory", nargs="?")
    k("stats", help="Counts across your spaces")
    r = k("rank", help="Knowledge ranked by relevance")
    r.add_argument("directory", nargs="?")
    r.add_argument("-a", "--all", action="store_true")
    li = k("links", help="Connections of one memory")
    li.add_argument("memory_id")
    dup = k("check-duplicates", help="Existing knowledge to compare before saving")
    dup.add_argument("content", nargs="+")
    dup.add_argument("-d", "--directory")
    ex = k("export", help="Export as Markdown")
    ex.add_argument("directory", nargs="?")
    ex.add_argument("-a", "--all", action="store_true")
    ask = k("ask", help="Answer a question from Jejak")
    ask.add_argument("question", nargs="+")
    ask.add_argument("-d", "--directory")
    g = k("get", help="One memory in full")
    g.add_argument("memory_id")
    g.add_argument("--json", action="store_true")
    de = k("delete", help="Soft-delete a memory")
    de.add_argument("memory_id")
    de.add_argument("-r", "--reason")
    v = k("visibility", help="Mark memories public or confidential")
    v.add_argument("visibility", choices=jejak.VISIBILITIES)
    v.add_argument("memory_ids", nargs="+")
    rs = k("restore", help="Bring a deleted memory back")
    rs.add_argument("memory_id")
    rs.add_argument("-d", "--directory")
    rs.add_argument("-s", "--session")

    lg = sub.add_parser("login", help="Connect this machine to a Jejak server")
    lg.add_argument("url")
    lg.add_argument("-u", "--username")
    lg.add_argument("--invite", action="store_true", help="redeem a single-use invite code (stdin or JEJAK_INVITE)")
    lg.add_argument("--token-stdin", action="store_true", help="use an existing token (stdin or JEJAK_TOKEN)")
    lg.add_argument("--insecure", action="store_true", help="allow http:// to another machine (not recommended)")
    lg.set_defaults(func=cmd_login)
    sub.add_parser("logout").set_defaults(func=cmd_logout)
    sub.add_parser("whoami").set_defaults(func=cmd_whoami)
    sub.add_parser("status", help="Is Jejak ready here? (server, login, space, offline queue)").set_defaults(func=cmd_status)
    sub.add_parser("password", help="Change your password (interactive)").set_defaults(func=cmd_password)

    sp = sub.add_parser("space", help="Spaces and members")
    sp.set_defaults(func=cmd_space)
    ssub = sp.add_subparsers(dest="op", required=True)
    ssub.add_parser("list")
    c = ssub.add_parser("create")
    c.add_argument("slug")
    c.add_argument("--title")
    u = ssub.add_parser("use", help="send saves from a directory to a space")
    u.add_argument("slug")
    u.add_argument("directory", nargs="?")
    mm = ssub.add_parser("members")
    mm.add_argument("slug")
    am = ssub.add_parser("add-member")
    am.add_argument("slug")
    am.add_argument("username")
    am.add_argument("role", choices=["viewer", "editor", "owner"])
    rm = ssub.add_parser("remove-member")
    rm.add_argument("slug")
    rm.add_argument("username")
    for name in ("accept", "decline"):
        ssub.add_parser(name, help=f"{name} an invitation to a space").add_argument("slug")
    pb = ssub.add_parser("public")
    pb.add_argument("slug")
    pb.add_argument("state", choices=["on", "off"])

    tk = sub.add_parser("token", help="Tokens for MCP clients and other machines")
    tk.set_defaults(func=cmd_token)
    tk.add_argument("op", choices=["list", "create", "revoke"])
    tk.add_argument("name", nargs="?", default="token", help="name (create) or id (revoke)")
    tk.add_argument("--days", type=int)
    tk.add_argument("--file")

    us = sub.add_parser("user", help="Users (server admins)")
    us.set_defaults(func=cmd_user)
    us.add_argument("op", choices=["list", "create", "invite", "disable", "enable"])
    us.add_argument("name", nargs="?")
    us.add_argument("--space")
    us.add_argument("--role", default="viewer", choices=["viewer", "editor", "owner"])
    us.add_argument("--admin", action="store_true")
    us.add_argument("--hours", type=int, default=24)
    # Agents write `jejak status --json` as often as `jejak --json status`: accept both.
    targets = [sp for n, sp in sub.choices.items() if n in ("status", "whoami", "token", "user")]
    targets += list(ssub.choices.values())
    for sp in targets:
        sp.add_argument("--json", dest="as_json", action="store_true", default=argparse.SUPPRESS)
    return p


def main():
    p = parser()
    args = p.parse_args()
    if not args.command:
        p.print_help()
        sys.exit(1)
    try:
        args.func(args)
    except client.ClientError as e:
        fail(str(e), e.hint)


if __name__ == "__main__":
    main()

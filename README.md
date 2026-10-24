# Jejak

**Persistent, self-ranking memory for AI coding agents: [Claude Code](https://claude.com/claude-code),
and any MCP client such as Gemini CLI, Antigravity or Cursor. Run it for yourself, or as
one server for your whole team.**

*Jejak* is Indonesian for *trail* — the trail of everything you and your AI assistant
worked out together, so the next session doesn't start from zero.

AI coding agents forget everything when a session ends. Jejak stores the decisions and
root causes worth keeping as a knowledge graph and brings the most relevant ones back
the next time you work in that project, whichever agent you use.

Jejak works like a database server. Installing it gives you a **Jejak server** (Neo4j
included). Everything else is a **client** that has to sign in: the Claude Code hooks,
the `jejak` CLI, and any MCP client. Run the server on your laptop for yourself, or on a
VPS for a team, with users, shared spaces and roles.

- **Any AI agent** (Claude, Gemini CLI, Cursor, Antigravity, …) gets the server's address
  and nothing else. The first time it connects, it opens your browser on the Jejak
  server; you sign in and click Allow. It then has full create/read/update/delete,
  limited to your spaces and your role.
- **Claude Code** also gets the automatic version: hooks extract what was learned in
  each session and load the right knowledge back into context on their own.

No manual note-taking. No `NOTES.md` that goes stale. The graph decides what matters
by scoring what you actually keep coming back to.

---

## Quick start

**Just me, on this laptop:**

```bash
git clone https://github.com/samuelmanalu/jejak.git && cd jejak && ./install.sh
jejak status
```

**A server for my team** (on a VPS with Docker and a domain name):

```bash
cd jejak/deploy && cp .env.example .env    # set JEJAK_DOMAIN and NEO4J_PASSWORD
docker compose up -d
docker compose run --rm jejak admin init --admin <you>     # prints your invite code
```

**Adding a teammate and their AI agent:**

```bash
jejak user create rani --space my-team --role editor    # you: prints a one-time invite code
```

Rani gives her agent `https://jejak.example.com/mcp`. Her browser opens, she enters the
invite code, chooses a password and clicks Allow. That's all; no token to copy. For the
automatic Claude Code hooks as well: `./install.sh --client https://jejak.example.com`.

---

## What it does

| | |
|---|---|
| **Remembers automatically** | A `Stop` hook reads each exchange, extracts what's worth keeping, and saves it — typed as `decision`, `learning`, `solution`, `convention`, `issue`, `architecture`, `context`, or `finding`. |
| **Ranks by real usage** | Every memory carries a `relevance_score` computed from hits, session spread, project spread, and graph connections — weighted by type, decayed over time. Nothing is manually promoted. |
| **Loads in layers** | Top-5 global knowledge loads in *every* session; top-10 project knowledge loads when you're in that directory; everything else is one `/jejak search` away. |
| **Shares with a team** | One server, many users. Knowledge lives in spaces (your personal one, plus team spaces) and every member has a role: owner, editor or viewer. |
| **Links across projects** | Memories connect through shared topics inside a space, so a lesson learned in one repo surfaces in another when it's relevant. |
| **Deduplicates semantically** | Before saving, a conflict-pruning pass compares against existing knowledge: duplicates are dropped, contradictions mark the old entry `superseded` (reversibly). |
| **Redacts secrets** | Tokens, API keys, passwords and connection strings are scrubbed *before* anything is stored, on the client and again on the server. |
| **Built for agents** | Most callers are AI agents, so every error ends with a `hint:` naming the command that fixes it, and `jejak status` answers "is Jejak ready here?" in one call. |

---

## How it works

```
 Claude Code hooks ─┐                    ┌────────────── Jejak server ──────────────┐
 jejak CLI ─────────┼── HTTPS + token ──►│ /v1   HTTP API   ┐                       │
 Gemini, Cursor, …  ┘   (MCP over HTTP)  │ /mcp  MCP        ├─ auth → role → query ─┼─► Neo4j
                                         │ /     graph page ┘                       │   (private)
                                         └──────────────────────────────────────────┘
```

| Hook | What it does |
|---|---|
| `SessionStart` → `jejak.py` | loads L1 + L2 memories into context |
| `UserPromptSubmit` → `jejak.py` | adds related past knowledge for the prompt's topics |
| `PostToolUse` → `jejak-artifact.py` | indexes documents and artifacts |
| `Stop` → `jejak-extract.py` | extracts and saves what was learned |
| `SessionEnd` → `jejak.py` | writes a short session summary |

The server is the only thing that talks to Neo4j. Neo4j Community has no roles, so
users, spaces and roles are enforced by the server, and Neo4j is never exposed.

### The graph

```
(Memory)--[:ABOUT]------->(Topic)
(Memory)--[:IN_PROJECT]-->(Project)          every Memory, Project and Session
(Memory)--[:IN_SESSION]-->(Session)          belongs to one space (space_id)
(Memory)--[:RELATES_TO {strength}]-->(Memory)   only ever inside one space
(Session)--[:IN_PROJECT]->(Project)
(User)--[:MEMBER_OF {role}]->(Space)         role: owner | editor | viewer
(Token)--[:OF]->(User)                        only the token's sha256 is stored
```

A project is identified by its git remote (`github.com/org/repo`), or by the directory
name when there is none, so teammates with different paths share one project.

### The scoring formula

```
score = (hits*3 + session_spread*5 + project_spread*10 + connections*2) * type_weight
```

Type weights: `architecture` 1.5 · `decision` 1.4 · `convention` 1.3 · `learning` 1.2 ·
`solution` 1.1 · `context` 1.0 · `issue` 0.9 · `finding` 0.8 · `prompt` 0.3.

A lesson that keeps proving useful across projects outranks one you saved once and
never touched. That's the whole design: **the math decides, not you.**

### Loading layers

| Layer | What | Loaded |
|---|---|---|
| **L1** | Top 5 by score, all projects | Every session, any directory |
| **L2** | Top 10 for the current project | On session start in that directory |
| **L3** | Everything else | On demand — `/jejak search`, `/jejak ask` |
| **L4** | Not in the graph | Falls back to reading the codebase |


---

## Install (personal: server + client on this machine)

**Requirements:** Python 3.9+, [uv](https://docs.astral.sh/uv/), and either Docker or
Homebrew (for Neo4j), plus Claude Code.

```bash
git clone https://github.com/samuelmanalu/jejak.git
cd jejak
./install.sh
```

`install.sh`:

1. installs `jejak-server` into `~/.jejak/app`;
2. finds or starts Neo4j on `127.0.0.1`: an existing Jejak install's Neo4j, else a Docker
   container, else Homebrew, with a random password kept in `~/.jejak/server.json` (chmod 600);
3. creates you as the server admin, with a personal space, and starts the server on
   `http://127.0.0.1:7700` (launchd on macOS, a systemd user service on Linux);
4. copies the hooks, CLI and skill into `~/.claude`, registers the hooks in
   `settings.json`, inserts the Jejak rules into `CLAUDE.md`, and adds a `jejak` command
   to `~/.local/bin`;
5. signs this machine in. No password or token is printed.

Then restart Claude Code and run `jejak status`.

**Upgrading from a pre-server Jejak** (one local Neo4j, no server): `./install.sh` reuses
your Neo4j. `./install.sh --migrate` also backs up your graph and moves every memory into
your personal space. Nothing is deleted. Copies of the same repo in two folders become one
project.

Re-running is safe. Hooks are matched on (event, matcher, command) and never registered
twice, and every file the installer rewrites is backed up first.

### What the CLAUDE.md rules do

They are what makes Jejak automatic rather than a tool you have to remember. They tell
Claude that the graph is the single interface for knowledge:

- **Add** — save after every meaningful action, typed, with `-f` linking it to the code
- **Update** — correct an entry with `--supersedes <id>`, never leave two entries
  disagreeing; run `check-duplicates` before every save
- **Look up** — query the graph with `jejak ask` / `search` / `map` *before* reading the
  codebase, and stop as soon as it answers


## Run a team server

On any VPS with Docker and a DNS name pointing at it:

```bash
git clone https://github.com/samuelmanalu/jejak.git && cd jejak/deploy
cp .env.example .env              # set JEJAK_DOMAIN and NEO4J_PASSWORD
docker compose up -d              # Neo4j (private) + jejak-server + Caddy (HTTPS)
docker compose run --rm jejak admin init --admin <you>
```

The last command prints a **single-use invite code** (valid 24 hours) for you. Run
`./backup.sh` nightly from cron. It dumps Neo4j to `deploy/backups/`; Neo4j pauses for
seconds, and clients queue their saves meanwhile.

### Connect a machine (client only)

```bash
./install.sh --client https://jejak.example.com
jejak login https://jejak.example.com --invite     # paste the invite code
jejak status
```

The client is standard-library Python, with no Neo4j or database driver.

### Users, spaces and roles

```bash
jejak space create billing-team                            # you are its owner
jejak user create rani --space billing-team --role editor  # server admins; prints an invite code
jejak space add-member billing-team dimas viewer           # owners: sends an invitation
jejak space accept billing-team                            # dimas: joins (or: decline)
jejak space members billing-team
jejak space use billing-team ~/work/billing    # saves under this folder go to billing-team
jejak user disable rani                                    # server admins
```

| Role | Can |
|---|---|
| `viewer` | read: search, ask, map, rank, links, graph |
| `editor` | read, plus save, update, delete, restore, set visibility |
| `owner` | edit, plus manage members and the space's public graph |

Joining a space is always the member's choice: an owner can only invite, and the space
stays invisible to the invited user until they accept. (Otherwise an owner could push
entries into someone else's agent context.) Server admins add members directly.

Reads cover every space you belong to; saves go to your default (personal) space unless
`jejak space use` or `--space` says otherwise. Knowledge is never linked across spaces.

**Signing in.** New users get a single-use invite code (valid 24 hours) instead of a
password or a long-lived token. If the code ends up in a chat transcript, it is useless
once it has been used or has expired. They redeem it either:

- the first time an MCP app opens the browser sign-in page, where they also choose a
  password; or
- with `jejak login <url> --invite`, then `jejak password`.

Scripts and CI use personal tokens:

```bash
jejak token create gemini --file ~/.jejak/gemini.token    # written to a file, never printed
jejak token list
jejak token revoke <id>
```

## Usage

```
/jejak save <type> <content>      Save a knowledge entry (dedup-checked)
/jejak save ... --supersedes <id> Update: replace an existing entry
/jejak save ... --space <slug>    Save to a specific space
/jejak check-duplicates <text>    Check for duplicates/conflicts before saving
/jejak search <query>             Search across all your spaces
/jejak ask <question>             Answer a question from the graph
/jejak map [directory]            Full knowledge map for a project
/jejak rank [directory] [-a]      Knowledge ranked by relevance score
/jejak links <memory_id>          Every connection for one memory
/jejak topics [directory]         Topic frequency clusters
/jejak session [session_id]       Everything from one session
/jejak stats                      Counts across your spaces
/jejak export [dir] [-a]          Export as Markdown documentation
/jejak get <memory_id> [--json]   One memory in full, any status
/jejak delete <memory_id> -r why  Soft-delete (hidden everywhere, kept for audit)
/jejak restore <memory_id>        Bring a deleted memory back as a new entry
/jejak visibility public <id>...  Mark memories public (default: confidential)

jejak status | whoami | login | logout | password
jejak space ... | user ... | token ...
```

Link knowledge to code with `-f`:

```bash
jejak save learning "Disbursal uses LAZY fetch to prevent OOM" \
  -f "src/main/java/Disbursal.java"
```

### For AI agents

- `jejak status` (add `--json` for JSON, before or after the command) reports the
  server, the login, the space and role for the current directory, pending invitations,
  and queued offline saves. It exits non-zero with a `hint:` when something needs fixing.
- Every error is `error: …` plus `hint: <command that fixes it>` on stderr. HTTP
  errors are `{"error": …, "hint": …}`.
- Nothing prompts for input except a password login, which is for the human:
  `! jejak login <url>`. Agents use `--invite` (code on stdin or in `JEJAK_INVITE`)
  or `--token-stdin`.
- Secrets never go in command arguments, and tokens are written only to chmod-600 files.

### Offline

If the server can't be reached, saves are queued in `~/.jejak/outbox/` (chmod 600) and
sent automatically by the next `jejak` command. Context lookups give up after 2 seconds,
so a session never waits on the server.

### Other AI tools (MCP): Gemini CLI, Antigravity, Cursor, Claude, …

The server is an MCP server, over streamable HTTP at `/mcp`. **Give your agent only the
URL.** On first use it opens your browser on the Jejak server. You sign in there (the
first time with your invite code, choosing a password), see which app is asking and
where access will go, and click Allow. The agent gets short-lived tokens and renews them
by itself; you never copy a token.

```json
{ "mcpServers": { "jejak": { "url": "https://jejak.example.com/mcp" } } }
```

```bash
claude mcp add --transport http jejak https://jejak.example.com/mcp     # Claude Code
```

You choose what a connected app may do: read (always), save and change knowledge
(ticked by default), and manage members, users and tokens (unticked by default). Your
role in each space still applies on top. Connected apps show up in `jejak token list`;
`jejak token revoke <id>` disconnects one immediately.

How the sign-in is protected (standard OAuth 2.1 for MCP):

- **PKCE (S256) is required**, so a stolen authorization code is useless without the
  client's secret verifier.
- **Codes only go to your own machine.** Apps may register:
  - `http://127.0.0.1` or `localhost` addresses (any port);
  - reverse-DNS app links (`com.example.app:/callback`);
  - Cursor's own callback.

  Websites are allowed only for allowlisted callbacks (`JEJAK_OAUTH_REDIRECT_ALLOWLIST`;
  Claude's by default). App links that hand the address to a browser or Office
  (`microsoft-edge:https://…` and similar) are refused, so a phishing site cannot get a
  code sent to it.
- **Codes are single-use and live 60 seconds.** Using one twice revokes everything it
  produced.
- **Access tokens live 1 hour; refresh tokens are replaced on every use.** If an old
  refresh token is presented again, it was stolen, so the whole sign-in is revoked.
- **Tokens are bound to this server's `/mcp`** and stored only as hashes. Refresh tokens
  never work as access tokens.
- **The consent page** is protected against forged submissions (a strict cookie plus a
  form token) and against being shown inside another site. It runs no script, escapes
  everything an app says about itself, and states in plain words where access goes.
- **Rate limits** on `/register`, `/authorize`, `/token` and `/revoke`, per network and
  server-wide. Password and invite attempts are throttled per network and per account,
  and a network you have signed in from before is never locked out of your account.
- **Reviewed adversarially.** An independent review attacked this code. Every finding was
  fixed (see the git history) and is covered by `tests/test_oauth.py` and
  `tests/test_server.py`.

Personal access tokens still work, for scripts, CI and clients without OAuth:

```bash
jejak token create ci --file ~/.jejak/ci.token     # then send Authorization: Bearer <token>
```

For clients that only speak stdio, `hooks/jejak-mcp.py` is a bridge that uses this
machine's `jejak login`:

```json
{ "mcpServers": { "jejak": { "command": "uv",
  "args": ["run", "--quiet", "--script", "/Users/<you>/.claude/hooks/jejak-mcp.py"],
  "env": { "JEJAK_CLIENT": "gemini" } } } }
```

Automatic capture stays Claude Code only. Other clients save when the model decides to,
guided by the server's instructions. Whatever a client reads from Jejak goes to that
client's model provider, so check that your organisation allows that.

### Visualize

Open the server in a browser (`http://127.0.0.1:7700` for a personal install) and sign
in. A force-directed map: colour by type, filter by project, pick a space, click to
inspect. It works on phones (drag, pinch, tap; details in a bottom sheet). Press `L` to
lay the map out again, `F` to fit it to the screen.

Every active memory is drawn, but each brings only its 10 strongest links
(`JEJAK_EDGES_PER_NODE` to change it). The trimming is display-only: every link stays
in Neo4j for ranking, `links` and `ask`.

#### Public view: share the map, keep the details

Every memory is `confidential` unless it is marked `public`:

```bash
jejak save decision "Dark mode is the default" --visibility public   # when saving
jejak visibility public 3f8b6a70 9c1d2e4f                           # later
jejak space public my-site on        # anyone can open https://<server>/s/my-site
```

The public view draws every memory and link of that space, but a confidential memory is
a faint, anonymous bubble. Its content, topics, projects and sessions are blanked inside
the Neo4j query, so they never reach the server process, let alone the browser. Prompts
always stay confidential.

### Bonus: `/recap`

A companion skill that fuses the graph, the prompt log, and your git commits into a
short end-of-day report:

```bash
/recap                  # today
/recap --yesterday
/recap --days 7 --detail
```

`/recap` reads Neo4j and the MySQL prompt log directly, so it runs on the machine that
hosts the server (a personal install).

---

## Moving knowledge

A team server replaces the old git-based sync: every machine signed in to one server
sees the same knowledge, live. To move knowledge from one Jejak database to another
(for example from a personal install to a team server), use `tools/jejak-sync.py` on
the hosts:

```bash
python3 tools/jejak-sync.py export --project ~/dev/myrepo -o myrepo.jsonl.gz    # source host
python3 tools/jejak-sync.py import myrepo.jsonl.gz                             # target host
python -m jejak_server admin migrate --owner <user>    # on the server: put them in a space
```

Bundles exclude prompts and links (links are rebuilt), and are redacted again on export.
A bundle is your knowledge in plaintext: move it the way you'd move a database dump,
never through a public repo.

## Security

- **Only the server touches Neo4j.** Neo4j listens on 127.0.0.1 (personal install) or
  only inside the compose network (team server).
- **Authentication.**
  - Passwords: argon2 hashes, at least 12 characters, at most 4 hashes computed at a time.
  - Tokens: 32 random bytes, stored only as sha256; they can expire and be revoked.
  - Invite codes: single-use, and they expire.
  - Throttling: every attempt counts per network (an IPv6 /64 counts as one), and
    failures count per account. Throttle memory is bounded.
  - Changing your password signs out every other token and connected app.
- **Abuse limits.** Request bodies are capped at 1 MiB, and saved knowledge at 50,000
  characters. Redaction patterns are bounded, so no input can make them run for minutes.
- **Authorization** happens on the server for every request. Every query is limited to
  the caller's spaces; a test fails if a memory query isn't. Tokens carry scopes (read,
  write, admin) on top of space roles, and the browser page uses a read-only token.
- **Membership is consent-based.** An owner can only invite; the space stays invisible
  until the invited user accepts.
- **Transport.** The client refuses to send a token over plain `http://` to another
  machine. The team server gets HTTPS from Caddy automatically.
- **Prompts stay on the device.** Hooks send only a prompt's detected topics. Raw
  prompts are stored only by a personal server, and only from the same machine.
- **Secrets are redacted before write**, on the client and again on the server:
  `ghp_*`, `sk-*`, `AKIA*`, `xox*`, Jejak's own `jjk_`/`jji_`/`jja_`/`jjr_`, `password:`/`token=`
  assignments, and `user:pass@host` connection strings.
- **Your knowledge is not in this repo.** This repo is the engine; the graph is yours.

> If you work on proprietary code, your graph will contain proprietary knowledge. Host
> the server where that knowledge is allowed to live, and back it up like a database.

---

## Contributing

Issues and PRs welcome.

| File | Role |
|---|---|
| `server/jejak_server/core.py` | Every command's queries, scoped to spaces — the only Cypher for knowledge |
| `server/jejak_server/auth.py` | Users, passwords, tokens, scopes, invites, spaces, roles, throttling |
| `server/jejak_server/app.py` | HTTP API and graph page |
| `server/jejak_server/mcp_tools.py` | MCP over HTTP at `/mcp`, rate limits |
| `server/jejak_server/oauth.py` | OAuth 2.1 for MCP clients: registration policy, codes, grants, refresh rotation |
| `server/jejak_server/consent.py` | The browser sign-in and approval page |
| `server/jejak_server/schema.py` | Schema and the pre-server migration |
| `server/jejak_server/__main__.py` | `serve` and the `admin` CLI |
| `hooks/jejak_common.py` | Redaction, topics, project identity, the one canonical scoring query |
| `hooks/jejak_client.py` | Client config, HTTP, offline outbox |
| `hooks/jejak-cli.py` | The `jejak` / `/jejak` command surface |
| `hooks/jejak.py` | Session lifecycle hooks (start / prompt / end) |
| `hooks/jejak-extract.py` | `Stop` hook — headless extraction of what was learned |
| `hooks/jejak-artifact.py` | `PostToolUse` hook — document & artifact indexing |
| `hooks/jejak-mcp.py` | stdio MCP bridge |
| `deploy/` | Dockerfile, docker-compose (Neo4j + server + Caddy), backups |
| `tests/` | `cd server && uv run --group dev pytest ../tests` (integration tests need a throwaway Neo4j; see the file header) |

One rule: **the scoring formula lives in `jejak_common.py` and nowhere else.**

---

## License

MIT — see [LICENSE](LICENSE).

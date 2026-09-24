# Jejak

**Persistent, self-ranking memory for [Claude Code](https://claude.com/claude-code).**

*Jejak* is Indonesian for *trail* — the trail of everything you and your AI assistant
worked out together, so the next session doesn't start from zero.

Claude Code forgets everything when a session ends. Jejak hooks into the session
lifecycle, extracts the decisions and root causes worth keeping, stores them as a
graph in Neo4j, and loads the most relevant ones back into context automatically the
next time you open a terminal in that project.

No manual note-taking. No `NOTES.md` that goes stale. The graph decides what matters
by scoring what you actually keep coming back to.

---

## What it does

| | |
|---|---|
| **Remembers automatically** | A `Stop` hook reads each exchange, extracts what's worth keeping, and saves it — typed as `decision`, `learning`, `solution`, `convention`, `issue`, `architecture`, `context`, or `finding`. |
| **Ranks by real usage** | Every memory carries a `relevance_score` computed from hits, session spread, project spread, and graph connections — weighted by type, decayed over time. Nothing is manually promoted. |
| **Loads in layers** | Top-5 global knowledge loads in *every* session; top-10 project knowledge loads when you're in that directory; everything else is one `/jejak search` away. |
| **Links across projects** | Memories connect through shared topics, so a lesson learned in one repo surfaces in another when it's relevant. |
| **Deduplicates semantically** | Before saving, a conflict-pruning pass compares against existing knowledge: duplicates are dropped, contradictions mark the old entry `superseded` (reversibly). |
| **Indexes your documents** | Markdown, PDFs, spreadsheets, notebooks and published artifacts are captured automatically and made searchable. |
| **Redacts secrets** | Tokens, API keys, passwords and connection strings are scrubbed *before* anything is written to disk. |

---

## How it works

```
            ┌──────────────── Claude Code session ────────────────┐
            │                                                     │
 SessionStart ──> jejak.py         loads L1 + L2 memories into context
 UserPromptSubmit ─> jejak.py      injects related past knowledge
                   prompt-logger.py  logs the prompt (MySQL)
 PostToolUse ────> jejak-artifact.py  indexes documents & artifacts
 Stop ───────────> jejak-extract.py   extracts + saves what was learned
 SessionEnd ─────> jejak.py         writes the session summary
            │                                                     │
            └─────────────────────┬───────────────────────────────┘
                                  ▼
                     Neo4j  (the knowledge graph)
                     MySQL  (flat prompt log)
```

### The graph

```
(Memory)--[:ABOUT]------->(Topic)
(Memory)--[:IN_PROJECT]-->(Project)
(Memory)--[:IN_SESSION]-->(Session)
(Memory)--[:RELATES_TO {strength}]-->(Memory)
(Session)--[:IN_PROJECT]->(Project)
```

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

## Set up a new machine

One command, for a machine that should pick up knowledge you already have elsewhere:

```bash
git clone https://github.com/samuelmanalu/jejak.git ~/dev/jejak
cd ~/dev/jejak
./setup-machine.sh
```

It checks prerequisites, stores a GitHub credential, collects this machine's own database
passwords, verifies Neo4j and MySQL are reachable *before* doing any work, installs
everything, connects to your private knowledge repo, pulls, verifies checksums and starts
the sync daemon. Idempotent — re-run it if a step fails.

Non-interactive, for a scripted build:

```bash
JEJAK_KNOWLEDGE_REPO=https://github.com/you/jejak-knowledge.git \
JEJAK_COMMIT_EMAIL=you@example.com \
JEJAK_INTERVAL=900 ./setup-machine.sh
```

Knowledge syncs; prompt history does not — that machine's `/recap` starts empty. Move a
`backup` tarball if you want the whole history.

## Install

**Requirements:** Python 3.9+, [Neo4j](https://neo4j.com/download/) 5.x or 2025+ (calendar
versions; Homebrew ships 2026.x), MySQL 8.x (`brew install mysql@8.4` — plain `mysql` is newer),
Claude Code.

```bash
git clone https://github.com/samuelmanalu/jejak.git
cd jejak
./install.sh
# edit ~/.claude/hooks/db-config.json with your credentials
./install.sh          # re-run: applies schema and finishes
```

That's the whole install. `install.sh` does all of it:

1. installs the Python dependencies
2. copies the hooks and skills into `~/.claude`
3. seeds `db-config.json` (and never overwrites one you already have)
4. applies the Neo4j and MySQL schema
5. **registers the hooks in `~/.claude/settings.json`**
6. **inserts the Jejak rules into `~/.claude/CLAUDE.md`**
7. verifies the CLI can reach the database

Steps 5 and 6 merge into whatever you already have — your own hooks, your own rules,
and any other top-level settings are preserved. The `CLAUDE.md` rules live between
`<!-- JEJAK:BEGIN -->` / `<!-- JEJAK:END -->` markers, so upgrading refreshes that block
in place and leaves the rest of the file untouched.

Re-running is safe: hooks are matched on (event, matcher, command) so nothing is ever
registered twice, and every file the installer rewrites is backed up as
`<file>.pre-jejak.<timestamp>` first.

Installing somewhere else:

```bash
CLAUDE_HOME=/path/to/.claude ./install.sh
```

Restart Claude Code, then:

```bash
/jejak stats
```

### What the CLAUDE.md rules do

They are what makes Jejak automatic rather than a tool you have to remember. They tell
Claude that the graph is the single interface for knowledge:

- **Add** — save after every meaningful action, typed, with `-f` linking it to the code
- **Update** — correct an entry with `--supersedes <id>`, never leave two entries
  disagreeing; run `check-duplicates` before every save
- **Look up** — query the graph with `jejak ask` / `search` / `map` *before* reading the
  codebase, and stop as soon as it answers

## Usage

```
/jejak save <type> <content>     Save a knowledge entry (dedup-checked)
/jejak save ... --supersedes <id> Update: replace an existing entry
/jejak check-duplicates <text>   Check for duplicates/conflicts before saving
/jejak search <query>            Search across all knowledge
/jejak ask <question>            Answer a question from the graph
/jejak map [directory]           Full knowledge map for a project
/jejak rank [directory] [-a]     Knowledge ranked by relevance score
/jejak links <memory_id>         Every connection for one memory
/jejak topics [directory]        Topic frequency clusters
/jejak session [session_id]      Everything from one session
/jejak stats                     Global stats
/jejak export [dir] [-a]         Export as Markdown documentation
```

Link knowledge to code with `-f`:

```bash
jejak save learning "Disbursal uses LAZY fetch to prevent OOM" \
  -f "src/main/java/Disbursal.java"
```

### Visualize

```bash
python3 ~/.claude/hooks/serve-graph.py     # http://127.0.0.1:8787
```

A force-directed canvas graph — colour by type, filter by project, click to inspect.
Bound to loopback only. Neo4j Browser at `http://localhost:7474` also works.

### Bonus: `/recap`

A companion skill that fuses the graph, the prompt log, and your git commits into a
short end-of-day report:

```bash
/recap                  # today
/recap --yesterday
/recap --days 7 --detail
```

---

## Syncing across machines

Jejak's knowledge lives in your local Neo4j, so a second machine starts empty. `tools/jejak-sync.py`
moves knowledge between them — for a one-off migration or an ongoing two-way sync.

```bash
# on the machine that has the knowledge
python3 tools/jejak-sync.py export
#   -> jejak-<host>-<timestamp>.jsonl.gz

# move it however you like: scp, git, USB, Dropbox

# on the other machine
python3 tools/jejak-sync.py inspect jejak-....jsonl.gz    # look before you leap
python3 tools/jejak-sync.py import  jejak-....jsonl.gz --dry-run
python3 tools/jejak-sync.py import  jejak-....jsonl.gz
```

Run it in both directions and the two machines converge.

### Changing laptops: full backup

`export` carries knowledge only. For a machine move — or a real backup — `backup` takes
everything: all memories *including* prompts, the MySQL prompt log, and your Jejak config.

```bash
python3 tools/jejak-sync.py backup
#   -> jejak-backup-<host>-<timestamp>.tar.gz
```

```
manifest.json                      what this is, where it came from
graph.jsonl.gz                     every memory, prompts included
prompt_logs.jsonl.gz               the MySQL prompt log
config/CLAUDE.md.jejak-block.md    your Jejak rules block
config/settings.hooks.json         your hook registrations
```

On the new laptop:

```bash
git clone https://github.com/samuelmanalu/jejak.git && cd jejak && ./install.sh
# edit ~/.claude/hooks/db-config.json
./install.sh                                          # schema + wiring
python3 tools/jejak-sync.py restore jejak-backup-....tar.gz
```

A whole working setup — 3,788 memories and 1,621 prompt-log rows — packs into under 1 MB.

Restore is a **merge**, not an overwrite, so it is safe to run onto a machine already in
use: memories dedupe on `memory_id`, prompt-log rows on `(machine_name, session_id,
created_at)`. `--dry-run` shows the plan first.

**Credentials are never in the backup.** `db-config.json` is excluded by design — move
your passwords yourself. The manifest records `contains_credentials: false`.

### Syncing through a private git repo

Instead of copying files around, point Jejak at a git repo and let it be the transport.

> **Two repos, always.** This repository is the *engine* and is public. Your knowledge goes
> in a **separate, private** repo — never here. `jejak-sync` enforces that: it calls the
> GitHub API before every push and refuses if the target is public.

```bash
# once, on each machine
python3 tools/jejak-sync.py remote init git@github.com:you/jejak-knowledge.git

# thereafter
python3 tools/jejak-sync.py push     # local graph -> repo
python3 tools/jejak-sync.py pull     # repo -> local graph (a merge)
python3 tools/jejak-sync.py sync     # pull then push: converge
```

The clone lives at `~/.claude/jejak-knowledge`, well outside any project.

**Storage is sharded, not one blob.** Memories are written to `knowledge/<first 2 chars of
memory_id>.jsonl` — 256 files, each sorted, with sorted keys. This matters:

| | |
|---|---|
| **Small diffs** | Adding one memory changes exactly two files: its shard and the manifest. Not a 500 KB blob rewritten every sync. |
| **Few conflicts** | Two machines adding different memories almost always land in different shards, so git merges them without a word. |
| **Readable history** | `git log -p knowledge/` shows what you learned, as text. |
| **Deterministic** | Re-running `push` with no new knowledge reports *nothing changed*. |

That last point took a fix worth knowing about: `last_accessed_at` is bumped by the session
hooks on every surfaced memory, so syncing it churned five shards per session with no
knowledge change. It is now treated as local telemetry and never leaves the machine.
`hit_count` *is* synced — it only moves on a real re-save — and merges as `max()`, so
"this proved useful" survives across machines.

Conflicts, if two machines really do edit the same entry, resolve the same way as any
import: newest `updated_at` wins, and `superseded` stays monotonic.

#### Syncing on a timer

`SessionEnd` only fires when a session ends cleanly, and it only pushes. For knowledge to
reach the repo regardless — and for other machines' knowledge to reach this one — install the
daemon. Each tick pulls first, then pushes anything new (or any commit a failed push left
behind), so two machines converge within one interval:

```bash
python3 tools/jejak-sync.py daemon install --interval 900   # every 15 min
python3 tools/jejak-sync.py daemon status
python3 tools/jejak-sync.py daemon run                      # one-shot, verbose
python3 tools/jejak-sync.py daemon uninstall
```

On macOS this writes a launchd agent (`tech.jejak.sync`), so it survives logout and reboot.
Elsewhere it writes the config and prints the crontab line to use. The interval lives in
`~/.claude/hooks/jejak-daemon.json` and the minimum is 60s.

**Detection is deterministic; the model only writes the message.** Whether knowledge is
unsynced is a set difference between the graph and the repo — a fact, not a judgement — so
no model sits in that path. An idle tick costs ~2s (mostly the `git pull`), makes no commit,
writes no log line and calls no model.

When there *is* new knowledge, Haiku (via the same `claude -p` path the extractor uses)
turns the commit subject into something you can read months later:

```
Jejak daemon: launchd scheduling and deterministic sync detection
Jejak sync: untested paths, autosync, hook caching
```

instead of `knowledge: 2380 memories from MMI0122312`. If the model is slow, unavailable or
returns nonsense, the sync still happens with a deterministic subject
(`knowledge: 3 learnings, 1 decision`) — `--no-summarize` skips it entirely.

A lockfile stops overlapping runs from fighting over the git index, and a lock older than
30 minutes is treated as stale, so a killed run never wedges the daemon permanently.

#### Deleting knowledge

Import only ever merges, so deleting a memory locally is not enough — another machine still
holding it pushes it straight back. Deletion therefore needs a tombstone:

```bash
python3 tools/jejak-sync.py forget 44925a40 --reason "was wrong about the pool size"
```

It shows what will go, asks for confirmation (`--yes` to skip), deletes locally, records the
tombstone, strips the record from the shards, and pushes. Every other machine deletes it at
its next `pull`, even one that never saw the original delete.

```
tombstones.jsonl
{"deleted_at":"...","machine":"...","memory_id":"...","reason":"...","type":"finding"}
```

Tombstones are append-only and additive — writing them never truncates another machine's
entries — and `push` applies them *before* collecting knowledge, so a machine that has not
pulled yet cannot re-add what someone else deleted. `verify` reports the count and flags any
tombstoned id that reappears in a shard as `RESURRECTED`.

Most of the time you want `--supersedes` instead: it corrects an entry while keeping the
history. Reserve `forget` for knowledge that should not exist at all. The content stays
recoverable from git history either way.

#### Making the repo dependable

A sync tool you have to remember to run, that fails when another machine got there first,
and that cannot tell you whether the stored data is intact, is not storage. Four properties
close that gap.

**1. It runs itself.** A `SessionEnd` hook (`jejak-autosync.py`) pushes when a session ends.
Detached, so it never delays exit; a no-op unless a remote is configured; failures are logged,
never raised, because a missed push is recoverable and a blocked session is not.

**2. Push converges instead of failing.** A plain `git push` is rejected the moment another
machine has pushed — leaving your knowledge committed locally and never stored. `push` now
merges and retries (3 attempts). If a shard conflicts, it is re-derived from the union of both
sides rather than hand-resolved. And because deletion only ever happens through tombstones, a
repo record this machine has not pulled yet is kept, never erased by a push from the graph.

**3. Integrity is checkable.** The manifest carries a SHA-256 and record count per shard:

```bash
python3 tools/jejak-sync.py verify
```
```
  shards        : 256
  records       : 2361  (manifest says 2361)
  duplicate ids : 0

  OK - every shard matches its checksum.
```

This catches the failure that matters most — a shard truncated at a *record boundary* is
still valid JSON, so it would load fine while silently having lost knowledge. `verify`
reports it as `CHANGED 05.jsonl (manifest 11 records, file has 2)`. Recovery is
`git checkout -- knowledge/`, because every version is in git history.

**4. Corruption is diagnosed, not tracebacked.** A malformed line is reported as
`knowledge/07.jsonl:14: Unterminated string`, and in strict mode it *stops* the run — a
store that silently drops records is worse than one that refuses to load.

#### The private-repo gate

```
REFUSING: you/jejak-knowledge is PUBLIC.
Your graph contains employer-internal detail. Make the repo private:
    gh repo edit you/jejak-knowledge --visibility private
```

It also refuses when it *cannot verify* — a 404, or no token available — rather than
failing open. Override with `--allow-unverified` only for a non-GitHub remote you trust.

Failing closed nearly broke the automation: the `SessionEnd` hook has no `GITHUB_TOKEN` in
its environment, so the gate refused every unattended push. Rather than weaken it to trust a
verification recorded at setup time, the gate now asks `git credential fill` for the very
credential git already uses to push. Nothing new is stored, and every push — attended or
not — is still verified live against the API.

> Measured on a real working graph: **42% of non-prompt memories contained
> employer-internal detail** — service names, production config, ticket and MR numbers.
> A public knowledge repo is a data leak, not a privacy preference.

### What makes this safe

| Concern | How it's handled |
|---|---|
| **Re-running** | `memory_id` is a UUID and is the merge key. Import is idempotent and order-independent — re-importing the same bundle writes nothing. |
| **Two machines editing the same entry** | Newest `updated_at` wins. `superseded` is monotonic: once an entry is superseded anywhere, it stays superseded, so a correction is never silently undone. |
| **Different usernames / layouts** | Project paths export as `~/`-relative and expand to the local `$HOME` on import, so `/Users/sam/dev/x` and `/home/sami/dev/x` stay one project instead of forking. |
| **Bundle size** | `RELATES_TO` is *derived* from shared topics and outnumbers memories ~150:1 (558k edges for 3.7k memories on a real graph). It is never exported — it's rebuilt locally after import. A full knowledge base is a few hundred KB. |
| **Scores** | `relevance_score` depends on hits, spread and connection count, all of which change when graphs merge. It is not exported; it's recomputed locally with the canonical formula. |
| **Secrets** | Content is redacted on write *and* again on export. |
| **Noise** | Raw prompt nodes are excluded by default (`--include-prompts` to keep them). |

Partial exports, for sharing one project's knowledge with a teammate rather than your whole graph:

```bash
python3 tools/jejak-sync.py export --project ~/dev/myrepo
python3 tools/jejak-sync.py export --since 2026-01-01
```

> The bundle is your knowledge in plaintext. If it covers proprietary work, move it the way
> you'd move a database dump — not through a public repo.

## Security

Jejak stores everything **locally**. Nothing is sent anywhere.

- **Secrets are redacted before write.** GitHub PATs (`ghp_*`), OpenAI keys (`sk-*`),
  AWS keys (`AKIA*`), Slack tokens (`xox*`), `password:`/`token=` assignments, and
  `user:pass@host` connection strings are scrubbed in `jejak_common.redact()` — applied
  to prompts *and* extracted knowledge.
- **`db-config.json` is gitignored** and `chmod 600`. Only the example ships.
- **`serve-graph.py` binds to 127.0.0.1** and serves exactly one file — never the
  hooks directory, which holds your credentials.
- **Your knowledge never leaves your machine.** This repo is the engine; the graph
  it builds is yours and is not part of it.

> If you work on proprietary code, your graph will contain proprietary knowledge.
> Back it up like a database, not like a notes folder — and never commit a dump.

---

## Contributing

Issues and PRs welcome. The codebase is small and deliberately dependency-light:

| File | Role |
|---|---|
| `hooks/jejak_common.py` | Shared helpers — config, redaction, topics, the one canonical scoring query |
| `hooks/jejak.py` | Session lifecycle hooks (start / prompt / end) |
| `hooks/jejak-cli.py` | The `/jejak` command surface |
| `hooks/jejak-extract.py` | `Stop` hook — headless extraction of what was learned |
| `hooks/jejak-artifact.py` | `PostToolUse` hook — document & artifact indexing |
| `hooks/prompt-logger.py` | MySQL prompt log |
| `hooks/recap-cli.py` | `/recap` data collector |
| `hooks/serve-graph.py` | Loopback server for the visualization |
| `tools/bootstrap.py` | Merges hooks into `settings.json`, Jejak rules into `CLAUDE.md` |
| `tools/apply_schema.py` | Applies the Neo4j + MySQL schema from `db-config.json` |
| `tools/jejak-sync.py` | Export / import knowledge bundles between machines |

One rule: **the scoring formula lives in `jejak_common.py` and nowhere else.** Hooks and
CLI must both call it, or rankings drift apart.

---

## License

MIT — see [LICENSE](LICENSE).

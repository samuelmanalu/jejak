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

## Install

**Requirements:** Python 3.9+, [Neo4j](https://neo4j.com/download/) 5.x, MySQL 8.x, Claude Code.

```bash
git clone https://github.com/samuelmanalu/jejak.git
cd jejak
./install.sh
```

Then:

1. **Credentials** — edit `~/.claude/hooks/db-config.json` (created from the example, `chmod 600`).
2. **Schema**
   ```bash
   cypher-shell -u neo4j -p YOURPASS -f schema/neo4j.cypher
   mysql -u root -p < schema/mysql.sql
   ```
3. **Wire it up** — merge `config/settings.example.json` into `~/.claude/settings.json`,
   and append `config/CLAUDE.md.snippet` to `~/.claude/CLAUDE.md` (this is what makes
   Claude save knowledge without being asked).

Restart Claude Code, then:

```bash
/jejak stats
```

---

## Usage

```
/jejak save <type> <content>     Save a knowledge entry (dedup-checked)
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

One rule: **the scoring formula lives in `jejak_common.py` and nowhere else.** Hooks and
CLI must both call it, or rankings drift apart.

---

## License

MIT — see [LICENSE](LICENSE).

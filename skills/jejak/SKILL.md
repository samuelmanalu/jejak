---
name: jejak
description: "Jejak — save, search, map, rank, and browse linked knowledge across sessions and projects. Backed by Neo4j."
user-invocable: true
keywords: [jejak, knowledge, graph, memory, remember, recall, save, map, search, rank, context, session, neo4j]
---

# Jejak (jejak)

Save, search, map, rank, and browse linked knowledge stored in Neo4j. Knowledge is automatically segregated by project directory, linked by shared topics, and ranked by a continuous relevance score.

## Usage

```
/jejak                              Interactive menu
/jejak save <type> <content>        Save a knowledge entry (with dedup check)
/jejak map [directory]              Show full knowledge map for a project
/jejak search <query>               Search across all knowledge
/jejak session [session_id]         Show all knowledge from a session
/jejak topics [directory]           Show topic frequency clusters
/jejak stats                        Global stats across all projects and machines
/jejak rank [directory] [-a]        Show knowledge ranked by relevance score
/jejak links <memory_id>            Show all connections for a specific memory
/jejak check-duplicates <content>   Check for duplicates and conflicts before saving
/jejak export [dir] [-a]            Export knowledge as Markdown documentation
/jejak ask <question>               Answer a question from the knowledge graph
```

## Question Flow (No Arguments or just `/jejak`)

If the user types just `/jejak` with no subcommand, present this menu:

```yaml
question: "What would you like to do with the Knowledge Graph?"
header: "Jejak Action"
options:
  - label: "Save knowledge"
    description: "Store a decision, learning, finding, or other knowledge"
  - label: "Map this project"
    description: "Show all knowledge for the current directory"
  - label: "Search"
    description: "Search across all stored knowledge"
  - label: "Rank & Stats"
    description: "View relevance rankings, stats, or topic clusters"
```

### If "Save knowledge" is selected

```yaml
question: "What type of knowledge are you saving?"
header: "Type"
options:
  - label: "decision"
    description: "A choice made — why option A over B"
  - label: "learning"
    description: "Something discovered — root cause, insight, pattern"
  - label: "solution"
    description: "A fix or resolution that worked"
  - label: "finding"
    description: "An observation or discovery during investigation"
```

Then ask:
```yaml
question: "What is the knowledge content? (describe it in your own words)"
header: "Content"
```

For types not in the menu, the full list is: `decision`, `learning`, `finding`, `issue`, `solution`, `architecture`, `convention`, `context`.

## Execution

All commands use the `jejak-cli.py` script at `~/.claude/hooks/jejak-cli.py`.

### save

```bash
python3 ~/.claude/hooks/jejak-cli.py save <type> "<content>" -d "$(pwd)" -s "$SESSION_ID" -f "<source_file>"
```

- `<type>`: decision, learning, finding, issue, solution, architecture, convention, context
- `-d`: current working directory
- `-s`: session ID if available
- `-f`: (optional) source file path related to this knowledge (e.g. the file where a pattern was found)

**Before saving, you MUST run `check-duplicates` first (see Deduplication below).**

When the knowledge relates to a specific file, ALWAYS include `-f` with the file path. This creates a code link so `/jejak links` and `/jejak ask` can point back to the relevant code.

After saving, confirm to the user with the memory ID, score, and project name.

### map

```bash
python3 ~/.claude/hooks/jejak-cli.py map "<directory>"
```

If no directory given, use `$(pwd)`. Present output grouped by knowledge type with connections.

### search

```bash
python3 ~/.claude/hooks/jejak-cli.py search <query>
```

Present results showing type, project, topics, score, and content.

### session

```bash
python3 ~/.claude/hooks/jejak-cli.py session <session_id>
```

If no session_id given, try to use the current session ID.

### topics

```bash
python3 ~/.claude/hooks/jejak-cli.py topics "<directory>"
```

If no directory given, use `$(pwd)`.

### stats

```bash
python3 ~/.claude/hooks/jejak-cli.py stats
```

### rank

```bash
python3 ~/.claude/hooks/jejak-cli.py rank "<directory>"    # current project
python3 ~/.claude/hooks/jejak-cli.py rank -a               # all projects
```

Shows all non-prompt knowledge sorted by relevance score with layer labels (L1/L2/L3) and score breakdown (hits, sessions, connections).

### links

```bash
python3 ~/.claude/hooks/jejak-cli.py links <memory_id>
```

`<memory_id>` can be a full ID or a short prefix (e.g. `d8e7879f`). Shows the memory's details, topics, projects, sessions, and all connected memories with relationship strength.

### check-duplicates

```bash
python3 ~/.claude/hooks/jejak-cli.py check-duplicates "<content>" -d "$(pwd)"
```

Pulls existing knowledge from the same project for semantic comparison. See Deduplication below.

### export

```bash
python3 ~/.claude/hooks/jejak-cli.py export "<directory>"    # one project
python3 ~/.claude/hooks/jejak-cli.py export -a               # all projects
```

Exports all non-prompt knowledge as a structured Markdown document grouped by project and type. Useful for onboarding teammates, writing docs, or backing up knowledge.

### ask

```bash
python3 ~/.claude/hooks/jejak-cli.py ask "<question>" -d "$(pwd)"
```

Searches the graph using keywords from the question, returns matching knowledge with related connections, then you (Claude) synthesize an answer from the results. If the graph doesn't have enough, suggest `/jejak search` or codebase exploration.

## Auto-Save Behavior

When Claude identifies important knowledge during a conversation, it MUST proactively save it using this skill. Auto-save triggers:

1. **After solving a bug** — save as `learning` (root cause) + `solution` (the fix)
2. **After making an architectural choice** — save as `decision` or `architecture`
3. **After discovering a codebase pattern** — save as `convention`
4. **After finding an issue** — save as `issue`
5. **After answering a "why" question** — save as `context`

When auto-saving, briefly tell the user what was saved. Example:
> Saved to Knowledge Graph: [learning] score:42 Kafka consumer timeout caused by DB pool exhaustion

## Deduplication (MANDATORY before every save)

Before saving ANY knowledge, you MUST check for semantic duplicates. This is a two-step process:

### Step 1: Pull existing knowledge

```bash
python3 ~/.claude/hooks/jejak-cli.py check-duplicates "<content to save>" -d "$(pwd)"
```

### Step 2: You (Claude) judge whether it's a duplicate

Compare the new content against the returned existing entries. A duplicate is NOT just an exact text match — it means the **same intent, meaning, or information**, even if worded differently.

Examples of duplicates (DO NOT save):
- New: "DB pool exhaustion causes Kafka timeout" vs Existing: "Kafka consumer timeout was caused by slow DB connection pool exhaustion"
- New: "Chose Redis for caching" vs Existing: "We decided to use Redis as our caching layer"

Examples of NOT duplicates (DO save):
- New: "DB pool size should be 25" vs Existing: "Kafka consumer timeout was caused by DB pool exhaustion" (root cause vs fix)
- New: "PaymentService uses retry pattern" vs Existing: "Modified PaymentService.java" (pattern vs action)

### Decision:
- If **duplicate found**: Do NOT save. Silently skip it.
- If **conflict found**: The new knowledge CONTRADICTS an existing entry (e.g. "pool size 10" vs "pool size 25"). Ask the user which is current. If the new one is correct, save it and note the old one is superseded.
- If **unique**: Proceed with the save command.
- If **no existing knowledge** (`NO_EXISTING_KNOWLEDGE`): Proceed — it's the first entry.

## Relevance Scoring & Layered Loading

Every memory has a continuous `relevance_score` computed from:

```
score = (hit_count * 3 + session_spread * 5 + project_spread * 10 + connection_count * 2) * type_weight
```

Type weights: architecture 1.5, decision 1.4, convention 1.3, learning 1.2, solution 1.1, context 1.0, issue 0.9, finding 0.8, prompt 0.3.

Knowledge self-organizes into loading layers by score:

| Layer | What | When loaded |
|---|---|---|
| L1 (global) | Top 5 by score across all projects | Every session, any directory |
| L2 (project) | Top 10 from current project (minus L1) | On SessionStart in that directory |
| L3 (deep) | Everything else in the graph | On demand via `/jejak search` or `/jejak links` |
| L4 (code) | Not in graph at all | Fall back to codebase exploration |

Scores update automatically on every hit. No manual promotion — the math decides.

## Neo4j Graph Structure

```
(Memory) --ABOUT--> (Topic)
(Memory) --IN_PROJECT--> (Project)
(Memory) --IN_SESSION--> (Session)
(Memory) --RELATES_TO {strength: N}--> (Memory)
(Session) --IN_PROJECT--> (Project)
```

Memory types: `decision`, `learning`, `finding`, `issue`, `solution`, `architecture`, `convention`, `context`, `artifact`, `prompt`

## Artifacts & Documents (auto-captured)

A `PostToolUse` hook (`jejak-artifact.py`) automatically indexes documents and published artifacts as `artifact` memories:
- **Documents** — `Write`/`NotebookEdit` of `.md/.html/.pdf/.csv/.docx/.pptx/.xlsx/.ipynb/…` (code files, deps, temp, and hook files are skipped). Title comes from the first heading; a short summary from the opening prose; the file path is stored as `source_file`.
- **Published artifacts** — the `Artifact` tool: title + description + the claude.ai URL.

Rewriting a document **supersedes** its previous version (only the latest is active; old versions stay for audit). Captured artifacts are searchable via `jejak search`/`jejak ask`, appear in the graph view, and are indexed per project. Runs detached so it never blocks a file write.

## Config

Database config is at `~/.claude/hooks/db-config.json` (chmod 600). Neo4j runs on `bolt://localhost:7687`.

## Visualization

- Neo4j Browser: http://localhost:7474
- Knowledge Graph Observatory: http://localhost:8787/jejak.html

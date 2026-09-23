---
name: recap
description: "Recap a day (or several) of work — pulls knowledge-graph memories, prompt history, and git commits for a time window and writes a short supervisor-ready bullet report (or the long engineering version with --detail)."
user-invocable: true
keywords: [recap, standup, summary, daily, day, yesterday, week, what did i do, eod, report, retrospective]
---

# recap

Reconstructs what actually happened in a work window from three local sources and turns it
into a narrative the user can paste into a standup, an EOD update, or a weekly report.

Sources (all local, all already populated by the Jejak hooks — no new instrumentation needed):

| Source | What it gives | Populated by |
|---|---|---|
| Neo4j `Memory` / `Session` / `Project` | learnings, decisions, findings, conventions, per-project grouping, relevance scores | `jejak.py`, `jejak-extract.py` (Stop hook), manual `/jejak save` |
| MySQL `claude_logs.prompt_logs` | every prompt asked, timestamped, tagged, grouped by project dir | `prompt-logger.py` (UserPromptSubmit hook) |
| git (`~/IdeaProjects`, `~/dev`, `~/.claude`, depth 5) | commits authored in window, branch, uncommitted file counts | the user's own commits |

## Usage

```
/recap                       Today (00:00 → now)
/recap yesterday             Yesterday, full day
/recap 2026-08-18            A specific calendar date
/recap week                  Last 7 days
/recap 3 days                Last N days
/recap sphere-platform       Today, narrowed to one project
/recap --detail              The long engineering version (default is the short report)
/recap --save                Recap, then save the summary to Jejak as a `context` memory
/recap --artifact            Recap, then publish it as a shareable Artifact page
```

## Step 1 — Collect

Always run the collector first. Never hand-roll the queries.

```bash
python3 ~/.claude/hooks/recap-cli.py                     # today
python3 ~/.claude/hooks/recap-cli.py --yesterday
python3 ~/.claude/hooks/recap-cli.py --date 2026-08-18
python3 ~/.claude/hooks/recap-cli.py --days 7
python3 ~/.claude/hooks/recap-cli.py --since 2026-08-20T09:00 --until 2026-08-20T13:00
python3 ~/.claude/hooks/recap-cli.py --no-git            # skip the repo scan (faster)
python3 ~/.claude/hooks/recap-cli.py --json              # structured, for further processing
```

Map the user's words to flags: bare `/recap` → no flags; `yesterday` → `--yesterday`;
`week`/`this week`/`last 7 days` → `--days 7`; a bare `YYYY-MM-DD` → `--date`; `N days` → `--days N`.
A project name in the args is NOT a flag — collect everything, then filter while writing.

The script takes ~5–8s (the git scan dominates). Each section degrades independently: a section
that prints `[unavailable]` means that source is down (Neo4j/MySQL not running) — say so in the
recap rather than silently omitting it, and note what is therefore missing.

## Step 2 — Read the raw report critically

Before writing anything, reason over the collected data:

- **Prompts are the storyline, memories are the substance, commits are the proof.** A project with
  many prompts but no memories and no commits was probably exploration that did not land — say
  that, don't inflate it.
- **Merge by project, not by source.** One project = one section that weaves its prompts,
  memories, and commits together.
- **Distinguish landed vs in-flight.** Commits = landed. Uncommitted files on a branch = in-flight
  and worth flagging as tomorrow's first task.
- **Promote the high-signal knowledge.** Prefer `learning`, `decision`, `issue`, `architecture`,
  and high `<score>` items for the narrative body; treat low-score `finding`s as detail.
- **Timestamps are local wall-clock.** Use them to show the shape of the day (what got picked up
  when, where context switched).
- **Never invent.** If the data does not show an outcome, say the outcome is unrecorded.

## Step 3 — Write the recap

**The default output is a short report for the user's supervisor, NOT an engineering write-up.**
This skill exists to feed a reporting flow upward. Write the long version only when the user
passes `--detail` or explicitly asks for the engineering depth.

### Default: the report (use this unless told otherwise)

One heading, then one flat bullet list. Six to eight bullets for a single project; add a short
heading per project only if more than one project is in scope.

```
**<Project> — <what the work was about> · <date>**

- <what was fixed/built and why it matters to someone who does not read code>
- <the next thing, one bullet each>
- <what is still open, in one bullet at the end>
```

Rules for the report — these are the point of the skill, follow them strictly:

- **No commit SHAs, no branch names, no memory ids, no file paths, no session ids.** The reader
  cannot use them. Keep them for `--detail`.
- **One bullet per outcome, one line each where possible.** If a bullet needs a sub-bullet, the
  bullet is doing too much — cut it down instead.
- **Lead with the effect, not the mechanism.** "Could have recommended caching where it does not
  pay off" beats "gap_s=0 was scored as a hit".
- **Translate every internal term.** No `L1/L2`, `jacoco`, `stage-release`, `dedup`, `P95`,
  `denominator`, `snapshot fan-in`, `prefix`. If a number needs the term to make sense, drop the
  number.
- **Numbers only when they carry the point**, and at most two or three in the whole report. Round
  them. Never build a table.
- **Say where a number came from** if it is not production: "measured on the test environment".
- **Cap the detail.** Five findings become one bullet naming the theme and the count, not five
  bullets. The reader wants to know a performance review happened and what came out of it.
- **Close with what is open** — one bullet, plainly: what is unfinished, what is waiting on
  someone else, what is documented but not yet scheduled.
- **Effort context is optional and at most one bullet** (how long, how many projects that day).
  Drop it entirely if the user did not ask about workload.
- **Never editorialise the user's productivity.** Report what happened; do not praise it.

After the report, if something material is missing or uncertain, add it as one or two plain
sentences BELOW the bullets, clearly outside the report — e.g. an unrecorded outcome, or work in
GitLab/meetings that this skill cannot see. Do not bury caveats inside the bullets the user is
going to paste.

### `--detail`: the engineering version

Only on request. Same content, expanded: per-project prose sections, commit shas, memory ids in
parens (so `/jejak links` works), the full findings list, `## Open threads / tomorrow`, and a
`## By the numbers` line (N prompts · N knowledge items · N commits across N repos · N sessions).

### Scoping

If the user names a project, report only that project. Mention the rest of the day in at most one
closing bullet ("one of six projects that day") so the context switching is visible — and only if
it is relevant to the reporting flow.

## Step 4 — Optional follow-ups

Only when asked (`--save`, `--artifact`) or when the user says yes to an offer:

**Save to Jejak.** Per the global auto-save rule, check duplicates first:
```bash
python3 ~/.claude/hooks/jejak-cli.py check-duplicates "<the headline + key outcomes>"
python3 ~/.claude/hooks/jejak-cli.py save context "<Day recap YYYY-MM-DD: ...>" -d "$(pwd)"
```
Save one compact `context` memory holding the headline and outcomes — not the whole narrative,
and never the individual items (they are already in the graph; re-saving them creates duplicates).

**Publish as an Artifact.** Load the `artifact-design` skill, write the recap to a file in the
scratchpad, publish, and hand back the link. Good for a weekly report someone else will read.

## Notes & known limits

- The prompt log records what was *asked*, not what was *answered*; outcomes come from memories
  and commits. A busy prompt count alone is not progress.
- Only work done *through Claude Code* is captured (plus any git commits, however authored).
  Meetings, reviews in GitLab/OpenProject, and terminal work outside a session are invisible —
  if the user's day included those, ask them to fill the gap rather than guessing.
- Git commits are filtered by `git config --global user.email`. Override with `--author`.
- Neo4j stores UTC; the collector already converts to local wall-clock. Do not re-shift times.
- Repo scan roots default to `~/IdeaProjects ~/dev ~/.claude` at depth 5. Override with
  `--roots ~/other ~/place`.

#!/usr/bin/env python3
"""
Stop hook: automatic, LLM-judged knowledge extraction.

Fires when the assistant finishes a turn. It does NOT block — it spawns a
detached background worker that:
  1. reads the last user<->assistant exchange from the transcript
  2. asks Haiku (via `claude -p`, headless, using existing Claude Code auth) to
     judge importance and CATEGORIZE anything worth keeping
  3. saves each important item to Jejak (dedup + scoring reused
     from jejak-cli.py)

Trivial chatter is ignored; decisions, explicit user instructions, root-cause
learnings, solutions, conventions, findings, and important context are saved.

Recursion guard: the worker sets JEJAK_EXTRACTING=1 before calling `claude -p`.
Every Jejak hook checks that env var and bails, so the headless call cannot log
its own prompts or trigger another extraction.
"""
import json
import os
import sys
import subprocess

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
import jejak_common as jejak

HOOKS_DIR = os.path.expanduser("~/.claude/hooks")
VALID_TYPES = {"decision", "learning", "finding", "issue", "solution",
               "architecture", "convention", "context"}
MAX_ITEMS = 8
MAX_EXCHANGE_CHARS = 6000

EXTRACT_PROMPT = """You are a knowledge extractor for a developer's knowledge graph (Jejak). Below is the most recent exchange between a user and an AI coding assistant, plus the EXISTING knowledge already stored for this project.

Extract ONLY genuinely important, reusable knowledge from the exchange. Save-worthy examples:
- a design/architectural DECISION
- something the user explicitly INSTRUCTED or decided ("we will...", "use X not Y", "always...")
- a root-cause LEARNING from a bug
- a SOLUTION that worked
- a codebase CONVENTION or pattern
- a notable FINDING from investigation
- important CONTEXT explaining "why"

IGNORE: greetings, acknowledgements, status updates, questions without answers,
trivial file reads, tool mechanics, anything not durably useful later.

Compare each candidate against the EXISTING knowledge:
- If an existing entry already captures the SAME information (even if worded differently), DO NOT include it (it's a duplicate).
- If the candidate CONTRADICTS or REPLACES an existing entry (e.g. a reversed decision, a changed value, a superseded approach), include it and set "supersedes" to that existing entry's id.
- Otherwise it is new; set "supersedes" to null.

For each item to save, output an object:
  {"type": "<decision|learning|solution|finding|issue|architecture|convention|context>",
   "content": "<concise, self-contained statement someone could use months later>",
   "supersedes": "<8-char existing id this replaces, or null>"}

Respond with ONLY a JSON array. If nothing is worth saving, respond with [].
Do not use any tools. Do not add prose before or after the JSON.

EXISTING KNOWLEDGE (id | type | content):
%s

EXCHANGE:
%s
"""


def extract_last_exchange(transcript_path):
    try:
        raw = open(transcript_path).read().splitlines()
    except OSError:
        return ""
    entries = []
    for ln in raw:
        try:
            entries.append(json.loads(ln))
        except Exception:
            pass
    if not entries:
        return ""

    # Find the last user message; take everything from there to the end.
    last_user = None
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        role = e.get("type") or e.get("message", {}).get("role")
        if role == "user":
            last_user = i
            break
    relevant = entries[last_user:] if last_user is not None else entries[-6:]

    texts = []
    for e in relevant:
        msg = e.get("message", {})
        role = (msg.get("role") or e.get("type") or "").upper()
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(p for p in parts if p)
        if text.strip():
            texts.append(f"{role}: {text.strip()}")

    joined = "\n\n".join(texts)
    return joined[-MAX_EXCHANGE_CHARS:]


def fetch_existing_knowledge(cwd, limit=40):
    """Active (non-superseded) project knowledge, as a list of (id8, type, content)."""
    from neo4j import GraphDatabase
    cfg = jejak.load_config("neo4j")
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        with driver.session() as s:
            rows = s.run("""
                MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {path: $path})
                WHERE m.type <> 'prompt' AND m.type <> 'session_summary'
                  AND coalesce(m.superseded, false) = false
                RETURN left(m.memory_id, 8) AS id, m.type AS type, m.content AS content
                ORDER BY m.relevance_score DESC LIMIT $limit
            """, path=cwd, limit=limit)
            return [(r["id"], r["type"], r["content"]) for r in rows]
    finally:
        driver.close()


def judge_and_categorize(exchange_text, existing):
    """Call Haiku headlessly to extract + categorize + flag conflicts."""
    existing_str = "\n".join(f"{i} | {t} | {c[:140]}" for i, t, c in existing) or "(none)"
    env = dict(os.environ)
    env["JEJAK_EXTRACTING"] = "1"  # guard: hooks bail inside this subprocess
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--output-format", "text"],
            input=EXTRACT_PROMPT % (existing_str, exchange_text),
            capture_output=True, text=True, env=env, timeout=120,
        )
    except Exception as e:
        jejak.log_error("jejak-extract/claude", e)
        return []

    out = (proc.stdout or "").strip()
    if not out:
        return []
    # Strip markdown fences if present
    if out.startswith("```"):
        out = out.split("```", 2)[1]
        if out.startswith("json"):
            out = out[4:]
        out = out.strip("` \n")
    # Grab the JSON array
    start, end = out.find("["), out.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        items = json.loads(out[start:end + 1])
    except Exception as e:
        jejak.log_error("jejak-extract/parse", e)
        return []

    clean = []
    for it in items[:MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        t = str(it.get("type", "")).lower().strip()
        c = str(it.get("content", "")).strip()
        sup = it.get("supersedes")
        sup = str(sup).strip() if sup and str(sup).lower() not in ("null", "none", "") else None
        if t in VALID_TYPES and len(c) >= 10:
            clean.append({"type": t, "content": c, "supersedes": sup})
    return clean


def run_worker(session_id, transcript_path, cwd):
    exchange = extract_last_exchange(transcript_path)
    if not exchange.strip():
        return
    existing = fetch_existing_knowledge(cwd)
    valid_ids = {i for i, _, _ in existing}
    items = judge_and_categorize(exchange, existing)
    if not items:
        return
    env = dict(os.environ)
    env["JEJAK_EXTRACTING"] = "1"
    saved = []
    for it in items:
        cmd = ["python3", os.path.join(HOOKS_DIR, "jejak-cli.py"), "save",
               it["type"], it["content"], "-d", cwd, "-s", session_id]
        # Only pass a supersedes id the model actually saw (guards hallucinated ids)
        if it.get("supersedes") and it["supersedes"] in valid_ids:
            cmd += ["--supersedes", it["supersedes"]]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
            saved.append(r.stdout.strip())
        except Exception as e:
            jejak.log_error("jejak-extract/save", e)
    if saved:
        jejak.log_error("jejak-extract/info", "auto-saved: " + " | ".join(saved))


def main():
    # Recursion guard: never run inside our own headless extraction call.
    if os.environ.get("JEJAK_EXTRACTING"):
        print("{}")
        return

    # Worker mode (re-spawned detached).
    if len(sys.argv) >= 5 and sys.argv[1] == "--worker":
        run_worker(sys.argv[2], sys.argv[3], sys.argv[4])
        return

    try:
        data = json.load(sys.stdin)
    except Exception:
        print("{}")
        return

    # Avoid loops if a Stop hook already forced continuation.
    if data.get("stop_hook_active"):
        print("{}")
        return

    session_id = data.get("session_id", "unknown")
    transcript_path = data.get("transcript_path", "")
    cwd = data.get("cwd", os.getcwd())

    if transcript_path and os.path.exists(transcript_path):
        # Fire-and-forget: detached worker, so the user is never blocked.
        try:
            subprocess.Popen(
                ["python3", os.path.abspath(__file__), "--worker",
                 session_id, transcript_path, cwd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            jejak.log_error("jejak-extract/spawn", e)

    print("{}")  # allow the stop


main()

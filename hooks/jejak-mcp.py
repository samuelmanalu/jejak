# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0,<2"]
# ///
"""
Jejak MCP server — the knowledge graph for any MCP client (Gemini CLI,
Antigravity, Cursor, Claude, ...), with full create/read/update/delete.

Every tool shells out to jejak-cli.py, the single interface to Jejak. Nothing
here talks to Neo4j directly, so redaction, exact-content dedup, scoring,
superseding and the soft-delete rules stay in ONE place.

Run:       uv run --script ~/.claude/hooks/jejak-mcp.py
Env:
  JEJAK_CLIENT       who is calling (e.g. gemini, antigravity); becomes the
                     session id "<client>-<YYYY-MM-DD>" on saves. Default "mcp".
  JEJAK_DEFAULT_DIR  project directory when a tool call gives none
                     (default: the server's working directory).
  JEJAK_PYTHON       interpreter that has the neo4j driver (default: the first
                     of /usr/bin/python3, python3 that imports neo4j).
"""
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP

HERE = Path(__file__).resolve().parent
CLI = HERE / "jejak-cli.py"
MAX_OUTPUT = 60_000
TYPES = ("decision", "learning", "finding", "issue", "solution",
         "architecture", "convention", "context")

INSTRUCTIONS = """Jejak is the user's engineering knowledge graph. Rules:
- LOOK UP first: for any "why/how" question about a project, call jejak_ask or
  jejak_search before reading code. If Jejak answers it, don't re-derive it.
- CREATE: call jejak_check_duplicates first and judge the result by meaning.
  Same meaning -> don't save. Contradicts an entry -> ask the user which is
  current, then use jejak_update on the old one. Unique -> jejak_save.
  Always pass `directory` (the project's absolute path) and `source_file`
  when the knowledge is about a file.
- UPDATE never edits in place: jejak_update saves a new version that
  supersedes the old one (the old one stays for audit).
- DELETE is soft and reversible (jejak_delete / jejak_restore). Only delete
  when the user asks or the entry is plainly wrong.
- Never save secrets, trivial file reads, or conversational filler."""

mcp = FastMCP("jejak", instructions=INSTRUCTIONS)


def _python():
    explicit = os.environ.get("JEJAK_PYTHON")
    if explicit:
        return explicit
    for cand in ("/usr/bin/python3", shutil.which("python3")):
        if cand and subprocess.run([cand, "-c", "import neo4j"],
                                   capture_output=True).returncode == 0:
            return cand
    return "python3"


PYTHON = _python()


def _dir(directory):
    return directory or os.environ.get("JEJAK_DEFAULT_DIR") or os.getcwd()


def _session():
    return f"{os.environ.get('JEJAK_CLIENT', 'mcp')}-{date.today().isoformat()}"


def _cli(*args, timeout=120):
    env = {**os.environ, "CLAUDE_SESSION_ID": _session()}
    try:
        r = subprocess.run([PYTHON, str(CLI), *args], capture_output=True,
                           text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return f"jejak timed out after {timeout}s"
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        out = f"{out}\n[error] {err[-1] if err else f'exit {r.returncode}'}".strip()
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n… truncated ({len(out)} chars)"
    return out or "(no output)"


# ── create ─────────────────────────────────────────────────────────────────

@mcp.tool()
def jejak_check_duplicates(content: str, directory: str = "") -> str:
    """Show existing knowledge in this project that may duplicate or contradict
    `content`. Call before every jejak_save and judge the result by meaning."""
    return _cli("check-duplicates", content, "-d", _dir(directory))


@mcp.tool()
def jejak_save(type: str, content: str, directory: str = "", source_file: str = "") -> str:
    """Save a new knowledge entry. `type` is one of: decision, learning, finding,
    issue, solution, architecture, convention, context. `directory` is the
    project's absolute path; `source_file` links it to a file. Secrets are
    redacted automatically. Run jejak_check_duplicates first."""
    if type not in TYPES:
        return f"invalid type '{type}'; use one of: {', '.join(TYPES)}"
    args = ["save", type, content, "-d", _dir(directory)]
    if source_file:
        args += ["-f", source_file]
    return _cli(*args)


# ── read ───────────────────────────────────────────────────────────────────

@mcp.tool()
def jejak_ask(question: str, directory: str = "") -> str:
    """Answer a question from stored knowledge: returns the matching entries and
    their connections for you to synthesize. Use before exploring code."""
    return _cli("ask", question, "-d", _dir(directory))


@mcp.tool()
def jejak_search(query: str) -> str:
    """Keyword search across all knowledge (newest 20 matches, any project)."""
    return _cli("search", query)


@mcp.tool()
def jejak_get(memory_id: str) -> str:
    """One entry in full, whatever its status (active, superseded, deleted),
    with its supersede chain. `memory_id` may be the 8-character prefix."""
    return _cli("get", memory_id)


@mcp.tool()
def jejak_links(memory_id: str) -> str:
    """Why an entry matters: its topics, projects, sessions and related entries."""
    return _cli("links", memory_id)


@mcp.tool()
def jejak_map(directory: str = "") -> str:
    """Everything known about one project, grouped by type."""
    return _cli("map", _dir(directory))


@mcp.tool()
def jejak_rank(directory: str = "", all_projects: bool = False) -> str:
    """Entries ranked by relevance score, for this project or all projects."""
    return _cli("rank", "-a") if all_projects else _cli("rank", _dir(directory))


@mcp.tool()
def jejak_topics(directory: str = "") -> str:
    """Recurring topics in one project."""
    return _cli("topics", _dir(directory))


@mcp.tool()
def jejak_session(session_id: str) -> str:
    """Everything saved in one session."""
    return _cli("session", session_id)


@mcp.tool()
def jejak_stats() -> str:
    """Whole-graph counts."""
    return _cli("stats")


# ── update ─────────────────────────────────────────────────────────────────

@mcp.tool()
def jejak_update(memory_id: str, content: str, directory: str = "", source_file: str = "") -> str:
    """Replace an entry with a corrected version. Saves `content` as a new entry
    of the SAME type that supersedes `memory_id`; the old one stays for audit
    but leaves active views. Use this instead of saving a contradicting entry."""
    import json
    raw = _cli("get", memory_id, "--json")
    try:
        old = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if old["status"] != "active":
        return f"{old['memory_id'][:8]} is {old['status']}; only an active entry can be updated."
    target_dir = directory or (old["project_paths"][0] if old["project_paths"] else _dir(""))
    args = ["save", old["type"], content, "-d", target_dir, "--supersedes", old["memory_id"][:8]]
    file = source_file or old.get("source_file")
    if file:
        args += ["-f", file]
    out = _cli(*args)
    if "SUPERSEDES:" not in out:
        out += "\n[warning] the old entry was NOT marked superseded; check with jejak_get."
    return out


# ── delete ─────────────────────────────────────────────────────────────────

@mcp.tool()
def jejak_delete(memory_id: str, reason: str) -> str:
    """Soft-delete an entry: it leaves every active view on all synced machines,
    but stays in the graph and can be brought back with jejak_restore."""
    return _cli("delete", memory_id, "-r", reason)


@mcp.tool()
def jejak_restore(memory_id: str) -> str:
    """Bring a deleted entry back as a new active entry linked to the old one."""
    return _cli("restore", memory_id)


if __name__ == "__main__":
    mcp.run()

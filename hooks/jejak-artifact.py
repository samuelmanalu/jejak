#!/usr/bin/env python3
"""
PostToolUse hook: capture Claude artifacts & documents into Jejak.

Fires after Write / NotebookEdit / Artifact. It records *documents* (not code)
and published artifacts as `artifact` memories, so the pile of docs produced
during a session becomes searchable (jejak search / ask), visible in the graph,
and indexed per project.

Rewriting a document supersedes its previous version (reuses conflict-pruning),
so only the latest copy is active while old versions stay for audit.

Deterministic capture (no LLM) — a document was created, so we index it.
"""
import json
import os
import sys
import subprocess

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
import jejak_common as jejak
from neo4j import GraphDatabase

HOOKS_DIR = os.path.expanduser("~/.claude/hooks")

# Only these extensions count as "documents" worth indexing as artifacts.
DOC_EXTS = {".md", ".markdown", ".html", ".htm", ".txt", ".rst",
            ".pdf", ".csv", ".docx", ".pptx", ".xlsx", ".ipynb"}

# Never index writes under these paths (self, temp, deps, vcs).
SKIP_PATH_MARKERS = [
    "/.claude/hooks/", "/node_modules/", "/.git/", "/dist/", "/build/",
    "/__pycache__/", "/.venv", "/venv/", "/scratchpad/", "/.next/", "/target/",
]


def get_driver():
    cfg = jejak.load_config("neo4j")
    return GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))


def summarize_document(path, content):
    """Return (title, summary) from a document's text."""
    title = os.path.basename(path)
    summary = ""
    if content:
        lines = [ln.strip() for ln in content.splitlines()]
        # Title: first markdown/HTML heading, else first non-empty line
        for ln in lines:
            if not ln:
                continue
            if ln.startswith("#"):
                title = ln.lstrip("#").strip()
                break
            low = ln.lower()
            if "<title>" in low:
                title = ln[low.find("<title>") + 7: low.find("</title>")].strip() or title
                break
            if "<h1" in low:
                title = ln[low.find(">") + 1: low.rfind("</h1")].strip() or title
                break
            title = ln[:100]
            break
        # Summary: first meaningful prose lines after the title
        prose = [ln for ln in lines if ln and not ln.startswith(("#", "<", "|", "```", "-", "*"))]
        summary = " ".join(prose)[:240]
    return title, summary


def existing_artifact_id(cwd, source_file):
    driver = get_driver()
    try:
        with driver.session() as s:
            rec = s.run("""
                MATCH (m:Memory {type: 'artifact'})-[:IN_PROJECT]->(p:Project {path: $path})
                WHERE m.source_file = $sf AND coalesce(m.superseded, false) = false
                RETURN left(m.memory_id, 8) AS id LIMIT 1
            """, path=cwd, sf=source_file).single()
            return rec["id"] if rec else None
    finally:
        driver.close()


def save_artifact(content_text, cwd, session_id, source_file):
    prior = existing_artifact_id(cwd, source_file)
    cmd = ["python3", os.path.join(HOOKS_DIR, "jejak-cli.py"), "save",
           "artifact", content_text, "-d", cwd, "-s", session_id, "-f", source_file]
    if prior:
        cmd += ["--supersedes", prior]  # new version replaces old
    env = dict(os.environ)
    env["JEJAK_EXTRACTING"] = "1"  # jejak-cli is a plain script; guard is belt-and-suspenders
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
        jejak.log_error("jejak-artifact/info", "indexed: " + (r.stdout.strip() or "(no output)"))
    except Exception as e:
        jejak.log_error("jejak-artifact/save", e)


def handle(data):
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    tr = data.get("tool_response", {}) or {}
    cwd = data.get("cwd", os.getcwd())
    session_id = data.get("session_id", "unknown")

    # --- Published Artifact (claude.ai artifact) -------------------------
    if tool == "Artifact":
        title = ti.get("title") or (ti.get("file_path") and os.path.basename(ti["file_path"])) or "Artifact"
        desc = ti.get("description", "")
        # Find a URL anywhere in the response
        url = ""
        blob = json.dumps(tr)
        m = None
        import re
        m = re.search(r'https?://[^\s"\']+artifact[^\s"\']*', blob)
        if not m:
            m = re.search(r'https?://[^\s"\']+', blob)
        if m:
            url = m.group(0)
        content = f"Artifact: {title}"
        if desc:
            content += f" — {desc}"
        if url:
            content += f" ({url})"
        sf = url or (ti.get("file_path") or title)
        save_artifact(content, cwd, session_id, sf)
        return

    # --- Written documents (Write / NotebookEdit) ------------------------
    if tool in ("Write", "NotebookEdit"):
        path = ti.get("file_path") or tr.get("filePath") or ""
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in DOC_EXTS:
            return
        if any(marker in path for marker in SKIP_PATH_MARKERS):
            return
        content = ti.get("content") or ""
        if not content:
            try:
                with open(path) as f:
                    content = f.read()
            except OSError:
                content = ""
        # Skip trivially small docs
        if len(content.strip()) < 80:
            return
        title, summary = summarize_document(path, content)
        rel = path.replace(os.path.expanduser("~"), "~")
        entry = f"Document: {title} ({rel})"
        if summary:
            entry += f" — {summary}"
        save_artifact(entry, cwd, session_id, path)
        return


def main():
    if os.environ.get("JEJAK_EXTRACTING"):  # don't index writes made during extraction
        print("{}")
        return

    # Worker mode: do the actual DB work (spawned detached).
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        try:
            data = json.loads(sys.stdin.read())
            handle(data)
        except Exception as e:
            jejak.log_error("jejak-artifact/worker", e)
        return

    # Hook mode: read the event, spawn a detached worker, return immediately so
    # the user's Write is never blocked on the database.
    try:
        raw = sys.stdin.read()
    except Exception:
        print("{}")
        return
    try:
        proc = subprocess.Popen(
            ["python3", os.path.abspath(__file__), "--worker"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        proc.stdin.write(raw.encode())
        proc.stdin.close()  # hand off data; do NOT wait()
    except Exception as e:
        jejak.log_error("jejak-artifact/spawn", e)
    print("{}")


main()

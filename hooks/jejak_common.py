#!/usr/bin/env python3
"""
Shared helpers for the Jejak knowledge-graph system.

Single source of truth for: config loading, secret redaction, error logging,
topic detection, project-name extraction, and — critically — the ONE canonical
relevance-scoring query (with time decay) used by both the hooks and the CLI.
"""
import getpass
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime

# --- Config -----------------------------------------------------------------

CONFIG_PATH = os.path.expanduser("~/.claude/hooks/db-config.json")
LOG_DIR = os.path.expanduser("~/.claude/logs")

def load_config(which):
    with open(CONFIG_PATH) as f:
        return json.load(f)[which]

# --- Error logging (replaces silent `except: pass`) -------------------------

def log_error(where, exc):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(LOG_DIR, "jejak-errors.log"), "a") as f:
            f.write(f"[{ts}] {where}: {type(exc).__name__}: {exc}\n")
    except Exception:
        pass  # never let logging itself break a hook

# --- Secret redaction -------------------------------------------------------

_REDACTIONS = [
    # KEY: value / DB_PASSWORD = value / token=value  (quoted or bare).
    # Allows UPPER_SNAKE prefixes like ORACLE_REST_PASSWORD; requires a word
    # boundary AFTER the keyword so "passenger:" won't match.
    (re.compile(r'(?i)([A-Za-z]*[_-]?(?:password|passwd|pass|secret|token|'
                r'api[_-]?key|access[_-]?key|client[_-]?secret|credential)s?)\b'
                r'\s*[:=]\s*["\']?([^\s"\']{3,})'),
     lambda m: f"{m.group(1)}: [REDACTED]"),
    # provider-style keys
    (re.compile(r'\bsk-[A-Za-z0-9]{16,}\b'), lambda m: "[REDACTED_KEY]"),
    (re.compile(r'\bghp_[A-Za-z0-9]{20,}\b'), lambda m: "[REDACTED_KEY]"),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), lambda m: "[REDACTED_KEY]"),
    (re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}\b'), lambda m: "[REDACTED_KEY]"),
    # connection strings user:pass@host
    (re.compile(r'(//[^:/\s]+:)([^@/\s]{3,})(@)'), lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}"),
]

def redact(text):
    if not text:
        return text
    out = text
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out

# --- Topic detection --------------------------------------------------------

TOPIC_PATTERNS = {
    "git": [r"\bgit\b", r"\bcommit\b", r"\bpush\b", r"\bpull\b", r"\bbranch\b", r"\bmerge\b", r"\brebase\b"],
    "debugging": [r"\bdebug\b", r"\bbug\b", r"\bfix\b", r"\berror\b", r"\bfail\b", r"\bcrash\b", r"\bbroken\b"],
    "testing": [r"\btest\b", r"\bspec\b", r"\bjest\b", r"\bpytest\b", r"\bjunit\b", r"\bcoverage\b", r"\btdd\b"],
    "refactoring": [r"\brefactor\b", r"\bclean.?up\b", r"\brestructur\b", r"\bsimplif\b"],
    "architecture": [r"\barchitect\b", r"\bdesign\b", r"\bpattern\b", r"\bmodule\b", r"\bservice\b", r"\bmicroservice\b"],
    "api": [r"\bapi\b", r"\bendpoint\b", r"\brest\b", r"\bgraphql\b", r"\bgrpc\b"],
    "database": [r"\bdb\b", r"\bdatabase\b", r"\bsql\b", r"\bmigration\b", r"\bschema\b", r"\bquery\b"],
    "security": [r"\bsecur\b", r"\bauth\b", r"\btoken\b", r"\bencrypt\b", r"\bvulnerab\b"],
    "deployment": [r"\bdeploy\b", r"\brelease\b", r"\bci.cd\b", r"\bpipeline\b", r"\bdocker\b", r"\bk8s\b"],
    "performance": [r"\bperformance\b", r"\boptimiz\b", r"\blatency\b", r"\bcache\b", r"\bslow\b"],
    "frontend": [r"\bfrontend\b", r"\breact\b", r"\bvue\b", r"\bcss\b", r"\bui\b", r"\bcomponent\b"],
    "backend": [r"\bbackend\b", r"\bserver\b", r"\bjava\b", r"\bspring\b", r"\bnode\b", r"\bpython\b"],
    "kafka": [r"\bkafka\b", r"\bconsumer\b", r"\bproducer\b", r"\btopic\b", r"\bevent\b", r"\bstream\b"],
    "config": [r"\bconfig\b", r"\bsetting\b", r"\benv\b", r"\benvironment\b", r"\bsetup\b"],
    "documentation": [r"\bdoc\b", r"\breadme\b", r"\bexplain\b", r"\bwhat does\b", r"\bhow does\b"],
}

# "general" is a catch-all, NOT a real topic. Memories are never linked on it
# (that caused the O(n^2) edge explosion), but it stays as a label for display.
CATCHALL_TOPIC = "general"

def detect_topics(text):
    lower = text.lower()
    matched = []
    for topic, patterns in TOPIC_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                matched.append(topic)
                break
    return matched if matched else [CATCHALL_TOPIC]

def extract_project_name(cwd):
    parts = cwd.rstrip("/").split("/")
    # The current user's home-directory name is never a meaningful project name.
    skip = {"Users", "home", getpass.getuser(), "Documents", "Desktop", "Projects", "repos", "src", "code"}
    for part in reversed(parts):
        if part and part not in skip:
            return part
    return parts[-1] if parts else "unknown"

def machine_name():
    # On macOS with no HostName set, gethostname() is whatever the current
    # network's DHCP/VPN hands out, so one laptop drifts across many names.
    # Prefer an explicit HostName, then the stable LocalHostName.
    if sys.platform == "darwin":
        for key in ("HostName", "LocalHostName"):
            try:
                name = subprocess.run(["scutil", "--get", key], capture_output=True,
                                      text=True, timeout=2).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                name = ""
            if name:
                return name
    return socket.gethostname()

# --- Canonical scoring (the ONE formula, with decay) ------------------------
#
# score = (hits*3 + sessions*5 + projects*10 + non_prompt_connections*2)
#         * type_weight * decay_factor
#
# - connections count only NON-prompt neighbours (prompts are noise)
# - decay: after DECAY_DAYS with no access, score fades 0.3%/day, floored at 10%

DECAY_DAYS = 30

_SCORE_BODY = """
    OPTIONAL MATCH (m)-[:IN_SESSION]->(s:Session)
    OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
    OPTIONAL MATCH (m)-[:RELATES_TO]-(other:Memory)
    WHERE other.type <> 'prompt'
    WITH m,
         coalesce(m.hit_count, 1) AS hits,
         count(DISTINCT s) AS session_spread,
         count(DISTINCT p) AS project_spread,
         count(DISTINCT other) AS connections
    WITH m, hits, session_spread, project_spread, connections,
         CASE m.type
           WHEN 'architecture' THEN 1.5 WHEN 'decision' THEN 1.4
           WHEN 'convention' THEN 1.3 WHEN 'learning' THEN 1.2
           WHEN 'solution' THEN 1.1 WHEN 'context' THEN 1.0
           WHEN 'issue' THEN 0.9 WHEN 'finding' THEN 0.8
           WHEN 'artifact' THEN 1.0
           ELSE 0.3
         END AS type_weight,
         toFloat(hits) * 3.0 +
         toFloat(session_spread) * 5.0 +
         toFloat(project_spread) * 10.0 +
         toFloat(connections) * 2.0 AS raw_score,
         duration.inDays(coalesce(m.last_accessed_at, m.created_at), datetime()).days AS days_idle
    WITH m, raw_score, type_weight,
         CASE WHEN days_idle > %d
           THEN CASE WHEN (1.0 - toFloat(days_idle - %d) * 0.003) < 0.1
                     THEN 0.1 ELSE (1.0 - toFloat(days_idle - %d) * 0.003) END
           ELSE 1.0
         END AS decay_factor
    SET m.relevance_score = raw_score * type_weight * decay_factor,
        m.score_updated_at = datetime()
""" % (DECAY_DAYS, DECAY_DAYS, DECAY_DAYS)

def update_relevance_score(session, memory_id):
    """Recalculate ONE memory's score (canonical, with decay)."""
    session.run("MATCH (m:Memory {memory_id: $memory_id})\n" + _SCORE_BODY,
                memory_id=memory_id)

def recalculate_all_scores(session):
    """Recalculate every non-prompt memory's score (canonical, with decay)."""
    session.run("MATCH (m:Memory)\nWHERE m.type <> 'prompt'\n" + _SCORE_BODY)

# Reusable WHERE fragment: exclude superseded (conflict-pruned) memories.
# `m` must be the memory variable in the surrounding query.
NOT_SUPERSEDED = "coalesce(m.superseded, false) = false"

def mark_superseded(session, old_id_prefix, new_memory_id):
    """Mark an existing memory as superseded by a newer one (reversible prune).

    Returns the pruned memory's {id, type, content} or None if no match.
    Guard: only supersede an ACTIVE memory of the same type as the new one.
    """
    new = session.run("""
        MATCH (m:Memory {memory_id: $new_id}) RETURN m.type AS type
    """, new_id=new_memory_id).single()
    if not new:
        return None
    old = session.run("""
        MATCH (m:Memory)
        WHERE m.memory_id STARTS WITH $prefix
          AND m.memory_id <> $new_id
          AND m.type = $type
          AND coalesce(m.superseded, false) = false
        RETURN m.memory_id AS id, m.type AS type, m.content AS content
        LIMIT 1
    """, prefix=old_id_prefix, new_id=new_memory_id, type=new["type"]).single()
    if not old:
        return None
    session.run("""
        MATCH (m:Memory {memory_id: $id})
        SET m.superseded = true, m.superseded_at = datetime(), m.superseded_by = $by
    """, id=old["id"], by=new_memory_id)
    return dict(old)

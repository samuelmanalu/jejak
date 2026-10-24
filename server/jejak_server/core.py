"""
Every Jejak command, as the server runs it.

This is the CLI's query logic (hooks/jejak-cli.py before the server existed),
moved here so there is ONE place that talks to Neo4j. Two things changed:

  * SCOPE. Every Memory, Project and Session belongs to one space
    (`space_id`). Each query is limited to the spaces the caller may read
    (`$spaces`), and writes go to one space the caller may write (`$space`).
    RELATES_TO links are only ever made inside a space, so nothing about a
    space leaks into another through links, ranking or `ask`.
  * OUTPUT. Commands write through `out` instead of print() and raise
    JejakError instead of sys.exit(1), so a server thread can capture them.
    The text is what the CLI printed before, so the skill, CLAUDE.md rules and
    the MCP tools read the same output as always.

Projects are keyed by (space_id, key). The client computes `key` (normalized
git remote, or the directory name), because absolute paths differ per person.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import jejak_common as jejak

VALID_TYPES = ["decision", "learning", "finding", "issue", "solution", "architecture",
               "convention", "context", "artifact"]
WRITE_COMMANDS = {"save", "delete", "restore", "visibility"}


class JejakError(Exception):
    """A command failed for a reason the user should read (exit code 1)."""


class Forbidden(JejakError):
    """The caller's role does not allow this."""


@dataclass
class Ctx:
    """Who is asking, what they may touch, and where they are working."""
    username: str
    read_spaces: list
    write_spaces: list
    space: str = None            # space_id that writes go to
    project_key: str = None
    project_name: str = None
    directory: str = None        # the client's path, for display only
    machine: str = "unknown"
    session: str = "manual"
    lines: list = field(default_factory=list)

    def out(self, *parts):
        self.lines.append(" ".join(str(p) for p in parts))

    def text(self):
        return "\n".join(self.lines)

    def write_space(self):
        if not self.space:
            raise Forbidden("No space to write to. Pick one with: jejak space use <space>")
        if self.space not in self.write_spaces:
            raise Forbidden("You can read this space but not write to it (your role is viewer).")
        return self.space

    def need_project(self):
        if not self.project_key:
            raise JejakError("No project given: run inside a project directory or pass one.")
        return self.project_key


def in_scope(var="m"):
    """WHERE fragment: `var` is a memory in a space the caller may read."""
    return f"{var}.space_id IN $spaces"


PROJECT = "(p:Project {key: $key}) WHERE p.space_id IN $spaces"


# --- helpers ------------------------------------------------------------------

def _score(session, memory_id):
    jejak.update_relevance_score(session, memory_id)
    r = session.run("MATCH (m:Memory {memory_id: $mid}) RETURN m.relevance_score AS score",
                    mid=memory_id).single()
    return r["score"] if r else 0


def _attach(session, ctx, space, memory_id, key, name, path):
    """Link a memory to its project and session (both inside `space`)."""
    session.run("""
        MERGE (p:Project {space_id: $space, key: $key})
        ON CREATE SET p.created_at = datetime()
        SET p.name = $name, p.path = coalesce($path, p.path), p.last_seen = datetime()
        WITH p
        MATCH (m:Memory {memory_id: $memory_id})
        MERGE (m)-[:IN_PROJECT]->(p)
        MERGE (s:Session {space_id: $space, session_id: $session_id})
        SET s.last_active = datetime(), s.machine_name = $machine
        MERGE (m)-[:IN_SESSION]->(s)
        MERGE (s)-[:IN_PROJECT]->(p)
    """, space=space, key=key, name=name, path=path, memory_id=memory_id,
         session_id=ctx.session, machine=ctx.machine)


def _mark_superseded(session, space, old_prefix, new_id):
    """Supersede an ACTIVE memory of the same type, in the same space."""
    old = session.run("""
        MATCH (n:Memory {memory_id: $new_id})
        MATCH (m:Memory {space_id: $space})
        WHERE m.memory_id STARTS WITH $prefix AND m.memory_id <> $new_id
          AND m.type = n.type AND coalesce(m.superseded, false) = false
        RETURN m.memory_id AS id LIMIT 1
    """, new_id=new_id, space=space, prefix=old_prefix).single()
    if not old:
        return None
    session.run("""
        MATCH (m:Memory {memory_id: $id})
        SET m.superseded = true, m.superseded_at = datetime(), m.superseded_by = $by,
            m.updated_at = datetime()
    """, id=old["id"], by=new_id)
    return old["id"]


def resolve_memory(session, ctx, prefix, for_write=False):
    """Exactly one memory id the caller may see (and change, if for_write)."""
    if not prefix or len(prefix) < 4:
        raise JejakError("Give at least 4 characters of the memory id.")
    rows = list(session.run(
        "MATCH (m:Memory) WHERE m.memory_id STARTS WITH $prefix AND " + in_scope() +
        " RETURN m.memory_id AS id, m.space_id AS space LIMIT 6",
        prefix=prefix, spaces=ctx.read_spaces))
    if not rows:
        raise JejakError(f"No memory found matching '{prefix}'")
    if len(rows) > 1:
        raise JejakError(f"'{prefix}' is ambiguous: {', '.join(r['id'][:12] for r in rows)} "
                         "— use more characters.")
    if for_write and rows[0]["space"] not in ctx.write_spaces:
        raise Forbidden(f"{rows[0]['id'][:8]} is in a space where your role is viewer.")
    return rows[0]["id"]


def memory_details(session, memory_id):
    rec = session.run("""
        MATCH (m:Memory {memory_id: $id})
        OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
        OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
        OPTIONAL MATCH (m)-[:IN_SESSION]->(s:Session)
        OPTIONAL MATCH (older:Memory {superseded_by: m.memory_id})
        WITH m, collect(DISTINCT t.name) AS topics, collect(DISTINCT p.path) AS project_paths,
             collect(DISTINCT p.name) AS projects, collect(DISTINCT p.key) AS project_keys,
             count(DISTINCT s) AS sessions, collect(DISTINCT older.memory_id) AS supersedes
        RETURN m {.*} AS m, topics, project_paths, projects, project_keys, sessions, supersedes
    """, id=memory_id).single()
    m = dict(rec["m"])
    for k, v in list(m.items()):
        if hasattr(v, "iso_format"):
            m[k] = v.iso_format()
    if not m.get("superseded"):
        status = "active"
    elif m.get("superseded_by") == "deleted":
        status = "deleted"
    else:
        status = "superseded"
    return {
        "memory_id": m.get("memory_id"), "type": m.get("type"), "status": status,
        "content": m.get("content"), "source_file": m.get("source_file"),
        "created_at": m.get("created_at"), "updated_at": m.get("updated_at"),
        "machine_name": m.get("machine_name"), "space_id": m.get("space_id"),
        "created_by": m.get("created_by"),
        "visibility": m.get("visibility") or "confidential",
        "relevance_score": m.get("relevance_score"), "hit_count": m.get("hit_count"),
        "superseded_at": m.get("superseded_at"), "superseded_by": m.get("superseded_by"),
        "deleted_reason": m.get("deleted_reason"), "restored_from": m.get("restored_from"),
        "supersedes": rec["supersedes"], "topics": sorted(rec["topics"]),
        "projects": sorted(x for x in rec["projects"] if x),
        "project_paths": sorted(x for x in rec["project_paths"] if x),
        "project_keys": sorted(x for x in rec["project_keys"] if x),
        "sessions": rec["sessions"],
    }


# --- create / update ----------------------------------------------------------

def save_memory(session, ctx, memory_type, content, source_file=None, supersedes=None,
                visibility=None, space=None, key=None, name=None, path=None):
    """Save one entry. Returns (memory_id, existed, score, topics, superseded_id)."""
    space = space or ctx.write_space()
    if space not in ctx.write_spaces:
        raise Forbidden("You can read this space but not write to it (your role is viewer).")
    key = key or ctx.need_project()
    name = name or ctx.project_name or key
    path = path if path is not None else ctx.directory
    content = jejak.redact(content)
    if visibility == "public" and memory_type == "prompt":
        visibility = "confidential"

    # Exact-content dedup inside the space. Active nodes only: bumping a
    # superseded or deleted twin would leave the re-saved knowledge invisible.
    dup = session.run("""
        MATCH (m:Memory {space_id: $space}) WHERE m.content = $content
          AND coalesce(m.superseded, false) = false
        RETURN m.memory_id AS memory_id LIMIT 1
    """, space=space, content=content).single()
    if dup:
        mid = dup["memory_id"]
        session.run("""
            MATCH (m:Memory {memory_id: $mid})
            SET m.updated_at = datetime(), m.hit_count = coalesce(m.hit_count, 1) + 1,
                m.last_accessed_at = datetime(), m.updated_by = $user
        """, mid=mid, user=ctx.username)
        _attach(session, ctx, space, mid, key, name, path)
        if visibility:
            set_visibility(session, mid, visibility)
        pruned = _mark_superseded(session, space, supersedes, mid) if supersedes else None
        return mid, True, _score(session, mid), None, pruned

    topics = jejak.detect_topics(content)
    mid = str(uuid.uuid4())
    session.run("""
        CREATE (m:Memory {memory_id: $mid, type: $type, content: $content, space_id: $space,
            machine_name: $machine, hit_count: 1, relevance_score: 0.0,
            source_file: $source_file, visibility: $visibility, created_by: $user,
            last_accessed_at: datetime(), created_at: datetime(), updated_at: datetime()})
    """, mid=mid, type=memory_type, content=content, space=space, machine=ctx.machine,
         source_file=source_file, visibility=visibility or "confidential", user=ctx.username)
    _attach(session, ctx, space, mid, key, name, path)

    if memory_type != "prompt":
        # The catch-all "general" is a hot node and never a reason to link.
        real = [t for t in topics if t != jejak.CATCHALL_TOPIC]
        session.run("""
            MATCH (m:Memory {memory_id: $mid})
            UNWIND $topics AS topic
            MERGE (t:Topic {name: topic})
            MERGE (m)-[:ABOUT]->(t)
        """, mid=mid, topics=real)
        # Link to non-prompt memories on real shared topics, in this space only.
        session.run("""
            MATCH (m:Memory {memory_id: $mid})-[:ABOUT]->(t:Topic)<-[:ABOUT]-(other:Memory)
            WHERE other.space_id = $space AND other.memory_id <> $mid
              AND other.type <> 'prompt' AND t.name <> $catchall
              AND NOT exists((m)-[:RELATES_TO]-(other))
            WITH m, other, count(t) AS shared_topics
            MERGE (m)-[:RELATES_TO {strength: shared_topics}]->(other)
        """, mid=mid, space=space, catchall=jejak.CATCHALL_TOPIC)

    score = _score(session, mid)
    pruned = _mark_superseded(session, space, supersedes, mid) if supersedes else None
    return mid, False, score, topics, pruned


def cmd_save(session, ctx, type, content, file=None, supersedes=None, visibility=None, **_):
    if type not in VALID_TYPES:
        raise JejakError(f"Invalid type '{type}'. Valid types: {', '.join(VALID_TYPES)}")
    if visibility and visibility not in jejak.VISIBILITIES:
        raise JejakError(f"Invalid visibility '{visibility}'.")
    content = " ".join(content) if isinstance(content, list) else content
    mid, existed, score, topics, pruned = save_memory(
        session, ctx, type, content, source_file=file, supersedes=supersedes, visibility=visibility)
    name = ctx.project_name or ctx.project_key
    if existed:
        msg = f"Existing [{type}] id:{mid[:8]} score:{score:.0f} project:{name}"
    else:
        msg = (f"Saved [{type}] id:{mid[:8]} score:{score:.0f} topics:{topics} "
               f"project:{name} visibility:{visibility or 'confidential'}")
    if pruned:
        msg += f" SUPERSEDES:{pruned[:8]}"
    ctx.out(msg)


def set_visibility(session, memory_id, visibility):
    session.run("""
        MATCH (m:Memory {memory_id: $id})
        SET m.visibility = $visibility, m.updated_at = datetime()
    """, id=memory_id, visibility=visibility)


def cmd_visibility(session, ctx, visibility, memory_ids, **_):
    if visibility not in jejak.VISIBILITIES:
        raise JejakError(f"Invalid visibility '{visibility}'.")
    ids = [resolve_memory(session, ctx, p, for_write=True) for p in memory_ids]
    for mid in ids:
        d = memory_details(session, mid)
        if d["type"] == "prompt" and visibility == "public":
            ctx.out(f"Skipped {mid[:8]}: prompts always stay confidential.")
            continue
        set_visibility(session, mid, visibility)
        ctx.out(f"{mid[:8]} [{d['type']}] {d['visibility']} -> {visibility}")


def cmd_delete(session, ctx, memory_id, reason=None, **_):
    """Soft delete: hidden from every active view, kept for audit, restorable."""
    mid = resolve_memory(session, ctx, memory_id, for_write=True)
    d = memory_details(session, mid)
    if d["status"] != "active":
        raise JejakError(f"Not deleted: {mid[:8]} is already {d['status']}.")
    session.run("""
        MATCH (m:Memory {memory_id: $id})
        SET m.superseded = true, m.superseded_at = datetime(),
            m.superseded_by = 'deleted', m.deleted_reason = $reason,
            m.updated_at = datetime(), m.updated_by = $user
    """, id=mid, reason=jejak.redact(reason or ""), user=ctx.username)
    ctx.out(f"Deleted [{d['type']}] id:{mid[:8]} (soft; restore with: jejak restore {mid[:8]})")


def cmd_restore(session, ctx, memory_id, **_):
    """Bring a deleted memory back as a NEW active entry (restored_from = old id),
    in the same space and, unless a directory was given, the same project."""
    old_id = resolve_memory(session, ctx, memory_id, for_write=True)
    d = memory_details(session, old_id)
    if d["status"] == "active":
        raise JejakError(f"Nothing to restore: {old_id[:8]} is active.")
    if d["status"] == "superseded":
        raise JejakError(
            f"{old_id[:8]} was superseded by {d['superseded_by'][:8]}, not deleted. To make it current "
            f"again, save its content with --supersedes {d['superseded_by'][:8]} so only one version "
            "stays active.")
    if ctx.project_key:
        key, name, path = ctx.project_key, ctx.project_name, ctx.directory
    elif d["project_keys"]:
        proj = session.run("""
            MATCH (:Memory {memory_id: $id})-[:IN_PROJECT]->(p:Project)
            RETURN p.key AS key, p.name AS name, p.path AS path LIMIT 1
        """, id=old_id).single()
        key, name, path = proj["key"], proj["name"], proj["path"]
    else:
        raise JejakError("The deleted entry has no project; pass a directory.")
    mid, existed, score, topics, _ = save_memory(
        session, ctx, d["type"], d["content"], source_file=d["source_file"],
        visibility=d["visibility"], space=d["space_id"], key=key, name=name, path=path)
    session.run("MATCH (m:Memory {memory_id: $mid}) SET m.restored_from = $old", mid=mid, old=old_id)
    ctx.out(f"Saved [{d['type']}] id:{mid[:8]} score:{score:.0f} topics:{topics} "
            f"project:{name} visibility:{d['visibility']}")


# --- read -----------------------------------------------------------------------

def cmd_map(session, ctx, **_):
    key = ctx.need_project()
    name = ctx.project_name or key
    stats = session.run(f"""
        MATCH (m:Memory)-[:IN_PROJECT]->{PROJECT}
        OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
        OPTIONAL MATCH (s:Session)-[:IN_PROJECT]->(p)
        RETURN count(DISTINCT m) AS memories, count(DISTINCT t) AS topics,
               count(DISTINCT s) AS sessions
    """, key=key, spaces=ctx.read_spaces).single()
    if stats["memories"] == 0:
        ctx.out(f"No knowledge found for {name} ({ctx.directory or key})")
        return
    ctx.out(f"=== Knowledge Map: {name} ===")
    ctx.out(f"    Path: {ctx.directory or key}")
    ctx.out(f"    Memories: {stats['memories']} | Topics: {stats['topics']} | Sessions: {stats['sessions']}")
    ctx.out()

    by_type = {}
    for r in session.run(f"""
        MATCH (m:Memory)-[:IN_PROJECT]->{PROJECT}
          AND coalesce(m.superseded, false) = false
        OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
        WITH m, collect(t.name) AS topics
        ORDER BY m.created_at DESC
        RETURN m.memory_id AS id, m.type AS type, m.content AS content,
               m.created_at AS created_at, topics
    """, key=key, spaces=ctx.read_spaces):
        by_type.setdefault(r["type"], []).append(dict(r))

    for t in ["decision", "architecture", "convention", "learning", "solution", "finding",
              "issue", "context", "artifact", "prompt"]:
        if t not in by_type:
            continue
        entries = by_type[t]
        ctx.out(f"  [{t.upper()}] ({len(entries)})")
        for e in entries[:10]:
            preview = e["content"][:120].replace("\n", " ")
            topic_str = ", ".join(e["topics"][:3]) if e["topics"] else ""
            ts = str(e["created_at"])[:16] if e["created_at"] else ""
            ctx.out(f"    {e['id'][:8]}  {ts}  [{topic_str}]")
            ctx.out(f"             {preview}")
        if len(entries) > 10:
            ctx.out(f"    ... and {len(entries) - 10} more")
        ctx.out()

    links = [dict(r) for r in session.run(f"""
        MATCH (m1:Memory)-[r:RELATES_TO]->(m2:Memory)
        WHERE EXISTS {{ MATCH (m1)-[:IN_PROJECT]->{PROJECT} }}
          AND {in_scope('m2')}
        RETURN m1.memory_id AS from_id, left(m1.content, 50) AS from_text,
               r.strength AS strength,
               m2.memory_id AS to_id, left(m2.content, 50) AS to_text
        ORDER BY r.strength DESC
        LIMIT 15
    """, key=key, spaces=ctx.read_spaces)]
    if links:
        ctx.out("  [CONNECTIONS]")
        for l in links:
            ctx.out(f"    {l['from_id'][:8]} --({l['strength']})--> {l['to_id'][:8]}")
            ctx.out(f"      \"{l['from_text']}...\"")
            ctx.out(f"      \"{l['to_text']}...\"")
        ctx.out()


def cmd_session(session, ctx, session_id, json=False, **_):
    results = [dict(r) for r in session.run(f"""
        MATCH (m:Memory)-[:IN_SESSION]->(s:Session {{session_id: $session_id}})
        WHERE {in_scope()} AND coalesce(m.superseded, false) = false
        OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
        OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
        WITH m, collect(DISTINCT t.name) AS topics, collect(DISTINCT p.name) AS projects
        ORDER BY m.created_at ASC
        RETURN m.memory_id AS id, m.type AS type, m.content AS content,
               m.created_at AS created_at, m.source_file AS source_file,
               m.relevance_score AS score, topics, projects
    """, session_id=session_id, spaces=ctx.read_spaces)]
    if json:
        import json as _json
        for r in results:
            r["created_at"] = str(r["created_at"]) if r["created_at"] else None
        ctx.out(_json.dumps(results))
        return
    if not results:
        ctx.out(f"No memories found for session {session_id}")
        return
    ctx.out(f"=== Session: {session_id} ===")
    ctx.out(f"    Memories: {len(results)}")
    ctx.out()
    for m in results:
        preview = m["content"][:150].replace("\n", " ")
        ts = str(m["created_at"])[:16] if m["created_at"] else ""
        ctx.out(f"  {m['id'][:8]}  [{m['type']}]  {ts}")
        ctx.out(f"    Topics: {', '.join(m['topics'][:4])}  Project: {', '.join(m['projects'])}")
        ctx.out(f"    {preview}")
        ctx.out()


def cmd_search(session, ctx, query, **_):
    term = " ".join(query) if isinstance(query, list) else query
    rows = [dict(r) for r in session.run(f"""
        MATCH (m:Memory)
        WHERE {in_scope()} AND toLower(m.content) CONTAINS toLower($term)
        OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
        OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
        WITH m, collect(DISTINCT t.name) AS topics, collect(DISTINCT p.name) AS projects
        ORDER BY m.created_at DESC
        LIMIT 20
        RETURN m.memory_id AS id, m.type AS type, m.content AS content,
               m.created_at AS created_at, topics, projects,
               coalesce(m.superseded, false) AS superseded, m.superseded_by AS superseded_by
    """, term=term, spaces=ctx.read_spaces)]
    if not rows:
        ctx.out(f"No results for '{term}'")
        return
    ctx.out(f"=== Search: '{term}' ({len(rows)} results) ===")
    ctx.out()
    for m in rows:
        preview = m["content"][:150].replace("\n", " ")
        ts = str(m["created_at"])[:16] if m["created_at"] else ""
        status = ""
        if m["superseded"]:
            status = ("  (DELETED)" if m["superseded_by"] == "deleted"
                      else f"  (SUPERSEDED by {(m['superseded_by'] or '?')[:8]})")
        ctx.out(f"  {m['id'][:8]}  [{m['type']}]  {ts}  project:{', '.join(m['projects'])}{status}")
        ctx.out(f"    [{', '.join(m['topics'][:3])}] {preview}")
        ctx.out()


def cmd_topics(session, ctx, **_):
    key = ctx.need_project()
    rows = [dict(r) for r in session.run(f"""
        MATCH (m:Memory)-[:IN_PROJECT]->{PROJECT}
        MATCH (m)-[:ABOUT]->(t:Topic)
        WITH t.name AS topic, count(m) AS count, collect(m.type) AS types
        ORDER BY count DESC
        RETURN topic, count, types
    """, key=key, spaces=ctx.read_spaces)]
    name = ctx.project_name or key
    if not rows:
        ctx.out(f"No topics found for {name}")
        return
    ctx.out(f"=== Topics: {name} ===")
    ctx.out()
    for r in rows:
        summary = {}
        for t in r["types"]:
            summary[t] = summary.get(t, 0) + 1
        type_str = ", ".join(f"{k}:{v}" for k, v in sorted(summary.items(), key=lambda x: -x[1]))
        ctx.out(f"  {r['topic']:15s}  {r['count']:3d}  {'#' * min(r['count'], 30)}  ({type_str})")


def cmd_links(session, ctx, memory_id, **_):
    rows = list(session.run(f"""
        MATCH (m:Memory) WHERE m.memory_id STARTS WITH $prefix AND {in_scope()}
        RETURN m.memory_id AS id, m.type AS type, m.content AS content,
               m.relevance_score AS score, m.hit_count AS hits
        LIMIT 1
    """, prefix=memory_id, spaces=ctx.read_spaces))
    if not rows:
        ctx.out(f"No memory found matching '{memory_id}'")
        return
    mem = dict(rows[0])
    full_id = mem["id"]
    ctx.out(f"=== Links for {full_id[:8]} ===")
    ctx.out(f"    Type: {mem['type']}  Score: {mem['score'] or 0:.0f}  Hits: {mem['hits'] or 0}")
    ctx.out(f"    {mem['content'][:150]}")
    ctx.out()
    d = memory_details(session, full_id)
    ctx.out(f"  Topics: {', '.join(d['topics']) if d['topics'] else 'none'}")
    for p in session.run("""
        MATCH (:Memory {memory_id: $id})-[:IN_PROJECT]->(p:Project)
        RETURN p.name AS name, coalesce(p.path, p.key) AS where
    """, id=full_id):
        ctx.out(f"  Project: {p['name']} ({p['where']})")
    sids = session.run("""
        MATCH (:Memory {memory_id: $id})-[:IN_SESSION]->(s:Session)
        RETURN collect(s.session_id) AS sids
    """, id=full_id).single()["sids"]
    if sids:
        ctx.out(f"  Sessions: {', '.join(s[:8] for s in sids)}")
    ctx.out()
    links = [dict(r) for r in session.run(f"""
        MATCH (m:Memory {{memory_id: $id}})-[r:RELATES_TO]-(other:Memory)
        WHERE other.type <> 'prompt' AND {in_scope('other')}
        RETURN other.memory_id AS id, other.type AS type, other.content AS content,
               other.relevance_score AS score, r.strength AS strength
        ORDER BY r.strength DESC, other.relevance_score DESC
    """, id=full_id, spaces=ctx.read_spaces)]
    if not links:
        ctx.out("  No connections")
        return
    ctx.out(f"  Connections ({len(links)}):")
    for l in links:
        ctx.out(f"    --({l['strength']})--> {l['id'][:8]} [{l['type']}] score:{l['score'] or 0:.0f}")
        ctx.out(f"           {l['content'][:80].replace(chr(10), ' ')}")


def cmd_check_duplicates(session, ctx, content, **_):
    key = ctx.need_project()
    content = " ".join(content) if isinstance(content, list) else content
    existing = [dict(r) for r in session.run(f"""
        MATCH (m:Memory)-[:IN_PROJECT]->{PROJECT}
          AND m.type <> 'prompt' AND coalesce(m.superseded, false) = false
        WITH m ORDER BY m.created_at DESC LIMIT 30
        OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
        RETURN m.memory_id AS id, m.type AS type, m.content AS content,
               collect(t.name) AS topics
    """, key=key, spaces=ctx.read_spaces)]
    if not existing:
        ctx.out("NO_EXISTING_KNOWLEDGE")
        return
    ctx.out(f"=== Existing knowledge in project ({len(existing)} entries) ===")
    ctx.out(f"New content to check: {content}")
    ctx.out()
    for e in existing:
        ctx.out(f"  [{e['type']}][{', '.join(e['topics'][:3])}] {e['content']}")
    ctx.out()
    ctx.out("INSTRUCTIONS: Compare the new content against each existing entry above.")
    ctx.out("1. If any existing entry captures the SAME intent/meaning (even with different wording): DUPLICATE")
    ctx.out("2. If the new content CONTRADICTS an existing entry (e.g. different values for the same setting,")
    ctx.out("   opposite decisions about the same topic): CONFLICT:<memory_id> and explain the contradiction.")
    ctx.out("   Ask the user which version is current before saving.")
    ctx.out("3. If the new content is genuinely new knowledge: UNIQUE")


def recalculate_scores(session, spaces):
    session.run("MATCH (m:Memory)\nWHERE m.type <> 'prompt' AND m.space_id IN $spaces\n"
                + jejak._SCORE_BODY, spaces=spaces)


def cmd_rank(session, ctx, all=False, **_):
    recalculate_scores(session, ctx.read_spaces)
    if all:
        items = [dict(r) for r in session.run(f"""
            MATCH (m:Memory) WHERE {in_scope()} AND m.type <> 'prompt'
              AND coalesce(m.superseded, false) = false
            OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
            OPTIONAL MATCH (m)-[:IN_SESSION]->(s:Session)
            OPTIONAL MATCH (m)-[:RELATES_TO]-(other:Memory)
            WITH m, collect(DISTINCT t.name) AS topics,
                 count(DISTINCT s) AS sessions, count(DISTINCT other) AS connections
            ORDER BY m.relevance_score DESC
            RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                   m.relevance_score AS score, m.hit_count AS hits, sessions, connections, topics
        """, spaces=ctx.read_spaces)]
        label = "ALL PROJECTS"
    else:
        key = ctx.need_project()
        items = [dict(r) for r in session.run(f"""
            MATCH (m:Memory)-[:IN_PROJECT]->{PROJECT}
              AND m.type <> 'prompt' AND coalesce(m.superseded, false) = false
            OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
            OPTIONAL MATCH (m)-[:IN_SESSION]->(s:Session)
            OPTIONAL MATCH (m)-[:RELATES_TO]-(other:Memory)
            WITH m, collect(DISTINCT t.name) AS topics,
                 count(DISTINCT s) AS sessions, count(DISTINCT other) AS connections
            ORDER BY m.relevance_score DESC
            RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                   m.relevance_score AS score, m.hit_count AS hits, sessions, connections, topics
        """, key=key, spaces=ctx.read_spaces)]
        label = ctx.project_name or key
    if not items:
        ctx.out(f"No scored knowledge found for {'all projects' if all else label}")
        return
    max_score = max((i["score"] or 0) for i in items) or 1
    ctx.out(f"=== Knowledge Ranking: {label} ({len(items)} entries) ===")
    ctx.out("    Score formula: (hits*3 + sessions*5 + projects*10 + connections*2) * type_weight")
    ctx.out()
    for i, item in enumerate(items):
        score = item["score"] or 0
        normalized = int((score / max_score) * 100)
        depth = " [L1:global]" if i < 5 else (" [L2:project]" if i < 15 else " [L3:deep]")
        ctx.out(f"  {i+1:3d}. {item['id'][:8]}  score:{score:6.1f}  norm:{normalized:3d}%  "
                f"{'#' * int(normalized / 5)}{depth}")
        ctx.out(f"       [{item['type']}][{', '.join(item['topics'][:3])}] hits:{item['hits'] or 0} "
                f"sess:{item['sessions'] or 0} conn:{item['connections'] or 0}")
        ctx.out(f"       {item['content'][:90].replace(chr(10), ' ')}")
        if i < len(items) - 1:
            ctx.out()
    ctx.out()
    ctx.out("  Loading depth:")
    ctx.out("    L1 (global)  = top 5 by score — loaded in EVERY session")
    ctx.out("    L2 (project) = next 10 from current project — loaded on SessionStart")
    ctx.out("    L3 (deep)    = remaining — available via /jejak search")
    ctx.out("    L4 (code)    = not in graph — fall back to codebase search")


def cmd_export(session, ctx, all=False, **_):
    if all:
        projects = [dict(r) for r in session.run("""
            MATCH (p:Project) WHERE p.space_id IN $spaces
            RETURN p.name AS name, p.key AS key, p.path AS path, p.space_id AS space ORDER BY p.name
        """, spaces=ctx.read_spaces)]
    else:
        projects = [dict(r) for r in session.run(f"""
            MATCH {PROJECT}
            RETURN p.name AS name, p.key AS key, p.path AS path, p.space_id AS space
        """, key=ctx.need_project(), spaces=ctx.read_spaces)]
    if not projects:
        ctx.out("No projects found")
        return
    ctx.out("# Jejak Export")
    ctx.out(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    ctx.out()
    for proj in projects:
        items = [dict(r) for r in session.run("""
            MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {space_id: $space, key: $key})
            WHERE m.type <> 'prompt' AND m.type <> 'session_summary'
              AND coalesce(m.superseded, false) = false
            OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
            WITH m, collect(DISTINCT t.name) AS topics
            ORDER BY m.relevance_score DESC
            RETURN m.type AS type, m.content AS content, m.relevance_score AS score,
                   m.source_file AS source_file, topics
        """, space=proj["space"], key=proj["key"])]
        if not items:
            continue
        ctx.out(f"## {proj['name']}")
        ctx.out(f"Path: `{proj['path'] or proj['key']}`")
        ctx.out()
        by_type = {}
        for item in items:
            by_type.setdefault(item["type"], []).append(item)
        for t in ["architecture", "decision", "convention", "learning", "solution", "finding",
                  "issue", "context", "artifact"]:
            if t not in by_type:
                continue
            ctx.out(f"### {t.title()}s")
            for item in by_type[t]:
                ctx.out(f"- **[score:{item['score'] or 0:.0f}]** {item['content']}")
                if item["topics"]:
                    ctx.out(f"  Topics: {', '.join(item['topics'][:4])}")
                if item.get("source_file"):
                    ctx.out(f"  File: `{item['source_file']}`")
            ctx.out()


def cmd_ask(session, ctx, question, **_):
    question = " ".join(question) if isinstance(question, list) else question
    keywords = [w.lower() for w in question.split() if len(w) > 3]
    items = [dict(r) for r in session.run(f"""
        MATCH (m:Memory)
        WHERE {in_scope()} AND m.type <> 'prompt' AND m.type <> 'session_summary'
          AND coalesce(m.superseded, false) = false
          AND any(kw IN $keywords WHERE toLower(m.content) CONTAINS kw)
        OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
        OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
        OPTIONAL MATCH (m)-[:RELATES_TO]-(related:Memory)
        WHERE related.type <> 'prompt' AND {in_scope('related')}
        WITH m, collect(DISTINCT t.name) AS topics, collect(DISTINCT p.name) AS projects,
             collect(DISTINCT {{type: related.type, content: left(related.content, 100)}}) AS related_items
        ORDER BY m.relevance_score DESC
        LIMIT 8
        RETURN m.memory_id AS id, m.type AS type, m.content AS content,
               m.relevance_score AS score, m.source_file AS source_file,
               topics, projects, related_items
    """, keywords=keywords, spaces=ctx.read_spaces)]
    if not items:
        ctx.out(f"No knowledge found for: {question}")
        ctx.out("Try /jejak search with different terms, or explore the codebase directly.")
        return
    ctx.out("=== Answer from Jejak ===")
    ctx.out(f"Question: {question}")
    ctx.out()
    for i, item in enumerate(items):
        ctx.out(f"  {i+1}. [{item['type']}] score:{item['score'] or 0:.0f} "
                f"project:{', '.join(item['projects'][:2])}")
        ctx.out(f"     {item['content']}")
        if item.get("source_file"):
            ctx.out(f"     File: {item['source_file']}")
        if item["topics"]:
            ctx.out(f"     Topics: {', '.join(item['topics'][:3])}")
        related = [r for r in (item.get("related_items") or []) if r.get("content")]
        if related:
            ctx.out("     Related:")
            for r in related[:3]:
                ctx.out(f"       - [{r['type']}] {r['content']}")
        ctx.out()
    ctx.out("INSTRUCTIONS: Use the knowledge above to answer the user's question.")
    ctx.out("If the answer is incomplete, suggest checking /jejak search or exploring the codebase.")


def cmd_stats(session, ctx, **_):
    r = session.run("""
        OPTIONAL MATCH (m:Memory) WHERE m.space_id IN $spaces WITH count(m) AS memories
        OPTIONAL MATCH (p:Project) WHERE p.space_id IN $spaces WITH memories, count(p) AS projects
        OPTIONAL MATCH (s:Session) WHERE s.space_id IN $spaces
        WITH memories, projects, count(s) AS sessions
        OPTIONAL MATCH (t:Topic) WITH memories, projects, sessions, count(t) AS topics
        RETURN memories, projects, sessions, topics
    """, spaces=ctx.read_spaces).single()
    ctx.out("=== Jejak Stats ===")
    ctx.out(f"  Memories:  {r['memories']}")
    ctx.out(f"  Projects:  {r['projects']}")
    ctx.out(f"  Sessions:  {r['sessions']}")
    ctx.out(f"  Topics:    {r['topics']}")
    vis = session.run(f"""
        MATCH (m:Memory) WHERE {in_scope()} AND {jejak.NOT_SUPERSEDED}
        RETURN sum(CASE WHEN {jejak.IS_PUBLIC} THEN 1 ELSE 0 END) AS public, count(m) AS active
    """, spaces=ctx.read_spaces).single()
    ctx.out(f"  Public:    {vis['public'] or 0} of {vis['active']} active (the rest are confidential)")
    ctx.out()
    ctx.out("  Projects:")
    for p in session.run(f"""
        MATCH (m:Memory)-[:IN_PROJECT]->(p:Project) WHERE {in_scope()}
        WITH p, count(m) AS mem_count ORDER BY mem_count DESC
        RETURN p.name AS name, coalesce(p.path, p.key) AS where, mem_count
    """, spaces=ctx.read_spaces):
        ctx.out(f"    {p['name'] or '?':25s}  {p['mem_count']:4d} memories  ({p['where']})")
    ctx.out()
    ctx.out("  Machines:")
    for m in session.run(f"""
        MATCH (m:Memory) WHERE {in_scope()}
        WITH m.machine_name AS machine, count(m) AS count ORDER BY count DESC
        RETURN machine, count
    """, spaces=ctx.read_spaces):
        ctx.out(f"    {m['machine'] or '?':25s}  {m['count']:4d} memories")


def cmd_get(session, ctx, memory_id, json=False, **_):
    d = memory_details(session, resolve_memory(session, ctx, memory_id))
    if json:
        import json as _json
        ctx.out(_json.dumps(d))
        return
    ctx.out(f"=== {d['memory_id']} ===")
    ctx.out(f"    [{d['type']}] {d['status'].upper()}  score:{d['relevance_score'] or 0:.0f}  "
            f"hits:{d['hit_count'] or 0}  sessions:{d['sessions']}")
    ctx.out(f"    created:{str(d['created_at'])[:19]}  updated:{str(d['updated_at'])[:19]}  "
            f"machine:{d['machine_name']}")
    ctx.out(f"    visibility:{d['visibility']}")
    ctx.out(f"    projects:{', '.join(d['projects'])}  topics:{', '.join(d['topics'])}")
    if d["source_file"]:
        ctx.out(f"    file:{d['source_file']}")
    if d["status"] == "superseded":
        ctx.out(f"    superseded by {d['superseded_by'][:8]} at {str(d['superseded_at'])[:19]}")
    if d["status"] == "deleted":
        ctx.out(f"    deleted at {str(d['superseded_at'])[:19]}: {d['deleted_reason'] or '(no reason)'}")
    if d["supersedes"]:
        ctx.out(f"    supersedes: {', '.join(i[:8] for i in d['supersedes'])}")
    if d["restored_from"]:
        ctx.out(f"    restored from: {d['restored_from'][:8]}")
    ctx.out()
    ctx.out(d["content"])


def cmd_recent(session, ctx, limit=40, **_):
    """JSON list of the project's active knowledge, best first (for the Stop hook's judge)."""
    import json as _json
    key = ctx.need_project()
    rows = session.run(f"""
        MATCH (m:Memory)-[:IN_PROJECT]->{PROJECT}
          AND m.type <> 'prompt' AND m.type <> 'session_summary'
          AND coalesce(m.superseded, false) = false
        RETURN DISTINCT left(m.memory_id, 8) AS id, m.type AS type, m.content AS content,
               m.relevance_score AS score
        ORDER BY score DESC LIMIT $limit
    """, key=key, spaces=ctx.read_spaces, limit=int(limit)).data()
    ctx.out(_json.dumps([[r["id"], r["type"], r["content"]] for r in rows]))


def cmd_artifact_id(session, ctx, source_file, **_):
    """The active artifact entry for a file in this project, so a rewrite supersedes it."""
    key = ctx.need_project()
    rec = session.run(f"""
        MATCH (m:Memory {{type: 'artifact'}})-[:IN_PROJECT]->{PROJECT}
          AND m.source_file = $sf AND coalesce(m.superseded, false) = false
        RETURN left(m.memory_id, 8) AS id LIMIT 1
    """, key=key, spaces=ctx.read_spaces, sf=source_file).single()
    ctx.out(rec["id"] if rec else "")


COMMANDS = {
    "recent": cmd_recent, "artifact-id": cmd_artifact_id,
    "save": cmd_save, "map": cmd_map, "session": cmd_session, "search": cmd_search,
    "topics": cmd_topics, "stats": cmd_stats, "rank": cmd_rank, "links": cmd_links,
    "check-duplicates": cmd_check_duplicates, "export": cmd_export, "ask": cmd_ask,
    "get": cmd_get, "delete": cmd_delete, "restore": cmd_restore, "visibility": cmd_visibility,
}


# --- what the Claude Code hooks need ---------------------------------------------

def _touch(session, ids):
    session.run("UNWIND $ids AS id MATCH (m:Memory {memory_id: id}) SET m.last_accessed_at = datetime()",
                ids=list(ids))


def prompt_context(session, ctx, topics, limit=5):
    """UserPromptSubmit: the project's best entries, nudged by the prompt's topics.
    The prompt itself is not sent; the client detects its topics locally."""
    if not ctx.project_key:
        return ""
    rows = [dict(r) for r in session.run(f"""
        MATCH (m:Memory)-[:IN_PROJECT]->{PROJECT}
          AND m.type <> 'prompt' AND coalesce(m.superseded, false) = false
        OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
        WHERE t.name IN $topics AND t.name <> $catchall
        WITH m, count(t) AS topic_relevance
        ORDER BY m.relevance_score DESC, topic_relevance DESC
        LIMIT $limit
        RETURN m.memory_id AS id, m.type AS type, m.content AS content, m.relevance_score AS score
    """, key=ctx.project_key, spaces=ctx.read_spaces, topics=topics,
         catchall=jejak.CATCHALL_TOPIC, limit=limit)]
    if not rows:
        return ""
    _touch(session, (r["id"] for r in rows))
    lines = ["[Jejak] Related context from previous sessions:"]
    for r in rows:
        lines.append(f"- [{r['type']}](score:{(r['score'] or 0):.0f}) {r['content'][:200]}")
    return "\n".join(lines)


def session_start_context(session, ctx):
    """SessionStart: L1 global top 5, L2 project top 10, L2.5 related projects."""
    spaces = ctx.read_spaces
    global_list = [dict(r) for r in session.run(f"""
        MATCH (m:Memory)
        WHERE {in_scope()} AND m.type <> 'prompt' AND m.type <> 'session_summary'
          AND m.relevance_score > 0 AND coalesce(m.superseded, false) = false
        WITH m ORDER BY m.relevance_score DESC LIMIT 5
        OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
        OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
        WITH m, collect(DISTINCT t.name) AS topics, collect(DISTINCT p.name) AS projects
        ORDER BY m.relevance_score DESC
        RETURN m.memory_id AS id, m.type AS type, m.content AS content,
               m.relevance_score AS score, m.hit_count AS hits, topics, projects
    """, spaces=spaces)]
    global_ids = {g["id"] for g in global_list}
    _touch(session, global_ids)

    project_list, project_stats, cross_list, last_sum = [], None, [], None
    key = ctx.project_key
    if key and session.run(f"MATCH {PROJECT} RETURN p LIMIT 1", key=key, spaces=spaces).peek():
        project_list = [dict(r) for r in session.run(f"""
            MATCH (m:Memory)-[:IN_PROJECT]->{PROJECT}
              AND m.type <> 'prompt' AND m.type <> 'session_summary'
              AND coalesce(m.superseded, false) = false
            WITH DISTINCT m ORDER BY m.relevance_score DESC LIMIT 10
            OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
            WITH m, collect(DISTINCT t.name) AS topics
            ORDER BY m.relevance_score DESC
            RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                   m.relevance_score AS score, m.hit_count AS hits, topics
        """, key=key, spaces=spaces)]
        _touch(session, (p["id"] for p in project_list))
        project_stats = dict(session.run(f"""
            MATCH (m:Memory)-[:IN_PROJECT]->{PROJECT}
            WITH count(DISTINCT m) AS memories
            OPTIONAL MATCH (s:Session)-[:IN_PROJECT]->(p2:Project {{key: $key}})
            WHERE p2.space_id IN $spaces
            RETURN memories, count(DISTINCT s) AS sessions
        """, key=key, spaces=spaces).single())
        shown = global_ids | {p["id"] for p in project_list}
        cross = session.run(f"""
            MATCH (m:Memory)-[:IN_PROJECT]->{PROJECT}
            MATCH (m)-[:ABOUT]->(t:Topic) WHERE t.name <> $catchall
            WITH collect(DISTINCT t.name) AS project_topics
            MATCH (other:Memory)-[:ABOUT]->(t2:Topic)
            WHERE t2.name IN project_topics AND {in_scope('other')}
              AND other.type <> 'prompt' AND other.type <> 'session_summary'
              AND coalesce(other.superseded, false) = false
              AND NOT exists {{ (other)-[:IN_PROJECT]->(:Project {{key: $key}}) }}
            OPTIONAL MATCH (other)-[:IN_PROJECT]->(op:Project)
            WITH other, collect(DISTINCT t2.name) AS shared_topics,
                 collect(DISTINCT op.name) AS from_projects, count(DISTINCT t2) AS overlap
            WHERE overlap >= 2
            ORDER BY other.relevance_score DESC
            LIMIT 5
            RETURN other.memory_id AS id, other.type AS type, other.content AS content,
                   other.relevance_score AS score, shared_topics, from_projects
        """, key=key, spaces=spaces, catchall=jejak.CATCHALL_TOPIC)
        cross_list = [dict(r) for r in cross if r["id"] not in shown]
        last_sum = session.run(f"""
            MATCH (m:Memory {{type: 'session_summary'}})-[:IN_PROJECT]->{PROJECT}
            RETURN m.content AS content ORDER BY m.created_at DESC LIMIT 1
        """, key=key, spaces=spaces).single()

    lines = []
    if last_sum:
        lines += [f"[Jejak] Last session: {last_sum['content']}", ""]
    if global_list:
        lines.append("[Jejak] Layer 1 — Global knowledge (highest scored):")
        for g in global_list:
            lines.append(f"  - [{g['type']}][{', '.join([x for x in g['topics'] if x][:3])}] "
                         f"score:{(g['score'] or 0):.0f} hits:{g['hits'] or 0} "
                         f"from:{', '.join([x for x in g['projects'] if x][:2])}")
            lines.append(f"    {g['content'][:120].replace(chr(10), ' ')}")
        lines.append("")
    filtered = [p for p in project_list if p["id"] not in global_ids]
    if filtered:
        lines.append(f"[Jejak] Layer 2 — Project: {ctx.project_name or key} "
                     f"({project_stats['memories']} memories, {project_stats['sessions']} sessions):")
        for p in filtered:
            lines.append(f"  - [{p['type']}][{', '.join([x for x in p['topics'] if x][:3])}] "
                         f"score:{(p['score'] or 0):.0f} hits:{p['hits'] or 0}")
            lines.append(f"    {p['content'][:120].replace(chr(10), ' ')}")
        lines.append("")
    if cross_list:
        lines.append("[Jejak] Layer 2.5 — Related knowledge from other projects:")
        for c in cross_list[:3]:
            lines.append(f"  - [{c['type']}] score:{(c['score'] or 0):.0f} "
                         f"shared:[{', '.join(c['shared_topics'][:3])}] "
                         f"from:{', '.join([x for x in c['from_projects'] if x][:2])}")
            lines.append(f"    {c['content'][:120].replace(chr(10), ' ')}")
        lines.append("")
        lines.append("  Layer 3 (deeper) via /jejak search | Layer 4 (code) via codebase exploration")
    return "\n".join(lines)


def session_end(session, ctx, session_id):
    """SessionEnd: a short summary of what was saved, on the session's main project."""
    session.run("MATCH (s:Session {session_id: $sid}) WHERE s.space_id IN $spaces SET s.ended_at = datetime()",
                sid=session_id, spaces=ctx.write_spaces)
    project = session.run(f"""
        MATCH (m:Memory)-[:IN_SESSION]->(s:Session {{session_id: $sid}})
        WHERE {in_scope()} AND m.type <> 'prompt' AND m.type <> 'session_summary'
        MATCH (m)-[:IN_PROJECT]->(p:Project)
        WITH p, count(m) AS cnt ORDER BY cnt DESC LIMIT 1
        RETURN p.space_id AS space, p.key AS key, p.name AS name, p.path AS path
    """, sid=session_id, spaces=ctx.write_spaces).single()
    if not project:
        return None
    entries = session.run(f"""
        MATCH (m:Memory)-[:IN_SESSION]->(s:Session {{session_id: $sid, space_id: $space}})
        WHERE m.type <> 'prompt' AND m.type <> 'session_summary'
        WITH m ORDER BY m.created_at ASC
        RETURN collect(m.type + ': ' + left(m.content, 80)) AS entries
    """, sid=session_id, space=project["space"]).single()["entries"]
    if not entries:
        return None
    text = "; ".join(entries) if len(entries) <= 3 else "; ".join(entries[:3]) + f" (+{len(entries) - 3} more)"
    if len(text) > 300:
        text = text[:297] + "..."
    mid = str(uuid.uuid4())
    session.run("""
        CREATE (m:Memory {memory_id: $id, type: 'session_summary', content: $content,
            space_id: $space, machine_name: $machine, hit_count: 1, relevance_score: 0.0,
            visibility: 'confidential', created_by: $user,
            last_accessed_at: datetime(), created_at: datetime(), updated_at: datetime()})
    """, id=mid, content=text, space=project["space"], machine=ctx.machine, user=ctx.username)
    _attach(session, Ctx(username=ctx.username, read_spaces=[], write_spaces=[], machine=ctx.machine,
                         session=session_id),
            project["space"], mid, project["key"], project["name"], project["path"])
    return mid


# --- graph view ---------------------------------------------------------------------

GRAPH_SIGNATURE = """
    MATCH (m:Memory) WHERE m.space_id IN $spaces
    RETURN count(m) AS n, toString(max(m.updated_at)) AS updated,
           sum(coalesce(m.hit_count, 1)) AS hits,
           round(sum(coalesce(m.relevance_score, 0.0))) AS score,
           sum(CASE WHEN coalesce(m.superseded, false) THEN 1 ELSE 0 END) AS inactive,
           sum(CASE WHEN coalesce(m.visibility, 'confidential') = 'public' THEN 1 ELSE 0 END) AS public
"""

# A memory not marked public comes back with its text, topics, projects and
# sessions blanked when $public_view is set, so confidential text never leaves
# Neo4j in a public view.
GRAPH_NODES = """
    MATCH (m:Memory)
    WHERE m.space_id IN $spaces AND coalesce(m.superseded, false) = false
    OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
    OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
    OPTIONAL MATCH (m)-[:IN_SESSION]->(sess:Session)
    WITH m, collect(DISTINCT t.name) AS topics,
         collect(DISTINCT p.name) AS projects,
         collect(DISTINCT sess.session_id) AS sessions,
         coalesce(m.visibility, 'confidential') = 'public' AS public
    WITH m, topics, projects, sessions, public,
         public OR NOT $public_view AS shown
    RETURN left(m.memory_id, 8) AS id, m.type AS type,
           CASE WHEN shown THEN m.content ELSE '' END AS content,
           coalesce(m.hit_count, 1) AS hits,
           coalesce(m.relevance_score, 0.0) AS score,
           CASE WHEN shown THEN topics ELSE [] END AS topics,
           CASE WHEN shown THEN projects ELSE [] END AS projects,
           CASE WHEN shown THEN [x IN sessions | left(x, 8)] ELSE [] END AS sessions,
           public, NOT shown AS confidential
"""

# Display only: each memory's top-k links. Every link stays in Neo4j.
GRAPH_EDGES = """
    MATCH (a:Memory)-[r:RELATES_TO]-(b:Memory)
    WHERE a.space_id IN $spaces AND b.space_id IN $spaces
      AND coalesce(a.superseded, false) = false AND coalesce(b.superseded, false) = false
    WITH a, b, coalesce(r.strength, 1) AS strength
    ORDER BY strength DESC, coalesce(b.relevance_score, 0.0) DESC
    WITH a, collect({target: left(b.memory_id, 8), strength: strength})[..$k] AS top
    UNWIND top AS t
    RETURN left(a.memory_id, 8) AS source, t.target AS target, t.strength AS strength
"""


def graph_signature(session, spaces):
    return tuple(session.run(GRAPH_SIGNATURE, spaces=spaces).single().values())


def graph(session, spaces, public_view, edges_per_node=10):
    nodes = [dict(r) for r in session.run(GRAPH_NODES, spaces=spaces, public_view=public_view)]
    strongest = {}
    for r in session.run(GRAPH_EDGES, spaces=spaces, k=edges_per_node):
        pair = tuple(sorted((r["source"], r["target"])))
        strongest[pair] = max(strongest.get(pair, 0), r["strength"])
    edges = [{"source": a, "target": b, "strength": s} for (a, b), s in strongest.items()]
    return {"nodes": nodes, "edges": edges, "public_view": public_view}

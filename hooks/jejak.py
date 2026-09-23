#!/usr/bin/env python3
"""
Jejak knowledge-graph manager for Claude Code sessions.

Features:
  - Relevance scoring with time decay (canonical formula in jejak_common)
  - Layered context loading (L1 global, L2 project, L2.5 cross-project)
  - Session summary on end (attached to the session's dominant project)
  - Secret redaction before any content is stored
  - Errors logged to ~/.claude/logs/jejak-errors.log (no silent failures)

Graph structure:
  (Memory) -[:ABOUT]-> (Topic)
  (Memory) -[:IN_PROJECT]-> (Project)
  (Memory) -[:IN_SESSION]-> (Session)
  (Memory) -[:RELATES_TO]-> (Memory)   # non-prompt only, on real (non-general) topics
  (Session) -[:IN_PROJECT]-> (Project)
"""
import json
import sys
import os
import uuid
from neo4j import GraphDatabase

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
import jejak_common as jejak


def get_driver():
    config = jejak.load_config("neo4j")
    return GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))


def save_memory(driver, memory_type, content, topics, project_path, project_name, session_id, machine_name):
    content = jejak.redact(content)
    with driver.session() as session:
        # Global exact-content dedup: if this content already exists ANYWHERE,
        # reuse that node and just attach the current project/session.
        dup = session.run("""
            MATCH (m:Memory) WHERE m.content = $content
            RETURN m.memory_id AS memory_id LIMIT 1
        """, content=content).single()

        if dup:
            existing_id = dup["memory_id"]
            session.run("""
                MATCH (m:Memory {memory_id: $memory_id})
                SET m.updated_at = datetime(),
                    m.hit_count = coalesce(m.hit_count, 1) + 1,
                    m.last_accessed_at = datetime()
            """, memory_id=existing_id)
            session.run("""
                MERGE (p:Project {path: $path}) SET p.name = $name, p.last_seen = datetime()
                WITH p
                MATCH (m:Memory {memory_id: $memory_id})
                MERGE (m)-[:IN_PROJECT]->(p)
            """, path=project_path, name=project_name, memory_id=existing_id)
            session.run("""
                MERGE (s:Session {session_id: $session_id})
                SET s.last_active = datetime(), s.machine_name = $machine_name
                WITH s
                MATCH (m:Memory {memory_id: $memory_id})
                MERGE (m)-[:IN_SESSION]->(s)
                WITH s
                MATCH (p:Project {path: $project_path})
                MERGE (s)-[:IN_PROJECT]->(p)
            """, session_id=session_id, machine_name=machine_name, memory_id=existing_id, project_path=project_path)
            jejak.update_relevance_score(session, existing_id)
            return existing_id

        memory_id = str(uuid.uuid4())
        session.run("""
            CREATE (m:Memory {memory_id: $memory_id, type: $type, content: $content,
                machine_name: $machine_name, hit_count: 1, relevance_score: 0.0,
                last_accessed_at: datetime(), created_at: datetime(), updated_at: datetime()})
        """, memory_id=memory_id, type=memory_type, content=content, machine_name=machine_name)

        session.run("""
            MERGE (p:Project {path: $path}) SET p.name = $name, p.last_seen = datetime()
            WITH p
            MATCH (m:Memory {memory_id: $memory_id})
            MERGE (m)-[:IN_PROJECT]->(p)
        """, path=project_path, name=project_name, memory_id=memory_id)

        session.run("""
            MERGE (s:Session {session_id: $session_id})
            SET s.last_active = datetime(), s.machine_name = $machine_name
            WITH s
            MATCH (m:Memory {memory_id: $memory_id})
            MERGE (m)-[:IN_SESSION]->(s)
            WITH s
            MATCH (p:Project {path: $project_path})
            MERGE (s)-[:IN_PROJECT]->(p)
        """, session_id=session_id, machine_name=machine_name, memory_id=memory_id, project_path=project_path)

        # Topic nodes are only useful for linking + topic-based reads, which
        # apply to non-prompt knowledge. Prompts don't need them, and the
        # catch-all "general" is a hot lock magnet — skip both to avoid
        # cross-session write contention on shared Topic nodes.
        if memory_type != "prompt":
            for topic in topics:
                if topic == jejak.CATCHALL_TOPIC:
                    continue
                session.run("""
                    MERGE (t:Topic {name: $topic})
                    WITH t
                    MATCH (m:Memory {memory_id: $memory_id})
                    MERGE (m)-[:ABOUT]->(t)
                """, topic=topic, memory_id=memory_id)

        # Link only non-prompt memories, and only on REAL (non-general) shared
        # topics. This prevents the catch-all "general" clump and prompt noise.
        if memory_type != "prompt":
            session.run("""
                MATCH (m:Memory {memory_id: $memory_id})-[:ABOUT]->(t:Topic)<-[:ABOUT]-(other:Memory)
                WHERE other.memory_id <> $memory_id
                  AND other.type <> 'prompt'
                  AND t.name <> $catchall
                  AND NOT exists((m)-[:RELATES_TO]-(other))
                WITH m, other, count(t) AS shared
                WHERE shared >= 1
                MERGE (m)-[:RELATES_TO {strength: shared}]->(other)
            """, memory_id=memory_id, catchall=jejak.CATCHALL_TOPIC)

        jejak.update_relevance_score(session, memory_id)
        return memory_id


def query_related(driver, project_path, topics, limit=5):
    with driver.session() as session:
        result = session.run("""
            MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {path: $project_path})
            WHERE m.type <> 'prompt' AND coalesce(m.superseded, false) = false
            OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
            WHERE t.name IN $topics AND t.name <> $catchall
            WITH m, count(t) AS topic_relevance
            ORDER BY m.relevance_score DESC, topic_relevance DESC
            LIMIT $limit
            RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                   m.relevance_score AS score
        """, project_path=project_path, topics=topics, catchall=jejak.CATCHALL_TOPIC, limit=limit)
        rows = [dict(r) for r in result]
        for r in rows:
            session.run("MATCH (m:Memory {memory_id: $id}) SET m.last_accessed_at = datetime()", id=r["id"])
        return rows


def handle_prompt_submit(data):
    prompt = data.get("prompt", "")
    cwd = data.get("cwd", os.getcwd())
    session_id = data.get("session_id", "unknown")
    if not prompt.strip():
        return

    topics = jejak.detect_topics(prompt)
    project_name = jejak.extract_project_name(cwd)
    driver = get_driver()
    try:
        save_memory(driver, "prompt", prompt, topics, cwd, project_name, session_id, jejak.machine_name())
        related = query_related(driver, cwd, topics, limit=5)
        if related:
            lines = ["[Jejak] Related context from previous sessions:"]
            redacted_prompt = jejak.redact(prompt)
            for r in related:
                if r["content"] != redacted_prompt:
                    lines.append(f"- [{r['type']}](score:{(r['score'] or 0):.0f}) {r['content'][:200]}")
            if len(lines) > 1:
                print("\n".join(lines))
    finally:
        driver.close()


def handle_session_start(data):
    cwd = data.get("cwd", os.getcwd())
    driver = get_driver()
    try:
        project_name = jejak.extract_project_name(cwd)
        with driver.session() as session:
            global_top = session.run("""
                MATCH (m:Memory)
                WHERE m.type <> 'prompt' AND m.type <> 'session_summary'
                  AND m.relevance_score > 0 AND coalesce(m.superseded, false) = false
                OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
                WITH m, collect(DISTINCT t.name) AS topics, collect(DISTINCT p.name) AS projects
                ORDER BY m.relevance_score DESC
                LIMIT 5
                RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                       m.relevance_score AS score, m.hit_count AS hits, topics, projects
            """)
            global_list = [dict(r) for r in global_top]
            global_ids = {g["id"] for g in global_list}
            for g in global_list:
                session.run("MATCH (m:Memory {memory_id: $id}) SET m.last_accessed_at = datetime()", id=g["id"])

            has_project = session.run(
                "MATCH (p:Project {path: $path}) RETURN p.name AS name", path=cwd
            ).peek() is not None

            project_list, project_stats, cross_project_list = [], None, []

            if has_project:
                project_top = session.run("""
                    MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {path: $path})
                    WHERE m.type <> 'prompt' AND m.type <> 'session_summary'
                      AND coalesce(m.superseded, false) = false
                    OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                    WITH m, collect(DISTINCT t.name) AS topics
                    ORDER BY m.relevance_score DESC
                    LIMIT 10
                    RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                           m.relevance_score AS score, m.hit_count AS hits, topics
                """, path=cwd)
                project_list = [dict(r) for r in project_top]
                for p in project_list:
                    session.run("MATCH (m:Memory {memory_id: $id}) SET m.last_accessed_at = datetime()", id=p["id"])

                project_stats = dict(session.run("""
                    MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {path: $path})
                    WITH count(m) AS memories
                    OPTIONAL MATCH (s:Session)-[:IN_PROJECT]->(p2:Project {path: $path})
                    RETURN memories, count(DISTINCT s) AS sessions
                """, path=cwd).single())

                all_shown = global_ids | {p["id"] for p in project_list}
                cross = session.run("""
                    MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {path: $path})
                    MATCH (m)-[:ABOUT]->(t:Topic) WHERE t.name <> $catchall
                    WITH collect(DISTINCT t.name) AS project_topics
                    MATCH (other:Memory)-[:ABOUT]->(t2:Topic)
                    WHERE t2.name IN project_topics
                      AND other.type <> 'prompt' AND other.type <> 'session_summary'
                      AND coalesce(other.superseded, false) = false
                      AND NOT exists((other)-[:IN_PROJECT]->(:Project {path: $path}))
                    OPTIONAL MATCH (other)-[:IN_PROJECT]->(op:Project)
                    WITH other, collect(DISTINCT t2.name) AS shared_topics,
                         collect(DISTINCT op.name) AS from_projects, count(DISTINCT t2) AS overlap
                    WHERE overlap >= 2
                    ORDER BY other.relevance_score DESC
                    LIMIT 5
                    RETURN other.memory_id AS id, other.type AS type, other.content AS content,
                           other.relevance_score AS score, shared_topics, from_projects
                """, path=cwd, catchall=jejak.CATCHALL_TOPIC)
                cross_project_list = [dict(r) for r in cross if r["id"] not in all_shown]

            last_sum = session.run("""
                MATCH (m:Memory {type: 'session_summary'})-[:IN_PROJECT]->(p:Project {path: $path})
                RETURN m.content AS content ORDER BY m.created_at DESC LIMIT 1
            """, path=cwd).single()

        lines = []
        if last_sum:
            lines.append(f"[Jejak] Last session: {last_sum['content']}")
            lines.append("")

        if global_list:
            lines.append("[Jejak] Layer 1 — Global knowledge (highest scored):")
            for g in global_list:
                topic_str = ", ".join([x for x in g["topics"] if x][:3])
                proj_str = ", ".join([x for x in g["projects"] if x][:2])
                lines.append(f"  - [{g['type']}][{topic_str}] score:{(g['score'] or 0):.0f} hits:{g['hits'] or 0} from:{proj_str}")
                lines.append(f"    {g['content'][:120].replace(chr(10), ' ')}")
            lines.append("")

        if project_list:
            filtered = [p for p in project_list if p["id"] not in global_ids]
            if filtered:
                mem_count = project_stats["memories"] if project_stats else 0
                sess_count = project_stats["sessions"] if project_stats else 0
                lines.append(f"[Jejak] Layer 2 — Project: {project_name} ({mem_count} memories, {sess_count} sessions):")
                for p in filtered:
                    topic_str = ", ".join([x for x in p["topics"] if x][:3])
                    lines.append(f"  - [{p['type']}][{topic_str}] score:{(p['score'] or 0):.0f} hits:{p['hits'] or 0}")
                    lines.append(f"    {p['content'][:120].replace(chr(10), ' ')}")
                lines.append("")

        if cross_project_list:
            lines.append("[Jejak] Layer 2.5 — Related knowledge from other projects:")
            for c in cross_project_list[:3]:
                shared = ", ".join(c["shared_topics"][:3])
                from_proj = ", ".join([x for x in c["from_projects"] if x][:2])
                lines.append(f"  - [{c['type']}] score:{(c['score'] or 0):.0f} shared:[{shared}] from:{from_proj}")
                lines.append(f"    {c['content'][:120].replace(chr(10), ' ')}")
            lines.append("")
            lines.append("  Layer 3 (deeper) via /jejak search | Layer 4 (code) via codebase exploration")

        if lines:
            print("\n".join(lines))
    finally:
        driver.close()


def handle_session_end(data):
    session_id = data.get("session_id", "unknown")
    driver = get_driver()
    try:
        with driver.session() as session:
            session.run("MATCH (s:Session {session_id: $sid}) SET s.ended_at = datetime()", sid=session_id)

            summary_data = session.run("""
                MATCH (m:Memory)-[:IN_SESSION]->(s:Session {session_id: $sid})
                WHERE m.type <> 'prompt' AND m.type <> 'session_summary'
                WITH m ORDER BY m.created_at ASC
                RETURN collect(m.type + ': ' + left(m.content, 80)) AS entries
            """, sid=session_id).single()

            if not (summary_data and summary_data["entries"]):
                return

            entries = summary_data["entries"]
            if len(entries) <= 3:
                summary_text = "; ".join(entries)
            else:
                summary_text = "; ".join(entries[:3]) + f" (+{len(entries) - 3} more)"
            if len(summary_text) > 300:
                summary_text = summary_text[:297] + "..."

            # Attach to the project where the MOST knowledge was saved this
            # session (not an arbitrary LIMIT 1).
            project = session.run("""
                MATCH (m:Memory)-[:IN_SESSION]->(s:Session {session_id: $sid})
                WHERE m.type <> 'prompt' AND m.type <> 'session_summary'
                MATCH (m)-[:IN_PROJECT]->(p:Project)
                WITH p.path AS path, p.name AS name, count(m) AS cnt
                ORDER BY cnt DESC LIMIT 1
                RETURN path, name
            """, sid=session_id).single()

            if project:
                summary_id = str(uuid.uuid4())
                session.run("""
                    CREATE (m:Memory {memory_id: $id, type: 'session_summary', content: $content,
                        machine_name: $machine, hit_count: 1, relevance_score: 0.0,
                        last_accessed_at: datetime(), created_at: datetime(), updated_at: datetime()})
                """, id=summary_id, content=summary_text, machine=jejak.machine_name())
                session.run("""
                    MATCH (m:Memory {memory_id: $id})
                    MERGE (p:Project {path: $path})
                    MERGE (m)-[:IN_PROJECT]->(p)
                    MERGE (s:Session {session_id: $sid})
                    MERGE (m)-[:IN_SESSION]->(s)
                """, id=summary_id, path=project["path"], sid=session_id)
    finally:
        driver.close()


def main():
    # Recursion guard: bail inside the Stop hook's headless extraction call.
    if os.environ.get("JEJAK_EXTRACTING"):
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    event = data.get("hook_event_name", "UserPromptSubmit")
    try:
        if event == "UserPromptSubmit":
            handle_prompt_submit(data)
        elif event == "SessionStart":
            handle_session_start(data)
        elif event == "SessionEnd":
            handle_session_end(data)
    except Exception as e:
        jejak.log_error(f"jejak.py/{event}", e)


main()

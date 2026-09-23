#!/usr/bin/env python3
"""
Jejak CLI — save, query, and map knowledge across sessions.

Usage:
  jejak save <type> <content>         Save a knowledge entry
  jejak map [directory]               Show knowledge map for a directory
  jejak session <session_id>          Show all knowledge from a session
  jejak search <query>                Search across all knowledge
  jejak topics [directory]            Show topic clusters for a directory
  jejak links <memory_id>             Show all connections for a memory
  jejak stats                         Show global stats

Types: decision, learning, finding, issue, solution, architecture, convention, context
"""
import json
import sys
import os
import uuid
import argparse
from neo4j import GraphDatabase

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
import jejak_common as jejak

detect_topics = jejak.detect_topics
extract_project_name = jejak.extract_project_name
update_relevance_score = jejak.update_relevance_score

VALID_TYPES = ["decision", "learning", "finding", "issue", "solution", "architecture", "convention", "context", "artifact"]

def get_driver():
    config = jejak.load_config("neo4j")
    return GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))

def cmd_save(args):
    memory_type = args.type
    content = jejak.redact(" ".join(args.content))
    cwd = args.directory or os.getcwd()
    session_id = os.environ.get("CLAUDE_SESSION_ID", args.session or "manual")
    source_file = args.file or None
    supersedes = getattr(args, "supersedes", None)

    if memory_type not in VALID_TYPES:
        print(f"Invalid type '{memory_type}'. Valid types: {', '.join(VALID_TYPES)}")
        sys.exit(1)

    topics = detect_topics(content)
    project_name = extract_project_name(cwd)
    machine_name = jejak.machine_name()

    driver = get_driver()
    try:
        with driver.session() as session:
            # Global exact-content dedup (reuse a node that exists in any project)
            dup_record = session.run("""
                MATCH (m:Memory) WHERE m.content = $content
                RETURN m.memory_id AS memory_id LIMIT 1
            """, content=content).single()

            if dup_record:
                existing_id = dup_record["memory_id"]
                session.run("""
                    MATCH (m:Memory {memory_id: $memory_id})
                    SET m.updated_at = datetime(), m.hit_count = coalesce(m.hit_count, 1) + 1,
                        m.last_accessed_at = datetime()
                """, memory_id=existing_id)
                session.run("""
                    MERGE (p:Project {path: $path}) SET p.name = $name, p.last_seen = datetime()
                    WITH p
                    MATCH (m:Memory {memory_id: $memory_id})
                    MERGE (m)-[:IN_PROJECT]->(p)
                """, path=cwd, name=project_name, memory_id=existing_id)
                session.run("""
                    MERGE (s:Session {session_id: $session_id})
                    SET s.last_active = datetime(), s.machine_name = $machine_name
                    WITH s
                    MATCH (m:Memory {memory_id: $memory_id})
                    MERGE (m)-[:IN_SESSION]->(s)
                    WITH s
                    MATCH (p:Project {path: $path})
                    MERGE (s)-[:IN_PROJECT]->(p)
                """, session_id=session_id, machine_name=machine_name, memory_id=existing_id, path=cwd)
                update_relevance_score(session, existing_id)
                score_result = session.run(
                    "MATCH (m:Memory {memory_id: $mid}) RETURN m.relevance_score AS score", mid=existing_id
                ).single()
                score = score_result["score"] if score_result else 0
                pruned = jejak.mark_superseded(session, supersedes, existing_id) if supersedes else None
                msg = f"Existing [{memory_type}] id:{existing_id[:8]} score:{score:.0f} project:{project_name}"
                if pruned:
                    msg += f" SUPERSEDES:{pruned['id'][:8]}"
                print(msg)
                return

            memory_id = str(uuid.uuid4())
            session.run("""
                CREATE (m:Memory {memory_id: $memory_id, type: $type, content: $content,
                    machine_name: $machine_name, hit_count: 1, relevance_score: 0.0,
                    source_file: $source_file, last_accessed_at: datetime(),
                    created_at: datetime(), updated_at: datetime()})
            """, memory_id=memory_id, type=memory_type, content=content,
                machine_name=machine_name, source_file=source_file)

            session.run("""
                MERGE (p:Project {path: $path}) SET p.name = $name, p.last_seen = datetime()
                WITH p
                MATCH (m:Memory {memory_id: $memory_id})
                MERGE (m)-[:IN_PROJECT]->(p)
            """, path=cwd, name=project_name, memory_id=memory_id)

            session.run("""
                MERGE (s:Session {session_id: $session_id})
                SET s.last_active = datetime(), s.machine_name = $machine_name
                WITH s
                MATCH (m:Memory {memory_id: $memory_id})
                MERGE (m)-[:IN_SESSION]->(s)
                WITH s
                MATCH (p:Project {path: $path})
                MERGE (s)-[:IN_PROJECT]->(p)
            """, session_id=session_id, machine_name=machine_name, memory_id=memory_id, path=cwd)

            for topic in topics:
                if topic == jejak.CATCHALL_TOPIC:  # skip hot catch-all node
                    continue
                session.run("""
                    MERGE (t:Topic {name: $topic})
                    WITH t
                    MATCH (m:Memory {memory_id: $memory_id})
                    MERGE (m)-[:ABOUT]->(t)
                """, topic=topic, memory_id=memory_id)

            # Link only to non-prompt memories on REAL (non-general) topics
            session.run("""
                MATCH (m:Memory {memory_id: $memory_id})-[:ABOUT]->(t:Topic)<-[:ABOUT]-(other:Memory)
                WHERE other.memory_id <> $memory_id
                  AND other.type <> 'prompt'
                  AND t.name <> $catchall
                  AND NOT exists((m)-[:RELATES_TO]-(other))
                WITH m, other, count(t) AS shared_topics
                WHERE shared_topics >= 1
                MERGE (m)-[:RELATES_TO {strength: shared_topics}]->(other)
            """, memory_id=memory_id, catchall=jejak.CATCHALL_TOPIC)

            update_relevance_score(session, memory_id)
            score_result = session.run(
                "MATCH (m:Memory {memory_id: $mid}) RETURN m.relevance_score AS score", mid=memory_id
            ).single()
            score = score_result["score"] if score_result else 0
            pruned = jejak.mark_superseded(session, supersedes, memory_id) if supersedes else None

        msg = f"Saved [{memory_type}] id:{memory_id[:8]} score:{score:.0f} topics:{topics} project:{project_name}"
        if pruned:
            msg += f" SUPERSEDES:{pruned['id'][:8]}"
        print(msg)
    finally:
        driver.close()

def cmd_map(args):
    cwd = args.directory or os.getcwd()
    project_name = extract_project_name(cwd)
    driver = get_driver()

    try:
        with driver.session() as session:
            stats = session.run("""
                MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {path: $path})
                OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                OPTIONAL MATCH (s:Session)-[:IN_PROJECT]->(p)
                RETURN count(DISTINCT m) AS memories, count(DISTINCT t) AS topics,
                       count(DISTINCT s) AS sessions
            """, path=cwd).single()

            if stats["memories"] == 0:
                print(f"No knowledge found for {project_name} ({cwd})")
                return

            print(f"=== Knowledge Map: {project_name} ===")
            print(f"    Path: {cwd}")
            print(f"    Memories: {stats['memories']} | Topics: {stats['topics']} | Sessions: {stats['sessions']}")
            print()

            type_groups = session.run("""
                MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {path: $path})
                WHERE coalesce(m.superseded, false) = false
                OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                WITH m, collect(t.name) AS topics
                ORDER BY m.created_at DESC
                RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                       m.created_at AS created_at, topics
            """, path=cwd)

            by_type = {}
            for r in type_groups:
                t = r["type"]
                if t not in by_type:
                    by_type[t] = []
                by_type[t].append(dict(r))

            type_order = ["decision", "architecture", "convention", "learning", "solution", "finding", "issue", "context", "prompt"]
            for t in type_order:
                if t not in by_type:
                    continue
                entries = by_type[t]
                print(f"  [{t.upper()}] ({len(entries)})")
                for e in entries[:10]:
                    preview = e["content"][:120].replace("\n", " ")
                    topic_str = ", ".join(e["topics"][:3]) if e["topics"] else ""
                    ts = str(e["created_at"])[:16] if e["created_at"] else ""
                    print(f"    {e['id'][:8]}  {ts}  [{topic_str}]")
                    print(f"             {preview}")
                if len(entries) > 10:
                    print(f"    ... and {len(entries) - 10} more")
                print()

            links = session.run("""
                MATCH (m1:Memory)-[r:RELATES_TO]->(m2:Memory)
                WHERE EXISTS {
                    MATCH (m1)-[:IN_PROJECT]->(p:Project {path: $path})
                }
                RETURN m1.memory_id AS from_id, left(m1.content, 50) AS from_text,
                       r.strength AS strength,
                       m2.memory_id AS to_id, left(m2.content, 50) AS to_text
                ORDER BY r.strength DESC
                LIMIT 15
            """, path=cwd)
            link_list = [dict(r) for r in links]

            if link_list:
                print("  [CONNECTIONS]")
                for l in link_list:
                    print(f"    {l['from_id'][:8]} --({l['strength']})--> {l['to_id'][:8]}")
                    print(f"      \"{l['from_text']}...\"")
                    print(f"      \"{l['to_text']}...\"")
                print()

    finally:
        driver.close()

def cmd_session(args):
    session_id = args.session_id
    driver = get_driver()

    try:
        with driver.session() as session:
            memories = session.run("""
                MATCH (m:Memory)-[:IN_SESSION]->(s:Session {session_id: $session_id})
                OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
                WITH m, collect(DISTINCT t.name) AS topics, collect(DISTINCT p.name) AS projects
                ORDER BY m.created_at ASC
                RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                       m.created_at AS created_at, topics, projects
            """, session_id=session_id)

            results = [dict(r) for r in memories]
            if not results:
                print(f"No memories found for session {session_id}")
                return

            print(f"=== Session: {session_id} ===")
            print(f"    Memories: {len(results)}")
            print()
            for m in results:
                preview = m["content"][:150].replace("\n", " ")
                ts = str(m["created_at"])[:16] if m["created_at"] else ""
                topics = ", ".join(m["topics"][:4]) if m["topics"] else ""
                projects = ", ".join(m["projects"]) if m["projects"] else ""
                print(f"  {m['id'][:8]}  [{m['type']}]  {ts}")
                print(f"    Topics: {topics}  Project: {projects}")
                print(f"    {preview}")
                print()
    finally:
        driver.close()

def cmd_search(args):
    search_term = " ".join(args.query)
    driver = get_driver()

    try:
        with driver.session() as session:
            results = session.run("""
                MATCH (m:Memory)
                WHERE toLower(m.content) CONTAINS toLower($search_term)
                OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
                WITH m, collect(DISTINCT t.name) AS topics, collect(DISTINCT p.name) AS projects
                ORDER BY m.created_at DESC
                LIMIT 20
                RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                       m.created_at AS created_at, topics, projects
            """, search_term=search_term)

            result_list = [dict(r) for r in results]
            if not result_list:
                print(f"No results for '{search_term}'")
                return

            print(f"=== Search: '{search_term}' ({len(result_list)} results) ===")
            print()
            for m in result_list:
                preview = m["content"][:150].replace("\n", " ")
                ts = str(m["created_at"])[:16] if m["created_at"] else ""
                topics = ", ".join(m["topics"][:3]) if m["topics"] else ""
                projects = ", ".join(m["projects"]) if m["projects"] else ""
                print(f"  {m['id'][:8]}  [{m['type']}]  {ts}  project:{projects}")
                print(f"    [{topics}] {preview}")
                print()
    finally:
        driver.close()

def cmd_topics(args):
    cwd = args.directory or os.getcwd()
    project_name = extract_project_name(cwd)
    driver = get_driver()

    try:
        with driver.session() as session:
            results = session.run("""
                MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {path: $path})
                MATCH (m)-[:ABOUT]->(t:Topic)
                WITH t.name AS topic, count(m) AS count, collect(m.type) AS types
                ORDER BY count DESC
                RETURN topic, count, types
            """, path=cwd)

            result_list = [dict(r) for r in results]
            if not result_list:
                print(f"No topics found for {project_name}")
                return

            print(f"=== Topics: {project_name} ===")
            print()
            for r in result_list:
                type_summary = {}
                for t in r["types"]:
                    type_summary[t] = type_summary.get(t, 0) + 1
                type_str = ", ".join(f"{k}:{v}" for k, v in sorted(type_summary.items(), key=lambda x: -x[1]))
                bar = "#" * min(r["count"], 30)
                print(f"  {r['topic']:15s}  {r['count']:3d}  {bar}  ({type_str})")
    finally:
        driver.close()

def cmd_links(args):
    memory_id_prefix = args.memory_id
    driver = get_driver()
    try:
        with driver.session() as session:
            match = session.run("""
                MATCH (m:Memory)
                WHERE m.memory_id STARTS WITH $prefix
                RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                       m.relevance_score AS score, m.hit_count AS hits
            """, prefix=memory_id_prefix)
            record = match.single()
            if not record:
                print(f"No memory found matching '{memory_id_prefix}'")
                return

            mem = dict(record)
            full_id = mem["id"]
            print(f"=== Links for {full_id[:8]} ===")
            print(f"    Type: {mem['type']}  Score: {mem['score'] or 0:.0f}  Hits: {mem['hits'] or 0}")
            print(f"    {mem['content'][:150]}")
            print()

            topics = session.run("""
                MATCH (m:Memory {memory_id: $id})-[:ABOUT]->(t:Topic)
                RETURN collect(t.name) AS topics
            """, id=full_id).single()
            print(f"  Topics: {', '.join(topics['topics']) if topics['topics'] else 'none'}")

            projects = session.run("""
                MATCH (m:Memory {memory_id: $id})-[:IN_PROJECT]->(p:Project)
                RETURN collect(p.name) AS names, collect(p.path) AS paths
            """, id=full_id).single()
            if projects["names"]:
                for name, path in zip(projects["names"], projects["paths"]):
                    print(f"  Project: {name} ({path})")

            sessions = session.run("""
                MATCH (m:Memory {memory_id: $id})-[:IN_SESSION]->(s:Session)
                RETURN collect(s.session_id) AS sids
            """, id=full_id).single()
            if sessions["sids"]:
                print(f"  Sessions: {', '.join(s[:8] for s in sessions['sids'])}")

            print()
            relates = session.run("""
                MATCH (m:Memory {memory_id: $id})-[r:RELATES_TO]-(other:Memory)
                WHERE other.type <> 'prompt'
                RETURN other.memory_id AS id, other.type AS type, other.content AS content,
                       other.relevance_score AS score, r.strength AS strength
                ORDER BY r.strength DESC, other.relevance_score DESC
            """, id=full_id)
            links = [dict(r) for r in relates]
            if links:
                print(f"  Connections ({len(links)}):")
                for l in links:
                    preview = l["content"][:80].replace("\n", " ")
                    score = l["score"] or 0
                    print(f"    --({l['strength']})--> {l['id'][:8]} [{l['type']}] score:{score:.0f}")
                    print(f"           {preview}")
            else:
                print("  No connections")
    finally:
        driver.close()

def cmd_check_duplicates(args):
    content = " ".join(args.content)
    cwd = args.directory or os.getcwd()
    driver = get_driver()

    try:
        with driver.session() as session:
            results = session.run("""
                MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {path: $path})
                WHERE m.type <> 'prompt' AND coalesce(m.superseded, false) = false
                WITH m ORDER BY m.created_at DESC LIMIT 30
                OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                       collect(t.name) AS topics
            """, path=cwd)

            existing = [dict(r) for r in results]
            if not existing:
                print("NO_EXISTING_KNOWLEDGE")
                return

            print(f"=== Existing knowledge in project ({len(existing)} entries) ===")
            print(f"New content to check: {content}")
            print()
            for e in existing:
                topics = ", ".join(e["topics"][:3]) if e["topics"] else ""
                print(f"  [{e['type']}][{topics}] {e['content']}")
            print()
            print("INSTRUCTIONS: Compare the new content against each existing entry above.")
            print("1. If any existing entry captures the SAME intent/meaning (even with different wording): DUPLICATE")
            print("2. If the new content CONTRADICTS an existing entry (e.g. different values for the same setting,")
            print("   opposite decisions about the same topic): CONFLICT:<memory_id> and explain the contradiction.")
            print("   Ask the user which version is current before saving.")
            print("3. If the new content is genuinely new knowledge: UNIQUE")
    finally:
        driver.close()

def cmd_rank(args):
    cwd = args.directory or os.getcwd()
    show_all = args.all
    driver = get_driver()
    try:
        with driver.session() as session:
            jejak.recalculate_all_scores(session)  # canonical formula, with decay

            if show_all:
                query = """
                    MATCH (m:Memory) WHERE m.type <> 'prompt' AND coalesce(m.superseded, false) = false
                    OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                    OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
                    OPTIONAL MATCH (m)-[:IN_SESSION]->(s:Session)
                    OPTIONAL MATCH (m)-[:RELATES_TO]-(other:Memory)
                    WITH m, collect(DISTINCT t.name) AS topics, collect(DISTINCT p.name) AS projects,
                         count(DISTINCT s) AS sessions, count(DISTINCT other) AS connections
                    ORDER BY m.relevance_score DESC
                    RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                           m.relevance_score AS score, m.hit_count AS hits,
                           sessions, connections, topics, projects
                """
                results = session.run(query)
            else:
                query = """
                    MATCH (m:Memory)-[:IN_PROJECT]->(proj:Project {path: $path})
                    WHERE m.type <> 'prompt' AND coalesce(m.superseded, false) = false
                    OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                    OPTIONAL MATCH (m)-[:IN_SESSION]->(s:Session)
                    OPTIONAL MATCH (m)-[:RELATES_TO]-(other:Memory)
                    WITH m, collect(DISTINCT t.name) AS topics,
                         count(DISTINCT s) AS sessions, count(DISTINCT other) AS connections
                    ORDER BY m.relevance_score DESC
                    RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                           m.relevance_score AS score, m.hit_count AS hits,
                           sessions, connections, topics
                """
                results = session.run(query, path=cwd)

            items = [dict(r) for r in results]

            if not items:
                scope = "all projects" if show_all else extract_project_name(cwd)
                print(f"No scored knowledge found for {scope}")
                return

            max_score = max((i["score"] or 0) for i in items) if items else 1
            if max_score == 0:
                max_score = 1

            scope_label = "ALL PROJECTS" if show_all else extract_project_name(cwd)
            print(f"=== Knowledge Ranking: {scope_label} ({len(items)} entries) ===")
            print(f"    Score formula: (hits*3 + sessions*5 + projects*10 + connections*2) * type_weight")
            print()

            for i, item in enumerate(items):
                score = item["score"] or 0
                normalized = int((score / max_score) * 100) if max_score > 0 else 0
                bar_len = int(normalized / 5)
                bar = "#" * bar_len
                preview = item["content"][:90].replace("\n", " ")
                topic_str = ", ".join(item["topics"][:3]) if item["topics"] else ""
                hits = item["hits"] or 0
                sessions = item["sessions"] or 0
                connections = item["connections"] or 0

                depth_label = ""
                if i < 5:
                    depth_label = " [L1:global]"
                elif i < 15:
                    depth_label = " [L2:project]"
                else:
                    depth_label = " [L3:deep]"

                print(f"  {i+1:3d}. {item['id'][:8]}  score:{score:6.1f}  norm:{normalized:3d}%  {bar}{depth_label}")
                print(f"       [{item['type']}][{topic_str}] hits:{hits} sess:{sessions} conn:{connections}")
                print(f"       {preview}")
                if i < len(items) - 1:
                    print()

            print()
            print("  Loading depth:")
            print("    L1 (global)  = top 5 by score — loaded in EVERY session")
            print("    L2 (project) = next 10 from current project — loaded on SessionStart")
            print("    L3 (deep)    = remaining — available via /jejak search")
            print("    L4 (code)    = not in graph — fall back to codebase search")
    finally:
        driver.close()

def cmd_export(args):
    cwd = args.directory or os.getcwd()
    show_all = args.all
    driver = get_driver()
    try:
        with driver.session() as session:
            if show_all:
                projects = session.run("""
                    MATCH (p:Project)
                    RETURN p.name AS name, p.path AS path
                    ORDER BY p.name
                """)
            else:
                projects = session.run("""
                    MATCH (p:Project {path: $path})
                    RETURN p.name AS name, p.path AS path
                """, path=cwd)

            project_list = [dict(r) for r in projects]
            if not project_list:
                print(f"No projects found")
                return

            output = []
            output.append("# Jejak Export")
            output.append(f"# Generated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}")
            output.append("")

            for proj in project_list:
                memories = session.run("""
                    MATCH (m:Memory)-[:IN_PROJECT]->(p:Project {path: $path})
                    WHERE m.type <> 'prompt' AND m.type <> 'session_summary'
                      AND coalesce(m.superseded, false) = false
                    OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                    WITH m, collect(DISTINCT t.name) AS topics
                    ORDER BY m.relevance_score DESC
                    RETURN m.type AS type, m.content AS content,
                           m.relevance_score AS score, m.hit_count AS hits,
                           m.source_file AS source_file, topics
                """, path=proj["path"])
                items = [dict(r) for r in memories]
                if not items:
                    continue

                output.append(f"## {proj['name']}")
                output.append(f"Path: `{proj['path']}`")
                output.append("")

                by_type = {}
                for item in items:
                    t = item["type"]
                    if t not in by_type:
                        by_type[t] = []
                    by_type[t].append(item)

                type_order = ["architecture", "decision", "convention", "learning", "solution", "finding", "issue", "context"]
                for t in type_order:
                    if t not in by_type:
                        continue
                    output.append(f"### {t.title()}s")
                    for item in by_type[t]:
                        score = item["score"] or 0
                        topic_str = ", ".join(item["topics"][:4]) if item["topics"] else ""
                        output.append(f"- **[score:{score:.0f}]** {item['content']}")
                        if topic_str:
                            output.append(f"  Topics: {topic_str}")
                        if item.get("source_file"):
                            output.append(f"  File: `{item['source_file']}`")
                    output.append("")

            print("\n".join(output))
    finally:
        driver.close()

def cmd_ask(args):
    question = " ".join(args.question)
    cwd = args.directory or os.getcwd()
    driver = get_driver()
    try:
        with driver.session() as session:
            keywords = [w.lower() for w in question.split() if len(w) > 3]

            results = session.run("""
                MATCH (m:Memory)
                WHERE m.type <> 'prompt' AND m.type <> 'session_summary'
                  AND coalesce(m.superseded, false) = false
                  AND any(kw IN $keywords WHERE toLower(m.content) CONTAINS kw)
                OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
                OPTIONAL MATCH (m)-[:RELATES_TO]-(related:Memory)
                WHERE related.type <> 'prompt'
                WITH m, collect(DISTINCT t.name) AS topics,
                     collect(DISTINCT p.name) AS projects,
                     collect(DISTINCT {type: related.type, content: left(related.content, 100)}) AS related_items
                ORDER BY m.relevance_score DESC
                LIMIT 8
                RETURN m.memory_id AS id, m.type AS type, m.content AS content,
                       m.relevance_score AS score, m.source_file AS source_file,
                       topics, projects, related_items
            """, keywords=keywords)

            items = [dict(r) for r in results]
            if not items:
                print(f"No knowledge found for: {question}")
                print("Try /jejak search with different terms, or explore the codebase directly.")
                return

            print(f"=== Answer from Jejak ===")
            print(f"Question: {question}")
            print()

            for i, item in enumerate(items):
                score = item["score"] or 0
                topic_str = ", ".join(item["topics"][:3]) if item["topics"] else ""
                proj_str = ", ".join(item["projects"][:2]) if item["projects"] else ""
                print(f"  {i+1}. [{item['type']}] score:{score:.0f} project:{proj_str}")
                print(f"     {item['content']}")
                if item.get("source_file"):
                    print(f"     File: {item['source_file']}")
                if topic_str:
                    print(f"     Topics: {topic_str}")

                related = [r for r in (item.get("related_items") or []) if r.get("content")]
                if related:
                    print(f"     Related:")
                    for r in related[:3]:
                        print(f"       - [{r['type']}] {r['content']}")
                print()

            print("INSTRUCTIONS: Use the knowledge above to answer the user's question.")
            print("If the answer is incomplete, suggest checking /jejak search or exploring the codebase.")
    finally:
        driver.close()

def cmd_stats(args):
    driver = get_driver()
    try:
        with driver.session() as session:
            result = session.run("""
                MATCH (m:Memory) WITH count(m) AS memories
                MATCH (p:Project) WITH memories, count(p) AS projects
                MATCH (s:Session) WITH memories, projects, count(s) AS sessions
                MATCH (t:Topic) WITH memories, projects, sessions, count(t) AS topics
                RETURN memories, projects, sessions, topics
            """).single()

            print("=== Jejak Stats ===")
            print(f"  Memories:  {result['memories']}")
            print(f"  Projects:  {result['projects']}")
            print(f"  Sessions:  {result['sessions']}")
            print(f"  Topics:    {result['topics']}")
            print()

            projects = session.run("""
                MATCH (m:Memory)-[:IN_PROJECT]->(p:Project)
                WITH p, count(m) AS mem_count
                ORDER BY mem_count DESC
                RETURN p.name AS name, p.path AS path, mem_count
            """)
            print("  Projects:")
            for p in projects:
                print(f"    {p['name']:25s}  {p['mem_count']:4d} memories  ({p['path']})")

            print()
            machines = session.run("""
                MATCH (m:Memory)
                WITH m.machine_name AS machine, count(m) AS count
                ORDER BY count DESC
                RETURN machine, count
            """)
            print("  Machines:")
            for m in machines:
                print(f"    {m['machine']:25s}  {m['count']:4d} memories")
    finally:
        driver.close()

def main():
    parser = argparse.ArgumentParser(description="Jejak CLI")
    subparsers = parser.add_subparsers(dest="command")

    save_p = subparsers.add_parser("save", help="Save a knowledge entry")
    save_p.add_argument("type", choices=VALID_TYPES)
    save_p.add_argument("content", nargs="+")
    save_p.add_argument("-d", "--directory", help="Project directory (default: cwd)")
    save_p.add_argument("-s", "--session", help="Session ID (default: from env or 'manual')")
    save_p.add_argument("-f", "--file", help="Source file path related to this knowledge")
    save_p.add_argument("--supersedes", help="8-char id of an existing memory this one replaces (conflict prune)")

    map_p = subparsers.add_parser("map", help="Show knowledge map for a directory")
    map_p.add_argument("directory", nargs="?", help="Directory (default: cwd)")

    sess_p = subparsers.add_parser("session", help="Show all knowledge from a session")
    sess_p.add_argument("session_id")

    search_p = subparsers.add_parser("search", help="Search across all knowledge")
    search_p.add_argument("query", nargs="+")

    topics_p = subparsers.add_parser("topics", help="Show topic clusters")
    topics_p.add_argument("directory", nargs="?")

    subparsers.add_parser("stats", help="Show global stats")

    rank_p = subparsers.add_parser("rank", help="Show knowledge ranked by relevance score")
    rank_p.add_argument("directory", nargs="?", help="Project directory (default: cwd)")
    rank_p.add_argument("-a", "--all", action="store_true", help="Show all projects, not just current")

    links_p = subparsers.add_parser("links", help="Show all connections for a memory")
    links_p.add_argument("memory_id", help="Memory ID (full or prefix)")

    dup_p = subparsers.add_parser("check-duplicates", help="Check for duplicates and conflicts")
    dup_p.add_argument("content", nargs="+")
    dup_p.add_argument("-d", "--directory", help="Project directory (default: cwd)")

    export_p = subparsers.add_parser("export", help="Export knowledge as Markdown")
    export_p.add_argument("directory", nargs="?", help="Project directory (default: cwd)")
    export_p.add_argument("-a", "--all", action="store_true", help="Export all projects")

    ask_p = subparsers.add_parser("ask", help="Answer a question from Jejak")
    ask_p.add_argument("question", nargs="+")
    ask_p.add_argument("-d", "--directory", help="Project directory (default: cwd)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "save": cmd_save,
        "map": cmd_map,
        "session": cmd_session,
        "search": cmd_search,
        "topics": cmd_topics,
        "stats": cmd_stats,
        "rank": cmd_rank,
        "links": cmd_links,
        "check-duplicates": cmd_check_duplicates,
        "export": cmd_export,
        "ask": cmd_ask,
    }
    commands[args.command](args)

main()

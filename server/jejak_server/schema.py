"""
Schema and one-time migration of a pre-server (single-user) Jejak graph.

The migration is additive and idempotent:
  1. every Memory, Project and Session without a space gets the owner's
     personal space;
  2. every Project gets a `key` (normalized git remote, else directory name),
     and Projects that turn out to be the same repo are merged into one
     (their links move; no Memory is touched or removed);
  3. the per-path and per-session-id uniqueness rules are replaced by
     per-space ones.
"""
import jejak_common as jejak

STATEMENTS = [
    "CREATE CONSTRAINT unique_memory_id IF NOT EXISTS FOR (m:Memory) REQUIRE m.memory_id IS UNIQUE",
    "CREATE CONSTRAINT unique_topic_name IF NOT EXISTS FOR (t:Topic) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT unique_user_id IF NOT EXISTS FOR (u:User) REQUIRE u.user_id IS UNIQUE",
    "CREATE CONSTRAINT unique_username IF NOT EXISTS FOR (u:User) REQUIRE u.username IS UNIQUE",
    "CREATE CONSTRAINT unique_token_hash IF NOT EXISTS FOR (t:Token) REQUIRE t.hash IS UNIQUE",
    "CREATE CONSTRAINT unique_token_id IF NOT EXISTS FOR (t:Token) REQUIRE t.token_id IS UNIQUE",
    "CREATE CONSTRAINT unique_invite_hash IF NOT EXISTS FOR (i:Invite) REQUIRE i.hash IS UNIQUE",
    "CREATE CONSTRAINT unique_space_id IF NOT EXISTS FOR (s:Space) REQUIRE s.space_id IS UNIQUE",
    "CREATE CONSTRAINT unique_space_slug IF NOT EXISTS FOR (s:Space) REQUIRE s.slug IS UNIQUE",
    "CREATE INDEX memory_space_idx IF NOT EXISTS FOR (m:Memory) ON (m.space_id)",
    "CREATE INDEX memory_type_idx IF NOT EXISTS FOR (m:Memory) ON (m.type)",
    "CREATE INDEX memory_score_idx IF NOT EXISTS FOR (m:Memory) ON (m.relevance_score)",
    "CREATE INDEX memory_created_idx IF NOT EXISTS FOR (m:Memory) ON (m.created_at)",
    "CREATE INDEX memory_last_accessed_idx IF NOT EXISTS FOR (m:Memory) ON (m.last_accessed_at)",
    "CREATE INDEX project_key_idx IF NOT EXISTS FOR (p:Project) ON (p.key)",
    "CREATE INDEX project_name_idx IF NOT EXISTS FOR (p:Project) ON (p.name)",
    "CREATE INDEX session_id_idx IF NOT EXISTS FOR (s:Session) ON (s.session_id)",
]

# Needs every Project/Session to have a space first (see migrate).
SCOPED_UNIQUES = [
    "CREATE CONSTRAINT unique_project_in_space IF NOT EXISTS FOR (p:Project) REQUIRE (p.space_id, p.key) IS UNIQUE",
    "CREATE CONSTRAINT unique_session_in_space IF NOT EXISTS FOR (s:Session) REQUIRE (s.space_id, s.session_id) IS UNIQUE",
]

# Pre-server rules that are wrong once two spaces may share a path or session id.
OLD_UNIQUES = ("unique_project_path", "unique_session_id")


def apply(session):
    for name in OLD_UNIQUES:
        session.run(f"DROP CONSTRAINT {name} IF EXISTS")
    for stmt in STATEMENTS:
        session.run(stmt)
    unscoped = session.run("""
        OPTIONAL MATCH (p:Project) WHERE p.space_id IS NULL OR p.key IS NULL WITH count(p) AS a
        OPTIONAL MATCH (s:Session) WHERE s.space_id IS NULL WITH a, count(s) AS b RETURN a + b AS n
    """).single()["n"]
    if unscoped == 0:
        for stmt in SCOPED_UNIQUES:
            session.run(stmt)
    return unscoped


def counts(session):
    return dict(session.run("""
        OPTIONAL MATCH (m:Memory) WITH count(m) AS memories
        OPTIONAL MATCH ()-[r:RELATES_TO]->() WITH memories, count(r) AS links
        OPTIONAL MATCH (p:Project) WITH memories, links, count(p) AS projects
        OPTIONAL MATCH (s:Session) RETURN memories, links, projects, count(s) AS sessions
    """).single())


def migrate(driver, space_id, log=print, identity=jejak.project_identity):
    """Put a pre-server graph into `space_id`. Safe to re-run."""
    with driver.session() as s:
        before = counts(s)
        log(f"Before: {before}")
        n = s.run("""
            MATCH (m:Memory) WHERE m.space_id IS NULL
            CALL (m) { SET m.space_id = $sid } IN TRANSACTIONS OF 5000 ROWS
            RETURN count(m) AS n
        """, sid=space_id).single()["n"]
        log(f"Memories given a space: {n}")
        n = s.run("""
            MATCH (x:Session) WHERE x.space_id IS NULL SET x.space_id = $sid RETURN count(x) AS n
        """, sid=space_id).single()["n"]
        log(f"Sessions given a space: {n}")

        projects = [dict(r) for r in s.run("""
            MATCH (p:Project) WHERE p.space_id IS NULL OR p.key IS NULL
            RETURN elementId(p) AS eid, p.path AS path, p.name AS name
        """)]
        for p in projects:
            key, name = identity(p["path"]) if p["path"] else (p["name"], p["name"])
            s.run("""
                MATCH (p:Project) WHERE elementId(p) = $eid
                SET p.space_id = coalesce(p.space_id, $sid), p.key = $key,
                    p.name = coalesce(p.name, $name)
            """, eid=p["eid"], sid=space_id, key=key, name=name)
        log(f"Projects given a key: {len(projects)}")

        merged = 0
        groups = s.run("""
            MATCH (p:Project) WITH p.space_id AS sid, p.key AS key, collect(elementId(p)) AS ids
            WHERE size(ids) > 1 RETURN sid, key, ids
        """).data()
        for g in groups:
            keep, dups = g["ids"][0], g["ids"][1:]
            for d in dups:
                s.run("""
                    MATCH (keep:Project) WHERE elementId(keep) = $keep
                    MATCH (dup:Project) WHERE elementId(dup) = $dup
                    OPTIONAL MATCH (x)-[r:IN_PROJECT]->(dup)
                    WITH keep, dup, collect(x) AS xs, collect(r) AS rs
                    FOREACH (x IN xs | MERGE (x)-[:IN_PROJECT]->(keep))
                    FOREACH (r IN rs | DELETE r)
                    WITH dup DETACH DELETE dup
                """, keep=keep, dup=d)
                merged += 1
        log(f"Duplicate projects merged: {merged}")
        left = apply(s)
        after = counts(s)
        log(f"After: {after}")
        if left:
            log(f"!! {left} projects or sessions still have no space")
        if after["memories"] != before["memories"] or after["links"] != before["links"]:
            raise RuntimeError("Memory or link count changed during migration; investigate.")
        return before, after

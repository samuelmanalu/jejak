// Jejak — Neo4j schema (idempotent; safe to re-run)
//
// Graph shape:
//   (Memory)--[:ABOUT]->(Topic)
//   (Memory)--[:IN_PROJECT]->(Project)
//   (Memory)--[:IN_SESSION]->(Session)
//   (Memory)--[:RELATES_TO {strength}]->(Memory)
//   (Session)--[:IN_PROJECT]->(Project)

CREATE CONSTRAINT unique_memory_id  IF NOT EXISTS FOR (m:Memory)  REQUIRE m.memory_id  IS UNIQUE;
CREATE CONSTRAINT unique_project_path IF NOT EXISTS FOR (p:Project) REQUIRE p.path      IS UNIQUE;
CREATE CONSTRAINT unique_session_id IF NOT EXISTS FOR (s:Session)  REQUIRE s.session_id IS UNIQUE;
CREATE CONSTRAINT unique_topic_name IF NOT EXISTS FOR (t:Topic)    REQUIRE t.name       IS UNIQUE;

CREATE INDEX memory_type_idx          IF NOT EXISTS FOR (m:Memory) ON (m.type);
CREATE INDEX memory_score_idx         IF NOT EXISTS FOR (m:Memory) ON (m.relevance_score);
CREATE INDEX memory_created_idx       IF NOT EXISTS FOR (m:Memory) ON (m.created_at);
CREATE INDEX memory_last_accessed_idx IF NOT EXISTS FOR (m:Memory) ON (m.last_accessed_at);
CREATE INDEX memory_level_idx         IF NOT EXISTS FOR (m:Memory) ON (m.level);

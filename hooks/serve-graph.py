#!/usr/bin/env python3
"""
Serve ONLY the jejak.html file, bound to loopback (127.0.0.1).

The old approach (`python3 -m http.server` in ~/.claude/hooks) exposed the whole
directory — including db-config.json with plaintext DB passwords — on all network
interfaces. This server:
  - binds to 127.0.0.1 only (not reachable from other machines)
  - serves exactly one file, ignoring every other path

The view is READ-ONLY and trims only what the page receives, never the graph:
every active memory is sent, but each one brings only its strongest
EDGES_PER_NODE links (by shared-topic strength, then the neighbour's score).
Topic linking makes the stored graph nearly complete (1.9M links for 6k
memories), which no browser can draw. All links stay in Neo4j for ranking,
`links` and `ask`.
"""
import http.server
import json
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))

HOST = "127.0.0.1"
PORT = 8787
HTML_PATH = os.path.expanduser("~/.claude/hooks/jejak.html")
EDGES_PER_NODE = int(os.environ.get("JEJAK_GRAPH_EDGES_PER_NODE", "10"))

# The page polls every 10 s and the edge query takes seconds, so the graph is
# rebuilt only when this cheap fingerprint of the knowledge changes.
SIGNATURE_QUERY = """
    MATCH (m:Memory)
    RETURN count(m) AS n, toString(max(m.updated_at)) AS updated,
           sum(coalesce(m.hit_count, 1)) AS hits,
           round(sum(coalesce(m.relevance_score, 0.0))) AS score,
           sum(CASE WHEN coalesce(m.superseded, false) THEN 1 ELSE 0 END) AS inactive
"""

EDGES_QUERY = """
    MATCH (a:Memory)-[r:RELATES_TO]-(b:Memory)
    WHERE coalesce(a.superseded, false) = false AND coalesce(b.superseded, false) = false
    WITH a, b, coalesce(r.strength, 1) AS strength
    ORDER BY strength DESC, coalesce(b.relevance_score, 0.0) DESC
    WITH a, collect({target: left(b.memory_id, 8), strength: strength})[..$k] AS top
    UNWIND top AS t
    RETURN left(a.memory_id, 8) AS source, t.target AS target, t.strength AS strength
"""

_cache = {"signature": None, "data": None}


def live_graph():
    """Current nodes + trimmed edges as a JSON-able dict (cached until the knowledge changes)."""
    import jejak_common as jejak
    from neo4j import GraphDatabase
    cfg = jejak.load_config("neo4j")
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        with driver.session() as s:
            signature = tuple(s.run(SIGNATURE_QUERY).single().values())
            if signature == _cache["signature"]:
                return _cache["data"]
            nodes = [dict(r) for r in s.run("""
                MATCH (m:Memory)
                WHERE coalesce(m.superseded, false) = false
                OPTIONAL MATCH (m)-[:ABOUT]->(t:Topic)
                OPTIONAL MATCH (m)-[:IN_PROJECT]->(p:Project)
                OPTIONAL MATCH (m)-[:IN_SESSION]->(sess:Session)
                WITH m, collect(DISTINCT t.name) AS topics,
                     collect(DISTINCT p.name) AS projects,
                     collect(DISTINCT sess.session_id) AS sessions
                RETURN left(m.memory_id, 8) AS id, m.type AS type, m.content AS content,
                       coalesce(m.hit_count, 1) AS hits,
                       coalesce(m.relevance_score, 0.0) AS score,
                       topics, projects,
                       [x IN sessions | left(x, 8)] AS sessions
            """)]
            # Each memory's top-k, merged: a link chosen from both ends is sent once.
            strongest = {}
            for r in s.run(EDGES_QUERY, k=EDGES_PER_NODE):
                pair = (r["source"], r["target"]) if r["source"] < r["target"] else (r["target"], r["source"])
                strongest[pair] = max(strongest.get(pair, 0), r["strength"])
            edges = [{"source": a, "target": b, "strength": st} for (a, b), st in strongest.items()]
        data = {"nodes": nodes, "edges": edges}
        _cache["signature"], _cache["data"] = signature, data
        return data
    finally:
        driver.close()


class SingleFileHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/data.json":
            try:
                body = json.dumps(live_graph()).encode("utf-8")
            except Exception as e:
                self.send_error(500, f"Graph query failed: {e}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path not in ("/", "/jejak.html"):
            self.send_error(404, "Not found")
            return
        try:
            with open(HTML_PATH, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(500, "Graph file unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # quiet


if __name__ == "__main__":
    server = http.server.HTTPServer((HOST, PORT), SingleFileHandler)
    print(f"Jejak served at http://{HOST}:{PORT}/  (loopback only)")
    server.serve_forever()

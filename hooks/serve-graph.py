#!/usr/bin/env python3
"""
Serve ONLY the jejak.html file, bound to loopback (127.0.0.1).

The old approach (`python3 -m http.server` in ~/.claude/hooks) exposed the whole
directory — including db-config.json with plaintext DB passwords — on all network
interfaces. This server:
  - binds to 127.0.0.1 only (not reachable from other machines)
  - serves exactly one file, ignoring every other path
"""
import http.server
import json
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))

HOST = "127.0.0.1"
PORT = 8787
HTML_PATH = os.path.expanduser("~/.claude/hooks/jejak.html")


def live_graph():
    """Query Neo4j for the current nodes + edges. Returns a JSON-able dict."""
    import jejak_common as jejak
    from neo4j import GraphDatabase
    cfg = jejak.load_config("neo4j")
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    try:
        with driver.session() as s:
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
            edges = [dict(r) for r in s.run("""
                MATCH (a:Memory)-[r:RELATES_TO]->(b:Memory)
                RETURN left(a.memory_id, 8) AS source, left(b.memory_id, 8) AS target,
                       coalesce(r.strength, 1) AS strength
            """)]
        return {"nodes": nodes, "edges": edges}
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

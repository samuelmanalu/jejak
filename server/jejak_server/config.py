"""
Server settings: environment variables first, then a JSON file
(JEJAK_SERVER_CONFIG, default ~/.jejak/server.json, chmod 600).

    JEJAK_NEO4J_URI / _USER / _PASSWORD   the private Neo4j behind the server
    JEJAK_HOST, JEJAK_PORT                where the server listens (127.0.0.1:7700)
    JEJAK_ACCEPT_PROMPTS                  "1" lets clients on THIS machine store raw
                                          prompts (a personal localhost server). Off
                                          by default; prompts never cross the network.
    JEJAK_EDGES_PER_NODE                  graph view link cap per memory (10)
"""
import json
import os
from functools import lru_cache

from neo4j import GraphDatabase

DEFAULTS = {
    "neo4j_uri": "bolt://127.0.0.1:7687",
    "neo4j_user": "neo4j",
    "neo4j_password": None,
    "host": "127.0.0.1",
    "port": 7700,
    "accept_prompts": False,
    "edges_per_node": 10,
}


def _truthy(v):
    return str(v).lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def settings():
    s = dict(DEFAULTS)
    path = os.path.expanduser(os.environ.get("JEJAK_SERVER_CONFIG", "~/.jejak/server.json"))
    if os.path.exists(path):
        with open(path) as f:
            s.update(json.load(f))
    for key in DEFAULTS:
        env = os.environ.get("JEJAK_" + key.upper())
        if env is not None:
            s[key] = env
    s["port"] = int(s["port"])
    s["edges_per_node"] = int(s["edges_per_node"])
    s["accept_prompts"] = _truthy(s["accept_prompts"])
    if not s["neo4j_password"]:
        raise SystemExit("Set JEJAK_NEO4J_PASSWORD (or neo4j_password in ~/.jejak/server.json).")
    return s


@lru_cache(maxsize=1)
def driver():
    s = settings()
    return GraphDatabase.driver(s["neo4j_uri"], auth=(s["neo4j_user"], s["neo4j_password"]),
                                notifications_min_severity="OFF")

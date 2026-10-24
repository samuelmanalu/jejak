"""
Server settings: environment variables first, then a JSON file
(JEJAK_SERVER_CONFIG, default ~/.jejak/server.json, chmod 600).

    JEJAK_NEO4J_URI / _USER / _PASSWORD   the private Neo4j behind the server
    JEJAK_HOST, JEJAK_PORT                where the server listens (127.0.0.1:7700)
    JEJAK_ACCEPT_PROMPTS                  "1" lets clients on THIS machine store raw
                                          prompts (a personal localhost server). Off
                                          by default; prompts never cross the network.
    JEJAK_EDGES_PER_NODE                  graph view link cap per memory (10)
    JEJAK_PUBLIC_URL                      the server's address as clients see it
                                          (https://jejak.example.com; default http://127.0.0.1:PORT)
    JEJAK_OAUTH_REDIRECT_ALLOWLIST        extra https callbacks for hosted MCP clients
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
    # The address people and agents use to reach this server. OAuth issuer and
    # token audience are fixed to it, never taken from a request's Host header.
    "public_url": None,
    # https callbacks of hosted MCP clients that may receive sign-in codes
    # (comma-separated in JEJAK_OAUTH_REDIRECT_ALLOWLIST). Loopback and app
    # links (app-scheme://) are always allowed; any other https is refused.
    "oauth_redirect_allowlist": ["https://claude.ai/api/mcp/auth_callback",
                                 "https://claude.com/api/mcp/auth_callback"],
    # App-link schemes without a dot that may receive codes, each limited to
    # the app's own callback host (reverse-DNS schemes like com.example.app
    # always may). Env: "scheme=host,scheme=host".
    "oauth_app_schemes": {"cursor": "anysphere.cursor-retrieval"},
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
    s["public_url"] = (s["public_url"] or f"http://127.0.0.1:{s['port']}").rstrip("/")
    if isinstance(s["oauth_app_schemes"], str):
        s["oauth_app_schemes"] = dict(x.strip().lower().split("=", 1) for x in s["oauth_app_schemes"].split(",")
                                      if "=" in x)
    if isinstance(s["oauth_redirect_allowlist"], str):
        s["oauth_redirect_allowlist"] = [u.strip() for u in s["oauth_redirect_allowlist"].split(",") if u.strip()]
    if not s["neo4j_password"]:
        raise SystemExit("Set JEJAK_NEO4J_PASSWORD (or neo4j_password in ~/.jejak/server.json).")
    return s


@lru_cache(maxsize=1)
def driver():
    s = settings()
    return GraphDatabase.driver(s["neo4j_uri"], auth=(s["neo4j_user"], s["neo4j_password"]),
                                notifications_min_severity="OFF")

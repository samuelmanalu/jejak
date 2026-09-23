#!/usr/bin/env python3
"""
Jejak schema bootstrapper — applies schema/neo4j.cypher and schema/mysql.sql
using the credentials already in db-config.json, so no shell clients are needed.

Idempotent: every statement is CREATE ... IF NOT EXISTS.
"""
import json
import os
import re
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
CFG = os.path.join(DEST, "hooks", "db-config.json")


def load(section):
    with open(CFG) as f:
        return json.load(f)[section]


def statements(path):
    """Split a .cypher/.sql file into statements, dropping comments."""
    raw = open(path).read()
    raw = re.sub(r"^\s*(--|//).*$", "", raw, flags=re.MULTILINE)
    return [s.strip() for s in raw.split(";") if s.strip()]


def apply_neo4j():
    from neo4j import GraphDatabase
    cfg = load("neo4j")
    driver = GraphDatabase.driver(cfg["uri"], auth=(cfg["user"], cfg["password"]))
    n = 0
    try:
        with driver.session() as s:
            for stmt in statements(os.path.join(SRC, "schema", "neo4j.cypher")):
                s.run(stmt)
                n += 1
    finally:
        driver.close()
    print(f"    Neo4j: applied {n} statement(s)")


def apply_mysql():
    import mysql.connector
    cfg = load("mysql")
    conn = mysql.connector.connect(
        host=cfg["host"], user=cfg["user"], password=cfg["password"])
    n = 0
    try:
        cur = conn.cursor()
        for stmt in statements(os.path.join(SRC, "schema", "mysql.sql")):
            cur.execute(stmt)
            n += 1
        conn.commit()
        cur.close()
    finally:
        conn.close()
    print(f"    MySQL: applied {n} statement(s)")


def main():
    if not os.path.exists(CFG):
        print(f"    !! {CFG} not found")
        sys.exit(1)
    ok = True
    for name, fn in (("Neo4j", apply_neo4j), ("MySQL", apply_mysql)):
        try:
            fn()
        except Exception as e:
            print(f"    !! {name} schema failed: {e}")
            ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

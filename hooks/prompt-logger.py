#!/usr/bin/env python3
import json
import sys
import os
import re
import mysql.connector

sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
import jejak_common as jejak

TAGS = {
    "git": [r"\bgit\b", r"\bcommit\b", r"\bpush\b", r"\bpull\b", r"\bbranch\b", r"\bmerge\b", r"\brebase\b", r"\bcherry.pick\b", r"\bstash\b", r"\bcheckout\b", r"\bPR\b", r"\bpull request\b"],
    "debug": [r"\bdebug\b", r"\bbug\b", r"\bfix\b", r"\berror\b", r"\bfail\b", r"\bcrash\b", r"\bissue\b", r"\bbroken\b", r"\bnot working\b", r"\btraceback\b", r"\bstack trace\b"],
    "test": [r"\btest\b", r"\bspec\b", r"\bjest\b", r"\bpytest\b", r"\bjunit\b", r"\bcoverage\b", r"\btdd\b", r"\bunit test\b", r"\bintegration test\b", r"\be2e\b"],
    "refactor": [r"\brefactor\b", r"\bclean.?up\b", r"\brestructur\b", r"\breorganiz\b", r"\bsimplif\b", r"\bextract\b", r"\brename\b"],
    "feature": [r"\badd\b", r"\bcreate\b", r"\bimplement\b", r"\bbuild\b", r"\bnew\b", r"\bscaffold\b", r"\bgenerate\b"],
    "review": [r"\breview\b", r"\baudit\b", r"\bcheck\b", r"\banalyze\b", r"\binspect\b", r"\blint\b", r"\bcode.review\b"],
    "docs": [r"\bdoc\b", r"\breadme\b", r"\bcomment\b", r"\bexplain\b", r"\bdocument\b", r"\bwhat does\b", r"\bhow does\b", r"\bwhat is\b"],
    "deploy": [r"\bdeploy\b", r"\brelease\b", r"\bci.cd\b", r"\bpipeline\b", r"\bdocker\b", r"\bkubernetes\b", r"\bk8s\b", r"\bhelm\b"],
    "database": [r"\bdb\b", r"\bdatabase\b", r"\bsql\b", r"\bmigration\b", r"\bschema\b", r"\bquery\b", r"\btable\b", r"\bpostgres\b", r"\bmysql\b", r"\bredis\b", r"\bmongo\b"],
    "api": [r"\bapi\b", r"\bendpoint\b", r"\brest\b", r"\bgraphql\b", r"\bgrpc\b", r"\broute\b", r"\brequest\b", r"\bresponse\b"],
    "config": [r"\bconfig\b", r"\bsetting\b", r"\benv\b", r"\benvironment\b", r"\bsetup\b", r"\binstall\b", r"\binit\b"],
    "security": [r"\bsecur\b", r"\bauth\b", r"\btoken\b", r"\bpassword\b", r"\bcredential\b", r"\bvulnerab\b", r"\bencrypt\b", r"\bssl\b", r"\btls\b"],
    "performance": [r"\bperformance\b", r"\boptimiz\b", r"\bslow\b", r"\bfast\b", r"\blatency\b", r"\bcache\b", r"\bbenchmark\b", r"\bprofile\b"],
    "dependency": [r"\bdependenc\b", r"\bpackage\b", r"\bnpm\b", r"\bpip\b", r"\bgradle\b", r"\bmaven\b", r"\bupgrade\b", r"\bversion\b"],
    "search": [r"\bfind\b", r"\bsearch\b", r"\bgrep\b", r"\bwhere is\b", r"\blocate\b", r"\blook for\b"],
    "chat": [r"\bhey\b", r"\bhi\b", r"\bhello\b", r"\bthanks\b", r"\bthank you\b", r"\bhelp\b", r"\bquestion\b"],
}

def categorize(prompt):
    lower = prompt.lower()
    matched = []
    for tag, patterns in TAGS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                matched.append(tag)
                break
    return matched if matched else ["uncategorized"]

def load_db_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db-config.json")
    with open(config_path) as f:
        return json.load(f)["mysql"]

def main():
    if os.environ.get("JEJAK_EXTRACTING"):  # recursion guard (Stop-hook extraction)
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    prompt = data.get("prompt", "")
    cwd = data.get("cwd", os.getcwd())
    session_id = data.get("session_id", "unknown")

    if not prompt.strip():
        sys.exit(0)

    tags = categorize(prompt)
    prompt = jejak.redact(prompt)  # scrub secrets before storing
    machine_name = jejak.machine_name()

    try:
        db_config = load_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO prompt_logs (session_id, cwd, prompt, tags, machine_name) VALUES (%s, %s, %s, %s, %s)",
            (session_id, cwd, prompt, json.dumps(tags), machine_name)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        jejak.log_error("prompt-logger.py", e)

main()

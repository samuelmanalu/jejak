#!/usr/bin/env python3
"""
Jejak hooks for Claude Code sessions — a client of the Jejak server.

  SessionStart      loads layered context (L1 global, L2 project, L2.5 related)
  UserPromptSubmit  adds the project's most relevant entries for the prompt's topics
  SessionEnd        saves a short session summary

The prompt text itself never leaves this machine: only its detected topics are
sent. Raw prompts are stored only when the server runs on this same machine
and opted in (accept_prompts); a remote or team server never receives them.

Lookups give up after a short timeout and the session carries on without
context, so a slow or unreachable server never blocks Claude Code. Errors go
to ~/.claude/logs/jejak-errors.log.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jejak_client as client  # noqa: E402
import jejak_common as jejak  # noqa: E402

LOOKUP_TIMEOUT = 2


def handle_prompt_submit(data, cfg):
    prompt = data.get("prompt", "")
    if not prompt.strip():
        return
    ctx = client.build_ctx(data.get("cwd"), data.get("session_id"))
    if cfg.get("accepts_prompts") and client.is_loopback_url(cfg.get("server", "")):
        try:
            client.request("POST", "/v1/prompts", {"prompt": prompt, "ctx": ctx},
                           timeout=LOOKUP_TIMEOUT, cfg=cfg)
        except client.ClientError as e:
            jejak.log_error("jejak.py/prompt-save", e)
    topics = jejak.detect_topics(prompt)
    r = client.request("POST", "/v1/context/prompt", {"ctx": ctx, "topics": topics},
                       timeout=LOOKUP_TIMEOUT, cfg=cfg)
    if r.get("text"):
        print(r["text"])


def handle_session_start(data, cfg):
    ctx = client.build_ctx(data.get("cwd"), data.get("session_id"))
    r = client.request("POST", "/v1/context/start", {"ctx": ctx}, timeout=LOOKUP_TIMEOUT + 1, cfg=cfg)
    if r.get("text"):
        print(r["text"])


def handle_session_end(data, cfg):
    if client.outbox_items():
        client.flush_outbox(timeout=2)
    ctx = client.build_ctx(data.get("cwd"), data.get("session_id"))
    client.request("POST", "/v1/session-end", {"ctx": ctx, "session_id": data.get("session_id")},
                   timeout=3, cfg=cfg)


def main():
    # Recursion guard: bail inside the Stop hook's headless extraction call.
    if os.environ.get("JEJAK_EXTRACTING"):
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    cfg = client.load_config()
    if not (cfg.get("server") and cfg.get("token")):
        sys.exit(0)  # not connected yet: `jejak status` tells the user how
    event = data.get("hook_event_name", "UserPromptSubmit")
    try:
        if event == "UserPromptSubmit":
            handle_prompt_submit(data, cfg)
        elif event == "SessionStart":
            handle_session_start(data, cfg)
        elif event == "SessionEnd":
            handle_session_end(data, cfg)
    except Exception as e:  # never break the session
        jejak.log_error(f"jejak.py/{event}", e)


main()

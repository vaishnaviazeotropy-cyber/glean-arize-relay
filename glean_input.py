"""
glean_input.py  —  OPTIONAL, FAIL-SAFE lookup of the user's question from Glean.

WHY THIS EXISTS
  Glean's trace export never sends the user's question as a span attribute — it only
  sends the agent's output. But every span carries a conversation id. This module uses
  that id to ask Glean's Chat API for the conversation and pull the user's question text,
  so the relay can set input.value (making Arize show Input as well as Output).

HARD RULE — THIS MUST NEVER BREAK THE OUTPUT PATH
  Every function here is wrapped so that ANY failure (no token, network error, bad id,
  unexpected shape, timeout) returns None quietly. The relay treats None as "no input
  found" and forwards the span exactly as before. The already-working Output is never
  put at risk by this feature.

VERIFIED AGAINST GLEAN DOCS (Nov 2026)
  Endpoint:  POST {base}/rest/api/v1/getchat
  Auth:      Authorization: Bearer <GLEAN_TOKEN>
  Request:   {"id": "<chatId>"}        (we also send "chatId" for safety; extra keys ignored)
  Response:  { "chat": { "messages": [ { "author": "USER"|"GLEAN_AI",
                                         "fragments": [ {"text": "..."} ] }, ... ] } }
             (messages may also sit at the top level as "messages"; we handle both.)

CONFIG (env vars, all read at call time so they can be toggled without code changes)
  GLEAN_INPUT_LOOKUP   "1" to enable this feature at all (default off = behave as today)
  GLEAN_TOKEN          Glean API token (Bearer)
  GLEAN_INSTANCE       e.g. "qualitykiosk"  -> base https://<instance>-be.glean.com/rest/api/v1
  GLEAN_BASE_URL       optional explicit override of the base URL
  GLEAN_INPUT_TIMEOUT  seconds for the lookup (default 4) — kept short on purpose
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("glean_arize_relay.input")

# small in-process cache: conversation_id -> question text (or None if looked up & empty).
# One chat only needs one lookup even though it produces many spans.
_CACHE: dict[str, str | None] = {}


def enabled() -> bool:
    return os.getenv("GLEAN_INPUT_LOOKUP") == "1"


def _base_url() -> str | None:
    if os.getenv("GLEAN_BASE_URL"):
        return os.getenv("GLEAN_BASE_URL").rstrip("/")
    inst = os.getenv("GLEAN_INSTANCE")
    if inst:
        return f"https://{inst}-be.glean.com/rest/api/v1"
    return None


def question_for_conversation(conversation_id: str | None) -> str | None:
    """Return the user's question for a conversation id, or None on ANY problem.
    Never raises. Safe to call for every span."""
    if not conversation_id or not enabled():
        return None
    if conversation_id in _CACHE:            # already resolved (success or empty)
        return _CACHE[conversation_id]
    try:
        q = _fetch(conversation_id)
    except Exception as e:                   # belt-and-suspenders: never propagate
        log.info("glean input lookup failed for %s: %r", conversation_id, e)
        q = None
    _CACHE[conversation_id] = q
    return q


def _fetch(conversation_id: str) -> str | None:
    token = os.getenv("GLEAN_TOKEN") or ""
    base = _base_url()
    if not token or not base:
        log.info("glean input lookup skipped: GLEAN_TOKEN/instance not set")
        return None
    timeout = float(os.getenv("GLEAN_INPUT_TIMEOUT", "4"))
    url = f"{base}/getchat"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # send both key spellings; Glean ignores unknown keys.
    body = {"id": conversation_id, "chatId": conversation_id}

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=body, headers=headers)
    if resp.status_code != 200:
        log.info("getchat %s -> HTTP %s", conversation_id, resp.status_code)
        return None
    return _extract_first_user_question(resp.json())


def _extract_first_user_question(payload: Any) -> str | None:
    """Pull the FIRST USER message's text from a getchat response.
    Tolerates several shapes: {chat:{messages:[]}}, {messages:[]}, or a list."""
    messages = _find_messages(payload)
    if not messages:
        return None
    # the opening user turn is the question we want to show as Input
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        author = str(msg.get("author") or msg.get("role") or "").upper()
        if author in ("USER", "HUMAN"):
            text = _fragments_text(msg)
            if text:
                return text
    # fallback: if authors aren't labelled, take the very first message's text
    first = messages[0] if isinstance(messages[0], dict) else None
    return _fragments_text(first) if first else None


def _find_messages(payload: Any) -> list | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("messages"), list):
            return payload["messages"]
        chat = payload.get("chat")
        if isinstance(chat, dict) and isinstance(chat.get("messages"), list):
            return chat["messages"]
    return None


def _fragments_text(msg: dict | None) -> str | None:
    if not isinstance(msg, dict):
        return None
    frags = msg.get("fragments")
    if isinstance(frags, list):
        parts = [str(f.get("text")) for f in frags
                 if isinstance(f, dict) and f.get("text")]
        joined = " ".join(p.strip() for p in parts if p).strip()
        if joined:
            return joined
    # some shapes use a plain "content" string
    if isinstance(msg.get("content"), str) and msg["content"].strip():
        return msg["content"].strip()
    return None

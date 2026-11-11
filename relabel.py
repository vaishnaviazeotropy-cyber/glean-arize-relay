"""
relabel.py  —  the BRAIN of the relay. Pure functions, no web server, no network.

WHAT IT DOES (in one line)
  Take the span attributes Glean sends (OpenTelemetry GenAI style, nested + flattened)
  and ADD the OpenInference fields Arize needs to show Input / Output chat text.

WHY THIS EXISTS
  Glean's "Agent trace export" pushes spans where the real question/answer live in
  keys like:
      gen_ai.input.messages.0.role          = "user"
      gen_ai.input.messages.0.parts.0.content= "How do I report a lost debit card?"
      gen_ai.output.messages.0.role          = "assistant"
      gen_ai.output.messages.0.parts.0.type  = "reasoning"      <-- NOT the answer
      gen_ai.output.messages.0.parts.1.type  = "text"           <-- THIS is the answer
      gen_ai.output.messages.0.parts.1.content= "You can report a lost debit card by..."
  Arize auto-maps gen_ai.response.{id,model} -> output.value (metadata, not the text),
  so the chat body never appears. We fix that by writing the REAL text into:
      input.value                 = the user's question
      output.value                = the final assistant answer (skipping 'reasoning')
      openinference.span.kind     = AGENT / LLM / RETRIEVER / CHAIN (so Arize renders it)
      llm.input_messages.*        = structured messages (nice-to-have, Arize reads these too)
      llm.output_messages.*

  We DO NOT delete anything Glean sent. We only ADD the OpenInference keys. If Glean
  ever changes its shape, extra keys are harmless; missing keys just mean we add nothing.

This module works on a plain Python dict:  {attr_key: attr_value}.
The protobuf <-> dict conversion lives in otlp_codec.py, so this file stays testable
with hand-written dicts (see test_relabel.py).
"""
from __future__ import annotations

import json
import re
from typing import Any

# ---- OpenInference keys Arize looks for (Input/Output chat rendering) --------------
OI_SPAN_KIND = "openinference.span.kind"
OI_INPUT = "input.value"
OI_INPUT_MIME = "input.mime_type"
OI_OUTPUT = "output.value"
OI_OUTPUT_MIME = "output.mime_type"

# ---- regexes to read Glean's flattened message keys --------------------------------
# e.g. "gen_ai.input.messages.0.parts.1.content"
_MSG_RE = re.compile(
    r"^gen_ai\.(?P<io>input|output|prompt|completion)\.messages\."
    r"(?P<mi>\d+)\.(?P<rest>.+)$"
)


def _parse_json_messages(attrs: dict[str, Any], io: str) -> list[dict[str, Any]] | None:
    """Glean's REAL shape: the whole messages list is ONE attribute whose value is a
    JSON string, e.g.
        gen_ai.output.messages = '[{"finish_reason":"stop",
            "parts":[{"content":"...","type":"reasoning"},
                     {"content":"THE ANSWER","type":"text"}],
            "role":"assistant"}]'
    Returns a normalized [{"role","parts":[{"type","content"}]}] list, or None if the
    key isn't present / isn't parseable (so the caller can fall back to dotted keys).
    """
    for key in (f"gen_ai.{io}.messages", f"gen_ai.{_alias(io)}.messages"):
        raw = attrs.get(key)
        if raw is None:
            continue
        data = raw
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                return None
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return None
        out: list[dict[str, Any]] = []
        for msg in data:
            if not isinstance(msg, dict):
                continue
            parts_in = msg.get("parts")
            parts: list[dict[str, Any]] = []
            if isinstance(parts_in, list):
                for p in parts_in:
                    if isinstance(p, dict):
                        parts.append({"type": p.get("type"), "content": p.get("content")})
                    elif isinstance(p, str):
                        parts.append({"type": "text", "content": p})
            elif msg.get("content") is not None:   # message-level content, no parts
                parts.append({"type": "text", "content": msg.get("content")})
            out.append({"role": msg.get("role"), "parts": parts})
        return out
    return None


def _group_messages(attrs: dict[str, Any], io: str) -> list[dict[str, Any]]:
    """Rebuild gen_ai.<io>.messages[] from EITHER Glean's JSON-string key (preferred)
    OR the older flattened dotted keys (fallback).

    Returns a list ordered by message index, each like:
      {"role": "assistant", "parts": [{"type": "reasoning", "content": "..."},
                                       {"type": "text",      "content": "..."}]}
    """
    # Preferred: single JSON-string attribute (what Glean actually sends).
    from_json = _parse_json_messages(attrs, io)
    if from_json is not None:
        return from_json

    # Fallback: flattened dotted keys (gen_ai.<io>.messages.0.parts.1.content).
    # messages[idx] -> {"role": str, "parts": {part_idx: {field: value}}}
    msgs: dict[int, dict[str, Any]] = {}
    for key, val in attrs.items():
        m = _MSG_RE.match(key)
        if not m or m.group("io") not in (io, _alias(io)):
            continue
        mi = int(m.group("mi"))
        rest = m.group("rest")
        slot = msgs.setdefault(mi, {"role": None, "parts": {}})

        if rest == "role":
            slot["role"] = val
            continue
        if rest == "content":                       # message-level content (no parts array)
            slot["parts"].setdefault(0, {})["content"] = val
            continue
        # parts.<pi>.<field>   e.g. parts.1.content / parts.1.type
        pm = re.match(r"^parts\.(\d+)\.(.+)$", rest)
        if pm:
            pi = int(pm.group(1))
            field = pm.group(2)
            slot["parts"].setdefault(pi, {})[field] = val

    out = []
    for mi in sorted(msgs):
        slot = msgs[mi]
        parts = [slot["parts"][pi] for pi in sorted(slot["parts"])]
        out.append({"role": slot["role"], "parts": parts})
    return out


def _alias(io: str) -> str:
    # Glean/OTel sometimes use prompt/completion instead of input/output.
    return {"input": "prompt", "output": "completion"}.get(io, io)


def _answer_text_from_parts(parts: list[dict[str, Any]]) -> str | None:
    """Pick the FINAL human-readable answer from an assistant message's parts.

    Rule (important!): parts can include a 'reasoning' chunk BEFORE the real answer.
    We want the answer, not the chain-of-thought. Preference order:
      1) the LAST part whose type is 'text'   (the visible answer)
      2) the LAST part that has content but is NOT type 'reasoning'
      3) as a last resort, any content at all
    """
    text_parts = [p.get("content") for p in parts
                  if p.get("type") in (None, "text", "output_text") and p.get("content")]
    if text_parts:
        return "\n".join(str(t) for t in text_parts).strip() or None

    non_reasoning = [p.get("content") for p in parts
                     if p.get("type") != "reasoning" and p.get("content")]
    if non_reasoning:
        return "\n".join(str(t) for t in non_reasoning).strip() or None

    any_content = [p.get("content") for p in parts if p.get("content")]
    return ("\n".join(str(t) for t in any_content).strip() or None) if any_content else None


def _question_text_from_messages(msgs: list[dict[str, Any]]) -> str | None:
    """The user's question = content of the LAST message whose role is user/human.
    (Last, so multi-turn exports surface the current turn's question.)"""
    for msg in reversed(msgs):
        role = (msg.get("role") or "").lower()
        if role in ("user", "human"):
            joined = "\n".join(str(p.get("content")) for p in msg.get("parts", [])
                               if p.get("content"))
            if joined.strip():
                return joined.strip()
    return None


def _infer_span_kind(span_name: str, attrs: dict[str, Any]) -> str:
    """Best-effort OpenInference span.kind so Arize renders the span nicely.
    Glean doesn't send openinference.span.kind; we infer from signals it DOES send."""
    name = (span_name or "").lower()
    # explicit GenAI operation hint
    op = str(attrs.get("gen_ai.operation.name") or "").lower()

    if any(k in attrs for k in ("retrieval.documents", "gen_ai.retrieval.documents")) \
            or "retriev" in name or "search" in name:
        return "RETRIEVER"
    if op in ("chat", "generate", "completion", "text_completion") \
            or any(k.startswith("gen_ai.output.messages") for k in attrs) \
            or any(k.startswith("llm.") for k in attrs) \
            or "llm" in name or "generation" in name:
        return "LLM"
    if "tool" in name or any(k.startswith("tool.") for k in attrs):
        return "TOOL"
    if "agent" in name or "run" in name:
        return "AGENT"
    return "CHAIN"


def relabel_attributes(span_name: str, attrs: dict[str, Any]) -> dict[str, Any]:
    """Return a NEW dict = original attrs + added OpenInference keys.
    Never removes keys. Idempotent: re-running won't duplicate or corrupt.
    """
    out = dict(attrs)  # copy; we only add

    in_msgs = _group_messages(attrs, "input")
    out_msgs = _group_messages(attrs, "output")

    # ---- Input text ----
    if OI_INPUT not in out:
        q = _question_text_from_messages(in_msgs)
        # fallbacks Glean may use for a single-string prompt
        if not q:
            q = attrs.get("gen_ai.prompt") or attrs.get("input.value") \
                or attrs.get("gen_ai.request.prompt")
        if q:
            out[OI_INPUT] = q
            out.setdefault(OI_INPUT_MIME, "text/plain")

    # ---- Output text (THE fix for the empty Output in Arize) ----
    if OI_OUTPUT not in out:
        answer = None
        # prefer the last ASSISTANT message's final text part
        for msg in reversed(out_msgs):
            role = (msg.get("role") or "").lower()
            if role in ("assistant", "ai", "model", "glean_ai", ""):
                answer = _answer_text_from_parts(msg.get("parts", []))
                if answer:
                    break
        if not answer:
            answer = attrs.get("gen_ai.completion") or attrs.get("gen_ai.response.text")
        if answer:
            out[OI_OUTPUT] = answer
            out.setdefault(OI_OUTPUT_MIME, "text/plain")

    # ---- Structured messages (Arize also reads these) ----
    _write_llm_messages(out, "llm.input_messages", in_msgs, answer_mode=False)
    _write_llm_messages(out, "llm.output_messages", out_msgs, answer_mode=True)

    # ---- Span kind (so Arize classifies the span) ----
    if OI_SPAN_KIND not in out:
        out[OI_SPAN_KIND] = _infer_span_kind(span_name, attrs)

    return out


def _write_llm_messages(out: dict[str, Any], prefix: str,
                        msgs: list[dict[str, Any]], answer_mode: bool) -> None:
    """Emit llm.(input|output)_messages.N.message.{role,content} flattened keys.
    For output we collapse to the chosen answer text so Arize's message view is clean."""
    if not msgs:
        return
    # don't overwrite if already present
    if any(k.startswith(prefix + ".") for k in out):
        return
    idx = 0
    for msg in msgs:
        role = (msg.get("role") or "").lower() or ("assistant" if answer_mode else "user")
        if answer_mode:
            content = _answer_text_from_parts(msg.get("parts", []))
        else:
            content = "\n".join(str(p.get("content")) for p in msg.get("parts", [])
                                if p.get("content")) or None
        if not content:
            continue
        out[f"{prefix}.{idx}.message.role"] = role
        out[f"{prefix}.{idx}.message.content"] = content
        idx += 1

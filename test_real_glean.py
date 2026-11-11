"""
test_real_glean.py  —  test against the ACTUAL Glean shape seen in Render logs.

The real span puts the whole messages list into ONE attribute as a JSON string:
    gen_ai.output.messages = '[{"finish_reason":"stop","parts":[
        {"content":"**reasoning...","type":"reasoning"},
        {"content":"THE REAL ANSWER","type":"text"}],"role":"assistant"}]'

This proves the relay now reads that JSON string and pulls the final 'text' part
(not the 'reasoning' part) into output.value.

Run:
  cd ~/Downloads/glean-arize-bridge && source .venv/bin/activate
  python glean_arize_relay/test_real_glean.py
"""
from __future__ import annotations

import json
from relabel import relabel_attributes


# Exactly the shape from the logs: a JSON string under one key, reasoning + text parts.
OUTPUT_JSON = json.dumps([{
    "finish_reason": "stop",
    "role": "assistant",
    "parts": [
        {"type": "reasoning", "content": "**Analyzing monitoring options in docs**Preparing to search..."},
        {"type": "text", "content": "You can report a lost debit card by calling 1-800-KESTREL or using the app."},
    ],
}])

INPUT_JSON = json.dumps([{
    "role": "user",
    "parts": [{"type": "text", "content": "How do I report a lost debit card?"}],
}])

REAL_SPAN = {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "glean",
    "gen_ai.request.model": "CLAUDE_5_5_OPUS",
    "gen_ai.response.finish_reasons": "stop",
    "gen_ai.response.id": "resp_123",
    "gen_ai.response.model": "CLAUDE_5_5_OPUS",
    "gen_ai.output.messages": OUTPUT_JSON,   # <-- the real carrier (JSON string)
    "gen_ai.input.messages": INPUT_JSON,
}


def _check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return cond


def main():
    out = relabel_attributes("chat CLAUDE_5_5_OPUS", REAL_SPAN)
    print("ADDED:")
    for k in out:
        if k not in REAL_SPAN:
            print(f"   + {k} = {str(out[k])[:80]}")
    print()

    ok = True
    ok &= _check("output.value is the TEXT answer",
                 "1-800-KESTREL" in (out.get("output.value") or ""))
    ok &= _check("output.value is NOT the reasoning",
                 "Analyzing monitoring" not in (out.get("output.value") or ""))
    ok &= _check("input.value is the question",
                 out.get("input.value") == "How do I report a lost debit card?")
    ok &= _check("span.kind set", bool(out.get("openinference.span.kind")))

    print("\nOutput ->", repr((out.get("output.value") or "")[:90]))
    print("Input  ->", repr(out.get("input.value")))
    print("\n" + ("REAL-SHAPE TEST PASSED ✓" if ok else "FAILED ✗"))
    import sys
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

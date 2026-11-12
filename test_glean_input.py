"""
test_glean_input.py  —  prove the input-fetch is correct AND fail-safe.

Tests WITHOUT real network by monkeypatching the parse helper with sample Glean
getchat responses, and by checking the guard conditions (disabled / no token) return
None quietly instead of raising.

Run:
  cd ~/Downloads/glean-arize-bridge && source .venv/bin/activate
  python glean_arize_relay/test_glean_input.py
"""
from __future__ import annotations

import os
import glean_input


# A realistic getchat response (verified shape: messages[].author + fragments[].text)
SAMPLE = {
    "chat": {
        "messages": [
            {"author": "USER", "fragments": [{"text": "How do I report a lost debit card?"}]},
            {"author": "GLEAN_AI", "fragments": [{"text": "Call 1-800-KESTREL to block it."}]},
            {"author": "USER", "fragments": [{"text": "a later follow-up"}]},
        ]
    }
}
SAMPLE_TOPLEVEL = {"messages": [
    {"author": "USER", "fragments": [{"text": "What credit cards do you offer?"}]},
]}
SAMPLE_CONTENT = {"messages": [{"role": "user", "content": "plain content question"}]}


def _check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return cond


def main():
    ok = True

    # 1) correct extraction: first USER message's text
    q = glean_input._extract_first_user_question(SAMPLE)
    ok &= _check("picks first USER question", q == "How do I report a lost debit card?")

    # 2) top-level messages shape
    q2 = glean_input._extract_first_user_question(SAMPLE_TOPLEVEL)
    ok &= _check("handles top-level messages", q2 == "What credit cards do you offer?")

    # 3) plain content fallback
    q3 = glean_input._extract_first_user_question(SAMPLE_CONTENT)
    ok &= _check("handles plain 'content' field", q3 == "plain content question")

    # 4) junk shapes never raise, return None
    for junk in (None, {}, [], {"foo": "bar"}, {"chat": {}}, {"messages": "nope"}):
        try:
            r = glean_input._extract_first_user_question(junk)
            good = r is None
        except Exception:
            good = False
        ok &= _check(f"junk shape -> None, no raise ({junk!r:.25})", good)

    # 5) FAIL-SAFE: feature disabled -> None even with an id
    os.environ.pop("GLEAN_INPUT_LOOKUP", None)
    ok &= _check("disabled -> None", glean_input.question_for_conversation("conv-1") is None)

    # 6) FAIL-SAFE: enabled but no token/instance -> None, no raise
    os.environ["GLEAN_INPUT_LOOKUP"] = "1"
    os.environ.pop("GLEAN_TOKEN", None)
    os.environ.pop("GLEAN_INSTANCE", None)
    os.environ.pop("GLEAN_BASE_URL", None)
    glean_input._CACHE.clear()
    ok &= _check("enabled but no token -> None, no raise",
                 glean_input.question_for_conversation("conv-2") is None)

    # 7) caching: a resolved id is cached (no second lookup)
    glean_input._CACHE["conv-x"] = "cached question"
    ok &= _check("uses cache", glean_input.question_for_conversation("conv-x") == "cached question")

    print("\n" + ("INPUT-FETCH TESTS PASSED (correct + fail-safe) ✓" if ok else "FAILED ✗"))
    import sys
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

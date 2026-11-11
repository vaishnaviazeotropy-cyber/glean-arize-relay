"""
test_relabel.py  —  prove the mapping works, with NO web server and NO network.

Run it:
    cd ~/Downloads/glean-arize-bridge
    source .venv/bin/activate
    python glean_arize_relay/test_relabel.py

It builds the span attributes the way Glean's export flattens them (including the
'reasoning' part that must be SKIPPED), runs relabel, and checks that:
  • input.value  == the user's question
  • output.value == the FINAL assistant answer (not the reasoning text)
  • openinference.span.kind is set
Prints PASS/FAIL for each check and a readable before/after.
"""
from __future__ import annotations

from relabel import relabel_attributes  # run from inside the package dir


# This mirrors the real screenshot: Input "How do I report a lost debit card?" and a
# full Output answer. The assistant message has a 'reasoning' part FIRST, then the 'text'
# answer — exactly the trap we must avoid mapping.
GLEAN_SPAN_ATTRS = {
    "gen_ai.system": "glean",
    "gen_ai.operation.name": "chat",
    "gen_ai.response.id": "run_abc123",
    "gen_ai.response.model": "glean-agent",
    # ---- input (user) ----
    "gen_ai.input.messages.0.role": "user",
    "gen_ai.input.messages.0.parts.0.type": "text",
    "gen_ai.input.messages.0.parts.0.content": "How do I report a lost debit card?",
    # ---- output (assistant): reasoning part FIRST, real answer SECOND ----
    "gen_ai.output.messages.0.role": "assistant",
    "gen_ai.output.messages.0.parts.0.type": "reasoning",
    "gen_ai.output.messages.0.parts.0.content": "The user is asking about reporting a lost card; I'll pull the card-security doc...",
    "gen_ai.output.messages.0.parts.1.type": "text",
    "gen_ai.output.messages.0.parts.1.content": (
        "You can report a lost debit card 24/7 by calling our hotline at 1-800-KESTREL, "
        "using the mobile app (Cards > Report lost/stolen), or visiting any branch. "
        "We'll block the card immediately and ship a replacement within 3-5 business days."
    ),
}

EXPECTED_INPUT = "How do I report a lost debit card?"
EXPECTED_OUTPUT_CONTAINS = "report a lost debit card 24/7"
MUST_NOT_CONTAIN = "The user is asking about"   # the reasoning text


def _check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return cond


def main():
    print("Relabelling a realistic Glean span...\n")
    out = relabel_attributes("agent.run", GLEAN_SPAN_ATTRS)

    print("ADDED keys:")
    for k in out:
        if k not in GLEAN_SPAN_ATTRS:
            v = str(out[k])
            print(f"   + {k} = {v[:80]}{'...' if len(v) > 80 else ''}")
    print()

    ok = True
    ok &= _check("input.value equals the question",
                 out.get("input.value") == EXPECTED_INPUT)
    ok &= _check("output.value contains the real answer",
                 EXPECTED_OUTPUT_CONTAINS in (out.get("output.value") or ""))
    ok &= _check("output.value does NOT contain the reasoning text",
                 MUST_NOT_CONTAIN not in (out.get("output.value") or ""))
    ok &= _check("openinference.span.kind is set",
                 bool(out.get("openinference.span.kind")))
    ok &= _check("llm.input_messages.0.message.content set",
                 bool(out.get("llm.input_messages.0.message.content")))
    ok &= _check("llm.output_messages.0.message.content set",
                 bool(out.get("llm.output_messages.0.message.content")))
    ok &= _check("original keys preserved (nothing deleted)",
                 all(k in out for k in GLEAN_SPAN_ATTRS))

    print("\nInput  ->", repr(out.get("input.value")))
    print("Output ->", repr((out.get("output.value") or "")[:90] + "..."))
    print("Kind   ->", out.get("openinference.span.kind"))

    print("\n" + ("ALL CHECKS PASSED ✓" if ok else "SOME CHECKS FAILED ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

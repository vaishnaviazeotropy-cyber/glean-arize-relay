"""
test_protobuf_roundtrip.py  —  prove the PROTOBUF layer works end to end, offline.

Builds a real OTLP ExportTraceServiceRequest (the exact object Glean sends), with one
span whose attributes are flattened Glean gen_ai.* keys. Then:
  1. serialize -> bytes          (what travels over HTTP from Glean)
  2. decode_request(bytes)       (our codec)
  3. relabel every span          (our brain)
  4. apply back + encode -> bytes (what we forward to Arize)
  5. decode AGAIN and assert input.value / output.value survived the protobuf round-trip.

Run:
  cd ~/Downloads/glean-arize-bridge && source .venv/bin/activate
  python glean_arize_relay/test_protobuf_roundtrip.py
"""
from __future__ import annotations

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span
from opentelemetry.proto.common.v1.common_pb2 import KeyValue, AnyValue

from otlp_codec import decode_request, encode_request, iter_spans, \
    span_attrs_to_dict, apply_attrs_to_span
from relabel import relabel_attributes


GLEAN_ATTRS = {
    "gen_ai.system": "glean",
    "gen_ai.operation.name": "chat",
    "gen_ai.input.messages.0.role": "user",
    "gen_ai.input.messages.0.parts.0.type": "text",
    "gen_ai.input.messages.0.parts.0.content": "How do I report a lost debit card?",
    "gen_ai.output.messages.0.role": "assistant",
    "gen_ai.output.messages.0.parts.0.type": "reasoning",
    "gen_ai.output.messages.0.parts.0.content": "Thinking about card security policy...",
    "gen_ai.output.messages.0.parts.1.type": "text",
    "gen_ai.output.messages.0.parts.1.content": "Call 1-800-KESTREL or use the app to report it.",
}


def build_glean_request() -> ExportTraceServiceRequest:
    span = Span(name="agent.run",
                trace_id=b"\x01" * 16, span_id=b"\x02" * 8)
    for k, v in GLEAN_ATTRS.items():
        span.attributes.append(KeyValue(key=k, value=AnyValue(string_value=str(v))))
    ss = ScopeSpans(spans=[span])
    rs = ResourceSpans(scope_spans=[ss])
    return ExportTraceServiceRequest(resource_spans=[rs])


def main():
    # 1. what Glean sends
    wire = encode_request(build_glean_request())
    print(f"Glean wire bytes: {len(wire)}")

    # 2-4. our relay's job
    req = decode_request(wire)
    for sp in iter_spans(req):
        attrs = span_attrs_to_dict(sp)
        apply_attrs_to_span(sp, relabel_attributes(sp.name, attrs))
    forwarded = encode_request(req)
    print(f"Forwarded bytes:  {len(forwarded)}  (grew = added OpenInference keys)")

    # 5. decode what Arize would receive, and verify
    received = decode_request(forwarded)
    sp = next(iter_spans(received))
    got = span_attrs_to_dict(sp)

    checks = [
        ("input.value present", got.get("input.value") == "How do I report a lost debit card?"),
        ("output.value present", "Call 1-800-KESTREL" in (got.get("output.value") or "")),
        ("reasoning NOT leaked", "Thinking about" not in (got.get("output.value") or "")),
        ("span.kind set", bool(got.get("openinference.span.kind"))),
        ("original gen_ai keys kept", got.get("gen_ai.system") == "glean"),
        ("forwarded grew", len(forwarded) > len(wire)),
    ]
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok &= cond
    print("\n" + ("PROTOBUF ROUND-TRIP OK ✓" if ok else "ROUND-TRIP FAILED ✗"))
    import sys
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

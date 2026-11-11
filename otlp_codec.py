"""
otlp_codec.py  —  translate between OTLP protobuf bytes and plain Python.

Glean's "Agent trace export" sends an OTLP/HTTP request: the body is a protobuf
`ExportTraceServiceRequest` (Content-Type: application/x-protobuf). Its shape:

  ExportTraceServiceRequest
    └─ resource_spans: [ ResourceSpans
         ├─ resource.attributes: [KeyValue]
         └─ scope_spans: [ ScopeSpans
              └─ spans: [ Span
                   ├─ name
                   ├─ attributes: [ KeyValue{ key, value: AnyValue } ]   <-- what we relabel
                   └─ ... ] ] ]

An AnyValue holds ONE of: string_value / bool_value / int_value / double_value /
array_value / kvlist_value / bytes_value.

This module:
  • decode_request(body)                     -> ExportTraceServiceRequest (protobuf obj)
  • span_attrs_to_dict(span)                 -> {key: python_value}   (for relabel.py)
  • apply_attrs_to_span(span, new_attrs)     -> write a dict back onto the span
  • encode_request(req)                      -> bytes  (to forward to Arize)

We depend on the official `opentelemetry-proto` package (generated protobuf classes),
so we never hand-roll protobuf parsing.
"""
from __future__ import annotations

from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue


# ---- decode / encode the whole request --------------------------------------------
def decode_request(body: bytes) -> ExportTraceServiceRequest:
    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    return req


def encode_request(req: ExportTraceServiceRequest) -> bytes:
    return req.SerializeToString()


# ---- AnyValue <-> python -----------------------------------------------------------
def anyvalue_to_py(v: AnyValue) -> Any:
    which = v.WhichOneof("value")
    if which == "string_value":
        return v.string_value
    if which == "bool_value":
        return v.bool_value
    if which == "int_value":
        return v.int_value
    if which == "double_value":
        return v.double_value
    if which == "bytes_value":
        return v.bytes_value
    if which == "array_value":
        return [anyvalue_to_py(x) for x in v.array_value.values]
    if which == "kvlist_value":
        return {kv.key: anyvalue_to_py(kv.value) for kv in v.kvlist_value.values}
    return None


def py_to_anyvalue(val: Any) -> AnyValue:
    av = AnyValue()
    if isinstance(val, bool):
        av.bool_value = val
    elif isinstance(val, int):
        av.int_value = val
    elif isinstance(val, float):
        av.double_value = val
    elif isinstance(val, bytes):
        av.bytes_value = val
    elif isinstance(val, (list, tuple)):
        for x in val:
            av.array_value.values.append(py_to_anyvalue(x))
    elif isinstance(val, dict):
        for k, x in val.items():
            kv = KeyValue(key=str(k), value=py_to_anyvalue(x))
            av.kvlist_value.values.append(kv)
    else:
        av.string_value = "" if val is None else str(val)
    return av


# ---- span attributes <-> flat dict -------------------------------------------------
def span_attrs_to_dict(span) -> dict[str, Any]:
    return {kv.key: anyvalue_to_py(kv.value) for kv in span.attributes}


def apply_attrs_to_span(span, new_attrs: dict[str, Any]) -> None:
    """Overwrite the span's attribute list with new_attrs (which is original + added).
    We rebuild the whole list so added keys appear and nothing we kept is lost."""
    del span.attributes[:]
    for k, v in new_attrs.items():
        span.attributes.append(KeyValue(key=str(k), value=py_to_anyvalue(v)))


# ---- convenience: walk every span in a request -------------------------------------
def iter_spans(req: ExportTraceServiceRequest):
    for rs in req.resource_spans:
        for ss in rs.scope_spans:
            for sp in ss.spans:
                yield sp

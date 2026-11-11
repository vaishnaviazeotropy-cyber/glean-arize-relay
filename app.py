"""
app.py  —  the relay web service (FastAPI).

FLOW
  Glean "Agent trace export"  --OTLP/HTTP protobuf-->  THIS SERVICE  --OTLP/HTTP-->  Arize

  1. Glean POSTs an OTLP ExportTraceServiceRequest (protobuf) to  /v1/traces
  2. We decode it, and for EVERY span we add the OpenInference keys Arize needs
     (input.value / output.value / openinference.span.kind / llm.*_messages) by reading
     Glean's nested gen_ai.*.messages[].parts[].content.
  3. We re-encode and forward to Arize's OTLP endpoint with the Arize auth headers.
  4. We return the status Arize gave us back to Glean.

ENDPOINTS
  GET  /health            -> {"ok": true}  (sanity / uptime check)
  POST /v1/traces         -> the real relay (what you point Glean at)
  POST /debug/relabel     -> paste a JSON {span_name, attrs} and see the relabelled attrs
                             (lets you verify the mapping WITHOUT protobuf, great for testing)

CONFIG (environment variables)
  ARIZE_SPACE_ID          required  (Space ID, e.g. U3BhY2U6...==)
  ARIZE_API_KEY           required  (API key)
  ARIZE_PROJECT           optional  (project/model name; default "glean-banking-agent")
  ARIZE_OTLP_URL          optional  (default https://otlp.arize.com/v1/traces)
  RELAY_LOG_BODIES        optional  ("1" to log a short summary of each span relabelled)

Run locally:
  uvicorn glean_arize_relay.app:app --port 8081
Deploy (Render): start command = uvicorn glean_arize_relay.app:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import os
import logging

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

# Import works both as a package (glean_arize_relay.app:app from the project root)
# and as a flat module (app:app from inside this folder, which is how Render runs it).
try:
    from .otlp_codec import decode_request, encode_request, iter_spans, \
        span_attrs_to_dict, apply_attrs_to_span
    from .relabel import relabel_attributes
except ImportError:
    from otlp_codec import decode_request, encode_request, iter_spans, \
        span_attrs_to_dict, apply_attrs_to_span
    from relabel import relabel_attributes

log = logging.getLogger("glean_arize_relay")
logging.basicConfig(level=logging.INFO)

ARIZE_OTLP_URL = os.getenv("ARIZE_OTLP_URL", "https://otlp.arize.com/v1/traces")
ARIZE_SPACE_ID = os.getenv("ARIZE_SPACE_ID", "")
ARIZE_API_KEY = os.getenv("ARIZE_API_KEY", "")
ARIZE_PROJECT = os.getenv("ARIZE_PROJECT", "glean-banking-agent")
LOG_BODIES = os.getenv("RELAY_LOG_BODIES") == "1"

app = FastAPI(title="Glean -> Arize relabel relay", version="1.0")


def _arize_headers() -> dict[str, str]:
    # Arize OTLP/HTTP uses hyphenated headers (NOT the gRPC space_id/api_key form).
    return {
        "Content-Type": "application/x-protobuf",
        "arize-space-id": ARIZE_SPACE_ID,
        "arize-api-key": ARIZE_API_KEY,
        "x-project-name": ARIZE_PROJECT,
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "arize_url": ARIZE_OTLP_URL,
        "project": ARIZE_PROJECT,
        "space_id_set": bool(ARIZE_SPACE_ID),
        "api_key_set": bool(ARIZE_API_KEY),
    }


@app.post("/v1/traces")
async def relay_traces(request: Request):
    body = await request.body()
    if not body:
        return JSONResponse({"error": "empty body"}, status_code=400)

    try:
        req = decode_request(body)
    except Exception as e:
        log.warning("could not decode OTLP protobuf: %r", e)
        return JSONResponse({"error": f"bad OTLP protobuf: {e}"}, status_code=400)

    # relabel every span in place
    relabelled, with_output = 0, 0
    for sp in iter_spans(req):
        attrs = span_attrs_to_dict(sp)
        new_attrs = relabel_attributes(sp.name, attrs)

        # DEBUG: dump the RAW attribute keys so we can see exactly what Glean sends.
        # Turn on with RELAY_DUMP_KEYS=1. Logs names + a short value preview for each key.
        if os.getenv("RELAY_DUMP_KEYS") == "1":
            log.info("RAWSPAN name=%r keys=%s", sp.name, sorted(attrs.keys()))
            dump_len = int(os.getenv("RELAY_DUMP_LEN", "120"))
            for k in sorted(attrs.keys()):
                if any(t in k.lower() for t in ("message", "content", "output", "input",
                                                "completion", "prompt", "answer", "text",
                                                "question", "query", "tool", "gen_ai")):
                    log.info("RAWKV name=%r %s = %r", sp.name, k, str(attrs[k])[:dump_len])

        if new_attrs != attrs:
            apply_attrs_to_span(sp, new_attrs)
            relabelled += 1
            if "output.value" in new_attrs:
                with_output += 1
            if LOG_BODIES:
                log.info("span '%s': input=%r output=%r kind=%s",
                         sp.name,
                         str(new_attrs.get("input.value"))[:60],
                         str(new_attrs.get("output.value"))[:60],
                         new_attrs.get("openinference.span.kind"))

    out_body = encode_request(req)

    if not (ARIZE_SPACE_ID and ARIZE_API_KEY):
        log.error("ARIZE_SPACE_ID / ARIZE_API_KEY not set — not forwarding")
        return JSONResponse(
            {"error": "server missing ARIZE_SPACE_ID/ARIZE_API_KEY",
             "relabelled_spans": relabelled, "spans_with_output": with_output},
            status_code=500,
        )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(ARIZE_OTLP_URL, content=out_body,
                                     headers=_arize_headers())
    except Exception as e:
        log.error("forward to Arize failed: %r", e)
        return JSONResponse({"error": f"forward failed: {e}"}, status_code=502)

    log.info("relabelled %d span(s) (%d with output), Arize responded %d",
             relabelled, with_output, resp.status_code)

    # mirror Arize's status to Glean; OTLP success is 200 with an (often empty) protobuf body
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/x-protobuf"))


@app.post("/debug/relabel")
async def debug_relabel(request: Request):
    """Test the mapping WITHOUT protobuf. POST JSON:
        {"span_name": "agent.run", "attrs": {"gen_ai.input.messages.0.role": "user", ...}}
    Returns the relabelled attrs so you can confirm input.value / output.value are set."""
    payload = await request.json()
    span_name = payload.get("span_name", "")
    attrs = payload.get("attrs", {})
    new_attrs = relabel_attributes(span_name, attrs)
    added = {k: new_attrs[k] for k in new_attrs if k not in attrs}
    return {
        "added_keys": added,
        "input.value": new_attrs.get("input.value"),
        "output.value": new_attrs.get("output.value"),
        "openinference.span.kind": new_attrs.get("openinference.span.kind"),
        "full_attrs": new_attrs,
    }

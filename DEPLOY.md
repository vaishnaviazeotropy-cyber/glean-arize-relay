# Deploying the Glean → Arize relabel relay (free, on Render)

This service sits between Glean's **Agent trace export** and **Arize**. Glean pushes
traces to this service; the service rewrites the fields into the shape Arize understands
(so the **Input question** and **Output answer** actually show up), then forwards to Arize.

```
  Glean agent (UI chat)  ──OTLP push──▶  THIS RELAY (Render)  ──OTLP──▶  Arize
                                         adds input.value / output.value
                                         + openinference.span.kind
```

You do NOT run anything on your laptop in production. Render runs it 24/7 on a public
HTTPS URL. Free tier is fine for testing. (Free services sleep after ~15 min idle and
wake on the next request — the first trace after a nap may take ~30s. That's OK for a demo.)

---

## What you need first
- A GitHub account (free).
- Your Arize **Space ID** and **API key** (the same ones from `.env` — you'll paste them
  into Render's dashboard, never into a file that goes to GitHub).
- This folder: `glean_arize_relay/`.

---

## Step 1 — Prove it works locally (already done, but here's how to re-run)

```bash
cd ~/Downloads/glean-arize-bridge
source .venv/bin/activate
cd glean_arize_relay
python test_relabel.py              # pure mapping test  -> ALL CHECKS PASSED
python test_protobuf_roundtrip.py   # protobuf in/out    -> ROUND-TRIP OK
```

Both must say PASS before deploying. If they do, the logic is correct.

---

## Step 2 — Put this folder in a GitHub repo

The relay is self-contained in `glean_arize_relay/`. Make THAT folder the repo root
(so `app.py`, `requirements.txt`, `render.yaml` sit at the top level of the repo).

```bash
cd ~/Downloads/glean-arize-bridge/glean_arize_relay
git init
git add app.py relabel.py otlp_codec.py __init__.py \
        requirements.txt render.yaml Procfile runtime.txt .gitignore \
        test_relabel.py test_protobuf_roundtrip.py DEPLOY.md
git commit -m "Glean->Arize relabel relay"
```

Then create an EMPTY repo on github.com (e.g. `glean-arize-relay`), and push:

```bash
git remote add origin https://github.com/<your-username>/glean-arize-relay.git
git branch -M main
git push -u origin main
```

> `.env` is git-ignored, so no secret ever leaves your machine. Good.

---

## Step 3 — Create the service on Render

1. Go to https://render.com and sign in with GitHub.
2. Click **New +** → **Web Service**.
3. Pick your `glean-arize-relay` repo.
4. Render auto-detects Python and reads `render.yaml`. Confirm:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Plan:** Free
5. Click **Create Web Service**. It builds (2–3 min).

---

## Step 4 — Add your Arize secrets (in Render, not in code)

In the service's **Environment** tab, add two secret variables:

| Key              | Value                                   |
| ---------------- | --------------------------------------- |
| `ARIZE_SPACE_ID` | *(your Space ID)*                       |
| `ARIZE_API_KEY`  | *(your API key)*                        |

`ARIZE_PROJECT`, `ARIZE_OTLP_URL`, and `RELAY_LOG_BODIES` are already set by `render.yaml`.
Click **Save** — Render redeploys automatically.

Your service URL will look like: `https://glean-arize-relay.onrender.com`

---

## Step 5 — Check the relay is alive

Open in a browser (or curl):

```
https://glean-arize-relay.onrender.com/health
```

You should see `{"ok": true, ... "space_id_set": true, "api_key_set": true}`.
If `space_id_set` or `api_key_set` is false, the secrets in Step 4 aren't saved yet.

Optionally test the mapping (no protobuf needed):

```bash
curl -s -X POST https://glean-arize-relay.onrender.com/debug/relabel \
  -H 'Content-Type: application/json' \
  -d '{"span_name":"agent.run","attrs":{
        "gen_ai.input.messages.0.role":"user",
        "gen_ai.input.messages.0.parts.0.content":"How do I report a lost debit card?",
        "gen_ai.output.messages.0.role":"assistant",
        "gen_ai.output.messages.0.parts.1.type":"text",
        "gen_ai.output.messages.0.parts.1.content":"Call 1-800-KESTREL."}}'
```

It returns `input.value` and `output.value` filled in. 

---

## Step 6 — Point Glean's export at the relay

In the Glean Admin console → the **Agent trace export** destination you set up earlier,
change the endpoint URL to:

```
https://glean-arize-relay.onrender.com/v1/traces
```

Leave the auth header as whatever Glean requires for your destination (the relay doesn't
check it; it forwards to Arize using the Arize key from Render). Save.

> Previously you pointed Glean straight at `https://otlp.arize.com/v1/traces`. Now it goes
> to the relay instead, and the relay forwards to Arize. That's the whole change.

---

## Step 7 — Chat in the Glean UI and verify in Arize

1. Open your Glean agent in the **Glean UI** and have a short conversation
   (e.g. "How do I report a lost debit card?").
2. Wait ~30–60s (Glean batches its export; a free Render service may wake from sleep).
3. In Arize → project **glean-banking-agent** → **Traces**:
   - set the time range to **Last 24 Hours**, Live off;
   - if it says "no data", **change the time-range value** to force a refresh (don't just reload).
4. Open the newest trace. You should now see:
   - **Input** = your question
   - **Output** = the agent's answer (the full text, not metadata)

That matches your target screenshot. Done.

---

## Troubleshooting

- **Arize shows the trace but Input/Output still empty.** The real Glean span shape differs
  from our assumption. Turn on body logging (already on via `RELAY_LOG_BODIES=1`), check
  Render's **Logs** tab for a line like `span '...': input=... output=...`. If `output` is
  empty there, the answer lives under a different key. Capture one raw span (see below) and
  we adjust `relabel.py`'s part-selection rule.
- **Render logs show "could not decode OTLP protobuf".** Glean may be sending JSON, not
  protobuf. Tell me and I'll add a JSON branch to `/v1/traces`.
- **Arize responded 401/403 in the logs.** The Space ID / API key in Render is wrong or for
  the wrong region. Re-copy them from Arize.
- **Capturing a raw span to inspect:** temporarily set the Glean export endpoint to
  `https://glean-arize-relay.onrender.com/v1/traces` and watch Render Logs with
  `RELAY_LOG_BODIES=1`; the logged input/output snippets tell us if our key assumptions hold.

## Later: moving off Render to your VM / cloud
The service is just `uvicorn app:app`. On the VM:
```bash
pip install -r requirements.txt
ARIZE_SPACE_ID=... ARIZE_API_KEY=... uvicorn app:app --host 0.0.0.0 --port 8080
```
Put it behind your existing HTTPS (nginx/Caddy) and point Glean at `https://<your-host>/v1/traces`.
Nothing else changes.

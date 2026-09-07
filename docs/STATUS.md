# AI Interface — Feature Status

**Last verified:** 2026-09-07 on `testerp2.web.gargsystems.com`, against live Anthropic and Sarvam accounts.

This document tracks what is verified working, what is untested, and what is intentionally not yet shipped. Updated whenever a path is verified or a gap is found.

For how any of it works, see [ai_interface_handbook.md](ai_interface_handbook.md).

---

## Legend

- ✅ **Working** — End-to-end verified with a real call, or pinned by a test.
- ⚠️ **Untested** — Code exists and looks correct, but no live verification has been done. Use at your own risk.
- ❌ **Not implemented** — Listed/referenced in code or docs but the path will fail at runtime.

---

## Providers

Provider types are **records, not code**. `provider_type` is a Link to **AI Provider Type**, which holds the adapter path, endpoints, auth header and billing currency. A type cannot be saved unless its `adapter_path` imports and subclasses `BaseProvider`, so an option that would throw at call time can no longer be created.

| Provider | Path | Status | Notes |
|---|---|---|---|
| Anthropic | API Key (`sk-ant-api03-*`) | ✅ Working | Verified 2026-09-07. Must be a **workspace-scoped** key — an organization key is rejected with `400 not scoped to a workspace`, which needs an `anthropic-workspace-id` header this app does not send. 11 models discovered live, priced from the catalog. |
| Anthropic | Auth Token / OAuth (`sk-ant-oat01-*`) | ⚠️ Untested since Stage 1 | Header injection and system-prompt prefix still in `anthropic_provider.py`. Not re-verified after the registry refactor. |
| Claude Code | CLI subprocess (`claude -p`) | ⚠️ Untested since Stage 1 | Type now carries `no_credential`. Model list must be entered by hand — the CLI exposes no listing endpoint. |
| Sarvam | API Key (`sk_*`) | ✅ Working | Verified 2026-09-07, **zero new Python**. Chat on `/v1`; `/v2` is beta-gated per account and answers 400 without access. 6 models discovered via `/v2/models`. Bills INR. `sarvam-105b` is a reasoning model — it needs a large `max_tokens` or returns an empty answer. |
| OpenAI | API Key | ⚠️ Untested | Type is seeded and the generic adapter covers the wire format. No account was available to verify. |
| Groq, Together, Mistral, DeepSeek, OpenRouter, Ollama, vLLM | API Key | ⚠️ Untested | Same generic adapter. Each is a provider-type record, not a code change. |

**Adding a provider:** create an **AI Provider Type** record pointing at `openai_compatible.OpenAICompatibleProvider` with the vendor's base URL, paths and auth header. Only a genuinely different wire protocol needs a new adapter class, and that is registered by a record too — another app can ship its own without editing this one.

---

## Service Functions

| Function | Path | Status | Notes |
|---|---|---|---|
| `services.generator.generate()` | Sync, raw prompt | ✅ Working | Verified against live Anthropic and Sarvam. |
| `services.generator.generate()` | Async (default) | ⚠️ Untested | Enqueues via `frappe.enqueue`; `_execute_ai_call` publishes `ai_call_complete`. The worker path itself is covered by tests, but no listener-side smoke test has been run. |
| `services.generator.generate()` | Template + context | ⚠️ Untested | `_render_prompt` looks correct; no sample template ships. |
| `services.query_engine.query()` | NL → site data | ✅ Working | Rewritten 2026-09-07. Aggregates run in the database, so counts are counted rather than guessed from a capped page. Every field, filter, grouping and aggregate is validated against real metadata before execution. Schema is scoped to the asker's permissions and shortlisted by keyword, replacing the 200-doctype dump that used to cost 5–10k tokens per question. |
| `services.vision.extract()` | Single image | ⚠️ Untested | Generic adapter sends `image_url` data URIs; Anthropic adapter sends base64 blocks. Path validated but never exercised with a real file. |
| `services.vision.extract()` | PDF → multi-page | ⚠️ Untested | Depends on `pdf2image` + `pdftoppm`. Converts up to `max_pdf_pages`. |
| `api.assistant.ask()` | Chat widget | ✅ Working | Verified on testerp2, including a follow-up that names nothing ("and how many of those are enabled?") resolving correctly from history. |

---

## Doctypes

| DocType | Type | Status |
|---|---|---|
| AI Settings | Single | ✅ Working. Defaults for fields added after install are applied by patch — a field default only applies when a document is created, and this Single exists from install. |
| AI Provider Type | Regular | ✅ Working. Seeded with Anthropic, Claude Code, OpenAI, Sarvam. `adapter_path` validated on save. |
| AI Provider | Regular | ✅ Working. Health counters, Test Connection, per-account currency and exchange rate. |
| AI Provider Model | Child Table | ✅ Working. Merged by `fetch_models`, never replaced. |
| AI Routing Rule | Child Table | ✅ Working. Matched most-specific-first; lower priorities become the fallback chain. |
| AI App Budget | Child Table | ✅ Working. |
| AI Prompt Template | Regular | ⚠️ Untested. Exists, never exercised through `generate(template=...)`. |
| AI Call Log | Regular | ✅ Working. Two-currency cost, attempts, attribution, error taxonomy. 90-day auto-cleanup. |
| AI Chat Conversation | Regular | ✅ Working. One owner; a stranger cannot read another person's thread. |
| AI Chat Message | Child Table | ✅ Working. Stores the query behind each answer. |

---

## Methods on AI Provider Doctype

| Method | Status | Notes |
|---|---|---|
| `get_credential()` | ✅ | Reports an unset key in its own words rather than surfacing Frappe's "Password not found". |
| `fetch_models()` | ✅ | Discovers ids from the vendor and **merges**: hand-entered rates are stamped `Manual` and never overwritten, dropped models are disabled rather than deleted (old logs still cost against them), blanks are prefilled from the optional Pricing Source URL. Clear that URL and no outbound call is made. |
| `test_connection()` | ✅ | One real, billed call on the cheapest enabled model. Logged as `action=connection_test`. Fetch Models is not a substitute — a key can list models and still be rejected for chat. |
| `record_health()` | ✅ | Written with `db_set` on the hot path, so health bookkeeping can never itself fail a call. |

---

## Missing Pieces (Not Yet Shipped)

1. **No real consumer app.** Nothing outside this app imports `ai_interface.services` — Excom, DocPulse, WarehouseSuite, BNS and Insightly still do their own AI calls. The platform has never run under production traffic, only its own tests and the assistant.
2. **Sample prompt templates.** No fixtures ship; consumer apps create their own.
3. **Realtime listener helper (JS).** Each consumer still wires its own `frappe.realtime.on("ai_call_complete", cb)`.
4. **Sarvam's non-chat APIs.** TTS (`bulbul-v3`), STT (`saaras-v3`), `/translate`, `/transliterate` do not fit the chat/vision contract and would need their own `function_type`.
5. **Streaming.** Every call is request/response. The widget shows a typing indicator, not streamed tokens.
6. **Tests run on `dev-15.local`, not testerp2.** testerp2 has `allow_tests` off — that guard stops a test runner touching a real site, and it was deliberately left alone.

---

## Verification Recipes

### Smoke test generate (sync)

```python
# bench --site testerp2.web.gargsystems.com console
from ai_interface.services.generator import generate
print(generate(prompt="Say hi in 5 words", sync=True, calling_app="smoke"))
```

Expected: a short string. Check **AI Call Log** for `status=Completed`, `latency_ms > 0`, `cost > 0`, and `base_cost` populated.

### Smoke test the assistant

```python
from ai_interface.api import assistant
r = assistant.ask("How many users are there?")
print(r["answer"], r["query"])
```

Expected: a plain count, and a query using `aggregate: {function: count}` — **not** a list of rows the model counted.

### Smoke test a follow-up

```python
first = assistant.ask("How many users are there?")
print(assistant.ask("And how many of those are enabled?", conversation=first["conversation"])["answer"])
```

Expected: resolves without the second question naming a doctype. If it fails, history is not reaching the planner.

### Smoke test vision

```python
from ai_interface.services.vision import extract
print(extract(file_url="/files/invoice.png", sync=True, calling_app="vtest"))
```

Expected: extracted text, routed through `vision()` rather than `chat()`.

### Smoke test async path

```python
from ai_interface.services.generator import generate
log_name = generate(prompt="Long form thing", calling_app="async_test")

import frappe, time
for _ in range(30):
    s = frappe.get_value("AI Call Log", log_name, "status")
    print("Status:", s)
    if s in ("Completed", "Failed"):
        break
    time.sleep(2)
```

Expected: Queued → Running → Completed.

### Run the test suite

```bash
bench --site dev-15.local run-tests --app ai_interface
```

Expected: 131 tests, all passing, in roughly 15 seconds. Nothing reaches the network.

---

## How to Mark Something Working

When you verify a ⚠️ path:
1. Re-run the smoke recipe.
2. Open the resulting AI Call Log row, confirm tokens/cost/latency populated.
3. Edit this file: change ⚠️ to ✅ with `Verified YYYY-MM-DD` in the notes.
4. Commit with `docs: verify <feature> in STATUS`.

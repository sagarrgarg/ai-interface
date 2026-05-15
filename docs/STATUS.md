# AI Interface — Feature Status

**Last verified:** 2026-05-15 on `erpnext.local` (frappe-bench-new, port 8001)

This document tracks what is verified working, what is untested, and what is intentionally not yet shipped. Updated whenever a path is verified or a gap is found.

---

## Legend

- ✅ **Working** — End-to-end verified with a real call. Smoke-tested in console or via integration.
- ⚠️ **Untested** — Code exists and looks correct, but no live verification has been done. Use at your own risk.
- ❌ **Not implemented** — Listed/referenced in code or docs but the path will fail at runtime. Now removed or stubbed where possible.

---

## Providers

| Provider | Path | Status | Notes |
|---|---|---|---|
| Anthropic | API Key (`sk-ant-api03-*`) | ⚠️ Untested | Code path identical to OAuth minus header injection. Needs a real key to verify. |
| Anthropic | Auth Token / OAuth (`sk-ant-oat01-*`) | ✅ Working | Verified 2026-05-15. Requires `anthropic-beta: oauth-2025-04-20` header + Claude Code system prompt prefix — both injected automatically by `_get_client` and `_prepare_system`. |
| Claude Code | CLI subprocess (`claude -p`) | ✅ Working | Verified 2026-05-15. ~10x slower than OAuth direct (subprocess overhead). Token counts inflated (CLI reports reasoning tokens). |
| ~~OpenAI~~ | — | ❌ Removed from Select | No `OpenAIProvider` class. Removed from `ai_provider.json` provider_type options. |
| ~~Google~~ | — | ❌ Removed from Select | No `GoogleProvider` class. Removed from `ai_provider.json` provider_type options. |
| ~~Custom~~ | — | ❌ Removed from Select | No generic implementation. Removed from `ai_provider.json` provider_type options. |

**Adding a new provider:** create `providers/<name>_provider.py` implementing `BaseProvider`, register in `providers/__init__.py`, then re-add to the Select options. Until that happens, the option is hidden so users can't pick something that throws.

---

## Service Functions

| Function | Path | Status | Notes |
|---|---|---|---|
| `services.generator.generate()` | Sync, raw prompt | ✅ Working | Verified end-to-end via both Anthropic OAuth and Claude Code CLI. AI Call Log captures tokens, cost, latency. |
| `services.generator.generate()` | Async (default) | ⚠️ Untested | Code enqueues via `frappe.enqueue`. Realtime event `ai_call_complete` fires from `_execute_ai_call`. Needs a listener-side smoke test. |
| `services.generator.generate()` | Template + context | ⚠️ Untested | `_render_prompt` looks correct, no sample template shipped to verify. |
| `services.query_engine.query()` | Sync, 3-phase NL→ERPNext | ✅ Working | Verified 2026-05-15 with "How many active customers exist?" — returned coherent answer. **Known issue:** `_build_schema_context` dumps up to 200 doctypes × 20 fields into every query prompt. On large ERPNext installs this can blow context windows or burn ~5-10k input tokens per query. Hard cap at 100 results per query (correct by design). |
| `services.vision.extract()` | Single image | ⚠️ Untested | Anthropic vision API call path looks correct. `_load_images` and `_safe_resolve_path` guard against traversal. No live verification with a real file. |
| `services.vision.extract()` | PDF → multi-page | ⚠️ Untested | Depends on `pdf2image` (installed) + `pdftoppm` (installed at `/usr/bin/pdftoppm`). Code converts up to `max_pdf_pages` (default 10) pages to PNG. Untested end-to-end. |

---

## Doctypes

| DocType | Type | Migration | Controller | Status |
|---|---|---|---|---|
| AI Settings | Single | ✅ | Stub (6 lines) | ✅ Working. Now ships with inline field descriptions + HTML intro panel. |
| AI Provider | Regular | ✅ | `get_credential()` + `fetch_models()` whitelisted | ✅ Working for type=Anthropic and type=Claude Code. |
| AI Provider Model | Child Table | ✅ | Stub | ✅ Working. Populated by `fetch_models()`. |
| AI Prompt Template | Regular | ✅ | Stub | ⚠️ Untested. Doctype exists, no template ever created and exercised through `generate(template=...)`. |
| AI Call Log | Regular | ✅ | Stub | ✅ Working. Captures status, tokens, cost, latency, error_message. 90-day auto-cleanup via `default_log_clearing_doctypes`. |

---

## Methods on AI Provider Doctype

| Method | Status | Notes |
|---|---|---|
| `get_credential()` | ✅ | Returns `("", "Claude Code")` for CLI, otherwise password from `api_key` or `auth_token` field. |
| `fetch_models()` (whitelisted button) | ✅ for Claude Code (hardcoded list) | Anthropic+API Key path attempts a real `client.models.list()` call but falls back to a hardcoded `ANTHROPIC_MODELS` list on any exception. **Known gap:** OAuth (Auth Token) auth always falls back to the hardcoded list — the `models.list()` API doesn't accept OAuth tokens. Acceptable for now since the hardcoded list covers Opus/Sonnet/Haiku. |

---

## Missing Pieces (Not Yet Shipped)

These are tracked but intentionally not in this release:

1. **Tests.** Zero `test_*.py` files. Recommended next: `tests/test_ai_client.py` (mock provider), `tests/test_query_engine.py` (schema bounds), `tests/test_anthropic_provider.py` (OAuth header injection).
2. **Realtime listener helper (JS).** README references `frappe.realtime.on("ai_call_complete", cb)` but no shared JS helper ships. Each consumer app currently has to wire its own listener.
3. **Sample prompt templates.** No fixtures. Consumer apps create their own.
4. **Patches.** `patches.txt` is empty. Fine for v1, but no migration safety nets if schema changes.
5. **Per-app rate limiting.** Spec says "track usage only, no local enforcement" — by design. If a consumer app starts hammering, you currently have to disable the provider or remove the caller manually.
6. **OpenAI / Google / Custom providers.** Removed from Select; not on the near-term roadmap. Anthropic family covers the current consumer needs.

---

## Verification Recipes

### Smoke test generate (sync)

```python
# bench --site erpnext.local console
from ai_interface.services.generator import generate
print(generate(prompt="Say hi in 5 words", sync=True, calling_app="smoke"))
```

Expected: a short string. Check **AI Call Log** for status=Completed, latency_ms > 0, cost > 0.

### Smoke test query (sync)

```bash
bench --site erpnext.local execute ai_interface.services.query_engine.query \
  --args '["How many active customers exist?"]' \
  --kwargs "{'sync': True, 'calling_app': 'qtest'}"
```

Expected: coherent NL answer mentioning a count. Schema dump phase is silent.

### Smoke test vision

```python
# Upload an image to Frappe Files first, then:
from ai_interface.services.vision import extract
print(extract(file_url="/files/invoice.png", sync=True, calling_app="vtest"))
```

Expected: extracted text. Check provider hits — should go through `vision()` not `chat()`.

### Smoke test async path

```python
from ai_interface.services.generator import generate
log_name = generate(prompt="Long form thing", calling_app="async_test")
print("Log name:", log_name)
# Then poll:
import frappe, time
for _ in range(30):
    s = frappe.get_value("AI Call Log", log_name, "status")
    print("Status:", s)
    if s in ("Completed", "Failed"): break
    time.sleep(2)
```

Expected: status transitions Queued → Running → Completed.

---

## How to Mark Something Working

When you verify a ⚠️ path:
1. Re-run the smoke recipe.
2. Open the resulting AI Call Log row, confirm tokens/cost/latency populated.
3. Edit this file: change ⚠️ to ✅ with `Verified YYYY-MM-DD` in the notes.
4. Commit with `docs: verify <feature> in STATUS`.

# AI Interface — Handbook

The bench-wide AI layer. Every app calls one contract; whichever provider is configured is
the one that runs. Nothing about a vendor lives in code.

- **App:** `ai_interface` · **Module:** `Ai Interface` · **Doctypes:** 8
- **Dashboard:** `/app/ai-command-center`

---

## 1. What this is

Consumer apps — Excom, DocPulse, WarehouseSuite, BNS, Insightly — import one function and
never name a vendor. Swapping Anthropic for Sarvam is a configuration change, not a code
change across five repositories.

| Guarantee | In practice |
|---|---|
| **Vendor-agnostic** | A provider type is a database record holding an adapter path, endpoints, auth header and currency. Any vendor speaking the OpenAI chat-completions format needs **zero new Python**. |
| **Fully attributed** | Every call records which app, module, doctype, document and action caused it — so cost and failures trace to a cause, not just a total. |
| **Async by default** | Calls enqueue and return an `AI Call Log` name. Pass `sync=True` for inline text. |

### Directory map

```
ai_interface/
  providers/                  # wire protocols
    base.py                   # BaseProvider ABC, ProviderResponse, ProviderHTTPError
    openai_compatible.py      # generic adapter — most vendors need only this
    anthropic_provider.py     # Anthropic Messages API
    claude_code_provider.py   # shells out to the `claude -p` CLI
    __init__.py               # resolver: reads AI Provider Type, imports adapter_path
  services/                   # the public API consumer apps import
    ai_client.py              # dispatcher: budget, routing, execute, log, health
    router.py                 # capability routing + fallback chain
    budget.py                 # spend caps
    generator.py  vision.py  query_engine.py
  api/dashboard.py            # AI Command Center endpoints
  ai_interface/doctype/       # the 8 doctypes
  ai_interface/page/ai_command_center/
  patches/v1_0/
```

> **Rule:** consumer apps import from `services/` only. Never from `providers/` — that is
> the layer whose whole purpose is to be swappable.

---

## 2. How a call flows

```
  your app
     │  generate() / extract() / query()
     ▼
  call_ai()                                            dispatcher
     1  render prompt        template + context → text
     2  BUDGET CHECK         over cap? Warn or Block ──────┐
     3  BUILD ROUTING CHAIN  caller → template → rules → default
        └ capability filter  vision? tools? context window?
     4  create AI Call Log   status Queued, attribution set
     │                                                     │
     │  sync=False → frappe.enqueue           refused ─────┘
     ▼                                        (nothing spent)
  _execute_ai_call()                                       worker
     5  for each step in chain:
          adapter.chat() / .vision()
          ✓ success  → cost, currency, base_cost, health Healthy
          ✗ retryable (429 / timeout / 5xx) → next step
          ✗ auth or config → stop now, never retry
     6  publish_realtime + health counters
```

Validation runs **before** anything is queued, so a call that cannot succeed fails
immediately and costs nothing.

---

## 3. First-time setup

1. **Install and migrate** — seeds four `AI Provider Type` records and the `AI Manager` role.
   ```bash
   bench --site your-site.local install-app ai_interface
   bench --site your-site.local migrate
   ```
2. **Set the base currency** — *AI Settings → Currency*. Pick it before logging real
   traffic; changing it later does not retro-convert history.
3. **Create an AI Provider** — name, provider type, API key, save. Then **Fetch Models**
   (free). Rates and capabilities fill in automatically where the pricing catalog covers
   the vendor.
4. **Click Test Connection** — one real call. Green means the credential works *for chat*;
   Fetch Models succeeding is not proof of that.
5. **Set the default provider** — *AI Settings*. Leave **Default Model** blank so the
   router picks the cheapest capable model per call.
6. **Turn on spend caps** — start with **Warn**.

> **Anthropic keys:** use a **workspace-scoped** key. An *organization* key is rejected
> with `400 — not scoped to a workspace`, because it needs an `anthropic-workspace-id`
> header this app does not send.

---

## 4. Adding any provider

### Path A — the vendor speaks OpenAI format (no code)

Covers OpenAI, Sarvam, Groq, Together, Mistral, DeepSeek, OpenRouter, Ollama, vLLM.
Create an `AI Provider Type` record:

| Field | Value | Notes |
|---|---|---|
| `adapter_path` | `…openai_compatible.OpenAICompatibleProvider` | The default. Leave it. |
| `default_base_url` | `https://api.vendor.com` | Trailing slash is stripped. |
| `chat_path` | `/v1/chat/completions` | Where the vendor accepts chat. |
| `models_path` | `/v1/models` | Blank ⇒ no discovery; add models by hand. |
| `auth_header` | `Authorization` | Sarvam uses `api-subscription-key`. |
| `auth_prefix` | `Bearer ` | Blank for raw-key headers. Note the trailing space. |
| `default_currency` | `USD` | What the vendor actually bills in. |
| `unsupported_params` | `stream_options, service_tier` | Stripped before sending. |

**Worked example — Sarvam**

```
Type Name          Sarvam
Adapter Path       ai_interface.providers.openai_compatible.OpenAICompatibleProvider
Default Base URL   https://api.sarvam.ai
Chat Path          /v1/chat/completions
Models Path        /v2/models
Auth Header        api-subscription-key
Auth Prefix        (empty)
Default Currency   INR
Unsupported        stream_options, max_completion_tokens, service_tier
```

Then: New AI Provider → type *Sarvam* → paste `sk_…` → Fetch Models → type the ₹ rates →
Test Connection. **No Python was written.**

### Path B — a different wire protocol (one file)

1. Subclass `BaseProvider` in `providers/`, implementing `chat()`, `vision()` and
   `fetch_models()`. Read vendor config from `self.config`.
2. Create an `AI Provider Type` whose `adapter_path` points at your class. It is validated
   on save, so a typo fails then — not during a call at 2am.

Because registration is a record rather than a line in `providers/__init__.py`, another app
can ship its own adapter and register it with a fixture, without editing this one.

---

## 5. Using it from your app

### generate() — text

```python
from ai_interface.services.generator import generate

log_name = generate(
    prompt="Summarise this thread in two sentences.",
    calling_app="excom",
    reference_doctype="Excom Thread",
    reference_name=thread.name,
    action="summarize_thread",
)

text = generate(template="email_draft", context={"customer": "Acme"},
                sync=True, calling_app="excom")
```

### extract() — images and PDFs

```python
from ai_interface.services.vision import extract

log_name = extract(
    file_url="/private/files/invoice.pdf",
    prompt="Extract supplier, date, line items and grand total as JSON.",
    calling_app="docpulse",
    reference_doctype="Purchase Invoice",
    reference_name=pi.name,
    action="extract_invoice",
)
```

PDFs are rasterised up to `max_pdf_pages`. Vision is implied — do not pass `needs`.

### query() — natural language over ERPNext data

```python
from ai_interface.services.query_engine import query

answer = query("How many customers ordered this month?",
               calling_app="insightly", sync=True)
```

> Costs **two** calls: `query_generation` and `answer_synthesis`. Both are logged and
> attributed to your `calling_app`.

### Shared arguments

| Argument | Type | Purpose |
|---|---|---|
| `calling_app` | str | Which app caused this. Top level of every cost breakdown — always pass it. |
| `reference_doctype` | str | Doctype served. The module is derived from it automatically. |
| `reference_name` | str | The specific document, so a cost traces to one record. |
| `action` | str | What the call does, e.g. `extract_invoice`. |
| `needs` | list[str] | `["vision"]`, `["tools"]`. State capabilities, never a model name. |
| `sync` | bool | Inline execution returning text. Default `False` → returns a log name. |
| `provider` / `model` | str | Hard override. Wins over every rule — use sparingly. |
| `template` | str | An `AI Prompt Template` name; combine with `context`. |
| `max_tokens` | int | Ceiling, not a charge. Raise it for reasoning models. |

### Collecting an async result

```javascript
frappe.realtime.on("ai_call_complete", (d) => {
    if (d.log === log_name) frappe.db.get_value("AI Call Log", d.log, "output_text");
});
frappe.realtime.on("ai_call_failed", (d) => { /* d.error */ });
```

---

## 6. Doctype reference

### AI Provider Type — *autoname: `type_name`*

**The registry, as data.** One record per vendor protocol. This replaced a hardcoded
Python dictionary, and is why a new vendor is configuration.

| Field | Type | For |
|---|---|---|
| `type_name` | Data, reqd | Vendor name shown in the AI Provider form. |
| `enabled` | Check (1) | Disabling refuses every call routed through this type. |
| `adapter_path` | Data, reqd | Dotted path to a `BaseProvider` subclass. Validated on save. |
| `default_base_url` | Data | Vendor API root; a provider may override. |
| `chat_path` | Data | Default `/v1/chat/completions`. |
| `models_path` | Data | Blank ⇒ no discovery. |
| `no_credential` | Check (0) | Adapter authenticates itself (a local CLI). Hides key fields. |
| `auth_header` | Data | Default `Authorization`. |
| `auth_prefix` | Data | Default `Bearer `; blank for raw-key headers. |
| `default_currency` | Link Currency | What the vendor bills in. |
| `pricing_source_url` | Data | JSON catalog for rate prefill. **Clear to disable all outbound lookups.** |
| `catalog_prefix` | Data | Prefix tried when matching ids in that catalog. |
| `unsupported_params` | Small Text | Request keys the vendor rejects; stripped before sending. |

### AI Provider — *autoname: `provider_name`*

One configured account. You may have several of the same type.

| Field | Type | For |
|---|---|---|
| `provider_name` | Data, reqd | Also the record name; used in rules and logs. |
| `provider_type` | Link, reqd | Which wire protocol and endpoints. |
| `enabled` | Check (1) | Disabled providers are skipped by the router. |
| `auth_type` | Select | `API Key` or `Auth Token` (OAuth). |
| `api_key` | Password | Encrypted at rest. |
| `auth_token` | Password | OAuth token from `claude setup-token`. |
| `api_base_url` | Data | Per-account override of the type's base URL. |
| `currency` | Link Currency | What this account is billed in. |
| `exchange_rate` | Float | Manual rate to base. `0` ⇒ use ERPNext Currency Exchange at call time. |
| `models` | Table | The catalog — see below. |
| `health_status` | Select, ro | `Unknown / Healthy / Degraded / Down`. |
| `last_success` | Datetime, ro | When it last answered. |
| `last_failure` | Datetime, ro | When it last did not. |
| `consecutive_failures` | Int, ro | Reset to 0 on success. Threshold ⇒ Down. |
| `last_error` | Small Text, ro | Classified type plus the vendor's message. |

**Buttons.** *Fetch Models* — free; discovers ids and **merges**, never wiping hand-entered
rates. *Test Connection* — one small billed call on the cheapest enabled model; sets health.

### AI Provider Model — *child of AI Provider*

| Field | Type | For |
|---|---|---|
| `model_id` | Data, reqd | Exactly what the vendor's API expects. |
| `label` | Data | Human-readable name from discovery. |
| `enabled` | Check (1) | Cleared when a vendor stops returning it. **Not deleted** — old logs still cost against it. |
| `supports_vision` | Check | Gate for image calls. |
| `supports_tools` | Check | Gate for `needs=["tools"]`. |
| `context_window` | Int | Max input tokens; oversized prompts refused before queueing. |
| `priority` | Int (0) | Lower is preferred when auto-picking. Ties break on cost. |
| `cost_per_input_token` | Float | In the **provider's** currency. Per-million price ÷ 1,000,000. |
| `cost_per_output_token` | Float | Same. |
| `pricing_source` | Select, ro | `Manual` (never overwritten) · `Catalog` · `Unpriced`. |

> Sarvam lists `gemma4` at ₹36.6 per 1M input tokens → enter `0.0000366`. Saving stamps it
> `Manual`, and no future Fetch Models will touch it.

### AI Settings — *Single*

| Field | Type | For |
|---|---|---|
| `default_provider` | Link, reqd | Runs calls when nothing more specific matches. |
| `default_model` | Data, reqd | Pin a model, or leave blank to let the router pick. |
| `api_call_timeout` | Int (120) | Seconds before giving up. Raise to 300 for long PDFs. |
| `max_output_tokens` | Int (4096) | Default ceiling per call. |
| `max_pdf_pages` | Int (10) | Pages rasterised by `extract()`; each ≈ 1,500 input tokens. |
| `enable_logging` | Check (1) | Controls **payload retention**. The log row is always written. |
| `base_currency` | Link, reqd | The one currency all spend is normalised into. |
| `routing_rules` | Table | See below. |
| `enable_fallback` | Check (1) | Try the next rule on retryable failures only. |
| `enable_budget` | Check (0) | Master switch for spend caps. |
| `daily_budget` | Currency (0) | 0 ⇒ no daily cap. |
| `monthly_budget` | Currency (0) | 0 ⇒ no monthly cap. Calendar month. |
| `budget_action` | Select (Warn) | `Warn` notifies; `Block` refuses. |
| `budget_alert_threshold` | Int (80) | Percent of a cap that triggers a notification. |
| `app_budgets` | Table | See below. |
| `failure_threshold` | Int (3) | Consecutive failures before a provider reads as Down. |

### AI Routing Rule — *child of AI Settings*

| Field | Type | For |
|---|---|---|
| `enabled` | Check (1) | Turn a rule off without deleting it. |
| `function_type` | Select | `Generation` / `Vision` / `Query`. Blank matches any. |
| `calling_app` | Data | Restrict to one app. Blank matches any. |
| `provider` | Link, reqd | Which provider serves the match. |
| `model` | Data | **Leave blank** to auto-pick the cheapest capable model. |
| `priority` | Int (0) | Lower runs first; the rest become fallbacks. |

### AI App Budget — *child of AI Settings*

| Field | Type | For |
|---|---|---|
| `calling_app` | Data, reqd | Must match the `calling_app` your code passes. |
| `daily_budget` | Currency | 0 ⇒ no daily cap for this app. |
| `monthly_budget` | Currency | 0 ⇒ no monthly cap for this app. |
| `budget_action` | Select | Blank falls back to the global setting. |

### AI Prompt Template — *autoname: `template_name`*

| Field | Type | For |
|---|---|---|
| `template_name` | Data, reqd | What you pass as `template=`. |
| `category` | Select, reqd | `Generation / Vision / Query / System`. |
| `body` | Code, reqd | Jinja, rendered against `context`. |
| `variables` | Small Text | JSON list of expected names. |
| `provider_override` | Link | Force a provider. Beats routing rules. |
| `model_override` | Data | Force a model. |

### AI Call Log — *autoname: hash*

One row per call: the audit trail, the async delivery channel, and the sole data source for
the dashboard. All fields read-only.

**Identity & routing** — `status` (`Queued → Running → Completed / Failed`),
`function_type`, `provider` (who *actually served it*, updated on fallback), `model`,
`is_sync`, `attempts` (>1 ⇒ fell back), `prompt_template`.

**Attribution** — `calling_app`, `module` (derived from the reference doctype),
`reference_doctype`, `reference_name`, `action`, `user`.

**Payload, cost, outcome**

| Field | For |
|---|---|
| `input_text` / `output_text` | Retained only while `enable_logging` is on. |
| `error_type` | `Auth · Rate Limit · Timeout · Invalid Response · Provider Error · Config Error · File Error · Unknown`. Derived from HTTP status, not error text. |
| `error_message` | Full trail. **Kept even on success** if an earlier provider failed first. |
| `input_tokens` / `output_tokens` | As reported by the vendor. |
| `currency` | What the serving provider bills in. |
| `cost` | Billed amount, in that currency. |
| `exchange_rate` | Rate at call time, snapshotted. |
| `base_cost` | **What every dashboard total sums.** |
| `base_currency` | The base at the time of the call. |
| `latency_ms` | Wall-clock for the attempt that answered. |

**Indexes** (`on_doctype_update()`): `(status, creation)`, `(calling_app, creation)`,
`(module, creation)`, `(reference_doctype)`, `(provider, model)`.

---

## 7. How routing decides

**Pass 1 — candidates, in order:** caller override → template override → routing rules
(most-specific first: app + function beats function beats catch-all; priority orders
equally specific rules) → AI Settings default.

Specificity is compared **before** priority, so a rule written for one app is never
shadowed by a lower-priority catch-all.

**Pass 2 — capability filter**

| Requirement | Comes from | Checked against |
|---|---|---|
| vision | `needs=["vision"]`, or implied by `images` | `supports_vision` |
| tools | `needs=["tools"]` | `supports_tools` |
| min_context | Prompt length ÷ 4 chars per token | `context_window` |

A model named explicitly that cannot meet the requirement **throws**. A model being
auto-picked is simply excluded; survivors sort by `priority`, then combined token cost.

**Fallback**

| Error class | Behaviour | Why |
|---|---|---|
| Rate Limit | Try next provider | Transient; another vendor is not throttled. |
| Timeout | Try next provider | Transient. |
| Provider Error | Try next provider | A 5xx is the vendor's problem. |
| **Auth** | **Stop immediately** | A bad key fails the same way every time. |
| **Config Error** | **Stop immediately** | Misconfiguration is not fixed by repetition. |

Set `enable_fallback = 0` to make every chain a single entry.

---

## 8. Cost & currency

Providers do not all bill in the same currency, so cost is stored twice.

| Field | Holds | Used for |
|---|---|---|
| `cost` + `currency` | What the provider billed, in its own currency | Reconciling a vendor invoice |
| `base_cost` + `base_currency` | The same amount converted once, at call time | **Every dashboard total** |
| `exchange_rate` | The rate used, snapshotted | History a later rate change cannot rewrite |

**Rate resolution:** same currency ⇒ 1.0 → manual `exchange_rate` on the provider →
ERPNext Currency Exchange → **nothing usable ⇒ 0, never a silent 1.0.**

> A silent 1.0 would treat ₹100 as $100. Instead the call is left visibly unconverted and
> the dashboard raises *"N calls are missing from every cost total"*.

**Display currency** — the dashboard picker converts `base_cost` at read time. Switching
between ₹ and $ is a lens over the same history, never a rewrite.

---

## 9. Spend caps

Checked before a call is enqueued, so a blocked call costs nothing. Measured against
`base_cost`, so mixed-currency providers sit under one budget.

- **Scopes:** global and per calling app; daily and monthly (calendar).
- **Warn** notifies System Managers and lets the call through.
- **Block** refuses before it costs anything. If any applicable rule says Block, Block wins.
- **Alerts** fire at `budget_alert_threshold`, throttled to one per scope per day.

Start with **Warn**. A hard block that silently breaks five apps' AI features is a worse
first experience than an alert.

---

## 10. Provider health

| Status | Meaning |
|---|---|
| `Unknown` | Never called. Click Test Connection. |
| `Healthy` | Last call succeeded; failure counter 0. |
| `Degraded` | Failing, but below `failure_threshold`. |
| `Down` | Consecutive failures reached the threshold. |

Updated by every call on both paths, written with `db_set` so health bookkeeping can never
itself fail a call. Surfaced in the AI Provider list, the form header, and the dashboard
health strip.

**Test Connection vs Fetch Models:** Fetch Models proves nothing about chat. A credential
can list models happily and still be rejected for completions, or have no balance.

---

## 11. The dashboard

`/app/ai-command-center` — System Manager and AI Manager. Every endpoint returns
pre-aggregated rows; nothing fetches log documents, so it stays O(groups) not O(calls).

| Panel | Answers |
|---|---|
| Health strip | Which providers are answering right now. |
| Budget strip | Spend against caps, with an over-budget state. |
| KPIs | Spend, calls, success rate, p95 latency, deltas vs previous window. |
| Insights | Cost spikes, prompt bloat, repeated prompts, failure clusters, unconverted spend, fallback activity. |
| Spend over time | Stacked daily cost, regroupable by app/module/function/model/provider. |
| Attribution | Drill-down: app → module → doctype → action. |
| Reliability | Daily success rate, per-model cost per call. |
| Failures | By error type, module × day heatmap, recent rows with Retry. |

Endpoints in `ai_interface.api.dashboard`: `get_summary`, `get_timeseries`,
`get_attribution`, `get_reliability`, `get_failures`, `get_insights`,
`get_provider_health`, `get_budget_status`, `get_filter_options`, `retry_call`.

---

## 12. Troubleshooting

Every entry here was hit while building and testing this app.

| Symptom | Cause & fix |
|---|---|
| `400 — This API key is not scoped to a workspace` | An Anthropic *organization* key. Use a **workspace** key. |
| `This endpoint is currently in beta and not available` | Sarvam `/v2/chat/completions` is gated per account. Set `chat_path` to `/v1/chat/completions`; `/v2/models` discovery still works. |
| *"used its entire max_tokens budget on reasoning"* | A reasoning model such as `sarvam-105b` fills `reasoning_content` before `content`. Raise `max_tokens` — it is a ceiling, not a charge. |
| Cost logs as 0 on completed calls | The model row is `Unpriced`. Enter rates on the provider; they stamp `Manual` and survive re-fetch. |
| *"N calls are missing from every cost total"* | No exchange rate into base. Set `exchange_rate` on the provider or add a Currency Exchange record. |
| *"has no enabled model that supports images"* | Working as designed — refused before queueing. Enable a vision model or route Vision elsewhere. |
| `Enter the API Key for … before using it` | Provider saved without a credential. |
| *"Global monthly AI budget exhausted"* | A cap with `Block`. Raise it, switch to Warn, or wait. |
| Page 404 after install | `bench --site … clear-cache`, then hard-refresh. |
| Rates vanished after Fetch Models | Should be impossible — merge preserves `Manual` rows. Report it as a bug. |

---

## 13. Operations

```bash
bench --site <site> migrate           # after any doctype change
bench --site <site> clear-cache       # after editing page JS or CSS
bench --site <site> console           # interactive, with frappe loaded

cd apps/ai-interface
ruff check ai_interface/ && ruff format ai_interface/
```

### Patches

| Patch | Does |
|---|---|
| `create_ai_manager_role` | Creates the read-only `AI Manager` role. |
| `create_default_provider_types` | Seeds Anthropic, Claude Code, OpenAI, Sarvam. Skips existing. |
| `backfill_base_cost` | Copies `cost` into `base_cost` at rate 1 for pre-currency history; sets base to USD. |
| `sarvam_chat_path_v1` | Moves seeded Sarvam records off the beta-gated `/v2`, leaving deliberate overrides alone. |

### Roles

- **System Manager** — full access. Required for Fetch Models and Test Connection.
- **AI Manager** — read, report and export on AI Call Log; can open the dashboard. No
  configuration rights.

### Clearing test data

```sql
bench --site <site> mariadb
DELETE FROM `tabAI Call Log`;
```

This removes real history as well as test rows. There is no undo, and the dashboard has no
other data source.

### Known gaps

- **No automated test suite.** Behaviour has been verified by hand and against live
  accounts, but nothing guards it against the next change.
- **Test Connection calls are not written to AI Call Log**, so that (small) spend is
  untracked.
- **Scheduled health checks are not implemented** — health updates only when real calls run.
- **Sarvam's non-chat APIs** (TTS, STT, translate, transliterate) do not fit the
  chat/vision contract and would need their own function type.

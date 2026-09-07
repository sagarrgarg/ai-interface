# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

AI Interface — the AI infrastructure layer for the Frappe bench. Provides white-labeled functions (vision extraction, text generation, NL→data queries) that any app in the bench can import and call without knowing which AI provider is underneath.

Part of frappe-bench-new (`/home/ubuntu/frappe-bench-new/`), site `erpnextkgopl.local`, port 8001.
Required apps: `frappe`, `erpnext`.

## Commands

```bash
# Run bench (from bench root, not app dir)
cd /home/ubuntu/frappe-bench-new && bench start

# Migrate after doctype changes
bench --site erpnextkgopl.local migrate

# Clear cache
bench --site erpnextkgopl.local clear-cache

# Console (interactive Python with frappe context)
bench --site erpnextkgopl.local console

# Lint and format
ruff check ai_interface/ && ruff format ai_interface/

# Run tests for this app
bench --site erpnextkgopl.local run-tests --app ai_interface

# Run a single test
bench --site erpnextkgopl.local run-tests --module ai_interface.ai_interface.doctype.<doctype_name>.test_<doctype_name>

# Pre-commit (ruff + eslint + prettier)
pre-commit run --all-files
```

## Code Style

- **Python**: ruff — tabs, 110 char lines, double quotes, Python 3.10+ target
- **JS**: eslint + prettier — tabs, `frappe` globals assumed
- **JSON**: spaces (indent 2)
- Pre-commit hooks enforce all of the above

## Architecture

```
ai_interface/
  providers/            # Multi-provider abstraction
    base.py             # BaseProvider ABC + ProviderResponse + ProviderHTTPError
    openai_compatible.py # Generic adapter for any OpenAI-format vendor
    anthropic_provider.py
    claude_code_provider.py
    __init__.py         # Resolver - reads AI Provider Type, imports adapter_path
  services/             # White-labeled API — what consumer apps import
    ai_client.py        # Core dispatcher: resolve provider, enqueue/execute, log
    generator.py        # generate(prompt/template, context) → text
    vision.py           # extract(file_url, prompt) → extracted text
    query_engine.py     # query(question) → NL answer from ERPNext data
  ai_interface/         # Frappe module — doctypes
    doctype/
      ai_settings/      # Single: default provider, model, timeouts, base currency
      ai_provider/      # One per provider: API key, models, base URL
      ai_provider_type/ # Vendor wire config: adapter path, endpoints, auth header
      ai_provider_model/ # Child table: model ID, capabilities, token rates
      ai_prompt_template/ # Versioned Jinja templates for prompts
      ai_call_log/      # Full audit trail: prompt, response, tokens, cost
  hooks.py
  patches/
```

## Key Design Decisions

- **Async by default**: All calls go through `frappe.enqueue()`, return an AI Call Log name (job ID). Consumer listens via `frappe.realtime` or polls. Pass `sync=True` for immediate response.
- **Nothing about a vendor lives in code.** A provider type is an **AI Provider Type** record holding the adapter path, base URL, chat/models paths, auth header and prefix, billing currency and rejected parameters. There is no registry dict and no static model or price table - adding a vendor that speaks the OpenAI format (Sarvam, Groq, Together, Mistral, DeepSeek, OpenRouter, Ollama, vLLM) is **configuration, not a commit**. Only a genuinely different wire protocol needs a new adapter class, and even that is registered by a record, so another app can ship its own.
- **Model catalogs are discovered, never hardcoded.** `fetch_models` reads the listing endpoint, then **merges** by `model_id`: rates a human typed are stamped `Manual` and never overwritten, models the vendor drops are disabled rather than deleted (old call logs still cost against them), and blanks are prefilled from the optional **Pricing Source URL** - clear that field and no outbound catalog call is made at all. Anything still unpriced is flagged, so a zero cost reads as *unknown*, not *free*.
- **Errors classify by HTTP status**, not by matching vendor error text, so a reworded provider message cannot silently reshuffle the dashboard.
- **Cost is stored twice, on purpose.** `cost` is what the provider billed, in *its* currency (Sarvam INR, Anthropic USD). `base_cost` is the same amount converted once, at call time, into the single `base_currency` from AI Settings, with the rate snapshotted onto the row. Every dashboard total aggregates `base_cost`; the display-currency picker converts that at read time, so switching the view is a lens over history and never a rewrite of it. A rate that cannot be resolved stores **0, never a silent 1.0** — treating ₹100 as $100 is the failure this design exists to prevent — and those calls are surfaced as *"missing from every cost total"* rather than quietly under-reported.
- **Resolution chain**: caller override → template override → AI Settings default (for both provider and model).
- **Consumer pattern**: `from ai_interface.services.generator import generate` — never import from `providers/`.

## Consumer Integration Example

```python
# Async (default)
from ai_interface.services.generator import generate
log_name = generate(template="email_draft", context={...}, calling_app="excom")

# Sync
text = generate(prompt="Summarize this", sync=True, calling_app="excom")

# Vision
from ai_interface.services.vision import extract
log_name = extract(file_url="/files/invoice.pdf", calling_app="docpulse")

# Query
from ai_interface.services.query_engine import query
log_name = query("How many customers placed orders this month?", calling_app="insightly")
```

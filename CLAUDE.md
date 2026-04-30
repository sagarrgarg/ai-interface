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
    base.py             # BaseProvider ABC + ProviderResponse dataclass
    anthropic_provider.py
    __init__.py         # Provider registry (type string → class)
  services/             # White-labeled API — what consumer apps import
    ai_client.py        # Core dispatcher: resolve provider, enqueue/execute, log
    generator.py        # generate(prompt/template, context) → text
    vision.py           # extract(file_url, prompt) → extracted text
    query_engine.py     # query(question) → NL answer from ERPNext data
  ai_interface/         # Frappe module — doctypes
    doctype/
      ai_settings/      # Single: default provider, model, timeouts
      ai_provider/      # One per provider: API key, models, base URL
      ai_provider_model/ # Child table: model ID, vision support, token costs
      ai_prompt_template/ # Versioned Jinja templates for prompts
      ai_call_log/      # Full audit trail: prompt, response, tokens, cost
  hooks.py
  patches/
```

## Key Design Decisions

- **Async by default**: All calls go through `frappe.enqueue()`, return an AI Call Log name (job ID). Consumer listens via `frappe.realtime` or polls. Pass `sync=True` for immediate response.
- **Provider abstraction**: `BaseProvider` ABC with `chat()` and `vision()` methods. Adding a provider = one file + one registry line. Anthropic ships first.
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

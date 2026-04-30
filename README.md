# AI Interface

AI infrastructure layer for Frappe. Provides white-labeled AI capabilities (vision extraction, text generation, natural language queries) to any app in the bench — without exposing which provider is underneath.

## Features

- **Multi-provider abstraction** — Anthropic API, Claude Code CLI (OAuth tokens), extensible to OpenAI/Gemini
- **Vision extraction** — Extract text and structured data from images and PDFs
- **Text generation** — General-purpose generation via raw prompts or versioned Jinja templates
- **Query engine** — Natural language questions → ERPNext data → natural language answers
- **Async by default** — All calls go through `frappe.enqueue`, return a job ID. Opt into sync with `sync=True`
- **Full audit trail** — Every API call logged with prompt, response, tokens, cost, latency
- **Prompt template library** — Versioned, editable in Frappe UI, with optional provider/model overrides

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/sagarrgarg/ai-interface --branch main
bench --site your-site.local install-app ai_interface
bench --site your-site.local migrate
```

### System dependencies

```bash
# Required for PDF → image conversion
sudo apt install poppler-utils
```

### Python dependencies (installed automatically)

- `anthropic` — Anthropic SDK
- `pdf2image` — PDF to image conversion
- `Pillow` — Image handling

## Setup

1. **Create an AI Provider** — Go to `AI Provider` doctype
   - **Claude Code** (recommended): Set provider type to "Claude Code". No API key needed — uses your CLI auth (`claude setup-token`)
   - **Anthropic API**: Set provider type to "Anthropic", auth type to "API Key", paste your `sk-ant-api03-*` key from console.anthropic.com
2. **Click "Fetch Models"** — Populates available models with pricing
3. **Configure AI Settings** — Set default provider, default model, timeout, and logging preferences

## Usage

Consumer apps import from `ai_interface.services.*`. They never touch `providers/` directly.

### Text Generation

```python
from ai_interface.services.generator import generate

# Async (default) — returns AI Call Log name
log_name = generate(
    template="email_reply_draft",
    context={"thread": thread.as_dict(), "tone": "professional"},
    calling_app="excom",
)
# Listen: frappe.realtime.on("ai_call_complete", callback)
# Or poll: frappe.get_value("AI Call Log", log_name, ["status", "output_text"])

# Sync — returns response text directly
reply = generate(
    prompt="Summarize this invoice in 2 sentences",
    sync=True,
    calling_app="excom",
)
```

### Vision Extraction

```python
from ai_interface.services.vision import extract

# Extract text from a PDF (auto-converts pages to images)
log_name = extract(
    file_url="/private/files/invoice.pdf",
    calling_app="docpulse",
)

# Sync with custom prompt
text = extract(
    file_url="/files/label.png",
    prompt="Extract the product name, batch number, and expiry date as JSON",
    sync=True,
    calling_app="warehousesuite",
)
```

### Natural Language Query

```python
from ai_interface.services.query_engine import query

# Ask questions about ERPNext data in plain English
answer = query(
    "How many customers placed orders this month?",
    sync=True,
    calling_app="insightly",
)
```

### Override Provider/Model Per Call

```python
# Any service function accepts provider and model overrides
text = generate(
    prompt="Translate to Hindi",
    provider="My OpenAI Provider",
    model="gpt-4o",
    sync=True,
)
```

## Architecture

```
ai_interface/
├── providers/                    # Multi-provider abstraction
│   ├── base.py                   # BaseProvider ABC + ProviderResponse
│   ├── anthropic_provider.py     # Anthropic API (api_key or auth_token)
│   ├── claude_code_provider.py   # Claude Code CLI (oauth via setup-token)
│   └── __init__.py               # Provider registry
├── services/                     # White-labeled API for consumer apps
│   ├── ai_client.py              # Core dispatcher (call_ai → enqueue/execute → log)
│   ├── generator.py              # generate(prompt/template, context)
│   ├── vision.py                 # extract(file_url, prompt)
│   └── query_engine.py           # query(question) → NL answer
├── ai_interface/doctype/         # Frappe doctypes
│   ├── ai_settings/              # Single: default provider, model, timeouts
│   ├── ai_provider/              # One per provider: credentials, models
│   ├── ai_provider_model/        # Child table: model ID, costs, vision support
│   ├── ai_prompt_template/       # Versioned Jinja templates
│   └── ai_call_log/              # Full audit trail per API call
└── hooks.py
```

### Call Flow

```
Consumer app calls generate() / extract() / query()
    → ai_client.call_ai()
        → Resolve provider + model (caller → template → settings)
        → Create AI Call Log (status=Queued)
        → frappe.enqueue(_execute_ai_call)  [or inline if sync=True]
            → Load provider class from registry
            → Call provider.chat() / provider.vision()
            → Update AI Call Log (status=Completed, tokens, cost, latency)
            → frappe.publish_realtime("ai_call_complete")
```

### Adding a New Provider

1. Create `providers/my_provider.py` implementing `BaseProvider`
2. Add one line to `providers/__init__.py`: `register_provider("MyProvider", MyProviderClass)`
3. Add "MyProvider" to the provider_type Select options in `ai_provider.json`

## DocTypes

| DocType | Type | Purpose |
|---------|------|---------|
| AI Settings | Single | Default provider, model, timeout, logging toggle |
| AI Provider | Regular | One per provider — credentials, base URL, models |
| AI Provider Model | Child table | Model ID, label, vision support, token costs |
| AI Prompt Template | Regular | Versioned Jinja templates with category + overrides |
| AI Call Log | Regular | Full audit: status, prompt, response, tokens, cost |

## Contributing

This app uses `pre-commit` for code formatting and linting:

```bash
cd apps/ai_interface
pre-commit install
```

Tools: ruff, eslint, prettier, pyupgrade

### Code style

- Python: tabs, 110 char lines, double quotes (ruff)
- JSON: spaces, indent 2

## License

MIT

# AI Interface — Design Spec

## Context

ai_interface is the AI infrastructure layer for the Frappe bench. It provides white-labeled functions that any app (excom, docpulse, warehousesuite, etc.) can call without knowing which AI provider is underneath. No business logic lives here — only capabilities.

Primary consumers at launch: Excom (drafting), DocPulse (vision extraction), BNS (summaries), and a conversational query engine for NL → ERPNext data.

## Decisions

| Decision | Choice |
|----------|--------|
| Query scope | Full ERPNext read access (user permission scoped) |
| Integration style | White-labeled Python functions imported by consumer apps |
| Provider support | Multi-provider abstraction; Anthropic at launch, OpenAI/Gemini later |
| Provider routing | AI Settings default + per-call override |
| Model routing | AI Settings default model + per-call override |
| Execution model | Async by default (frappe.enqueue), sync opt-in |
| Prompt storage | DocType with Frappe's built-in version tracking |
| Vision input | Images + PDFs (PDF pages converted to images via pdf2image) |
| Call logging | Full audit trail — prompt, response, tokens, cost, latency |
| Rate limiting | Track usage only, no local enforcement |
| Drafting API | General-purpose generate() function |

## Architecture

```
ai_interface/
├── services/
│   ├── ai_client.py            → Core client: resolve provider, enqueue/execute, log
│   ├── vision.py               → extract(file_url, prompt, **kwargs)
│   ├── query_engine.py         → query(question, user, **kwargs)
│   └── generator.py            → generate(prompt/template, context, **kwargs)
├── providers/
│   ├── __init__.py             → Provider registry (type string → class)
│   ├── base.py                 → BaseProvider ABC + ProviderResponse dataclass
│   └── anthropic_provider.py   → Anthropic implementation
├── ai_interface/               → Frappe module
│   ├── ai_settings/            → Single doctype: defaults, timeout, logging toggle
│   ├── ai_provider/            → One doc per provider: API key, base URL, models
│   ├── ai_provider_model/      → Child table: model ID, vision support, token costs
│   ├── ai_prompt_template/     → Versioned templates: name, category, body, variables
│   └── ai_call_log/            → Full audit: status, tokens, cost, prompt, response
├── hooks.py
└── patches/
```

## Provider Abstraction

```python
# providers/base.py
class BaseProvider(ABC):
    @abstractmethod
    def chat(self, messages: list[dict], model: str, max_tokens: int,
             temperature: float = 0.7) -> ProviderResponse: ...

    @abstractmethod
    def vision(self, messages: list[dict], images: list[bytes], model: str,
               max_tokens: int) -> ProviderResponse: ...

    @abstractmethod
    def list_models(self) -> list[str]: ...

@dataclass
class ProviderResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str
    raw_response: dict
```

Provider registry in `providers/__init__.py`: a dict mapping provider_type string → class. Adding a new provider = one file + one registry entry.

## Core Client

`services/ai_client.py` — single entry point all service functions route through:

```python
def call_ai(
    prompt: str,
    *,
    images: list[str] | None = None,
    provider: str | None = None,
    model: str | None = None,
    template: str | None = None,
    context: dict | None = None,
    calling_app: str = "",
    function_type: str = "generation",  # vision / query / generation
    sync: bool = False,
    user: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.7,
) -> str:
    """
    Async (default): Creates AI Call Log, enqueues execution, returns log name.
    Sync (sync=True): Executes inline, creates log, returns response text.
    """
```

### Async Flow

1. Resolve provider + model (caller override → AI Settings default)
2. If `template` provided, load AI Prompt Template and render with `context` via Jinja
3. Create AI Call Log (status="Queued")
4. `frappe.enqueue("ai_interface.services.ai_client._execute_ai_call", log_name=log.name, queue="default")`
5. Return `log.name`

Background worker (`_execute_ai_call`):
1. Load AI Call Log, set status="Running"
2. Load provider class from registry
3. If images: call `provider.vision()`, else: call `provider.chat()`
4. Update log: status="Completed", response, tokens, cost (calculated from AI Provider Model rates), latency
5. `frappe.publish_realtime("ai_call_complete", {"log": log_name}, user=log.user)`

### Sync Flow

Same steps 1-3, but execute inline (no enqueue). Still creates the log. Returns `response.content` directly.

### Error Handling

- Provider API errors → log status="Failed", `error_message` set, `frappe.publish_realtime("ai_call_failed", ...)`
- Missing/disabled provider → `frappe.throw()` at call time
- Timeout → caught, logged as Failed

## Service Functions (White-labeled API)

### Vision — `services/vision.py`

```python
def extract(
    file_url: str,
    prompt: str = "Extract all text and structured data from this document",
    **kwargs,
) -> str:
```

- Accepts Frappe file URLs (from File doctype attachments)
- PDF: convert pages to images using `pdf2image` (poppler). Page limit configurable in AI Settings (default 10).
- Images: pass directly as bytes
- Routes through `call_ai()` with `function_type="vision"`

### Query Engine — `services/query_engine.py`

```python
def query(
    question: str,
    user: str | None = None,
    **kwargs,
) -> str:
```

Three-phase LLM flow:
1. **Schema introspection**: Build a system prompt containing ERPNext doctype metadata (names, key fields, relationships) relevant to the question. Use `frappe.get_meta()` to introspect.
2. **Query generation**: LLM generates a `frappe.get_all()` call from the question. We execute it with the user's permissions (Frappe's permission system auto-filters). Never raw SQL, never write operations.
3. **Answer synthesis**: Feed the query results + original question back to LLM. Return natural language answer.

Two LLM calls per query (phases 1+2 combined in one call, phase 3 is the second). Safety enforced by only using `frappe.get_all`/`frappe.get_list` (read-only, permission-scoped).

### Generator — `services/generator.py`

```python
def generate(
    prompt: str | None = None,
    template: str | None = None,
    context: dict | None = None,
    **kwargs,
) -> str:
```

- Either raw `prompt` or `template` name + `context` (resolved via AI Prompt Template)
- Routes through `call_ai()` with `function_type="generation"`
- Consumer apps compose domain-specific prompts; this function adds no opinions

## DocTypes

### AI Settings (Single)

| Field | Type | Purpose |
|-------|------|---------|
| default_provider | Link → AI Provider | Site-wide default provider |
| default_model | Data | Default model ID |
| api_call_timeout | Int | Timeout in seconds (default: 120) |
| max_output_tokens | Int | Default max tokens (default: 4096) |
| enable_logging | Check | Master switch for AI Call Log creation |
| max_pdf_pages | Int | Max pages to process for PDFs (default: 10) |

Permissions: System Manager (full).

### AI Provider

| Field | Type | Purpose |
|-------|------|---------|
| provider_name | Data (unique) | "Anthropic", "OpenAI" |
| provider_type | Select | Maps to class in providers/ registry |
| api_key | Password | Encrypted storage |
| api_base_url | Data | Custom endpoint for proxies/Azure |
| enabled | Check | Toggle on/off |
| models | Table → AI Provider Model | Available models |

Permissions: System Manager (full).

### AI Provider Model (child table)

| Field | Type |
|-------|------|
| model_id | Data |
| label | Data |
| supports_vision | Check |
| cost_per_input_token | Float |
| cost_per_output_token | Float |

### AI Prompt Template

| Field | Type | Purpose |
|-------|------|---------|
| template_name | Data (unique) | Lookup key for consumer apps |
| category | Select | vision / query / generation / system |
| body | Code (Jinja) | Template body with {{ variables }} |
| variables | Small Text | JSON list of expected variable names |
| model_override | Data | Force specific model (optional) |
| provider_override | Link → AI Provider | Force specific provider (optional) |

`autoname: field:template_name`. Frappe version tracking enabled.
Permissions: System Manager (full), AI Manager role (read/write).

### AI Call Log

| Field | Type | Purpose |
|-------|------|---------|
| status | Select | Queued / Running / Completed / Failed |
| calling_app | Data | Consumer app name |
| function_type | Select | vision / query / generation |
| provider | Link → AI Provider | Which provider handled it |
| model | Data | Actual model used |
| prompt_template | Link → AI Prompt Template | If template was used |
| input_text | Long Text | Full prompt sent |
| output_text | Long Text | Full response |
| input_tokens | Int | Tokens in |
| output_tokens | Int | Tokens out |
| cost | Currency | Calculated from model rates |
| latency_ms | Int | API response time |
| error_message | Long Text | If failed |
| user | Link → User | Triggering user |
| is_sync | Check | Sync or async call |

`autoname: "hash"`. Append-only, read-only for non-admins.
Permissions: System Manager (full), all roles (read own via user field).

## Consumer Integration Pattern

```python
# Async (default) — excom drafting an email
from ai_interface.services.generator import generate

log_name = generate(
    template="email_reply_draft",
    context={"thread": thread.as_dict(), "tone": "professional"},
    calling_app="excom",
)
# log_name is AI Call Log ID
# Listen: frappe.realtime.on("ai_call_complete", callback)
# Or poll: frappe.get_value("AI Call Log", log_name, ["status", "output_text"])

# Sync — quick inline call
reply_text = generate(
    template="email_reply_draft",
    context={...},
    calling_app="excom",
    sync=True,
)
```

Consumer apps import from `ai_interface.services.*` only. No access to `providers/`.

## Dependencies

**Python packages:**
- `anthropic` — Python SDK for Claude API
- `pdf2image` — PDF to image conversion (requires `poppler-utils` system package)
- `Pillow` — Image handling

**Frappe apps (required_apps in hooks.py):**
- `frappe`
- `erpnext` — query engine introspects ERPNext doctypes

## Verification Plan

1. Create AI Provider doc for Anthropic with a valid API key
2. Set it as default in AI Settings
3. Create a test AI Prompt Template
4. Call `generate(template="test", context={...}, sync=True)` from bench console — confirm response
5. Call `generate(template="test", context={...})` async — confirm AI Call Log created with status progression Queued → Running → Completed
6. Call `extract(file_url, sync=True)` with a test image — confirm vision extraction works
7. Call `extract(file_url)` with a PDF — confirm page conversion + extraction
8. Call `query("How many customers do we have?", sync=True)` — confirm NL → data → answer flow
9. Verify AI Call Log entries have correct token counts, cost, latency
10. Verify `frappe.publish_realtime` fires on async completion

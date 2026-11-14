"""Seed the provider types that used to be a hardcoded Select.

These are ordinary editable records, not a code table — the names match the old
Select values so existing AI Provider docs keep resolving after the field became
a Link.
"""

import frappe

LITELLM_CATALOG = (
	"https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)

PROVIDER_TYPES = [
	{
		"type_name": "Anthropic",
		"adapter_path": "ai_interface.providers.anthropic_provider.AnthropicProvider",
		"default_base_url": "https://api.anthropic.com",
		"models_path": "/v1/models",
		"default_currency": "USD",
		"pricing_source_url": LITELLM_CATALOG,
	},
	{
		"type_name": "Claude Code",
		"adapter_path": "ai_interface.providers.claude_code_provider.ClaudeCodeProvider",
		"no_credential": 1,
		"default_currency": "USD",
	},
	{
		"type_name": "OpenAI",
		"adapter_path": "ai_interface.providers.openai_compatible.OpenAICompatibleProvider",
		"default_base_url": "https://api.openai.com",
		"chat_path": "/v1/chat/completions",
		"models_path": "/v1/models",
		"default_currency": "USD",
		"pricing_source_url": LITELLM_CATALOG,
	},
	{
		"type_name": "Sarvam",
		"adapter_path": "ai_interface.providers.openai_compatible.OpenAICompatibleProvider",
		"default_base_url": "https://api.sarvam.ai",
		# /v2 serves more models but is beta-gated per account and returns 400
		# for keys without access; /v1 works for everyone. Model discovery still
		# uses /v2/models, which is open.
		"chat_path": "/v1/chat/completions",
		"models_path": "/v2/models",
		"auth_header": "api-subscription-key",
		"auth_prefix": "",
		"default_currency": "INR",
		"unsupported_params": "stream_options, max_completion_tokens, service_tier",
	},
	{
		# Gemini's OpenAI-compatible endpoint: the same adapter, a Gemini API key as Bearer
		"type_name": "Google Gemini",
		"adapter_path": "ai_interface.providers.openai_compatible.OpenAICompatibleProvider",
		"default_base_url": "https://generativelanguage.googleapis.com",
		"chat_path": "/v1beta/openai/chat/completions",
		"models_path": "/v1beta/openai/models",
		"default_currency": "USD",
		"pricing_source_url": LITELLM_CATALOG,
		"catalog_prefix": "gemini/",
	},
	{
		# Many vendors' models behind one key and one bill
		"type_name": "OpenRouter",
		"adapter_path": "ai_interface.providers.openai_compatible.OpenAICompatibleProvider",
		"default_base_url": "https://openrouter.ai",
		"chat_path": "/api/v1/chat/completions",
		"models_path": "/api/v1/models",
		"default_currency": "USD",
		"pricing_source_url": LITELLM_CATALOG,
		"catalog_prefix": "openrouter/",
	},
	{
		# Models running on your own machine or server: no key, no per-token cost
		"type_name": "Ollama",
		"adapter_path": "ai_interface.providers.openai_compatible.OpenAICompatibleProvider",
		"default_base_url": "http://localhost:11434",
		"chat_path": "/v1/chat/completions",
		"models_path": "/v1/models",
		"no_credential": 1,
		"default_currency": "USD",
	},
]


def execute():
	for spec in PROVIDER_TYPES:
		if frappe.db.exists("AI Provider Type", spec["type_name"]):
			continue
		doc = frappe.new_doc("AI Provider Type")
		doc.update(spec)
		doc.insert(ignore_permissions=True)

	frappe.db.commit()

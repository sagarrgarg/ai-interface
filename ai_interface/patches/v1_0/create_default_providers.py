"""Ready-made AI Provider records for the common vendors, so a new site only has to add a key.

Every one starts switched off and without a key: nothing calls a vendor until somebody adds the key,
switches it on and presses Test connection. A provider that already exists under the same name is
never touched (its key, models, prices and on/off are the user's), and one deleted after this runs is
not brought back: this runs once (a patch) and on a fresh install.

Models: Anthropic and Claude (subscription) come with their current models; the others fill theirs
from the vendor with Fetch models once the key is in, and prices come from the pricing catalog.
"""

import frappe

from ai_interface.patches.v1_0 import create_default_provider_types

PER_M = 1_000_000

# Anthropic list prices per million tokens (input, output, cache read, 5-minute cache write), 2026-11.
# Haiku's are for prompts up to 100K tokens. Stamped "Catalog", so Fetch models refreshes them from the
# pricing catalog; a rate a person types is kept.
ANTHROPIC_MODELS = [
	("claude-opus-5-5", "Claude Opus 5.5", 4.00, 20.00, 0.20, 5.00, 1),
	("claude-sonnet-5-5", "Claude Sonnet 5.5", 2.00, 10.00, 0.20, 2.50, 2),
	("claude-haiku-5-5", "Claude Haiku 5.5", 0.10, 0.50, 0.01, 0.125, 3),
	("claude-fable-5-1", "Claude Fable 5.1", 10.00, 50.00, 0.25, 12.50, 4),
]

DEFAULT_PROVIDERS = [
	{"provider_name": "Anthropic", "provider_type": "Anthropic", "models": "anthropic"},
	{"provider_name": "OpenAI", "provider_type": "OpenAI"},
	{"provider_name": "Google Gemini", "provider_type": "Google Gemini"},
	{"provider_name": "OpenRouter", "provider_type": "OpenRouter"},
	{"provider_name": "Ollama", "provider_type": "Ollama"},
	{"provider_name": "Sarvam", "provider_type": "Sarvam"},
	# Runs the `claude` command-line tool on this server, signed in with a Claude plan (claude setup-token):
	# no per-token bill, so its models carry no price.
	{"provider_name": "Claude (subscription)", "provider_type": "Claude Code", "models": "claude_code"},
]


def _models(kind: str | None) -> list[dict]:
	if kind == "anthropic":
		return [{"model_id": m, "label": label, "enabled": 1, "supports_vision": 1, "supports_tools": 1, "priority": prio,
			"cost_per_input_token": i / PER_M, "cost_per_output_token": o / PER_M,
			"cost_per_cache_read_token": r / PER_M, "cost_per_cache_write_token": w / PER_M, "pricing_source": "Catalog"}
			for m, label, i, o, r, w, prio in ANTHROPIC_MODELS]
	if kind == "claude_code":
		return [{"model_id": m, "label": label, "enabled": 1, "supports_vision": 1, "priority": prio}
			for m, label, _i, _o, _r, _w, prio in ANTHROPIC_MODELS]
	return []


def execute():
	create_default_provider_types.execute()  # the types these need (Gemini, OpenRouter, Ollama are new)
	for spec in DEFAULT_PROVIDERS:
		if frappe.db.exists("AI Provider", spec["provider_name"]) or not frappe.db.exists(
				"AI Provider Type", spec["provider_type"]):
			continue
		doc = frappe.new_doc("AI Provider")
		doc.update({"provider_name": spec["provider_name"], "provider_type": spec["provider_type"], "enabled": 0})
		for row in _models(spec.get("models")):
			doc.append("models", row)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()

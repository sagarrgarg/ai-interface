"""Fixtures for the AI Interface test suite.

Every record made here is prefixed `_T` and torn down afterwards, so a test run
can never disturb a real provider or a real key. Nothing in this suite touches
the network: provider calls are stubbed at `_attempt_call`, and any test that
needs a model catalog builds one rather than fetching it.
"""

import frappe

PREFIX = "_T"

TYPE_OPENAI = f"{PREFIX} Generic"
TYPE_LOCAL = f"{PREFIX} Local CLI"

PROVIDER_A = f"{PREFIX} Provider USD"
PROVIDER_B = f"{PREFIX} Provider INR"

ADAPTER = "ai_interface.providers.openai_compatible.OpenAICompatibleProvider"
CLI_ADAPTER = "ai_interface.providers.claude_code_provider.ClaudeCodeProvider"


def make_provider_type(name=TYPE_OPENAI, **overrides):
	if frappe.db.exists("AI Provider Type", name):
		frappe.delete_doc("AI Provider Type", name, force=True, ignore_permissions=True)

	doc = frappe.new_doc("AI Provider Type")
	doc.update({
		"type_name": name,
		"adapter_path": ADAPTER,
		"default_base_url": "https://example.invalid",
		"chat_path": "/v1/chat/completions",
		"models_path": "/v1/models",
		"pricing_source_url": "",  # never reach the network during tests
		"default_currency": "USD",
	})
	doc.update(overrides)
	doc.insert(ignore_permissions=True)
	return doc


def make_provider(name=PROVIDER_A, type_name=TYPE_OPENAI, models=None, **overrides):
	"""A provider with an explicit model catalog.

	`models` is a list of dicts; anything omitted takes a sensible default so a
	test only states the attribute it actually cares about.
	"""
	if frappe.db.exists("AI Provider", name):
		frappe.delete_doc("AI Provider", name, force=True, ignore_permissions=True)

	doc = frappe.new_doc("AI Provider")
	doc.update({
		"provider_name": name,
		"provider_type": type_name,
		"enabled": 1,
		"auth_type": "API Key",
		"api_key": "sk-test-not-a-real-key",
		"currency": "USD",
	})
	doc.update(overrides)

	for spec in models or []:
		row = {
			"model_id": spec["model_id"],
			"label": spec.get("label", spec["model_id"]),
			"enabled": spec.get("enabled", 1),
			"supports_vision": spec.get("supports_vision", 0),
			"supports_tools": spec.get("supports_tools", 0),
			"context_window": spec.get("context_window", 100000),
			"priority": spec.get("priority", 0),
			"cost_per_input_token": spec.get("cost_in", 0.000001),
			"cost_per_output_token": spec.get("cost_out", 0.000002),
		}
		doc.append("models", row)

	doc.insert(ignore_permissions=True)
	return doc


def settings_for_test(**overrides):
	"""AI Settings mutated in-memory only.

	Returned unsaved: the router and budget code read attributes off the doc, so
	a test can shape policy without writing to a Single that other tests share.
	"""
	doc = frappe.get_single("AI Settings")
	doc.routing_rules = []
	doc.app_budgets = []
	doc.enable_fallback = 1
	doc.enable_budget = 0
	doc.daily_budget = 0
	doc.monthly_budget = 0
	doc.budget_action = "Warn"
	doc.budget_alert_threshold = 80
	doc.default_provider = None
	doc.default_model = None
	doc.base_currency = "USD"
	doc.update(overrides)
	return doc


def add_rule(settings, provider, **kw):
	settings.append("routing_rules", {
		"enabled": kw.get("enabled", 1),
		"function_type": kw.get("function_type"),
		"calling_app": kw.get("calling_app"),
		"provider": provider,
		"model": kw.get("model"),
		"priority": kw.get("priority", 0),
	})
	return settings


def cleanup():
	"""Remove everything this module creates, children first."""
	frappe.db.sql("DELETE FROM `tabAI Call Log` WHERE calling_app LIKE %s", (PREFIX + "%",))

	for name in frappe.get_all(
		"AI Provider", filters={"provider_name": ["like", PREFIX + "%"]}, pluck="name"
	):
		frappe.delete_doc("AI Provider", name, force=True, ignore_permissions=True)

	for name in frappe.get_all(
		"AI Provider Type", filters={"type_name": ["like", PREFIX + "%"]}, pluck="name"
	):
		frappe.delete_doc("AI Provider Type", name, force=True, ignore_permissions=True)

	frappe.db.commit()

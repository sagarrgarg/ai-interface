"""Capability routing.

These tests pin the behaviour that makes a provider swappable: callers state what
they need, the router picks the model, and a call that cannot succeed is refused
before it is queued rather than failing inside a background job.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.services import router
from ai_interface.tests.utils import (
	PROVIDER_A,
	PROVIDER_B,
	TYPE_OPENAI,
	add_rule,
	cleanup,
	make_provider,
	make_provider_type,
	settings_for_test,
)

CHEAP = "t-cheap"
MID = "t-mid"
VISION = "t-vision"
TOOLS = "t-tools"
SMALL_CTX = "t-small"


class TestRouter(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup()
		make_provider_type(TYPE_OPENAI)
		make_provider(PROVIDER_A, TYPE_OPENAI, models=[
			{"model_id": CHEAP, "cost_in": 0.0000001, "cost_out": 0.0000002},
			{"model_id": MID, "cost_in": 0.00001, "cost_out": 0.00002},
			{"model_id": VISION, "supports_vision": 1, "cost_in": 0.000005, "cost_out": 0.00001},
			{"model_id": TOOLS, "supports_tools": 1, "cost_in": 0.000005, "cost_out": 0.00001},
			{"model_id": SMALL_CTX, "context_window": 100, "cost_in": 0.00000001,
			 "cost_out": 0.00000002},
		])
		make_provider(PROVIDER_B, TYPE_OPENAI, currency="INR", models=[
			{"model_id": "t-inr", "cost_in": 0.00003, "cost_out": 0.00007},
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cleanup()
		super().tearDownClass()

	def chain(self, settings, **kw):
		kw.setdefault("provider", None)
		kw.setdefault("model", None)
		kw.setdefault("template", None)
		kw.setdefault("function_type", "Generation")
		kw.setdefault("calling_app", "_Tapp")
		kw.setdefault("needs", [])
		# ~250 tokens: comfortably inside every model here except SMALL_CTX,
		# so "cheapest capable" is a meaningful assertion rather than a tie.
		kw.setdefault("prompt", "word " * 200)
		return router.build_chain(settings, **kw)

	# ---------------------------------------------------------------- picking

	def test_auto_picks_cheapest_capable_model(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		# SMALL_CTX is cheaper still, but cannot hold this prompt — so the
		# contract is cheapest *capable*, not cheapest outright.
		self.assertEqual(self.chain(s)[0]["model"], CHEAP)

	def test_cheapest_model_wins_when_it_can_hold_the_prompt(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		self.assertEqual(self.chain(s, prompt="hi")[0]["model"], SMALL_CTX)

	def test_context_window_excludes_a_model_that_cannot_hold_the_prompt(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		self.assertNotEqual(self.chain(s, prompt="word " * 200)[0]["model"], SMALL_CTX)

	def test_priority_beats_cost(self):
		provider = frappe.get_doc("AI Provider", PROVIDER_A)
		for m in provider.models:
			if m.model_id == MID:
				m.priority = -1
		provider.save(ignore_permissions=True)
		frappe.db.commit()
		try:
			s = settings_for_test(default_provider=PROVIDER_A)
			self.assertEqual(self.chain(s)[0]["model"], MID)
		finally:
			provider.reload()
			for m in provider.models:
				m.priority = 0
			provider.save(ignore_permissions=True)
			frappe.db.commit()

	def test_vision_requirement_selects_a_vision_model(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		self.assertEqual(self.chain(s, needs=["vision"])[0]["model"], VISION)

	def test_tools_requirement_selects_a_tool_model(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		self.assertEqual(self.chain(s, needs=["tools"])[0]["model"], TOOLS)

	def test_disabled_model_is_never_picked(self):
		provider = frappe.get_doc("AI Provider", PROVIDER_A)
		for m in provider.models:
			if m.model_id == CHEAP:
				m.enabled = 0
		provider.save(ignore_permissions=True)
		frappe.db.commit()
		try:
			s = settings_for_test(default_provider=PROVIDER_A)
			self.assertNotEqual(self.chain(s)[0]["model"], CHEAP)
		finally:
			provider.reload()
			for m in provider.models:
				m.enabled = 1
			provider.save(ignore_permissions=True)
			frappe.db.commit()

	# ---------------------------------------------------------------- ordering

	def test_specific_rule_beats_catch_all(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		add_rule(s, PROVIDER_A, priority=0)
		add_rule(s, PROVIDER_B, calling_app="_Tapp", priority=99)
		# The app-specific rule wins despite far worse priority: specificity is
		# compared first, or a catch-all could silently shadow a targeted rule.
		self.assertEqual(self.chain(s)[0]["provider"], PROVIDER_B)

	def test_function_type_rule_only_matches_that_type(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		add_rule(s, PROVIDER_B, function_type="Vision")
		self.assertEqual(self.chain(s, function_type="Generation")[0]["provider"], PROVIDER_A)

	def test_non_matching_app_rule_is_skipped(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		add_rule(s, PROVIDER_B, calling_app="somebody-else")
		self.assertEqual(self.chain(s)[0]["provider"], PROVIDER_A)

	def test_disabled_rule_is_ignored(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		add_rule(s, PROVIDER_B, calling_app="_Tapp", enabled=0)
		self.assertEqual(self.chain(s)[0]["provider"], PROVIDER_A)

	def test_caller_override_wins_outright(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		add_rule(s, PROVIDER_A, calling_app="_Tapp")
		out = self.chain(s, provider=PROVIDER_B, model="t-inr")[0]
		self.assertEqual((out["provider"], out["model"]), (PROVIDER_B, "t-inr"))

	# ---------------------------------------------------------------- fallback

	def test_lower_priority_rules_become_the_fallback_chain(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		add_rule(s, PROVIDER_B, priority=0)
		add_rule(s, PROVIDER_A, priority=10)
		chain = self.chain(s)
		self.assertEqual([c["provider"] for c in chain][:2], [PROVIDER_B, PROVIDER_A])

	def test_fallback_disabled_yields_a_single_entry(self):
		s = settings_for_test(default_provider=PROVIDER_A, enable_fallback=0)
		add_rule(s, PROVIDER_B, priority=0)
		add_rule(s, PROVIDER_A, priority=10)
		self.assertEqual(len(self.chain(s)), 1)

	def test_chain_has_no_duplicate_entries(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		add_rule(s, PROVIDER_A, priority=0)
		add_rule(s, PROVIDER_A, priority=1)
		chain = self.chain(s)
		self.assertEqual(len(chain), len({(c["provider"], c["model"]) for c in chain}))

	def test_auth_and_config_errors_are_never_retryable(self):
		# Retrying a bad key spends money to fail again; this set is the guard.
		self.assertNotIn("Auth", router.RETRYABLE)
		self.assertNotIn("Config Error", router.RETRYABLE)
		self.assertEqual(router.RETRYABLE, {"Rate Limit", "Timeout", "Provider Error"})

	# ---------------------------------------------------------------- refusals

	def test_named_model_without_vision_is_refused(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.chain(s, provider=PROVIDER_A, model=CHEAP, needs=["vision"])
		self.assertIn("does not support images", str(ctx.exception))

	def test_prompt_larger_than_every_context_window_is_refused(self):
		s = settings_for_test(default_provider=PROVIDER_A)
		with self.assertRaises(frappe.ValidationError):
			self.chain(s, prompt="x" * 5_000_000)

	def test_provider_with_no_capable_model_is_refused(self):
		s = settings_for_test(default_provider=PROVIDER_B)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.chain(s, needs=["vision"])
		self.assertIn("supports images", str(ctx.exception).lower())

	def test_disabled_provider_is_refused(self):
		frappe.db.set_value("AI Provider", PROVIDER_B, "enabled", 0)
		frappe.clear_cache()
		try:
			s = settings_for_test(default_provider=PROVIDER_B)
			with self.assertRaises(frappe.ValidationError):
				self.chain(s)
		finally:
			frappe.db.set_value("AI Provider", PROVIDER_B, "enabled", 1)
			frappe.clear_cache()

	def test_no_provider_configured_is_refused(self):
		s = settings_for_test(default_provider=None)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.chain(s)
		self.assertIn("No AI provider configured", str(ctx.exception))

	def test_unlisted_model_is_allowed_through(self):
		# The catalog can lag a vendor release; refusing a model the provider
		# would accept is worse than trusting an explicit instruction.
		s = settings_for_test(default_provider=PROVIDER_A)
		out = self.chain(s, provider=PROVIDER_A, model="model-released-yesterday")[0]
		self.assertEqual(out["model"], "model-released-yesterday")

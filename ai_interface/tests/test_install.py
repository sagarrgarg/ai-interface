"""A new site gets the provider types, roles and defaults that patches seed on old ones."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface import install
from ai_interface.patches.v1_0.create_default_provider_types import PROVIDER_TYPES


class TestInstall(FrappeTestCase):
	def test_after_install_seeds_what_patches_seed(self):
		names = [t["type_name"] for t in PROVIDER_TYPES]
		with patch.object(frappe.db, "commit"):
			frappe.db.delete("AI Provider Type", {"name": ["in", names]})
			self.assertEqual(frappe.db.count("AI Provider Type", {"name": ["in", names]}), 0)
			install.after_install()
		for name in names:
			self.assertTrue(frappe.db.exists("AI Provider Type", name), name)
		self.assertEqual(frappe.db.get_value("AI Provider Type", "Sarvam", "chat_path"), "/v1/chat/completions")
		for role in ("AI Manager", "AI User"):
			self.assertTrue(frappe.db.exists("Role", role))
		install.after_install()  # a second run changes nothing
		self.assertEqual(frappe.db.count("AI Provider Type", {"name": ["in", names]}), len(names))

	def test_after_migrate_restores_types_only_when_there_are_none(self):
		with patch.object(frappe.db, "commit"):
			frappe.db.delete("AI Provider Type", {"name": "OpenAI"})
			install.after_migrate()
			self.assertFalse(frappe.db.exists("AI Provider Type", "OpenAI"))  # a type deleted on purpose stays gone
			frappe.db.delete("AI Provider Type")
			install.after_migrate()
			self.assertTrue(frappe.db.exists("AI Provider Type", "Anthropic"))

	def test_ready_made_providers_start_off_and_are_never_overwritten(self):
		from ai_interface.patches.v1_0.create_default_providers import DEFAULT_PROVIDERS, execute

		names = [p["provider_name"] for p in DEFAULT_PROVIDERS]
		self.assertEqual(names, ["Anthropic", "OpenAI", "Google Gemini", "OpenRouter", "Ollama", "Sarvam",
			"Claude (subscription)"])
		with patch.object(frappe.db, "commit"):
			saved = {n: frappe.get_doc("AI Provider", n).as_dict() for n in names if frappe.db.exists("AI Provider", n)}
			for n in names:
				if frappe.db.exists("AI Provider", n):
					frappe.delete_doc("AI Provider", n, force=True, ignore_permissions=True)
			execute()
			for n in names:
				doc = frappe.get_doc("AI Provider", n)
				self.assertEqual(doc.enabled, 0, n)  # nothing calls a vendor until someone turns it on
				self.assertFalse(doc.get_password("api_key", raise_exception=False), n)
			anthropic = frappe.get_doc("AI Provider", "Anthropic")
			opus = next(m for m in anthropic.models if m.model_id == "claude-opus-5-5")
			self.assertAlmostEqual(opus.cost_per_input_token * 1_000_000, 4.0)
			self.assertAlmostEqual(opus.cost_per_output_token * 1_000_000, 20.0)
			self.assertAlmostEqual(opus.cost_per_cache_read_token * 1_000_000, 0.20)
			self.assertEqual(opus.pricing_source, "Catalog")  # a later Fetch models may refresh it
			sub = frappe.get_doc("AI Provider", "Claude (subscription)")
			self.assertEqual(sub.provider_type, "Claude Code")
			self.assertFalse(any(m.cost_per_input_token for m in sub.models))  # a subscription: no per-token bill
			self.assertEqual(frappe.db.get_value("AI Provider Type", "Ollama", "no_credential"), 1)
			self.assertEqual(frappe.db.get_value("AI Provider Type", "Google Gemini", "chat_path"),
				"/v1beta/openai/chat/completions")
			# a user's change survives a second run
			frappe.db.set_value("AI Provider", "OpenAI", "api_base_url", "https://proxy.example")
			execute()
			self.assertEqual(frappe.db.get_value("AI Provider", "OpenAI", "api_base_url"), "https://proxy.example")
			self.assertEqual(frappe.db.count("AI Provider", {"name": ["in", names]}), len(names))
			frappe.db.rollback()

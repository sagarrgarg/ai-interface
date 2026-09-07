"""Model catalog merge.

Regression guard. `fetch_models` once did `self.models = []` and re-appended,
which discarded every rate an admin had typed. These tests exist so that cannot
come back.
"""

from typing import ClassVar

import frappe
from frappe.tests.utils import FrappeTestCase

import ai_interface.providers as providers_mod
from ai_interface.providers.openai_compatible import OpenAICompatibleProvider
from ai_interface.tests.utils import (
	PROVIDER_A,
	TYPE_OPENAI,
	cleanup,
	make_provider,
	make_provider_type,
)


class StubProvider(OpenAICompatibleProvider):
	"""Returns whatever DISCOVERY holds, so a vendor adding and dropping models
	between two fetches can be simulated without touching the network."""

	DISCOVERY: ClassVar[list] = []

	def fetch_models(self, credential="", auth_type="API Key", api_base_url=""):
		return [dict(m) for m in StubProvider.DISCOVERY]


class TestCatalogMerge(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup()
		make_provider_type(TYPE_OPENAI)
		cls._real_get_provider = providers_mod.get_provider
		providers_mod.get_provider = cls._stub_get_provider

	@classmethod
	def tearDownClass(cls):
		providers_mod.get_provider = cls._real_get_provider
		cleanup()
		super().tearDownClass()

	@staticmethod
	def _stub_get_provider(provider_type):
		inst = StubProvider()
		inst.config = frappe.get_cached_doc("AI Provider Type", provider_type).as_config()
		return inst

	def setUp(self):
		self.provider = make_provider(PROVIDER_A, TYPE_OPENAI, models=[])
		frappe.db.commit()

	def tearDown(self):
		if frappe.db.exists("AI Provider", PROVIDER_A):
			frappe.delete_doc("AI Provider", PROVIDER_A, force=True, ignore_permissions=True)
		frappe.db.commit()

	def fetch(self, ids):
		StubProvider.DISCOVERY = [{"model_id": i, "label": i} for i in ids]
		self.provider.reload()
		self.provider.fetch_models()
		frappe.db.commit()
		self.provider.reload()
		return {m.model_id: m for m in self.provider.models}

	def test_discovery_adds_models(self):
		rows = self.fetch(["a", "b"])
		self.assertEqual(set(rows), {"a", "b"})

	def test_hand_entered_rate_survives_a_refetch(self):
		self.fetch(["a", "b"])
		self.provider.reload()
		for m in self.provider.models:
			if m.model_id == "a":
				m.cost_per_input_token = 0.0000366
				m.pricing_source = None  # as if a human just typed it
		self.provider.save(ignore_permissions=True)
		frappe.db.commit()

		rows = self.fetch(["a", "b"])
		self.assertEqual(rows["a"].cost_per_input_token, 0.0000366)
		self.assertEqual(rows["a"].pricing_source, "Manual")

	def test_dropped_model_is_disabled_not_deleted(self):
		# Historical call logs still cost against a retired model, so the row
		# must survive even when the vendor stops offering it.
		self.fetch(["a", "b"])
		rows = self.fetch(["a"])
		self.assertIn("b", rows)
		self.assertEqual(rows["b"].enabled, 0)
		self.assertEqual(rows["a"].enabled, 1)

	def test_returning_model_is_re_enabled(self):
		self.fetch(["a", "b"])
		self.fetch(["a"])
		rows = self.fetch(["a", "b"])
		self.assertEqual(rows["b"].enabled, 1)

	def test_label_refreshes_from_the_vendor(self):
		self.fetch(["a"])
		StubProvider.DISCOVERY = [{"model_id": "a", "label": "Model A v2"}]
		self.provider.reload()
		self.provider.fetch_models()
		frappe.db.commit()
		self.provider.reload()
		self.assertEqual(self.provider.models[0].label, "Model A v2")

	def test_unpriced_models_are_flagged(self):
		# A zero cost must read as unknown, not as free.
		rows = self.fetch(["a"])
		self.assertEqual(rows["a"].pricing_source, "Unpriced")

	def test_empty_discovery_leaves_existing_models_alone(self):
		# A vendor with no listing endpoint returns nothing; that is not a
		# signal to retire the catalog an admin curated by hand.
		self.fetch(["a", "b"])
		rows = self.fetch([])
		self.assertEqual(set(rows), {"a", "b"})
		self.assertTrue(all(r.enabled for r in rows.values()))

	def test_catalog_lookup_is_skipped_when_no_url_is_set(self):
		# An empty Pricing Source URL must mean zero outbound requests.
		config = frappe.get_cached_doc("AI Provider Type", TYPE_OPENAI).as_config()
		config["pricing_source_url"] = ""
		self.assertEqual(self.provider._load_catalog(config, {"a"}), {})

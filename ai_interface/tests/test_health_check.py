"""Connection tests are logged, and the scheduled ping is frugal.

Provider calls are stubbed, so nothing here reaches a vendor or spends anything.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.providers.base import ProviderHTTPError, ProviderResponse
from ai_interface.services import ai_client
from ai_interface.tests.utils import PROVIDER_A, TYPE_OPENAI, cleanup, make_provider, make_provider_type

MODEL_CHEAP = "t-cheap"
MODEL_DEAR = "t-dear"


def ok_stub(**kw):
	def _attempt(provider_doc, rendered_prompt, images, model, max_tokens, temperature, timeout):
		return ProviderResponse(
			content="OK", input_tokens=12, output_tokens=2, model=model, raw_response={}
		)

	return _attempt


def fail_stub(status=401):
	def _attempt(*a, **k):
		raise ProviderHTTPError("nope", status, "detail")

	return _attempt


class TestConnectionTestIsLogged(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup()
		make_provider_type(TYPE_OPENAI)
		make_provider(PROVIDER_A, TYPE_OPENAI, currency="USD", models=[
			{"model_id": MODEL_DEAR, "cost_in": 0.001, "cost_out": 0.002},
			{"model_id": MODEL_CHEAP, "cost_in": 0.000001, "cost_out": 0.000002},
		])
		frappe.db.commit()
		cls._real = ai_client._attempt_call

	@classmethod
	def tearDownClass(cls):
		ai_client._attempt_call = cls._real
		cleanup()
		super().tearDownClass()

	def tearDown(self):
		ai_client._attempt_call = self._real
		frappe.db.sql("DELETE FROM `tabAI Call Log` WHERE provider = %s", (PROVIDER_A,))
		frappe.db.commit()

	def test_a_successful_test_writes_a_log_row(self):
		# Spend the dashboard cannot see is spend nobody can account for.
		ai_client._attempt_call = ok_stub()
		result = ai_client.test_provider(PROVIDER_A)
		self.assertTrue(result["ok"])

		log = frappe.get_doc("AI Call Log", result["log"])
		self.assertEqual(log.status, "Completed")
		self.assertEqual(log.action, "connection_test")
		self.assertEqual(log.input_tokens, 12)
		self.assertGreater(log.cost, 0)
		self.assertGreater(log.base_cost, 0)

	def test_it_picks_the_cheapest_enabled_model(self):
		ai_client._attempt_call = ok_stub()
		self.assertEqual(ai_client.test_provider(PROVIDER_A)["model"], MODEL_CHEAP)

	def test_a_failed_test_is_logged_too(self):
		ai_client._attempt_call = fail_stub(401)
		result = ai_client.test_provider(PROVIDER_A)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error_type"], "Auth")

		rows = frappe.get_all(
			"AI Call Log", filters={"provider": PROVIDER_A}, fields=["status", "error_type"]
		)
		self.assertEqual(rows[0]["status"], "Failed")
		self.assertEqual(rows[0]["error_type"], "Auth")

	def test_the_action_is_labelled_so_it_can_be_filtered_out(self):
		ai_client._attempt_call = ok_stub()
		ai_client.test_provider(PROVIDER_A, action="health_check")
		actions = frappe.get_all("AI Call Log", filters={"provider": PROVIDER_A}, pluck="action")
		self.assertEqual(actions, ["health_check"])

	def test_a_provider_with_no_models_reports_config_error(self):
		bare = make_provider("_T Bare", TYPE_OPENAI, models=[])
		frappe.db.commit()
		try:
			result = ai_client.test_provider(bare.name)
			self.assertFalse(result["ok"])
			self.assertEqual(result["error_type"], "Config Error")
		finally:
			frappe.delete_doc("AI Provider", bare.name, force=True, ignore_permissions=True)


class TestScheduledHealthCheck(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup()
		make_provider_type(TYPE_OPENAI)
		make_provider(PROVIDER_A, TYPE_OPENAI, models=[
			{"model_id": MODEL_CHEAP, "cost_in": 0.000001, "cost_out": 0.000002},
		])
		frappe.db.commit()
		cls._real = ai_client._attempt_call

	@classmethod
	def tearDownClass(cls):
		ai_client._attempt_call = cls._real
		frappe.db.set_single_value("AI Settings", "enable_health_checks", 0)
		cleanup()
		super().tearDownClass()

	def setUp(self):
		self.calls = []

		def counting(provider_doc, rendered_prompt, images, model, max_tokens, temperature, timeout):
			self.calls.append(provider_doc.name)
			return ProviderResponse(
				content="OK", input_tokens=10, output_tokens=1, model=model, raw_response={}
			)

		ai_client._attempt_call = counting

	def tearDown(self):
		ai_client._attempt_call = self._real
		frappe.db.set_single_value("AI Settings", "enable_health_checks", 0)
		frappe.db.sql("DELETE FROM `tabAI Call Log` WHERE provider = %s", (PROVIDER_A,))
		frappe.db.commit()

	def test_disabled_means_no_calls_at_all(self):
		# Every ping is billed, so off must mean genuinely off.
		frappe.db.set_single_value("AI Settings", "enable_health_checks", 0)
		frappe.db.set_value("AI Provider", PROVIDER_A, "last_success", None)
		ai_client.check_provider_health()
		self.assertEqual(self.calls, [])

	def test_an_idle_provider_is_pinged(self):
		frappe.db.set_single_value("AI Settings", "enable_health_checks", 1)
		frappe.db.set_single_value("AI Settings", "health_check_idle_minutes", 60)
		frappe.db.set_value(
			"AI Provider", PROVIDER_A, "last_success",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-5),
		)
		ai_client.check_provider_health()
		self.assertIn(PROVIDER_A, self.calls)

	def test_a_recently_used_provider_is_skipped(self):
		# Real traffic already proved it alive, and proving it twice costs money.
		frappe.db.set_single_value("AI Settings", "enable_health_checks", 1)
		frappe.db.set_single_value("AI Settings", "health_check_idle_minutes", 60)
		frappe.db.set_value("AI Provider", PROVIDER_A, "last_success", frappe.utils.now())
		ai_client.check_provider_health()
		self.assertNotIn(PROVIDER_A, self.calls)

	def test_a_disabled_provider_is_never_pinged(self):
		frappe.db.set_single_value("AI Settings", "enable_health_checks", 1)
		frappe.db.set_value("AI Provider", PROVIDER_A, "enabled", 0)
		frappe.db.set_value("AI Provider", PROVIDER_A, "last_success", None)
		try:
			ai_client.check_provider_health()
			self.assertNotIn(PROVIDER_A, self.calls)
		finally:
			frappe.db.set_value("AI Provider", PROVIDER_A, "enabled", 1)

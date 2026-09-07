"""The worker: fallback, health and what lands in the log.

No provider is ever really called — `_attempt_call` is stubbed, so these tests
run offline and cost nothing.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.providers.base import ProviderHTTPError, ProviderResponse
from ai_interface.services import ai_client
from ai_interface.tests.utils import (
	PREFIX,
	PROVIDER_A,
	PROVIDER_B,
	TYPE_OPENAI,
	cleanup,
	make_provider,
	make_provider_type,
)

APP = f"{PREFIX}exec"
CHAIN = [
	{"provider": PROVIDER_B, "model": "t-inr"},
	{"provider": PROVIDER_A, "model": "t-usd"},
]


def stub(fail_status=None, fail_for=None, content="answered"):
	"""Fail for one provider with a given status; succeed for anything else."""

	def _attempt(provider_doc, rendered_prompt, images, model, max_tokens, temperature, timeout):
		if fail_for is not None and provider_doc.name == fail_for:
			raise ProviderHTTPError(f"boom from {provider_doc.name}", fail_status, "detail")
		if fail_for is None and fail_status is not None:
			raise ProviderHTTPError("everything is down", fail_status, "detail")
		return ProviderResponse(
			content=content, input_tokens=100, output_tokens=20, model=model, raw_response={}
		)

	return _attempt


class TestExecution(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup()
		make_provider_type(TYPE_OPENAI)
		make_provider(PROVIDER_A, TYPE_OPENAI, currency="USD", models=[
			{"model_id": "t-usd", "cost_in": 0.000003, "cost_out": 0.000015},
		])
		make_provider(PROVIDER_B, TYPE_OPENAI, currency="INR", exchange_rate=0.012, models=[
			{"model_id": "t-inr", "cost_in": 0.00003, "cost_out": 0.00007},
		])
		frappe.db.commit()
		cls._real_attempt = ai_client._attempt_call

	@classmethod
	def tearDownClass(cls):
		ai_client._attempt_call = cls._real_attempt
		cleanup()
		super().tearDownClass()

	def tearDown(self):
		ai_client._attempt_call = self._real_attempt

	def make_log(self):
		log = frappe.new_doc("AI Call Log")
		log.update({
			"status": "Queued", "function_type": "Generation", "calling_app": APP,
			"provider": PROVIDER_B, "model": "t-inr", "input_text": "hello",
			"user": "Administrator", "currency": "INR", "base_currency": "USD",
		})
		log.insert(ignore_permissions=True)
		frappe.db.commit()
		return log.name

	def run_call(self, chain=None):
		name = self.make_log()
		ai_client._execute_ai_call(
			name, "hello", None, PROVIDER_B, "t-inr", 100, 0.7, 30, chain=chain or CHAIN
		)
		return frappe.get_doc("AI Call Log", name)

	# ---------------------------------------------------------------- success

	def test_successful_call_records_cost_and_conversion(self):
		ai_client._attempt_call = stub()
		log = self.run_call(chain=[CHAIN[0]])
		self.assertEqual(log.status, "Completed")
		self.assertEqual(log.currency, "INR")
		self.assertEqual(log.exchange_rate, 0.012)
		# base_cost is a Currency field at precision 6, so the stored value is the
		# rounded product — asserting beyond that precision tests the database,
		# not the conversion.
		self.assertAlmostEqual(log.base_cost, log.cost * 0.012, places=6)
		self.assertEqual(log.attempts, 1)

	def test_output_is_stored(self):
		ai_client._attempt_call = stub(content="the answer")
		self.assertEqual(self.run_call(chain=[CHAIN[0]]).output_text, "the answer")

	# ---------------------------------------------------------------- fallback

	def test_retryable_failure_falls_through_to_the_next_provider(self):
		ai_client._attempt_call = stub(429, fail_for=PROVIDER_B)
		log = self.run_call()
		self.assertEqual(log.status, "Completed")
		self.assertEqual(log.provider, PROVIDER_A)
		self.assertEqual(log.attempts, 2)

	def test_first_failure_is_kept_for_diagnosis_even_on_success(self):
		ai_client._attempt_call = stub(429, fail_for=PROVIDER_B)
		log = self.run_call()
		self.assertIn("Rate Limit", log.error_message or "")
		self.assertIn(PROVIDER_B, log.error_message or "")

	def test_billing_currency_follows_the_provider_that_served_it(self):
		# The chain starts on an INR provider but a USD one answers; carrying the
		# planned currency would label a dollar amount as rupees.
		ai_client._attempt_call = stub(429, fail_for=PROVIDER_B)
		log = self.run_call()
		self.assertEqual(log.currency, "USD")
		self.assertEqual(log.exchange_rate, 1.0)

	def test_auth_failure_stops_at_the_first_provider(self):
		ai_client._attempt_call = stub(401, fail_for=PROVIDER_B)
		log = self.run_call()
		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.attempts, 1)
		self.assertEqual(log.error_type, "Auth")

	def test_exhausted_chain_fails_naming_every_provider_tried(self):
		ai_client._attempt_call = stub(503)
		log = self.run_call()
		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.attempts, len(CHAIN))
		self.assertIn(PROVIDER_A, log.error_message)
		self.assertIn(PROVIDER_B, log.error_message)

	# ---------------------------------------------------------------- health

	def test_success_marks_the_provider_healthy(self):
		frappe.db.set_value("AI Provider", PROVIDER_B, "consecutive_failures", 5)
		ai_client._attempt_call = stub()
		self.run_call(chain=[CHAIN[0]])
		doc = frappe.get_doc("AI Provider", PROVIDER_B)
		self.assertEqual(doc.health_status, "Healthy")
		self.assertEqual(doc.consecutive_failures, 0)
		self.assertTrue(doc.last_success)

	def test_failures_accumulate_and_cross_into_down(self):
		threshold = frappe.db.get_single_value("AI Settings", "failure_threshold") or 3
		frappe.db.set_value("AI Provider", PROVIDER_B, "consecutive_failures", 0)
		frappe.db.set_value("AI Provider", PROVIDER_B, "health_status", "Unknown")
		ai_client._attempt_call = stub(401, fail_for=PROVIDER_B)

		for i in range(1, threshold + 1):
			self.run_call(chain=[CHAIN[0]])
			doc = frappe.get_doc("AI Provider", PROVIDER_B)
			expected = "Down" if i >= threshold else "Degraded"
			self.assertEqual(doc.health_status, expected, f"after {i} failures")
			self.assertEqual(doc.consecutive_failures, i)

	def test_failure_records_the_error_on_the_provider(self):
		frappe.db.set_value("AI Provider", PROVIDER_B, "consecutive_failures", 0)
		ai_client._attempt_call = stub(401, fail_for=PROVIDER_B)
		self.run_call(chain=[CHAIN[0]])
		self.assertIn("Auth", frappe.db.get_value("AI Provider", PROVIDER_B, "last_error"))

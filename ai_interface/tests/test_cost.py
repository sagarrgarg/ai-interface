"""Cost, currency conversion and error classification.

The rule these protect: an unresolvable exchange rate must store 0, never a
silent 1.0. Treating a hundred rupees as a hundred dollars is the failure the
two-currency model exists to prevent.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.providers.base import ProviderHTTPError
from ai_interface.services.ai_client import (
	_base_currency,
	_calculate_cost,
	_classify_error,
	_exchange_rate,
)
from ai_interface.tests.utils import (
	PROVIDER_A,
	TYPE_OPENAI,
	cleanup,
	make_provider,
	make_provider_type,
)


class FakeProvider:
	"""Just enough of an AI Provider for the rate helper."""

	def __init__(self, currency, exchange_rate=0):
		self.currency = currency
		self.exchange_rate = exchange_rate


class TestExchangeRate(FrappeTestCase):
	def test_same_currency_is_one(self):
		self.assertEqual(_exchange_rate(FakeProvider("USD"), "USD"), 1.0)

	def test_manual_override_is_used(self):
		self.assertEqual(_exchange_rate(FakeProvider("INR", 0.012), "USD"), 0.012)

	def test_unresolvable_rate_returns_zero_not_one(self):
		# ZWL has no Currency Exchange record; the result must be 0 so the call
		# is visibly unconverted rather than silently counted at 1:1.
		self.assertEqual(_exchange_rate(FakeProvider("ZWL"), "USD"), 0.0)

	def test_base_currency_falls_back_when_unset(self):
		self.assertTrue(_base_currency())


class TestCostCalculation(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup()
		make_provider_type(TYPE_OPENAI)
		cls.provider = make_provider(PROVIDER_A, TYPE_OPENAI, models=[
			{"model_id": "priced", "cost_in": 0.000003, "cost_out": 0.000015},
			{"model_id": "unpriced", "cost_in": 0, "cost_out": 0},
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cleanup()
		super().tearDownClass()

	def test_cost_is_tokens_times_rates(self):
		cost = _calculate_cost(self.provider, "priced", 1000, 500)
		self.assertAlmostEqual(cost, 1000 * 0.000003 + 500 * 0.000015, places=9)

	def test_unpriced_model_costs_zero(self):
		self.assertEqual(_calculate_cost(self.provider, "unpriced", 1000, 500), 0)

	def test_unknown_model_costs_zero_rather_than_raising(self):
		# A model can vanish from the catalog while its logs remain; costing must
		# degrade, not explode.
		self.assertEqual(_calculate_cost(self.provider, "never-heard-of-it", 10, 10), 0.0)

	def test_pricing_source_marks_hand_entered_rates_manual(self):
		self.provider.reload()
		by_id = {m.model_id: m for m in self.provider.models}
		self.assertEqual(by_id["priced"].pricing_source, "Manual")
		self.assertEqual(by_id["unpriced"].pricing_source, "Unpriced")


class TestErrorClassification(FrappeTestCase):
	def test_status_code_drives_the_taxonomy(self):
		cases = {
			401: "Auth",
			403: "Auth",
			404: "Config Error",
			429: "Rate Limit",
			500: "Provider Error",
			503: "Provider Error",
			504: "Timeout",
			529: "Rate Limit",
		}
		for status, expected in cases.items():
			with self.subTest(status=status):
				self.assertEqual(_classify_error(ProviderHTTPError("x", status)), expected)

	def test_falls_back_to_text_when_there_is_no_status(self):
		self.assertEqual(_classify_error(RuntimeError("request timed out")), "Timeout")
		self.assertEqual(_classify_error(RuntimeError("invalid api key")), "Auth")

	def test_unrecognised_failure_is_unknown_not_misfiled(self):
		self.assertEqual(_classify_error(RuntimeError("something odd happened")), "Unknown")

	def test_no_vendor_name_decides_a_classification(self):
		# Classification used to match the literal string "anthropic"; a vendor
		# rewording its errors must not reshuffle the dashboard.
		self.assertEqual(_classify_error(RuntimeError("anthropic says hello")), "Unknown")

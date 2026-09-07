"""Spend caps.

The contract: checks run before a call is queued, Warn never raises, Block
always does, and a per-app cap constrains only that app.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.services import budget
from ai_interface.tests.utils import PREFIX, cleanup, settings_for_test

APP = f"{PREFIX}budgetapp"
OTHER = f"{PREFIX}otherapp"


def log_spend(app, base_cost, days_ago=0):
	"""Write a completed call log directly — cheaper and more precise than
	routing a real call, and these tests are about arithmetic, not providers."""
	when = frappe.utils.add_days(frappe.utils.nowdate(), -days_ago)
	frappe.db.sql(
		"""INSERT INTO `tabAI Call Log`
		   (name, creation, modified, owner, modified_by, docstatus, idx,
		    status, calling_app, cost, base_cost, base_currency, currency)
		   VALUES (%s, %s, %s, 'Administrator', 'Administrator', 0, 0,
		           'Completed', %s, %s, %s, 'USD', 'USD')""",
		(frappe.generate_hash(length=10), when, when, app, base_cost, base_cost),
	)
	frappe.db.commit()


class TestBudget(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup()
		log_spend(APP, 10.0)
		log_spend(OTHER, 4.0)

	@classmethod
	def tearDownClass(cls):
		cleanup()
		super().tearDownClass()

	def test_disabled_caps_never_interfere(self):
		s = settings_for_test(enable_budget=0, monthly_budget=0.01)
		self.assertIsNone(budget.check(s, APP))

	def test_zero_cap_means_no_cap(self):
		s = settings_for_test(enable_budget=1, daily_budget=0, monthly_budget=0)
		self.assertIsNone(budget.check(s, APP))

	def test_under_budget_passes(self):
		s = settings_for_test(enable_budget=1, monthly_budget=999999)
		self.assertIsNone(budget.check(s, APP))

	def test_warn_reports_a_breach_without_raising(self):
		s = settings_for_test(enable_budget=1, monthly_budget=0.01, budget_action="Warn")
		breach = budget.check(s, APP)
		self.assertIsNotNone(breach)
		self.assertEqual(breach["period"], "monthly")
		self.assertGreater(breach["used"], breach["cap"])

	def test_block_raises(self):
		s = settings_for_test(enable_budget=1, monthly_budget=0.01, budget_action="Block")
		with self.assertRaises(frappe.ValidationError) as ctx:
			budget.check(s, APP)
		self.assertIn("budget exhausted", str(ctx.exception).lower())

	def test_daily_cap_is_independent_of_monthly(self):
		s = settings_for_test(
			enable_budget=1, daily_budget=0.01, monthly_budget=999999, budget_action="Block"
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			budget.check(s, APP)
		self.assertIn("daily", str(ctx.exception).lower())

	def test_per_app_cap_constrains_only_that_app(self):
		s = settings_for_test(enable_budget=1, monthly_budget=999999, budget_action="Warn")
		s.append("app_budgets", {
			"calling_app": APP, "monthly_budget": 0.01, "budget_action": "Block",
		})
		with self.assertRaises(frappe.ValidationError):
			budget.check(s, APP)
		# the same settings must leave a different app alone
		self.assertIsNone(budget.check(s, OTHER))

	def test_block_wins_when_any_applicable_rule_blocks(self):
		s = settings_for_test(enable_budget=1, monthly_budget=0.01, budget_action="Warn")
		s.append("app_budgets", {
			"calling_app": APP, "monthly_budget": 0.01, "budget_action": "Block",
		})
		with self.assertRaises(frappe.ValidationError):
			budget.check(s, APP)

	def test_app_cap_inherits_global_action_when_blank(self):
		s = settings_for_test(enable_budget=1, monthly_budget=999999, budget_action="Block")
		s.append("app_budgets", {"calling_app": APP, "monthly_budget": 0.01})
		with self.assertRaises(frappe.ValidationError):
			budget.check(s, APP)

	def test_status_reports_usage_for_the_dashboard(self):
		s = frappe.get_single("AI Settings")
		before = {
			"enable_budget": s.enable_budget,
			"daily_budget": s.daily_budget,
			"monthly_budget": s.monthly_budget,
		}
		frappe.db.set_single_value("AI Settings", "enable_budget", 1)
		frappe.db.set_single_value("AI Settings", "monthly_budget", 100)
		frappe.db.set_single_value("AI Settings", "daily_budget", 0)
		frappe.clear_cache()
		try:
			status = budget.get_status()
			self.assertTrue(status["enabled"])
			monthly = [p for p in status["periods"] if p["period"] == "monthly"]
			self.assertEqual(len(monthly), 1)
			self.assertGreater(monthly[0]["used"], 0)
			# a zero cap is omitted rather than rendered as an empty bar
			self.assertNotIn("daily", [p["period"] for p in status["periods"]])
		finally:
			for k, v in before.items():
				frappe.db.set_single_value("AI Settings", k, v)
			frappe.clear_cache()

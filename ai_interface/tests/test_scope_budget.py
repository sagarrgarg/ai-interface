"""Budgets for a caller's own scope (one AI agent, one team): caller caps and admin caps."""

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.services import budget
from ai_interface.tests.utils import PREFIX, cleanup, settings_for_test

APP = f"{PREFIX}scope"
AGENT = "Excom Agent:QA-1"
OTHER = "Excom Agent:QA-2"


def spend(scope, amount):
	frappe.get_doc(
		{
			"doctype": "AI Call Log",
			"status": "Completed",
			"calling_app": APP,
			"budget_scope": scope,
			"base_cost": amount,
			"cost": amount,
		}
	).insert(ignore_permissions=True)


class TestScopeBudget(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup()
		spend(AGENT, 4.0)
		spend(OTHER, 0.5)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cleanup()
		super().tearDownClass()

	def test_caller_caps_hold_even_with_global_budgets_off(self):
		settings = settings_for_test(enable_budget=0)
		with self.assertRaises(budget.BudgetExceeded):
			budget.check(settings, APP, scope=AGENT, caps={"daily": 3.0, "action": "Block"})
		self.assertIsNone(
			budget.check(settings, APP, scope=OTHER, caps={"daily": 3.0, "action": "Block"}),
			"another agent's spend is its own",
		)
		self.assertIsNone(budget.check(settings, APP), "no scope, budgets off: nothing to check")

	def test_warn_lets_the_call_through(self):
		settings = settings_for_test(enable_budget=0)
		breach = budget.check(settings, APP, scope=AGENT, caps={"daily": 3.0, "action": "Warn"})
		self.assertEqual((breach["scope"], breach["action"]), (AGENT, "Warn"))

	def test_admin_caps_by_exact_scope_and_by_prefix(self):
		settings = settings_for_test(enable_budget=0)
		settings.append(
			"scope_budgets", {"scope": "Excom Agent:*", "daily_budget": 1.0, "budget_action": "Block"}
		)
		with self.assertRaises(budget.BudgetExceeded):
			budget.check(settings, APP, scope=AGENT)
		self.assertIsNone(
			budget.check(settings, APP, scope=OTHER), "each scope under a prefix is measured on its own"
		)
		settings = settings_for_test(enable_budget=0)
		settings.append("scope_budgets", {"scope": OTHER, "monthly_budget": 0.4, "budget_action": "Block"})
		with self.assertRaises(budget.BudgetExceeded):
			budget.check(settings, APP, scope=OTHER)

	def test_under_the_cap(self):
		settings = settings_for_test(enable_budget=0)
		self.assertIsNone(
			budget.check(settings, APP, scope=AGENT, caps={"daily": 10, "monthly": 100, "action": "Block"})
		)

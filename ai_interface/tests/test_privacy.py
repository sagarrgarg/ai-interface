"""What the call log keeps: masked contacts, emptied old prompts, rows and cost kept."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from ai_interface.services import privacy
from ai_interface.tests.utils import PREFIX, cleanup, settings_for_test

APP = f"{PREFIX}privacy"


class TestRedaction(FrappeTestCase):
	def test_contacts_are_masked_and_business_numbers_kept(self):
		cases = {
			"Call +91 98765 43210 or (07) 3275 2588": "Call [phone] or [phone]",
			"write to jane.doe@shop.com.au": "write to [email]",
			"Invoice SINV-2026-00123 on 2026-11-09 10:30": "Invoice SINV-2026-00123 on 2026-11-09 10:30",
			"$0.36 each for 3000 pcs, total 1080.00": "$0.36 each for 3000 pcs, total 1080.00",
			"Due 09/11/2026, order 12345": "Due 09/11/2026, order 12345",
		}
		for raw, masked in cases.items():
			self.assertEqual(privacy.redact(raw), masked, raw)

	def test_policy_per_app(self):
		settings = settings_for_test(payload_retention_days=30, redact_contacts=0)
		settings.app_policies = []
		settings.append(
			"app_policies", {"calling_app": "excom", "payload_retention_days": 7, "redact_contacts": "Yes"}
		)
		self.assertEqual(privacy.policy("excom", settings), {"retention_days": 7, "redact": True})
		self.assertEqual(privacy.policy("docpulse", settings), {"retention_days": 30, "redact": False})
		self.assertEqual(privacy.for_storage("mail a@b.co", "excom", settings), "mail [email]")
		self.assertEqual(privacy.for_storage("mail a@b.co", "docpulse", settings), "mail a@b.co")


class TestRetention(FrappeTestCase):
	@classmethod
	def tearDownClass(cls):
		cleanup()
		super().tearDownClass()

	def _log(self, app, days_old):
		log = frappe.get_doc(
			{
				"doctype": "AI Call Log",
				"status": "Completed",
				"calling_app": app,
				"input_text": "prompt",
				"output_text": "answer",
				"base_cost": 0.01,
			}
		).insert(ignore_permissions=True)
		frappe.db.sql(
			"UPDATE `tabAI Call Log` SET creation = %s WHERE name = %s",
			(add_days(now_datetime(), -days_old), log.name),
		)
		return log.name

	def test_old_prompts_are_emptied_and_the_row_kept(self):
		settings = settings_for_test(payload_retention_days=30)
		settings.app_policies = []
		settings.append("app_policies", {"calling_app": APP + "short", "payload_retention_days": 3})
		old_default = self._log(APP, 40)
		young_default = self._log(APP, 10)
		old_short = self._log(APP + "short", 5)
		with patch("frappe.get_single", return_value=settings):
			privacy.purge_payloads()
		row = frappe.db.get_value(
			"AI Call Log",
			old_default,
			["input_text", "output_text", "payload_purged", "base_cost"],
			as_dict=True,
		)
		self.assertEqual(
			(row.input_text, row.output_text, row.payload_purged, row.base_cost), ("", "", 1, 0.01)
		)
		self.assertEqual(frappe.db.get_value("AI Call Log", young_default, "input_text"), "prompt")
		self.assertEqual(
			frappe.db.get_value("AI Call Log", old_short, "payload_purged"),
			1,
			"the app's own, shorter period",
		)

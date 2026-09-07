"""Rate limits, message caps and retention.

Spend caps stop cost after it is incurred. These stop the volume that incurs it.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.ai_interface.doctype.ai_chat_conversation import ai_chat_conversation as conv
from ai_interface.services import rate_limit

USER = "_t_rate@example.com"
DOCTYPE = "AI Chat Conversation"


class FakeSettings(dict):
	"""A settings stand-in — the limiter only ever calls .get()."""

	def get(self, key, default=None):
		return dict.get(self, key, default)


class TestRateLimit(FrappeTestCase):
	def setUp(self):
		self.scope = frappe.generate_hash(length=8)

	def test_no_limit_configured_never_blocks(self):
		settings = FakeSettings(assistant_rate_per_minute=0, assistant_rate_per_hour=0)
		for _i in range(50):
			rate_limit.check(USER, scope=self.scope, settings=settings)

	def test_requests_within_the_limit_pass(self):
		settings = FakeSettings(assistant_rate_per_minute=3, assistant_rate_per_hour=0)
		for _i in range(3):
			rate_limit.check(USER, scope=self.scope, settings=settings)

	def test_one_request_past_the_limit_is_refused(self):
		settings = FakeSettings(assistant_rate_per_minute=3, assistant_rate_per_hour=0)
		for _i in range(3):
			rate_limit.check(USER, scope=self.scope, settings=settings)
		with self.assertRaises(frappe.ValidationError) as ctx:
			rate_limit.check(USER, scope=self.scope, settings=settings)
		self.assertIn("wait a moment", str(ctx.exception))

	def test_the_limit_is_per_user(self):
		# One noisy user must not lock everyone else out.
		settings = FakeSettings(assistant_rate_per_minute=2, assistant_rate_per_hour=0)
		for _i in range(2):
			rate_limit.check(USER, scope=self.scope, settings=settings)
		with self.assertRaises(frappe.ValidationError):
			rate_limit.check(USER, scope=self.scope, settings=settings)

		rate_limit.check("_t_other@example.com", scope=self.scope, settings=settings)

	def test_the_hourly_limit_applies_independently(self):
		settings = FakeSettings(assistant_rate_per_minute=0, assistant_rate_per_hour=2)
		for _i in range(2):
			rate_limit.check(USER, scope=self.scope, settings=settings)
		with self.assertRaises(frappe.ValidationError) as ctx:
			rate_limit.check(USER, scope=self.scope, settings=settings)
		self.assertIn("hour", str(ctx.exception))

	def test_usage_reports_without_incrementing(self):
		settings = FakeSettings(assistant_rate_per_minute=5, assistant_rate_per_hour=0)
		rate_limit.check(USER, scope=self.scope, settings=settings)
		first = rate_limit.usage(USER, scope=self.scope)["minute"]
		second = rate_limit.usage(USER, scope=self.scope)["minute"]
		self.assertEqual(first, second)


class TestConversationTrimming(FrappeTestCase):
	def tearDown(self):
		for name in frappe.get_all(DOCTYPE, filters={"user": "Administrator"}, pluck="name"):
			frappe.delete_doc(DOCTYPE, name, force=True, ignore_permissions=True)
		frappe.db.set_single_value("AI Settings", "max_messages_per_conversation", 40)

	def build(self, count):
		doc = frappe.new_doc(DOCTYPE)
		doc.user = "Administrator"
		for i in range(count):
			doc.append("messages", {
				"role": "User" if i % 2 == 0 else "Assistant",
				"content": f"m{i}",
			})
		doc.insert(ignore_permissions=True)
		return doc

	def test_a_thread_under_the_cap_is_untouched(self):
		frappe.db.set_single_value("AI Settings", "max_messages_per_conversation", 10)
		self.assertEqual(len(self.build(6).messages), 6)

	def test_a_thread_over_the_cap_keeps_the_newest(self):
		frappe.db.set_single_value("AI Settings", "max_messages_per_conversation", 6)
		doc = self.build(20)
		self.assertEqual(len(doc.messages), 6)
		# Dropping the newest would break follow-ups, which is the whole point
		# of keeping history at all.
		self.assertEqual(doc.messages[-1].content, "m19")

	def test_trimming_renumbers_rows(self):
		frappe.db.set_single_value("AI Settings", "max_messages_per_conversation", 4)
		doc = self.build(12)
		self.assertEqual([m.idx for m in doc.messages], [1, 2, 3, 4])

	def test_a_cap_of_zero_means_no_cap(self):
		frappe.db.set_single_value("AI Settings", "max_messages_per_conversation", 0)
		self.assertEqual(len(self.build(30).messages), 30)


class TestRetention(FrappeTestCase):
	def tearDown(self):
		for name in frappe.get_all(DOCTYPE, filters={"user": "Administrator"}, pluck="name"):
			frappe.delete_doc(DOCTYPE, name, force=True, ignore_permissions=True)
		frappe.db.set_single_value("AI Settings", "conversation_retention_days", 30)

	def make(self, days_old):
		doc = frappe.new_doc(DOCTYPE)
		doc.user = "Administrator"
		doc.append("messages", {"role": "User", "content": "hello"})
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(
			DOCTYPE, doc.name, "last_active",
			frappe.utils.add_days(frappe.utils.nowdate(), -days_old),
			update_modified=False,
		)
		return doc.name

	def test_stale_conversations_are_removed(self):
		frappe.db.set_single_value("AI Settings", "conversation_retention_days", 10)
		old, fresh = self.make(40), self.make(1)
		conv.clear_old_conversations()
		self.assertFalse(frappe.db.exists(DOCTYPE, old))
		self.assertTrue(frappe.db.exists(DOCTYPE, fresh))

	def test_retention_of_zero_keeps_everything(self):
		frappe.db.set_single_value("AI Settings", "conversation_retention_days", 0)
		old = self.make(400)
		conv.clear_old_conversations()
		self.assertTrue(frappe.db.exists(DOCTYPE, old))

"""Conversation memory and its privacy boundary.

No AI call is made — the planner is stubbed, so these exercise storage, history
shaping and who is allowed to read whose thread.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.ai_interface.doctype.ai_chat_conversation import ai_chat_conversation as conv
from ai_interface.api import assistant
from ai_interface.services import query_engine

DOCTYPE = "AI Chat Conversation"
OTHER_USER = "_t_assistant_other@example.com"


class FakeDoc:
	def __init__(self, user):
		self.user = user


def make_conversation(user="Administrator", turns=()):
	doc = frappe.new_doc(DOCTYPE)
	doc.user = user
	for role, content in turns:
		doc.append("messages", {"role": role, "content": content})
	doc.insert(ignore_permissions=True)
	return doc


class TestConversationPrivacy(FrappeTestCase):
	def test_owner_may_read_their_own(self):
		self.assertTrue(conv.has_permission(FakeDoc("a@x.com"), "read", "a@x.com"))

	def test_a_stranger_may_not(self):
		# The whole point of the doctype: one person's questions, and the answers
		# drawn from their permissions, are not another person's business.
		self.assertFalse(conv.has_permission(FakeDoc("a@x.com"), "read", "b@x.com"))

	def test_system_manager_may(self):
		self.assertTrue(conv.has_permission(FakeDoc("a@x.com"), "read", "Administrator"))

	def test_list_view_is_scoped_for_ordinary_users(self):
		condition = conv.get_permission_query_conditions("nobody@example.com")
		self.assertIn("nobody@example.com", condition)
		self.assertIn("`user`", condition)

	def test_list_view_is_unscoped_for_system_managers(self):
		self.assertEqual(conv.get_permission_query_conditions("Administrator"), "")

	def test_loading_someone_elses_thread_is_refused(self):
		"""The API's own loader, not just the permission hook.

		`_load` is what every endpoint goes through, so the check has to hold
		there — a correct hook is no help if the loader never consults it.
		"""
		doc = make_conversation(user="Administrator")
		try:
			# Reassigned at the database level so the Link field is never asked
			# to resolve a user that does not exist.
			frappe.db.set_value(DOCTYPE, doc.name, "user", OTHER_USER, update_modified=False)
			original_roles = frappe.get_roles

			# Administrator is a System Manager, and a manager is allowed to
			# read anything — so the check is proven against someone who is not.
			frappe.get_roles = lambda *a, **k: ["AI User"]
			with self.assertRaises(frappe.PermissionError):
				assistant._load(doc.name)
		finally:
			frappe.get_roles = original_roles
			frappe.db.set_value(DOCTYPE, doc.name, "user", "Administrator", update_modified=False)
			frappe.delete_doc(DOCTYPE, doc.name, force=True, ignore_permissions=True)


class TestConversationStorage(FrappeTestCase):
	def tearDown(self):
		for name in frappe.get_all(DOCTYPE, filters={"user": "Administrator"}, pluck="name"):
			frappe.delete_doc(DOCTYPE, name, force=True, ignore_permissions=True)

	def test_title_comes_from_the_first_question(self):
		doc = make_conversation(turns=[("User", "How many invoices are overdue?")])
		self.assertEqual(doc.title, "How many invoices are overdue?")

	def test_title_is_truncated(self):
		doc = make_conversation(turns=[("User", "x" * 200)])
		self.assertLessEqual(len(doc.title), 80)

	def test_message_count_tracks_the_table(self):
		doc = make_conversation(turns=[("User", "a"), ("Assistant", "b"), ("User", "c")])
		self.assertEqual(doc.message_count, 3)

	def test_user_defaults_to_the_session(self):
		doc = frappe.new_doc(DOCTYPE)
		doc.append("messages", {"role": "User", "content": "hello"})
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.user, frappe.session.user)

	def test_history_pairs_questions_with_answers(self):
		doc = make_conversation(turns=[
			("User", "q1"), ("Assistant", "a1"),
			("User", "q2"), ("Assistant", "a2"),
		])
		self.assertEqual(doc.history(), [
			{"question": "q1", "answer": "a1"},
			{"question": "q2", "answer": "a2"},
		])

	def test_history_ignores_a_question_still_awaiting_its_answer(self):
		doc = make_conversation(turns=[("User", "q1"), ("Assistant", "a1"), ("User", "q2")])
		self.assertEqual(doc.history(), [{"question": "q1", "answer": "a1"}])

	def test_history_is_capped(self):
		turns = []
		for i in range(20):
			turns += [("User", f"q{i}"), ("Assistant", f"a{i}")]
		doc = make_conversation(turns=turns)
		history = doc.history()
		self.assertEqual(len(history), conv.HISTORY_TURNS)
		# The cap keeps the most recent turns, not the oldest.
		self.assertEqual(history[-1]["question"], "q19")


class TestAskFlow(FrappeTestCase):
	"""The API path, with the planner stubbed so nothing is spent."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._real_answer = query_engine.answer
		frappe.db.set_single_value("AI Settings", "enable_assistant", 1)

	@classmethod
	def tearDownClass(cls):
		query_engine.answer = cls._real_answer
		super().tearDownClass()

	def tearDown(self):
		query_engine.answer = self._real_answer
		for name in frappe.get_all(DOCTYPE, filters={"user": "Administrator"}, pluck="name"):
			frappe.delete_doc(DOCTYPE, name, force=True, ignore_permissions=True)

	def test_a_question_creates_a_conversation_and_two_messages(self):
		query_engine.answer = lambda q, user=None, history=None: {
			"answer": "There are 7.", "query": {"doctype": "User"}, "row_count": 1, "rows": [],
		}
		out = assistant.ask("How many users?")
		self.assertTrue(out["ok"])
		self.assertTrue(out["conversation"])

		doc = frappe.get_doc(DOCTYPE, out["conversation"])
		self.assertEqual([m.role for m in doc.messages], ["User", "Assistant"])

	def test_history_is_passed_to_the_second_question(self):
		seen = {}

		def fake(q, user=None, history=None):
			seen["history"] = history
			return {"answer": "ok", "query": {"doctype": "User"}, "row_count": 0, "rows": []}

		query_engine.answer = fake
		first = assistant.ask("How many users?")
		assistant.ask("And how many are enabled?", conversation=first["conversation"])

		# Without this the follow-up is unanswerable: it names nothing.
		self.assertEqual(seen["history"], [{"question": "How many users?", "answer": "ok"}])

	def test_a_refusal_is_recorded_rather_than_raised(self):
		def refuse(q, user=None, history=None):
			raise query_engine.QueryRefused("You do not have access to Salary Slip.")

		query_engine.answer = refuse
		out = assistant.ask("Show me salaries")
		self.assertFalse(out["ok"])
		self.assertIn("do not have access", out["answer"])

		doc = frappe.get_doc(DOCTYPE, out["conversation"])
		self.assertEqual(doc.messages[-1].role, "Assistant")

	def test_empty_question_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			assistant.ask("   ")

	def test_overlong_question_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			assistant.ask("x" * (assistant.MAX_QUESTION + 1))

	def test_config_reports_whether_the_widget_should_render(self):
		config = assistant.get_config()
		self.assertIn("enabled", config)
		self.assertIsInstance(config["enabled"], bool)

	def test_disabled_assistant_refuses(self):
		frappe.db.set_single_value("AI Settings", "enable_assistant", 0)
		try:
			self.assertFalse(assistant.get_config()["enabled"])
			with self.assertRaises(frappe.PermissionError):
				assistant.ask("anything")
		finally:
			frappe.db.set_single_value("AI Settings", "enable_assistant", 1)

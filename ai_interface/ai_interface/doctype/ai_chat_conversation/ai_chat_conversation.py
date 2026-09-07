import frappe
from frappe import _
from frappe.model.document import Document

# Enough turns for follow-ups to make sense, few enough that an old conversation
# does not quietly inflate every prompt.
HISTORY_TURNS = 6


class AIChatConversation(Document):
	def validate(self):
		self.enforce_ownership()
		self.message_count = len(self.messages or [])
		self.last_active = frappe.utils.now()
		if not self.title and self.messages:
			first = next((m for m in self.messages if m.role == "User"), None)
			if first:
				self.title = (first.content or "")[:80]

	def enforce_ownership(self):
		"""A conversation belongs to one person.

		Permissions are `if_owner` for AI User, but System Manager can read
		everything, so ownership is asserted here as well rather than assumed
		from the permission layer alone.
		"""
		if not self.user:
			self.user = frappe.session.user
		if self.is_new():
			return
		if self.user != frappe.session.user and "System Manager" not in frappe.get_roles():
			frappe.throw(_("This conversation belongs to someone else."), frappe.PermissionError)

	def history(self) -> list[dict]:
		"""Recent turns as question/answer pairs for the prompt."""
		turns, pending = [], None
		for msg in self.messages or []:
			if msg.role == "User":
				pending = msg.content
			elif pending is not None:
				turns.append({"question": pending, "answer": msg.content})
				pending = None
		return turns[-HISTORY_TURNS:]


def has_permission(doc, ptype, user):
	"""Nobody reads another person's conversation but a System Manager."""
	if "System Manager" in frappe.get_roles(user):
		return True
	return doc.user == user


def get_permission_query_conditions(user):
	"""Scope list views to the reader's own conversations."""
	user = user or frappe.session.user
	if "System Manager" in frappe.get_roles(user):
		return ""
	return f"`tabAI Chat Conversation`.`user` = {frappe.db.escape(user)}"

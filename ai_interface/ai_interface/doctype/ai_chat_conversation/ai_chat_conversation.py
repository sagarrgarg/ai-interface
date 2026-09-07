import frappe
from frappe import _
from frappe.model.document import Document

# Enough turns for follow-ups to make sense, few enough that an old conversation
# does not quietly inflate every prompt.
HISTORY_TURNS = 6


class AIChatConversation(Document):
	def validate(self):
		self.enforce_ownership()
		self.trim()
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

	def trim(self):
		"""Drop the oldest turns past the configured ceiling.

		A thread nobody ever closes would otherwise grow without bound, and the
		document is loaded in full on every question.
		"""
		cap = int(frappe.db.get_single_value("AI Settings", "max_messages_per_conversation") or 0)
		if cap > 0 and len(self.messages or []) > cap:
			self.messages = self.messages[-cap:]
			for i, row in enumerate(self.messages, start=1):
				row.idx = i

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


def clear_old_conversations():
	"""Scheduled: forget threads nobody has touched in a long time.

	Questions people ask carry business context, so they are not kept
	indefinitely by default.
	"""
	days = int(frappe.db.get_single_value("AI Settings", "conversation_retention_days") or 0)
	if days <= 0:
		return

	cutoff = frappe.utils.add_days(frappe.utils.nowdate(), -days)
	stale = frappe.get_all(
		"AI Chat Conversation",
		filters={"last_active": ["<", cutoff]},
		pluck="name",
		limit_page_length=500,
	)
	for name in stale:
		frappe.delete_doc("AI Chat Conversation", name, force=True, ignore_permissions=True)

	if stale:
		frappe.db.commit()

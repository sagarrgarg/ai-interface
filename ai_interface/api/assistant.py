"""Whitelisted entry points for the site assistant.

Everything here runs as the calling user — never elevated. The assistant is a
convenience over data the person could already open, not a way around the
permissions that decide what they may open.
"""

import json

import frappe
from frappe import _

from ai_interface.services import query_engine

ROLES = {"System Manager", "AI User"}
MAX_QUESTION = 1000
CONVERSATION = "AI Chat Conversation"


def _check_access():
	if not frappe.db.get_single_value("AI Settings", "enable_assistant"):
		frappe.throw(_("The site assistant is turned off."), frappe.PermissionError)

	if not ROLES & set(frappe.get_roles(frappe.session.user)):
		frappe.throw(_("You need the AI User role to use the assistant."), frappe.PermissionError)


def _load(conversation: str | None):
	"""Fetch a conversation the caller owns, or start a new one."""
	if conversation and frappe.db.exists(CONVERSATION, conversation):
		doc = frappe.get_doc(CONVERSATION, conversation)
		if doc.user != frappe.session.user and "System Manager" not in frappe.get_roles():
			frappe.throw(_("This conversation belongs to someone else."), frappe.PermissionError)
		return doc

	doc = frappe.new_doc(CONVERSATION)
	doc.user = frappe.session.user
	return doc


@frappe.whitelist()
def ask(question: str, conversation: str | None = None) -> dict:
	"""Answer a question about this site, in the context of a conversation.

	Returns the answer together with the query behind it, so a reader can check
	the result rather than take it on trust.
	"""
	_check_access()

	question = (question or "").strip()
	if not question:
		frappe.throw(_("Ask a question first."))
	if len(question) > MAX_QUESTION:
		frappe.throw(_("That question is too long. Try a shorter one."))

	doc = _load(conversation)
	history = doc.history() if not doc.is_new() else []

	doc.append("messages", {"role": "User", "content": question})

	try:
		result = query_engine.answer(question, user=frappe.session.user, history=history)
	except query_engine.QueryRefused as e:
		# An expected refusal — no access, no usable query — is an answer, not a
		# crash. Recorded in the conversation so the thread stays coherent.
		message = str(e)
		doc.append("messages", {"role": "Assistant", "content": message})
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return {"ok": False, "answer": message, "query": None, "conversation": doc.name}
	except Exception as e:
		frappe.log_error(title="AI Interface: assistant failed", message=frappe.get_traceback())
		# The conversation is not saved here: a half-written turn is worse than
		# none, and the question is still in the user's input box.
		return {"ok": False, "answer": _("Something went wrong: {0}").format(str(e)[:300]),
		        "query": None, "conversation": conversation}

	show_query = bool(frappe.db.get_single_value("AI Settings", "assistant_show_query"))

	doc.append("messages", {
		"role": "Assistant",
		"content": result["answer"],
		"query_json": json.dumps(result["query"], default=str, indent=2),
		"row_count": result["row_count"],
	})
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"ok": True,
		"answer": result["answer"],
		"query": result["query"] if show_query else None,
		"row_count": result["row_count"],
		"conversation": doc.name,
		"title": doc.title,
	}


@frappe.whitelist()
def get_config() -> dict:
	"""What the widget needs before it renders anything."""
	enabled = bool(frappe.db.get_single_value("AI Settings", "enable_assistant"))
	permitted = bool(ROLES & set(frappe.get_roles(frappe.session.user)))
	return {
		"enabled": bool(enabled and permitted),
		"show_query": bool(frappe.db.get_single_value("AI Settings", "assistant_show_query")),
		"user": frappe.session.user,
		"greeting": _("Ask me anything about this site."),
	}


@frappe.whitelist()
def get_conversation(conversation: str) -> dict:
	"""Reload a thread, so the panel survives a page reload."""
	_check_access()
	doc = _load(conversation)
	if doc.is_new():
		return {"conversation": None, "messages": []}

	return {
		"conversation": doc.name,
		"title": doc.title,
		"messages": [
			{
				"role": m.role,
				"content": m.content,
				"query": json.loads(m.query_json) if m.query_json else None,
				"row_count": m.row_count,
			}
			for m in doc.messages
		],
	}


@frappe.whitelist()
def list_conversations(limit: int = 15) -> list[dict]:
	"""The caller's own recent threads, newest first."""
	_check_access()
	return frappe.get_all(
		CONVERSATION,
		filters={"user": frappe.session.user},
		fields=["name", "title", "last_active", "message_count"],
		order_by="last_active desc",
		limit_page_length=int(limit),
	)


@frappe.whitelist()
def delete_conversation(conversation: str) -> dict:
	_check_access()
	doc = _load(conversation)
	if doc.is_new():
		return {"ok": True}
	frappe.delete_doc(CONVERSATION, doc.name, ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}

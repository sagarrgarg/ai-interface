"""Whitelisted entry points for the site assistant.

Everything here runs as the calling user — never elevated. The assistant is a
convenience over data the person could already open, not a way around the
permissions that decide what they may open.
"""

import frappe
from frappe import _

from ai_interface.services import query_engine

ROLES = {"System Manager", "AI User"}


def _check_access():
	if not frappe.db.get_single_value("AI Settings", "enable_assistant"):
		frappe.throw(_("The site assistant is turned off."), frappe.PermissionError)

	if not ROLES & set(frappe.get_roles(frappe.session.user)):
		frappe.throw(_("You need the AI User role to use the assistant."), frappe.PermissionError)


@frappe.whitelist()
def ask(question: str, history=None) -> dict:
	"""Answer a question about this site.

	Returns the answer together with the query behind it, so a reader can check
	the result rather than take it on trust.
	"""
	_check_access()

	question = (question or "").strip()
	if not question:
		frappe.throw(_("Ask a question first."))
	if len(question) > 1000:
		frappe.throw(_("That question is too long. Try a shorter one."))

	history = _parse_history(history)

	try:
		result = query_engine.answer(question, user=frappe.session.user, history=history)
	except query_engine.QueryRefused as e:
		# An expected refusal — no access, no usable query — is an answer, not a
		# crash. The UI should show the reason rather than a stack trace.
		return {"ok": False, "answer": str(e), "query": None}

	show_query = bool(frappe.db.get_single_value("AI Settings", "assistant_show_query"))
	return {
		"ok": True,
		"answer": result["answer"],
		"query": result["query"] if show_query else None,
		"row_count": result["row_count"],
	}


@frappe.whitelist()
def get_config() -> dict:
	"""What the widget needs before it renders anything."""
	enabled = bool(frappe.db.get_single_value("AI Settings", "enable_assistant"))
	permitted = bool(ROLES & set(frappe.get_roles(frappe.session.user)))
	return {
		"enabled": enabled and permitted,
		"show_query": bool(frappe.db.get_single_value("AI Settings", "assistant_show_query")),
		"user": frappe.session.user,
	}


def _parse_history(history):
	if not history:
		return []
	if isinstance(history, str):
		import json

		try:
			history = json.loads(history)
		except (json.JSONDecodeError, ValueError):
			return []
	if not isinstance(history, list):
		return []
	# Only the last few turns, and only the two keys the prompt uses.
	return [
		{"question": str(t.get("question", ""))[:500], "answer": str(t.get("answer", ""))[:500]}
		for t in history[-6:]
		if isinstance(t, dict)
	]

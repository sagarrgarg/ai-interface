import frappe
from frappe.model.document import Document


class AICallLog(Document):
	pass


def on_doctype_update():
	"""Indexes for the AI Command Center dashboard aggregations."""
	frappe.db.add_index("AI Call Log", ["status", "creation"])
	frappe.db.add_index("AI Call Log", ["calling_app", "creation"])
	frappe.db.add_index("AI Call Log", ["module", "creation"])
	frappe.db.add_index("AI Call Log", ["reference_doctype"])
	frappe.db.add_index("AI Call Log", ["provider", "model"])

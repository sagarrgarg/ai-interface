"""Create the role that grants access to the site assistant.

Separate from AI Manager on purpose: AI Manager is about *watching* AI spend,
this is about *asking questions of the site*. Someone may reasonably have either
without the other.

The role grants no data access of its own — the assistant queries as the person
asking, so what they can see is still decided entirely by their existing roles.
"""

import frappe

ROLE = "AI User"


def execute():
	if frappe.db.exists("Role", ROLE):
		return

	frappe.get_doc({
		"doctype": "Role",
		"role_name": ROLE,
		"desk_access": 1,
		"is_custom": 0,
	}).insert(ignore_permissions=True)

	frappe.db.commit()

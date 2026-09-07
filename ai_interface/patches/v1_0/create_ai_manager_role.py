import frappe


def execute():
	"""Create the AI Manager role.

	Lets someone read AI usage and cost without holding System Manager — the
	AI Command Center and AI Prompt Template both grant it.
	"""
	if frappe.db.exists("Role", "AI Manager"):
		return

	role = frappe.new_doc("Role")
	role.role_name = "AI Manager"
	role.desk_access = 1
	role.insert(ignore_permissions=True)

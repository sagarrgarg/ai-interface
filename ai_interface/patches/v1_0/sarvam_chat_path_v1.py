"""Move the seeded Sarvam provider type off the beta-gated /v2 chat endpoint.

/v2/chat/completions serves more models but is gated per account and answers
400 "currently in beta and not available" for keys without access. /v1 works for
everyone, so that is the better default. Model discovery stays on /v2/models,
which is open.

Only rewrites the value this app originally seeded — an admin who deliberately
pointed the record at /v2 (because their account does have beta access) keeps
their setting.
"""

import frappe

OLD = "/v2/chat/completions"
NEW = "/v1/chat/completions"


def execute():
	if not frappe.db.exists("AI Provider Type", "Sarvam"):
		return

	if frappe.db.get_value("AI Provider Type", "Sarvam", "chat_path") != OLD:
		return

	frappe.db.set_value("AI Provider Type", "Sarvam", "chat_path", NEW, update_modified=False)
	frappe.db.commit()
	frappe.clear_cache()

"""Fill the health-check defaults added after assistant_defaults had already run.

Same cause as that patch: a field default only applies when a document is
created, and AI Settings exists from install. A separate patch rather than an
edit to the previous one, because a patch that has already executed never runs
again.

`enable_health_checks` is deliberately absent — its default is off, NULL is
already off, and writing a 1 here would start billing pings nobody asked for.
"""

import frappe

DEFAULTS = {
	"health_check_idle_minutes": 60,
}


def execute():
	# Read the stored rows rather than get_single_value. For an Int field with
	# no row, get_single_value returns 0 — indistinguishable from an
	# administrator who deliberately set zero, so the guard skipped the field
	# and the default never landed. Absence has to mean absence.
	stored = {
		row[0]
		for row in frappe.db.sql(
			"""SELECT field FROM `tabSingles`
			   WHERE doctype = 'AI Settings' AND field IN %(fields)s""",
			{"fields": tuple(DEFAULTS)},
		)
	}

	for field, default in DEFAULTS.items():
		if field not in stored:
			frappe.db.set_single_value("AI Settings", field, default)

	frappe.db.commit()
	frappe.clear_cache()

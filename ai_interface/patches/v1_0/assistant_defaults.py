"""Apply defaults for assistant settings added after AI Settings already existed.

A field default in the doctype JSON only applies when a document is created.
AI Settings is a Single that exists from install, so every field added later
lands as NULL on an upgraded site — and a NULL rate limit is no rate limit, a
NULL message cap is no cap, and a NULL retention period never deletes anything.
The features would have shipped silently inert.

Only NULLs are filled. An administrator who deliberately set a limit to 0
meaning "off" keeps that choice.
"""

import frappe

DEFAULTS = {
	"enable_assistant": 1,
	"assistant_show_query": 1,
	"assistant_max_doctypes": 12,
	"per_user_daily_budget": 0.10,
	"assistant_rate_per_minute": 6,
	"assistant_rate_per_hour": 60,
	"max_messages_per_conversation": 40,
	"conversation_retention_days": 30,
}


def execute():
	existing = dict(
		frappe.db.sql(
			"""SELECT field, value FROM `tabSingles`
			   WHERE doctype = 'AI Settings' AND field IN %(fields)s""",
			{"fields": tuple(DEFAULTS)},
		)
		or []
	)

	for field, default in DEFAULTS.items():
		value = existing.get(field)
		if value is None or value == "":
			frappe.db.set_single_value("AI Settings", field, default)

	frappe.db.commit()
	frappe.clear_cache()

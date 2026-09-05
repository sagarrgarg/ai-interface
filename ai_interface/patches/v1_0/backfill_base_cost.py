"""Backfill the base-currency columns onto call logs written before Stage 5.

Every existing row was logged as USD with no conversion, which is exactly what
it was: a single-currency history. Copying cost into base_cost at rate 1 keeps
those totals identical rather than inventing retrospective conversions.
"""

import frappe

DEFAULT_BASE_CURRENCY = "USD"


def execute():
	# Written straight to the DB, not through .save() — AI Settings has other
	# mandatory fields a site may not have filled in yet, and a currency
	# backfill must not depend on unrelated setup being complete.
	#
	# The base defaults to USD, NOT the site's own default currency. Every cost
	# already in the table was computed from USD-denominated provider rates and
	# stamped currency="USD". Adopting an INR site default here would relabel
	# that history as rupees at rate 1 — turning $18.43 into ₹18.43, which is
	# precisely the silent mis-scaling this stage exists to prevent.
	base = frappe.db.get_single_value("AI Settings", "base_currency")
	if not base:
		base = DEFAULT_BASE_CURRENCY
		frappe.db.set_single_value("AI Settings", "base_currency", base)

	# Providers created before Stage 3 have no billing currency; they were all
	# priced in USD, so that is what they are, not the site's base currency.
	frappe.db.sql(
		"UPDATE `tabAI Provider` SET currency = %s WHERE currency IS NULL OR currency = ''",
		(DEFAULT_BASE_CURRENCY,),
	)

	frappe.db.sql(
		"""
		UPDATE `tabAI Call Log`
		SET base_cost      = COALESCE(cost, 0),
		    exchange_rate  = 1,
		    base_currency  = %s,
		    currency       = COALESCE(NULLIF(currency, ''), %s)
		WHERE base_currency IS NULL OR base_currency = ''
		""",
		(base, DEFAULT_BASE_CURRENCY),
	)

	frappe.db.commit()

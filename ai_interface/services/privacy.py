"""What the call log keeps of prompts and answers.

Every call leaves a row: who called, for what record, which model, tokens and cost. That row is
the audit trail and the cost history, and it stays. The prompt and answer *text* is another
matter: it often holds customers' names, emails, phone numbers and messages. Two settings govern
it, globally in AI Settings and per calling app in its policy row:

- Redact contacts: email addresses and phone numbers are masked before the text is stored. The
  model always sees the real text; only the stored copy is masked.
- Keep prompts (days): every night, prompt and answer text older than this is emptied and the row
  is marked `payload_purged`. Tokens, cost, status and attribution are untouched.

`enable_logging` (off) still means no text is stored at all.
"""

from __future__ import annotations

import re

import frappe
from frappe.utils import add_days, cint, now_datetime

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Seven or more digits, allowing spaces, dots, dashes, brackets and a leading +, so that order
# numbers and prices (short, or with a decimal point followed by two digits) mostly survive.
# Not preceded by a letter, digit, dot, dash or slash, so document numbers (SINV-2026-00123) and
# paths are left alone; dates and times are skipped below.
PHONE = re.compile(r"(?<![\w./-])(?:\+|\()?\d[\d\s().-]{6,}\d(?![\w/:-])")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?|\d{2}[./-]\d{2}[./-]\d{4}")


def _phone(match) -> str:
	text = match.group()
	if DATE.match(text.strip(" (")) or sum(c.isdigit() for c in text) < 7:
		return text
	return "[phone]"


def redact(text: str | None) -> str | None:
	if not text:
		return text
	return PHONE.sub(_phone, EMAIL.sub("[email]", text))


def policy(calling_app: str | None, settings=None) -> frappe._dict:
	"""Retention days and redaction for one calling app: its policy row, else the globals."""
	settings = settings or frappe.get_cached_doc("AI Settings")
	out = frappe._dict(
		retention_days=cint(settings.get("payload_retention_days")),
		redact=bool(settings.get("redact_contacts")),
	)
	for row in settings.get("app_policies") or []:
		if calling_app and row.calling_app == calling_app:
			if cint(row.payload_retention_days):
				out.retention_days = cint(row.payload_retention_days)
			if row.redact_contacts:
				out.redact = row.redact_contacts == "Yes"
	return out


def for_storage(text: str | None, calling_app: str | None, settings=None) -> str | None:
	"""The text as it may be stored in the call log for this app."""
	if not text:
		return text
	return redact(text) if policy(calling_app, settings).redact else text


def purge_payloads() -> int:
	"""Nightly: empty prompt and answer text past each app's retention. Returns rows cleared."""
	settings = frappe.get_single("AI Settings")
	apps = {row.calling_app for row in settings.get("app_policies") or [] if cint(row.payload_retention_days)}
	cleared = 0
	for app in apps:
		cleared += _clear(policy(app, settings).retention_days, "AND calling_app = %(app)s", {"app": app})
	if cint(settings.get("payload_retention_days")):
		exclude = "AND calling_app NOT IN %(apps)s" if apps else ""
		cleared += _clear(
			cint(settings.payload_retention_days), exclude, {"apps": tuple(apps)} if apps else {}
		)
	frappe.db.commit()
	return cleared


def _clear(days: int, condition: str, params: dict) -> int:
	if days <= 0:
		return 0
	cutoff = add_days(now_datetime(), -days)
	frappe.db.sql(
		f"""UPDATE `tabAI Call Log`
		SET input_text = '', output_text = '', payload_purged = 1
		WHERE payload_purged = 0 AND creation < %(cutoff)s {condition}""",
		{"cutoff": cutoff, **params},
	)
	return frappe.db.sql("SELECT ROW_COUNT()")[0][0] or 0

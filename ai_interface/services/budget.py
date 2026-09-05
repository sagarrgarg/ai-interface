"""Spend caps.

The dashboard already shows what was spent; this is what stops it. Checks run
before a call is queued, so a blocked call costs nothing, and they read the
normalised `base_cost` so a mix of INR and USD providers is measured against one
budget.
"""

import frappe
from frappe import _
from frappe.utils import flt, get_first_day, nowdate

ALERT_CACHE_PREFIX = "ai_interface:budget_alert:"


class BudgetExceeded(frappe.ValidationError):
	pass


def check(settings, calling_app: str = "") -> dict | None:
	"""Enforce caps before a call is queued.

	Returns the breach that fired (or None). Raises when the applicable action
	is Block. Never raises for a Warn — a cap the user has not yet decided to
	enforce should not take five apps offline.
	"""
	if not settings.get("enable_budget"):
		return None

	breaches = []
	spend = _spend(calling_app)

	for scope, label, limits, action in _limits(settings, calling_app):
		for period, cap in limits.items():
			cap = flt(cap)
			if cap <= 0:
				continue
			used = spend[scope][period]
			pct = used * 100.0 / cap
			if used >= cap:
				breaches.append({
					"scope": label, "period": period, "used": used,
					"cap": cap, "pct": pct, "action": action,
				})
			elif pct >= flt(settings.get("budget_alert_threshold") or 80):
				_alert(settings, label, period, used, cap, pct, blocked=False)

	if not breaches:
		return None

	# Block wins if any applicable rule says so.
	worst = next((b for b in breaches if b["action"] == "Block"), breaches[0])
	_alert(settings, worst["scope"], worst["period"], worst["used"], worst["cap"],
	       worst["pct"], blocked=worst["action"] == "Block")

	if worst["action"] == "Block":
		frappe.throw(
			_("{0} {1} AI budget exhausted: {2} of {3} spent. Raise the cap in AI Settings or wait for the next period.").format(
				worst["scope"], worst["period"],
				_fmt(worst["used"], settings), _fmt(worst["cap"], settings),
			),
			exc=BudgetExceeded,
		)

	return worst


def _limits(settings, calling_app: str):
	"""(scope key, human label, {period: cap}, action) for every applicable cap."""
	default_action = settings.get("budget_action") or "Warn"
	out = [(
		"global",
		_("Global"),
		{"daily": settings.get("daily_budget"), "monthly": settings.get("monthly_budget")},
		default_action,
	)]

	if calling_app:
		for row in settings.get("app_budgets") or []:
			if row.calling_app == calling_app:
				out.append((
					"app",
					calling_app,
					{"daily": row.daily_budget, "monthly": row.monthly_budget},
					row.budget_action or default_action,
				))
	return out


def _spend(calling_app: str) -> dict:
	"""Spend so far today and this calendar month, in base currency."""
	today = nowdate()
	month_start = get_first_day(today)

	def total(extra_condition: str, params: dict) -> tuple[float, float]:
		row = frappe.db.sql(
			f"""
			SELECT
				COALESCE(SUM(CASE WHEN DATE(creation) = %(today)s THEN base_cost ELSE 0 END), 0) AS daily,
				COALESCE(SUM(CASE WHEN DATE(creation) >= %(month_start)s THEN base_cost ELSE 0 END), 0) AS monthly
			FROM `tabAI Call Log`
			WHERE DATE(creation) >= %(month_start)s {extra_condition}
			""",
			params,
			as_dict=True,
		)[0]
		return flt(row.daily), flt(row.monthly)

	params = {"today": today, "month_start": month_start}
	g_daily, g_monthly = total("", params)

	spend = {"global": {"daily": g_daily, "monthly": g_monthly}}
	if calling_app:
		a_daily, a_monthly = total("AND calling_app = %(app)s", {**params, "app": calling_app})
		spend["app"] = {"daily": a_daily, "monthly": a_monthly}
	return spend


def _alert(settings, scope: str, period: str, used: float, cap: float, pct: float, blocked: bool):
	"""Notify System Managers once per scope/period/day, not once per call."""
	key = f"{ALERT_CACHE_PREFIX}{scope}:{period}:{'block' if blocked else 'warn'}:{nowdate()}"
	if frappe.cache().get_value(key):
		return
	frappe.cache().set_value(key, 1, expires_in_sec=86400)

	subject = _("AI spend {0} for {1} ({2} budget)").format(
		_("blocked") if blocked else _("at {0}%").format(int(pct)), scope, period
	)
	message = _("{0} of {1} spent.").format(_fmt(used, settings), _fmt(cap, settings))

	try:
		for user in _managers():
			frappe.get_doc({
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"subject": subject,
				"email_content": message,
				"document_type": "AI Settings",
				"document_name": "AI Settings",
			}).insert(ignore_permissions=True)
	except Exception:
		# An alert that cannot be delivered must never break the AI call itself.
		frappe.log_error(title="AI Interface: budget alert failed", message=f"{subject}\n{message}")


def _managers() -> list[str]:
	return [
		u.parent
		for u in frappe.get_all(
			"Has Role", filters={"role": "System Manager", "parenttype": "User"}, fields=["parent"]
		)
		if u.parent not in ("Administrator", "Guest")
	] or ["Administrator"]


def _fmt(amount, settings) -> str:
	return frappe.utils.fmt_money(flt(amount, 4), currency=settings.get("base_currency") or "USD")


@frappe.whitelist()
def get_status() -> dict:
	"""Budget usage for the dashboard strip."""
	settings = frappe.get_cached_doc("AI Settings")
	if not settings.get("enable_budget"):
		return {"enabled": False}

	spend = _spend("")["global"]
	currency = settings.get("base_currency") or "USD"
	out = {"enabled": True, "currency": currency, "action": settings.get("budget_action") or "Warn",
	       "periods": []}

	for period, cap in (("daily", settings.get("daily_budget")), ("monthly", settings.get("monthly_budget"))):
		cap = flt(cap)
		if cap <= 0:
			continue
		used = spend[period]
		out["periods"].append({
			"period": period,
			"used": flt(used, 6),
			"cap": cap,
			"pct": min(flt(used * 100.0 / cap, 1), 999),
		})
	return out

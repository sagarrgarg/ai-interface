"""AI Command Center — aggregation endpoints.

Every function here returns pre-aggregated rows. Nothing fetches AI Call Log
documents; the log table grows without bound and the dashboard must stay O(groups)
rather than O(calls).
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, add_to_date, flt, now_datetime

# Dimensions the attribution drill-down is allowed to group by. This is an
# allowlist, not a suggestion — the value is interpolated into SQL.
DIMENSIONS = {
	"calling_app": "calling_app",
	"module": "module",
	"reference_doctype": "reference_doctype",
	"action": "action",
	"provider": "provider",
	"model": "model",
	"function_type": "function_type",
	"prompt_template": "prompt_template",
	"user": "user",
	"error_type": "error_type",
}

DRILL_ORDER = ["calling_app", "module", "reference_doctype", "action"]


def _check_access():
	roles = set(frappe.get_roles(frappe.session.user))
	if not roles & {"System Manager", "AI Manager"}:
		frappe.throw(_("Not permitted to view AI usage data."), frappe.PermissionError)


def _resolve_range(from_date: str | None, to_date: str | None, preset: str | None):
	"""Return (from, to) datetimes. Preset wins when supplied."""
	now = now_datetime()
	presets = {"today": 0, "7d": 7, "30d": 30, "90d": 90}

	if preset in presets:
		days = presets[preset]
		start = now.replace(hour=0, minute=0, second=0, microsecond=0)
		if days:
			start = add_days(start, -days)
		return start, now
	if preset == "mtd":
		return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now

	if from_date and to_date:
		return from_date, to_date
	return add_days(now, -30), now


def _build_conditions(filters: dict) -> tuple[str, dict]:
	"""Translate the filter bar into a WHERE fragment + bind params."""
	params = {}
	clauses = ["log.creation BETWEEN %(from_date)s AND %(to_date)s"]

	frm, to = _resolve_range(
		filters.get("from_date"), filters.get("to_date"), filters.get("preset")
	)
	params["from_date"] = frm
	params["to_date"] = to

	for field in ("calling_app", "module", "reference_doctype", "provider", "model",
	              "function_type", "status", "error_type", "action", "user"):
		value = filters.get(field)
		if value:
			clauses.append(f"log.`{field}` = %({field})s")
			params[field] = value

	return " AND ".join(clauses), params


def _parse_filters(filters) -> dict:
	"""Always return a fresh dict.

	Several endpoints narrow the filter set (a drill-down parent, a forced
	status). Returning the caller's own dict let those writes leak into every
	later call that reused it — which silently scoped whole panels to the last
	drill-down. Copy defensively.
	"""
	if isinstance(filters, str):
		try:
			return dict(json.loads(filters) or {})
		except (json.JSONDecodeError, ValueError, TypeError):
			return {}
	return dict(filters) if filters else {}


@frappe.whitelist()
def get_summary(filters=None):
	"""KPI strip: totals for the window, plus deltas against the previous window."""
	_check_access()
	filters = _parse_filters(filters)
	where, params = _build_conditions(filters)

	current = frappe.db.sql(
		f"""
		SELECT
			COUNT(*)                                              AS calls,
			COALESCE(SUM(log.cost), 0)                            AS cost,
			COALESCE(SUM(log.input_tokens), 0)                    AS input_tokens,
			COALESCE(SUM(log.output_tokens), 0)                   AS output_tokens,
			SUM(CASE WHEN log.status = 'Completed' THEN 1 ELSE 0 END) AS completed,
			SUM(CASE WHEN log.status = 'Failed'    THEN 1 ELSE 0 END) AS failed,
			SUM(CASE WHEN log.status IN ('Queued','Running') THEN 1 ELSE 0 END) AS in_flight,
			COALESCE(AVG(NULLIF(log.latency_ms, 0)), 0)           AS avg_latency
		FROM `tabAI Call Log` log
		WHERE {where}
		""",
		params,
		as_dict=True,
	)[0]

	# Same-length window immediately before this one, for the delta chips.
	span = frappe.utils.time_diff(params["to_date"], params["from_date"])
	prev_params = dict(params)
	prev_params["to_date"] = params["from_date"]
	prev_params["from_date"] = add_to_date(params["from_date"], seconds=-span.total_seconds())

	previous = frappe.db.sql(
		f"""
		SELECT COUNT(*) AS calls, COALESCE(SUM(log.cost), 0) AS cost
		FROM `tabAI Call Log` log
		WHERE {where}
		""",
		prev_params,
		as_dict=True,
	)[0]

	settled = (current.completed or 0) + (current.failed or 0)
	p95 = _percentile_latency(where, params, 0.95)

	return {
		"cost": flt(current.cost, 6),
		"calls": current.calls or 0,
		"completed": current.completed or 0,
		"failed": current.failed or 0,
		"in_flight": current.in_flight or 0,
		"success_rate": flt((current.completed or 0) * 100.0 / settled, 1) if settled else None,
		"input_tokens": current.input_tokens or 0,
		"output_tokens": current.output_tokens or 0,
		"avg_latency_ms": int(current.avg_latency or 0),
		"p95_latency_ms": p95,
		"avg_cost_per_call": flt((current.cost or 0) / current.calls, 6) if current.calls else 0,
		"prev_cost": flt(previous.cost, 6),
		"prev_calls": previous.calls or 0,
		"cost_delta_pct": _pct_change(previous.cost, current.cost),
		"calls_delta_pct": _pct_change(previous.calls, current.calls),
		"currency": "USD",
		"from_date": str(params["from_date"]),
		"to_date": str(params["to_date"]),
	}


def _percentile_latency(where: str, params: dict, pct: float) -> int:
	"""Approximate percentile via OFFSET — MariaDB 10.6 has no PERCENTILE_CONT."""
	total = frappe.db.sql(
		f"SELECT COUNT(*) FROM `tabAI Call Log` log WHERE {where} AND log.latency_ms > 0",
		params,
	)[0][0]
	if not total:
		return 0
	offset = max(int(total * pct) - 1, 0)
	row = frappe.db.sql(
		f"""
		SELECT log.latency_ms FROM `tabAI Call Log` log
		WHERE {where} AND log.latency_ms > 0
		ORDER BY log.latency_ms ASC LIMIT 1 OFFSET {offset}
		""",
		params,
	)
	return int(row[0][0]) if row else 0


def _pct_change(before, after) -> float | None:
	before, after = flt(before), flt(after)
	if not before:
		return None
	return flt((after - before) * 100.0 / before, 1)


@frappe.whitelist()
def get_timeseries(filters=None, group_by="calling_app", granularity="day"):
	"""Spend and volume over time, split by one dimension."""
	_check_access()
	filters = _parse_filters(filters)
	if group_by not in DIMENSIONS:
		frappe.throw(_("Invalid group_by dimension."))

	where, params = _build_conditions(filters)
	bucket = "DATE(log.creation)" if granularity == "day" else "DATE_FORMAT(log.creation, '%%Y-%%m-%%d %%H:00:00')"
	col = DIMENSIONS[group_by]

	rows = frappe.db.sql(
		f"""
		SELECT {bucket} AS bucket,
		       COALESCE(NULLIF(log.`{col}`, ''), 'Unattributed') AS series,
		       COALESCE(SUM(log.cost), 0) AS cost,
		       COUNT(*)                   AS calls
		FROM `tabAI Call Log` log
		WHERE {where}
		GROUP BY bucket, series
		ORDER BY bucket ASC
		""",
		params,
		as_dict=True,
	)

	buckets, series_totals = [], {}
	for r in rows:
		key = str(r.bucket)
		if key not in buckets:
			buckets.append(key)
		series_totals[r.series] = series_totals.get(r.series, 0) + flt(r.cost)

	# Cap at 7 series; the tail folds into "Other" rather than growing the palette.
	top = [s for s, _v in sorted(series_totals.items(), key=lambda kv: kv[1], reverse=True)[:7]]
	index = {(str(r.bucket), r.series): r for r in rows}

	data = []
	for name in top + (["Other"] if len(series_totals) > len(top) else []):
		cost_points, call_points = [], []
		for b in buckets:
			if name == "Other":
				c = sum(flt(r.cost) for r in rows if str(r.bucket) == b and r.series not in top)
				n = sum(r.calls for r in rows if str(r.bucket) == b and r.series not in top)
			else:
				hit = index.get((b, name))
				c, n = (flt(hit.cost), hit.calls) if hit else (0, 0)
			cost_points.append(flt(c, 6))
			call_points.append(n)
		data.append({"name": name, "cost": cost_points, "calls": call_points})

	return {"buckets": buckets, "series": data}


@frappe.whitelist()
def get_attribution(filters=None, dimension="calling_app", parent_filters=None, limit=12):
	"""Cost + volume grouped by one dimension — powers the drill-down bars."""
	_check_access()
	filters = _parse_filters(filters)
	filters.update(_parse_filters(parent_filters))

	if dimension not in DIMENSIONS:
		frappe.throw(_("Invalid dimension."))

	where, params = _build_conditions(filters)
	col = DIMENSIONS[dimension]

	rows = frappe.db.sql(
		f"""
		SELECT COALESCE(NULLIF(log.`{col}`, ''), 'Unattributed') AS label,
		       COALESCE(SUM(log.cost), 0)                        AS cost,
		       COUNT(*)                                          AS calls,
		       SUM(CASE WHEN log.status = 'Failed' THEN 1 ELSE 0 END) AS failed,
		       COALESCE(SUM(log.input_tokens), 0)                AS input_tokens,
		       COALESCE(SUM(log.output_tokens), 0)               AS output_tokens,
		       COALESCE(AVG(NULLIF(log.latency_ms, 0)), 0)       AS avg_latency
		FROM `tabAI Call Log` log
		WHERE {where}
		GROUP BY label
		ORDER BY cost DESC, calls DESC
		LIMIT {int(limit)}
		""",
		params,
		as_dict=True,
	)

	total = sum(flt(r.cost) for r in rows) or 1
	for r in rows:
		r["cost"] = flt(r.cost, 6)
		r["share"] = flt(flt(r.cost) * 100.0 / total, 1)
		r["avg_latency"] = int(r.avg_latency or 0)
		r["next_dimension"] = _next_dimension(dimension)
	return {"dimension": dimension, "rows": rows}


def _next_dimension(current: str) -> str | None:
	if current in DRILL_ORDER:
		i = DRILL_ORDER.index(current)
		if i + 1 < len(DRILL_ORDER):
			return DRILL_ORDER[i + 1]
	return None


@frappe.whitelist()
def get_reliability(filters=None):
	"""Daily success/failure counts and latency band, for the reliability panel."""
	_check_access()
	filters = _parse_filters(filters)
	where, params = _build_conditions(filters)

	daily = frappe.db.sql(
		f"""
		SELECT DATE(log.creation) AS bucket,
		       SUM(CASE WHEN log.status = 'Completed' THEN 1 ELSE 0 END) AS completed,
		       SUM(CASE WHEN log.status = 'Failed'    THEN 1 ELSE 0 END) AS failed,
		       COALESCE(AVG(NULLIF(log.latency_ms, 0)), 0)               AS avg_latency
		FROM `tabAI Call Log` log
		WHERE {where}
		GROUP BY bucket ORDER BY bucket ASC
		""",
		params,
		as_dict=True,
	)
	for d in daily:
		settled = (d.completed or 0) + (d.failed or 0)
		d["bucket"] = str(d.bucket)
		d["success_rate"] = flt((d.completed or 0) * 100.0 / settled, 1) if settled else None
		d["avg_latency"] = int(d.avg_latency or 0)

	models = frappe.db.sql(
		f"""
		SELECT COALESCE(NULLIF(log.model, ''), 'Unknown') AS model,
		       COUNT(*) AS calls,
		       COALESCE(SUM(log.cost), 0) AS cost,
		       COALESCE(AVG(NULLIF(log.latency_ms, 0)), 0) AS avg_latency,
		       SUM(CASE WHEN log.status = 'Completed' THEN 1 ELSE 0 END) AS completed,
		       SUM(CASE WHEN log.status = 'Failed'    THEN 1 ELSE 0 END) AS failed,
		       COALESCE(AVG(NULLIF(log.input_tokens, 0)), 0)  AS avg_input,
		       COALESCE(AVG(NULLIF(log.output_tokens, 0)), 0) AS avg_output
		FROM `tabAI Call Log` log
		WHERE {where}
		GROUP BY model ORDER BY cost DESC LIMIT 10
		""",
		params,
		as_dict=True,
	)
	for m in models:
		settled = (m.completed or 0) + (m.failed or 0)
		m["cost"] = flt(m.cost, 6)
		m["avg_latency"] = int(m.avg_latency or 0)
		m["success_rate"] = flt((m.completed or 0) * 100.0 / settled, 1) if settled else None
		m["avg_cost"] = flt(flt(m.cost) / m.calls, 6) if m.calls else 0
		m["avg_input"] = int(m.avg_input or 0)
		m["avg_output"] = int(m.avg_output or 0)

	return {"daily": daily, "models": models}


@frappe.whitelist()
def get_failures(filters=None, limit=25):
	"""Failure forensics: by type, the module x day heatmap, and recent rows."""
	_check_access()
	filters = _parse_filters(filters)
	filters["status"] = "Failed"
	where, params = _build_conditions(filters)

	by_type = frappe.db.sql(
		f"""
		SELECT COALESCE(NULLIF(log.error_type, ''), 'Unknown') AS label, COUNT(*) AS calls
		FROM `tabAI Call Log` log WHERE {where}
		GROUP BY label ORDER BY calls DESC
		""",
		params,
		as_dict=True,
	)

	heatmap = frappe.db.sql(
		f"""
		SELECT COALESCE(NULLIF(log.module, ''), 'Unattributed') AS row_label,
		       DATE(log.creation) AS bucket,
		       COUNT(*) AS calls
		FROM `tabAI Call Log` log WHERE {where}
		GROUP BY row_label, bucket
		""",
		params,
		as_dict=True,
	)
	for h in heatmap:
		h["bucket"] = str(h.bucket)

	recent = frappe.db.sql(
		f"""
		SELECT log.name, log.creation, log.calling_app, log.module, log.reference_doctype,
		       log.reference_name, log.action, log.error_type, log.provider, log.model,
		       log.user, LEFT(COALESCE(log.error_message, ''), 240) AS error_excerpt
		FROM `tabAI Call Log` log WHERE {where}
		ORDER BY log.creation DESC LIMIT {int(limit)}
		""",
		params,
		as_dict=True,
	)
	for r in recent:
		r["creation"] = str(r.creation)

	return {"by_type": by_type, "heatmap": heatmap, "recent": recent}


@frappe.whitelist()
def get_insights(filters=None):
	"""Computed observations — the panel that makes this more than a log viewer."""
	_check_access()
	filters = _parse_filters(filters)
	where, params = _build_conditions(filters)
	out = []

	summary = get_summary(filters)
	if summary["cost_delta_pct"] is not None and summary["cost_delta_pct"] >= 50:
		top = frappe.db.sql(
			f"""
			SELECT COALESCE(NULLIF(log.calling_app, ''), 'Unattributed') AS label,
			       COALESCE(SUM(log.cost), 0) AS cost
			FROM `tabAI Call Log` log WHERE {where}
			GROUP BY label ORDER BY cost DESC LIMIT 1
			""",
			params,
			as_dict=True,
		)
		driver = top[0].label if top else "unknown"
		out.append({
			"severity": "warning",
			"title": f"Spend up {summary['cost_delta_pct']}% vs previous period",
			"detail": f"Largest contributor: {driver}.",
		})

	# Prompts that spend far more on input than they get back in output.
	bloated = frappe.db.sql(
		f"""
		SELECT COALESCE(NULLIF(log.prompt_template, ''), CONCAT('action:', COALESCE(NULLIF(log.action,''),'unknown'))) AS label,
		       COUNT(*) AS calls,
		       AVG(log.input_tokens)  AS avg_in,
		       AVG(log.output_tokens) AS avg_out,
		       SUM(log.cost)          AS cost
		FROM `tabAI Call Log` log
		WHERE {where} AND log.status = 'Completed' AND log.output_tokens > 0
		GROUP BY label HAVING calls >= 5 AND avg_in > avg_out * 10
		ORDER BY cost DESC LIMIT 3
		""",
		params,
		as_dict=True,
	)
	for b in bloated:
		ratio = int(flt(b.avg_in) / max(flt(b.avg_out), 1))
		out.append({
			"severity": "info",
			"title": f"`{b.label}` sends {ratio}x more input than it returns",
			"detail": f"{int(b.avg_in)} in → {int(b.avg_out)} out over {b.calls} calls "
			          f"(${flt(b.cost, 4)}). Trimming the prompt is the cheapest win here.",
		})

	# Identical prompts repeated — a caching opportunity.
	repeats = frappe.db.sql(
		f"""
		SELECT LEFT(log.input_text, 200) AS snippet, COUNT(*) AS calls, SUM(log.cost) AS cost
		FROM `tabAI Call Log` log
		WHERE {where} AND log.input_text IS NOT NULL AND log.input_text != ''
		GROUP BY snippet HAVING calls >= 10
		ORDER BY cost DESC LIMIT 2
		""",
		params,
		as_dict=True,
	)
	for r in repeats:
		out.append({
			"severity": "info",
			"title": f"{r.calls} identical prompts in this window",
			"detail": f"${flt(r.cost, 4)} spent re-asking the same question. A cache would collapse this to one call.",
		})

	# Failure concentration — one place accounting for most errors.
	concentration = frappe.db.sql(
		f"""
		SELECT COALESCE(NULLIF(log.module, ''), 'Unattributed') AS label,
		       COUNT(*) AS failed
		FROM `tabAI Call Log` log
		WHERE {where} AND log.status = 'Failed'
		GROUP BY label ORDER BY failed DESC LIMIT 1
		""",
		params,
		as_dict=True,
	)
	if concentration and summary["failed"] and concentration[0].failed >= max(summary["failed"] * 0.6, 3):
		c = concentration[0]
		pct = int(c.failed * 100 / summary["failed"])
		out.append({
			"severity": "critical",
			"title": f"{pct}% of all failures come from {c.label}",
			"detail": f"{c.failed} of {summary['failed']} failed calls. Fixing this one module clears most of the error volume.",
		})

	# Unattributed spend — the dashboard cannot explain it, so say so.
	unattributed = frappe.db.sql(
		f"""
		SELECT COALESCE(SUM(log.cost), 0) AS cost, COUNT(*) AS calls
		FROM `tabAI Call Log` log
		WHERE {where} AND (log.calling_app IS NULL OR log.calling_app = '')
		""",
		params,
		as_dict=True,
	)[0]
	if unattributed.calls and summary["cost"] and flt(unattributed.cost) > flt(summary["cost"]) * 0.2:
		out.append({
			"severity": "warning",
			"title": f"${flt(unattributed.cost, 4)} of spend is unattributed",
			"detail": f"{unattributed.calls} calls arrived without a calling_app. "
			          "Pass calling_app / reference_doctype / action so this cost can be explained.",
		})

	if not out:
		out.append({
			"severity": "good",
			"title": "Nothing needs attention",
			"detail": "No cost spikes, prompt bloat, repeated calls or failure clusters in this window.",
		})
	return out


@frappe.whitelist()
def get_filter_options(filters=None):
	"""Distinct values for the filter bar, scoped to the current window."""
	_check_access()
	filters = _parse_filters(filters)
	where, params = _build_conditions(filters)
	out = {}
	for field in ("calling_app", "module", "provider", "model", "function_type", "error_type"):
		rows = frappe.db.sql(
			f"""
			SELECT DISTINCT log.`{field}` AS v FROM `tabAI Call Log` log
			WHERE {where} AND log.`{field}` IS NOT NULL AND log.`{field}` != ''
			ORDER BY v ASC LIMIT 50
			""",
			params,
			as_dict=True,
		)
		out[field] = [r.v for r in rows]
	return out


@frappe.whitelist()
def retry_call(log_name: str):
	"""Re-run a failed call with its original prompt and routing."""
	_check_access()
	log = frappe.get_doc("AI Call Log", log_name)
	if log.status != "Failed":
		frappe.throw(_("Only failed calls can be retried."))
	if not log.input_text:
		frappe.throw(_("Original prompt was not retained, so this call cannot be replayed."))

	from ai_interface.services.ai_client import call_ai

	return call_ai(
		prompt=log.input_text,
		provider=log.provider,
		model=log.model,
		calling_app=log.calling_app,
		function_type=log.function_type,
		user=log.user,
		reference_doctype=log.reference_doctype,
		reference_name=log.reference_name,
		module=log.module,
		action=log.action,
	)

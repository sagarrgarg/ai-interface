"""Natural language questions answered from this site's own data.

Three properties this module has to hold, in order of importance:

1. **It can never reveal what the asker could not already open.** Every query
   runs as the session user, so Frappe's own permissions decide the result. The
   schema offered to the model is filtered the same way, because a model that
   knows about doctypes the user cannot read will keep proposing them.

2. **It only ever reads.** Queries are built as structured parameters and run
   through `frappe.get_all` — never raw SQL, never a write. Every field, filter,
   grouping and aggregate is checked against the doctype's real metadata before
   execution, so a model that invents a field name gets a clear error rather
   than a database surprise.

3. **Counts are counted, not guessed.** Aggregates run in the database. Asking
   the model to count rows it was shown caps every total at the page size, which
   is wrong precisely for the questions people ask most.
"""

import json

import frappe
from frappe import _
from frappe.model import default_fields

from ai_interface.services.ai_client import call_ai

CALLING_APP = "site_assistant"

# Aggregate functions allowed into SQL. An allowlist, not a suggestion.
AGGREGATES = {"count", "sum", "avg", "min", "max"}

MAX_ROWS = 100
DEFAULT_ROWS = 20

# Never queryable, whatever the permission model says: doctypes that exist only
# to hold credentials. `User` is deliberately absent — the risk there was its
# secrets, not the doctype, and Password fields are excluded everywhere below.
ALWAYS_DENIED = {
	"OAuth Bearer Token",
	"OAuth Authorization Code",
	"Token Cache",
	"Social Login Key",
	"Webhook",
	"Server Script",
	"AI Provider",
	"AI Provider Type",
}

FIELD_TYPES_WORTH_SHOWING = (
	"Data", "Int", "Float", "Currency", "Percent", "Date", "Datetime", "Time",
	"Link", "Select", "Check", "Small Text", "Text", "Long Text", "Text Editor",
)


# Substrings that mark a field as a credential regardless of its fieldtype.
# Chosen to avoid false positives: "key" alone would catch key_account and
# keywords, so only the compound forms that actually carry secrets are listed.
SECRET_MARKERS = (
	"password", "secret", "token", "credential", "api_key", "auth_key",
	"access_key", "private_key", "encryption", "_salt", "otp",
)


class QueryRefused(frappe.ValidationError):
	pass


def is_secret_field(fieldname: str, fieldtype: str = "") -> bool:
	"""A field nobody should be able to read through the assistant."""
	if fieldtype == "Password":
		return True
	name = (fieldname or "").lower()
	return any(marker in name for marker in SECRET_MARKERS)


def readable_fields(meta) -> list:
	"""Meta fields with credentials removed — the one place that decides."""
	return [f for f in meta.fields if not is_secret_field(f.fieldname, f.fieldtype)]


# --------------------------------------------------------------------- public


def query(
	question: str,
	user: str | None = None,
	*,
	provider: str | None = None,
	model: str | None = None,
	calling_app: str = "",
	sync: bool = False,
	max_tokens: int | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	module: str | None = None,
	action: str | None = None,
	needs: list[str] | None = None,
	history: list[dict] | None = None,
	**kwargs,
) -> str:
	"""Answer a question about this site's data.

	Produces two AI Call Log rows: `query_generation` and `answer_synthesis`.
	Both are attributed to `calling_app`.
	"""
	user = user or frappe.session.user
	app = calling_app or CALLING_APP

	spec = plan_query(question, user=user, provider=provider, model=model,
	                  calling_app=app, history=history)
	rows = run_query(spec, user=user)

	return call_ai(
		prompt=_answer_prompt(question, spec, rows, history),
		provider=provider,
		model=model,
		calling_app=app,
		function_type="Query",
		sync=sync,
		user=user,
		max_tokens=max_tokens,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		module=module,
		action=action or "answer_synthesis",
		needs=needs,
		**kwargs,
	)


def answer(question: str, user: str | None = None, history: list[dict] | None = None) -> dict:
	"""Everything the chat UI needs: the answer, and the query behind it.

	Returned together on purpose — an answer a user cannot check is an answer
	they have to take on faith.
	"""
	user = user or frappe.session.user
	spec = plan_query(question, user=user, history=history)
	rows = run_query(spec, user=user)

	text = call_ai(
		prompt=_answer_prompt(question, spec, rows, history),
		calling_app=CALLING_APP,
		function_type="Query",
		sync=True,
		user=user,
		action="answer_synthesis",
	)
	return {"answer": text, "query": spec, "row_count": len(rows), "rows": rows[:MAX_ROWS]}


# ------------------------------------------------------------------- planning


def plan_query(
	question: str,
	*,
	user: str,
	provider: str | None = None,
	model: str | None = None,
	calling_app: str = CALLING_APP,
	history: list[dict] | None = None,
) -> dict:
	"""Turn a question into a validated, executable query specification."""
	allowed = accessible_doctypes(user)
	if not allowed:
		frappe.throw(_("You do not have read access to any data this assistant can query."),
		             exc=QueryRefused)

	shortlist = shortlist_doctypes(question, allowed)

	def plan(candidates):
		raw = call_ai(
			prompt=_planning_prompt(question, allowed, candidates, history),
			provider=provider,
			model=model,
			calling_app=calling_app,
			function_type="Query",
			sync=True,
			user=user,
			action="query_generation",
		)
		return _parse_json(raw)

	spec = plan(shortlist)

	# The model may reach past the keyword shortlist to a doctype it only saw the
	# name of. Rather than let it guess fieldnames blind, show it the real fields
	# and let it try once more. Only pays for a second call when it happens.
	chosen = spec.get("doctype")
	if isinstance(chosen, str) and chosen in allowed and chosen not in shortlist:
		spec = plan([chosen, *shortlist[:4]])

	return validate_spec(spec, user=user, allowed=allowed)


def accessible_doctypes(user: str, use_cache: bool = True) -> list[str]:
	"""Doctypes this user may read, minus the ones we refuse regardless.

	Built with Frappe's own permission helper rather than a hand-rolled check,
	so it tracks role changes and custom permissions automatically.

	Cached per user: resolving metadata for several hundred doctypes on every
	question is the slowest thing here, and a person's roles do not change
	between two sentences.
	"""
	cache_key = f"ai_interface:assistant_doctypes:{user}"
	if use_cache:
		cached = frappe.cache().get_value(cache_key)
		if cached:
			return cached

	from frappe.permissions import get_doctypes_with_read

	original = frappe.session.user
	try:
		frappe.set_user(user)
		names = set(get_doctypes_with_read())
	finally:
		frappe.set_user(original)

	denied = ALWAYS_DENIED | _configured_denylist()

	out = []
	for name in sorted(names):
		if name in denied:
			continue
		try:
			meta = frappe.get_meta(name)
		except Exception:
			# A bench that has had apps removed keeps permission rows for
			# doctypes that no longer exist. One stale row must not take the
			# whole assistant down.
			continue
		if meta.istable or meta.issingle:
			continue
		out.append(name)

	frappe.cache().set_value(cache_key, out, expires_in_sec=600)
	return out


# Words that appear in most questions and match nothing useful.
STOPWORDS = frozenset("""
the a an of for in on at to from by with and or is are was were be been
how many much what which who whom whose when where why show list give tell
me my our we us all any some this that these those total sum count average
number do does did have has had can could would should please
""".split())


def shortlist_doctypes(question: str, allowed: list[str], limit: int | None = None) -> list[str]:
	"""Rank doctypes by how well their names match the question.

	Deliberately not an AI call. Doctype names are the vocabulary users already
	speak in, so scoring them costs nothing and keeps a whole model round-trip
	out of every question.

	Matching is on singular stems: a question says "customers" and the doctype
	is "Customer", and an exact-word match finds neither that nor "ordered"
	against "Sales Order".
	"""
	limit = limit or int(frappe.db.get_single_value("AI Settings", "assistant_max_doctypes") or 12)
	words = {w for w in _stems(question) if len(w) > 2 and w not in STOPWORDS}
	if not words:
		return _fallback(allowed, limit)

	scored = []
	for name in allowed:
		tokens = set(_stems(name))
		overlap = len(words & tokens)
		if not overlap:
			continue
		# Prefer a tight match: "Item" beating "Item Price" for a question about
		# items means fewer irrelevant fields in the prompt.
		scored.append((overlap, -len(tokens), name))

	scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
	return [s[2] for s in scored[:limit]] or _fallback(allowed, limit)


def _fallback(allowed: list[str], limit: int) -> list[str]:
	"""When nothing matches, offer the doctypes that actually hold data.

	Alphabetical order put Access Log and Account Request in front of the model,
	which is worse than useless — it invites a confidently wrong answer.
	"""
	cached = frappe.cache().get_value("ai_interface:assistant_busiest")
	if not cached:
		# One read of information_schema rather than a COUNT per doctype: the
		# per-doctype loop fired hundreds of queries and errored on every
		# virtual doctype that has no table behind it.
		rows = frappe.db.sql(
			"""
			SELECT TABLE_NAME, TABLE_ROWS
			FROM information_schema.TABLES
			WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'tab%%'
			ORDER BY TABLE_ROWS DESC
			LIMIT 120
			""",
			as_dict=True,
		)
		cached = [r.TABLE_NAME[3:] for r in rows if r.TABLE_NAME.startswith("tab")]
		frappe.cache().set_value("ai_interface:assistant_busiest", cached, expires_in_sec=3600)

	ordered = [d for d in cached if d in set(allowed)]
	return (ordered + [d for d in allowed if d not in ordered])[:limit]


def _tokenise(text: str) -> list[str]:
	return [t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t]


def _stems(text: str) -> list[str]:
	"""Crude singularisation. Enough to align a question with a doctype name."""
	out = []
	for token in _tokenise(text):
		for suffix, replacement in (("ies", "y"), ("ses", "s"), ("es", ""), ("s", "")):
			if len(token) > 3 and token.endswith(suffix):
				token = token[: -len(suffix)] + replacement
				break
		out.append(token)
	return out


def _configured_denylist() -> set:
	raw = frappe.db.get_single_value("AI Settings", "assistant_denied_doctypes") or ""
	return {p.strip() for p in raw.replace(",", "\n").split("\n") if p.strip()}


# ------------------------------------------------------------------ validation


def validate_spec(spec: dict, *, user: str, allowed: list[str] | None = None) -> dict:
	"""Check every part of a model-authored query against real metadata.

	The model is a helpful but unreliable author: it invents plausible field
	names. Each one is resolved against the doctype before it can reach SQL.
	"""
	if not isinstance(spec, dict):
		frappe.throw(_("The assistant did not return a usable query."), exc=QueryRefused)

	doctype = spec.get("doctype")
	if not isinstance(doctype, str) or not doctype:
		frappe.throw(_("The assistant did not choose a doctype."), exc=QueryRefused)

	allowed = allowed if allowed is not None else accessible_doctypes(user)
	if doctype not in allowed:
		frappe.throw(
			_("You do not have access to {0}, so that question cannot be answered here.").format(doctype),
			exc=QueryRefused,
		)

	meta = frappe.get_meta(doctype)
	valid = {f.fieldname for f in readable_fields(meta)} | set(default_fields)

	clean: dict = {"doctype": doctype}

	# ---- aggregate
	agg = spec.get("aggregate") or None
	if agg:
		function = str(agg.get("function", "")).lower()
		if function not in AGGREGATES:
			frappe.throw(_("Unsupported aggregate: {0}").format(function), exc=QueryRefused)
		field = agg.get("field") or "name"
		if function != "count" and field not in valid:
			frappe.throw(_("Unknown field '{0}' on {1}.").format(field, doctype), exc=QueryRefused)
		if function == "count":
			field = "name"
		clean["aggregate"] = {"function": function, "field": field}

	# ---- group by
	group_by = spec.get("group_by")
	if group_by:
		if group_by not in valid:
			frappe.throw(_("Cannot group by '{0}' — not a field on {1}.").format(group_by, doctype),
			             exc=QueryRefused)
		clean["group_by"] = group_by

	# ---- filters
	filters = spec.get("filters") or {}
	if not isinstance(filters, dict):
		frappe.throw(_("Filters must be an object."), exc=QueryRefused)
	clean_filters = {}
	for key, value in filters.items():
		if key not in valid:
			frappe.throw(_("Unknown filter field '{0}' on {1}.").format(key, doctype), exc=QueryRefused)
		clean_filters[key] = value
	clean["filters"] = clean_filters

	# ---- selected fields
	fields = spec.get("fields") or []
	if not isinstance(fields, list):
		frappe.throw(_("Fields must be a list."), exc=QueryRefused)
	clean_fields = [f for f in fields if isinstance(f, str) and f in valid]
	if not clean_fields and not clean.get("aggregate"):
		clean_fields = ["name"]
	clean["fields"] = clean_fields

	# ---- ordering
	order_by = spec.get("order_by")
	if order_by:
		field = str(order_by).split()[0]
		direction = "desc" if str(order_by).lower().endswith("asc") is False else "asc"
		if field in valid:
			clean["order_by"] = f"`{field}` {direction}"

	clean["limit"] = max(1, min(int(spec.get("limit") or DEFAULT_ROWS), MAX_ROWS))
	return clean


# ------------------------------------------------------------------- execution


def run_query(spec: dict, *, user: str) -> list[dict]:
	"""Execute a validated spec as the asking user, read-only."""
	original = frappe.session.user
	try:
		frappe.set_user(user)

		fields = list(spec["fields"])
		group_by = spec.get("group_by")
		agg = spec.get("aggregate")

		if agg:
			# Counted in the database, so the answer is not capped by page size.
			fields = [f"{agg['function']}(`{agg['field']}`) as value"]
			if group_by:
				fields.insert(0, f"`{group_by}` as label")

		return frappe.get_all(
			spec["doctype"],
			filters=spec.get("filters") or None,
			fields=fields,
			group_by=f"`{group_by}`" if group_by else None,
			order_by=spec.get("order_by") if not agg else None,
			limit_page_length=spec["limit"],
		)
	except frappe.PermissionError:
		frappe.throw(
			_("You do not have permission to read {0}.").format(spec["doctype"]), exc=QueryRefused
		)
	finally:
		frappe.set_user(original)


# --------------------------------------------------------------------- prompts


def _planning_prompt(question, allowed, shortlist, history) -> str:
	detail = []
	for name in shortlist:
		meta = frappe.get_meta(name)
		fields = [
			f"{f.fieldname} ({f.fieldtype}{'→' + f.options if f.fieldtype == 'Link' and f.options else ''})"
			for f in readable_fields(meta)
			if f.fieldtype in FIELD_TYPES_WORTH_SHOWING
		][:28]
		detail.append(f"{name}\n  {', '.join(fields)}")

	# Full names cost little and let the model reach past the keyword shortlist;
	# full field detail is expensive, so only the likely candidates get it.
	others = [d for d in allowed if d not in shortlist][:250]

	return f"""Translate a question about a Frappe/ERPNext site into query parameters.

Return ONLY a JSON object. No markdown, no explanation.

{{
  "doctype": "<one doctype>",
  "filters": {{"fieldname": "value"}},
  "fields": ["fieldname"],
  "aggregate": {{"function": "count|sum|avg|min|max", "field": "fieldname"}},
  "group_by": "fieldname",
  "order_by": "fieldname desc",
  "limit": 20
}}

RULES
- Use "aggregate" for how many / total / average questions. Do NOT list rows and count them.
- "aggregate", "group_by" and "order_by" are optional — omit rather than guess.
- Only use fieldnames listed below for the doctype you pick.
- Dates: today is {frappe.utils.nowdate()}. Use ["between", ["start", "end"]] or [">=", "date"].
- Never invent a doctype or a field.
{_history_block(history)}
DOCTYPES WITH FIELDS
{chr(10).join(detail)}

OTHER READABLE DOCTYPES (name one here and you will be shown its fields to try again)
{", ".join(others)}

QUESTION: {question}

JSON:"""


def _answer_prompt(question, spec, rows, history) -> str:
	agg = spec.get("aggregate")
	shape = (
		f"{agg['function']}({agg['field']})" + (f" grouped by {spec['group_by']}" if spec.get("group_by") else "")
		if agg
		else f"{len(rows)} row(s)"
	)
	return f"""Answer the question using only the data below. Be direct and brief.

RULES
- State the number plainly when the result is a count or total.
- If the data does not answer the question, say so — never estimate.
- Do not mention JSON, queries, doctypes or field names.
- Plain sentences, no markdown headers.
{_history_block(history)}
QUESTION: {question}

QUERY RUN: {spec['doctype']}, {shape}
FILTERS: {json.dumps(spec.get('filters') or {}, default=str)}

RESULT:
{json.dumps(rows, default=str, indent=2)[:6000]}

ANSWER:"""


def _history_block(history) -> str:
	if not history:
		return ""
	turns = []
	for turn in history[-4:]:
		q, a = turn.get("question"), turn.get("answer")
		if q:
			turns.append(f"User: {q}")
		if a:
			turns.append(f"Assistant: {a}")
	return "\nEARLIER IN THIS CONVERSATION (for pronouns and follow-ups)\n" + "\n".join(turns) + "\n"


def _parse_json(raw: str) -> dict:
	text = (raw or "").strip()
	if text.startswith("```"):
		text = text.split("```")[1] if "```" in text[3:] else text[3:]
		text = text.removeprefix("json").strip()
	text = text.strip().strip("`").strip()
	if text.startswith("json"):
		text = text[4:].strip()

	try:
		return json.loads(text)
	except (json.JSONDecodeError, ValueError):
		start, end = text.find("{"), text.rfind("}")
		if start >= 0 and end > start:
			try:
				return json.loads(text[start : end + 1])
			except (json.JSONDecodeError, ValueError):
				pass
	frappe.throw(_("The assistant could not turn that into a query. Try rephrasing."), exc=QueryRefused)

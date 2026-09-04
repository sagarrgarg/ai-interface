import json

import frappe
from frappe import _

from ai_interface.services.ai_client import call_ai


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
	**kwargs,
) -> str:
	"""Natural language query against ERPNext data.

	Phase 1+2: LLM generates structured query params from the question.
	Phase 3: LLM synthesizes the results into a natural language answer.
	Both internal LLM calls are always synchronous. The sync param controls
	the final answer delivery only.

	Note: this produces TWO AI Call Log rows per question — one for query
	generation (action `query_generation`) and one for answer synthesis
	(action `answer_synthesis`). Both are attributed to `calling_app`.
	"""
	user = user or frappe.session.user

	schema_context = _build_schema_context()
	query_params = _generate_query(question, schema_context, provider, model, user, calling_app)
	results = _execute_query(query_params, user)
	answer_prompt = _build_answer_prompt(question, query_params, results)

	return call_ai(
		prompt=answer_prompt,
		provider=provider,
		model=model,
		calling_app=calling_app,
		function_type="Query",
		sync=sync,
		user=user,
		max_tokens=max_tokens,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		module=module,
		action=action or "answer_synthesis",
		**kwargs,
	)


def _build_schema_context() -> str:
	"""Build a schema summary of ERPNext doctypes for the LLM."""
	doctypes = frappe.get_all(
		"DocType",
		filters={"module": ["not in", ["Core", "Email", "Printing", "Website"]]},
		fields=["name"],
		limit_page_length=200,
	)

	schema_lines = []
	for dt in doctypes:
		try:
			meta = frappe.get_meta(dt.name)
		except Exception:
			continue
		fields = [
			f.fieldname
			for f in meta.fields
			if f.fieldtype
			in (
				"Data",
				"Int",
				"Float",
				"Currency",
				"Date",
				"Datetime",
				"Link",
				"Select",
				"Check",
				"Small Text",
				"Long Text",
				"Text",
			)
		]
		if fields:
			schema_lines.append(f"DocType: {dt.name}")
			schema_lines.append(f"  Fields: {', '.join(fields[:20])}")

	return "\n".join(schema_lines)


def _generate_query(
	question: str,
	schema_context: str,
	provider: str | None,
	model: str | None,
	user: str,
	calling_app: str = "",
) -> dict:
	"""Phase 1+2: LLM generates structured query parameters as JSON."""
	prompt = f"""You are an ERPNext data query assistant. Given a natural language question and the available doctypes/fields, generate query parameters as JSON.

RULES:
- Return ONLY valid JSON, no explanation or markdown
- The JSON must have these keys:
  - "doctype": string (the DocType to query)
  - "filters": object (field-value pairs, supports operators like [">=", "2024-01-01"])
  - "fields": array of strings (fields to return)
  - "order_by": string (optional, e.g. "creation desc")
  - "limit_page_length": integer (default 20, max 100)
- ONLY query doctypes and fields from the schema below
- NEVER include executable code

AVAILABLE SCHEMA:
{schema_context}

QUESTION: {question}

Generate the JSON query parameters:"""

	response = call_ai(
		prompt=prompt,
		provider=provider,
		model=model,
		calling_app=calling_app or "ai_interface",
		function_type="Query",
		sync=True,
		user=user,
		action="query_generation",
	)

	try:
		cleaned = response.strip().strip("`").strip()
		if cleaned.startswith("json"):
			cleaned = cleaned[4:].strip()
		params = json.loads(cleaned)
	except (json.JSONDecodeError, ValueError):
		return {"error": "LLM returned invalid JSON", "raw": response[:500]}

	if not isinstance(params.get("doctype"), str):
		return {"error": "Missing or invalid doctype in query params"}

	limit = params.get("limit_page_length", 20)
	params["limit_page_length"] = min(int(limit), 100)

	return params


def _execute_query(query_params: dict, user: str) -> str:
	"""Execute the structured query with user's permissions."""
	if "error" in query_params:
		return json.dumps(query_params)

	original_user = frappe.session.user
	try:
		frappe.set_user(user)
		result = frappe.get_all(
			query_params["doctype"],
			filters=query_params.get("filters"),
			fields=query_params.get("fields", ["name"]),
			order_by=query_params.get("order_by", "creation desc"),
			limit_page_length=query_params.get("limit_page_length", 20),
		)
		return json.dumps(result, default=str, indent=2)
	except Exception as e:
		return json.dumps({"error": str(e)})
	finally:
		frappe.set_user(original_user)


def _build_answer_prompt(question: str, query_params: dict, results: str) -> str:
	"""Phase 3: Build prompt for answer synthesis."""
	return f"""You are an ERPNext data analyst. Answer the user's question based on the query results.

QUESTION: {question}

QUERY EXECUTED:
{json.dumps(query_params, indent=2)}

RESULTS:
{results}

Provide a clear, concise natural language answer. If the results are empty, say so. Include relevant numbers and details from the data."""

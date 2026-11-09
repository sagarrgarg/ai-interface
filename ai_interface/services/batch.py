"""Batches: many requests at once, answered within hours, at the provider's batch discount.

For work nobody is waiting on: overnight research, first drafts for tomorrow, summaries,
evaluations. Not for replies to customers.

    from ai_interface.services import batch

    name = batch.submit(
        [{"key": lead.name, "prompt": text_for(lead), "reference_doctype": "Lead", "reference_name": lead.name}
         for lead in leads],
        system=[{"text": PERSONA, "cache": True}],
        output_schema=PROFILE_SCHEMA,
        calling_app="excom",
        action="profile_company",
        on_complete="excom.excom_ai.engine.night.batch_done",
    )

    # later, in on_complete(batch_name) or anywhere:
    for item in batch.results(name):
        item.key, item.status, item.data, item.text, item.error, item.log

How it works:
- Budget check and routing as for any call; the first provider whose adapter supports batches
  serves the whole batch (no fallback mid-batch).
- One AI Call Log per request (status Queued, `batch`, `batch_key`); the provider's custom id is
  the log name, so results land on the right row.
- Every 5 minutes `poll_open_batches` asks the provider. When a batch has ended, each log gets
  its answer, tokens and cost at the batch price (AI Provider Type's Batch Discount); JSON answers
  are checked against the batch's schema. Then `on_complete` is called and the user gets an
  `ai_batch_complete` realtime event.
- A batch still open after 30 hours is marked failed (providers expire them at 24).

Answers are stored even when logging is off, because they are the result the caller is waiting
for. Once `on_complete` has run, the logging, redaction and retention rules apply to them as to
any log. Without `on_complete`, read them with `results()` before the retention period ends.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, now_datetime

from ai_interface.providers.base import CompletionRequest, ProviderRefusal
from ai_interface.services import ai_client, budget, privacy, router
from ai_interface.services.chat import _parse_json, _system_blocks

STALE_HOURS = 30


def submit(
	requests: list[dict],
	*,
	calling_app: str,
	action: str | None = None,
	system=None,
	output_schema: dict | None = None,
	effort: str | None = None,
	cache_ttl: str = "5m",
	provider: str | None = None,
	model: str | None = None,
	needs: list | None = None,
	function_type: str = "Generation",
	max_tokens: int | None = None,
	budget_scope: str | None = None,
	budget_caps: dict | None = None,
	on_complete: str | None = None,
	user: str | None = None,
) -> str:
	"""Submit requests as one batch; returns the AI Batch name.

	Each request: {"key", "prompt" or "messages", optional "system" (replaces the shared one),
	"reference_doctype", "reference_name"}. `key` is the caller's own id for the request.
	"""
	if not requests:
		frappe.throw(_("A batch needs at least one request."))
	settings = frappe.get_single("AI Settings")
	user = user or frappe.session.user
	shared_system = _system_blocks(system)
	budget.check(settings, calling_app, user, scope=budget_scope, caps=budget_caps)

	features = {"batch"} | ({"system"} if shared_system else set()) | ({"json"} if output_schema else set())
	sample = "\n".join(
		[b["text"] for b in shared_system]
		+ [r.get("prompt") or json.dumps(r.get("messages"), default=str) for r in requests[:1]]
	)
	chain = router.build_chain(
		settings,
		provider=provider,
		model=model,
		template=None,
		function_type=function_type,
		calling_app=calling_app,
		needs=list(needs or []),
		prompt=sample,
	)
	chain = [step for step in chain if _adapter(step["provider"]).supports(features)]
	if not chain:
		frappe.throw(_("No configured provider can run this as a batch."), exc=router.RoutingError)
	step = chain[0]
	provider_doc = frappe.get_doc("AI Provider", step["provider"])
	adapter = _adapter(step["provider"])

	doc = frappe.get_doc(
		{
			"doctype": "AI Batch",
			"status": "Submitted",
			"calling_app": calling_app,
			"action": action,
			"budget_scope": budget_scope,
			"provider": step["provider"],
			"model": step["model"],
			"user": user,
			"total": len(requests),
			"output_schema": json.dumps(output_schema) if output_schema else None,
			"on_complete": on_complete,
		}
	).insert(ignore_permissions=True)

	items, max_tokens = [], int(max_tokens or settings.max_output_tokens or 16000)
	base_currency = ai_client._base_currency(settings)
	for r in requests:
		messages = r.get("messages") or [{"role": "user", "content": r.get("prompt") or ""}]
		blocks = _system_blocks(r["system"]) if r.get("system") else shared_system
		req = CompletionRequest(
			messages=messages,
			model=step["model"],
			max_tokens=max_tokens,
			system=blocks,
			output_schema=output_schema,
			effort=effort,
			cache_ttl=cache_ttl,
		)
		log = ai_client._create_call_log(
			settings,
			status="Queued",
			function_type=function_type,
			calling_app=calling_app,
			provider=step["provider"],
			model=step["model"],
			user=user,
			is_sync=0,
			reference_doctype=r.get("reference_doctype"),
			reference_name=r.get("reference_name"),
			module=ai_client._module_for_doctype(r["reference_doctype"])
			if r.get("reference_doctype")
			else None,
			action=action,
			budget_scope=budget_scope,
			batch=doc.name,
			batch_key=str(r.get("key") or ""),
			input_text="\n\n".join([b["text"] for b in blocks] + [json.dumps(messages, default=str)]),
			currency=provider_doc.currency or "USD",
			base_currency=base_currency,
		)
		items.append((log.name, req))

	credential, auth_type = provider_doc.get_credential()
	try:
		batch_id = adapter.submit_batch(
			items,
			credential=credential,
			auth_type=auth_type,
			api_base_url=provider_doc.api_base_url or "",
			timeout=settings.api_call_timeout or 120,
		)
	except Exception as e:
		_fail(doc, f"Submission failed: {e!s}", [cid for cid, _ in items])
		raise
	doc.db_set({"provider_batch_id": batch_id, "submitted_on": now_datetime()})
	frappe.db.commit()
	return doc.name


def poll_open_batches():
	"""Scheduler (every 5 minutes): collect every batch the provider has finished."""
	for name in frappe.get_all("AI Batch", {"status": "Submitted"}, pluck="name", order_by="creation asc"):
		try:
			poll(name)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"AI batch poll failed: {name}", message=frappe.get_traceback())


def poll(name: str) -> str:
	"""Check one batch; when it has ended, store every answer and call back. Returns the status."""
	doc = frappe.get_doc("AI Batch", name)
	if doc.status != "Submitted":
		return doc.status
	provider_doc = frappe.get_doc("AI Provider", doc.provider)
	adapter = _adapter(doc.provider)
	credential, auth_type = provider_doc.get_credential()
	auth = dict(credential=credential, auth_type=auth_type, api_base_url=provider_doc.api_base_url or "")

	if not doc.provider_batch_id or (
		doc.submitted_on and now_datetime() > add_to_date(doc.submitted_on, hours=STALE_HOURS)
	):
		_fail(doc, _("No result after {0} hours.").format(STALE_HOURS))
		return "Failed"
	state = adapter.batch_status(doc.provider_batch_id, **auth)
	doc.db_set("last_polled_on", now_datetime())
	if not state.get("ended"):
		frappe.db.commit()
		return "Submitted"

	schema = json.loads(doc.output_schema) if doc.output_schema else None
	type_doc = frappe.get_cached_doc("AI Provider Type", provider_doc.provider_type)
	factor = (
		1
		- flt(
			type_doc.get("batch_discount_percent")
			if type_doc.get("batch_discount_percent") is not None
			else 50
		)
		/ 100
	)
	rate = ai_client._exchange_rate(provider_doc, ai_client._base_currency())
	ok = failed = 0
	total_cost = 0.0
	for custom_id, outcome in adapter.batch_results(doc.provider_batch_id, **auth):
		if not frappe.db.exists("AI Call Log", custom_id):
			continue
		if isinstance(outcome, Exception):
			failed += 1
			frappe.db.set_value(
				"AI Call Log",
				custom_id,
				{
					"status": "Failed",
					"error_type": "Refused"
					if isinstance(outcome, ProviderRefusal)
					else ai_client._classify_error(outcome),
					"error_message": str(outcome)[:2000],
				},
				update_modified=False,
			)
			continue
		from ai_interface.services.chat import _cost

		cost = _cost(provider_doc, doc.model, outcome) * factor
		problem = None
		if schema:
			_data, problem = _parse_json(outcome.content, schema)
		values = {
			"status": "Failed" if problem else "Completed",
			"model": outcome.model or doc.model,
			"output_text": outcome.content,  # delivered to the caller first; policy applies after
			"input_tokens": outcome.input_tokens,
			"output_tokens": outcome.output_tokens,
			"cache_read_tokens": outcome.cache_read_tokens,
			"cache_write_tokens": outcome.cache_write_tokens,
			"stop_reason": outcome.stop_reason,
			"cost": cost,
			"currency": provider_doc.currency or ai_client._base_currency(),
			"exchange_rate": rate,
			"base_cost": cost * rate,
		}
		if problem:
			values.update(
				{"error_type": "Invalid Response", "error_message": f"Not valid for the schema: {problem}"}
			)
			failed += 1
		else:
			ok += 1
		total_cost += cost * rate
		frappe.db.set_value("AI Call Log", custom_id, values, update_modified=False)

	# Anything the provider never answered.
	missing = frappe.get_all("AI Call Log", {"batch": doc.name, "status": "Queued"}, pluck="name")
	for log in missing:
		frappe.db.set_value(
			"AI Call Log",
			log,
			{"status": "Failed", "error_type": "Provider Error", "error_message": "No result in the batch."},
			update_modified=False,
		)
	failed += len(missing)
	doc.db_set(
		{
			"status": "Ended",
			"succeeded": ok,
			"failed": failed,
			"ended_on": now_datetime(),
			"base_cost": total_cost,
		}
	)
	frappe.db.commit()

	if doc.on_complete:
		try:
			frappe.get_attr(doc.on_complete)(doc.name)
			_apply_policy(doc)  # delivered: from now on the usual logging rules apply
		except Exception:
			frappe.log_error(
				title=f"AI batch callback failed: {doc.on_complete}", message=frappe.get_traceback()
			)
	frappe.publish_realtime(
		"ai_batch_complete", {"batch": doc.name, "succeeded": ok, "failed": failed}, user=doc.user
	)
	frappe.db.commit()
	return "Ended"


def results(name: str) -> list[frappe._dict]:
	"""Every request of a batch: key, status, text, data (parsed JSON), error, log, reference."""
	doc = frappe.get_doc("AI Batch", name)
	schema = json.loads(doc.output_schema) if doc.output_schema else None
	out = []
	for row in frappe.get_all(
		"AI Call Log",
		{"batch": name},
		[
			"name",
			"batch_key",
			"status",
			"output_text",
			"error_message",
			"reference_doctype",
			"reference_name",
		],
		order_by="creation asc",
	):
		data = _parse_json(row.output_text, schema)[0] if schema and row.status == "Completed" else None
		out.append(
			frappe._dict(
				key=row.batch_key,
				status=row.status,
				text=row.output_text,
				data=data,
				error=row.error_message,
				log=row.name,
				reference_doctype=row.reference_doctype,
				reference_name=row.reference_name,
			)
		)
	return out


@frappe.whitelist()
def cancel(name: str):
	frappe.only_for(("System Manager", "AI Manager"))
	doc = frappe.get_doc("AI Batch", name)
	if doc.status != "Submitted":
		return doc.status
	provider_doc = frappe.get_doc("AI Provider", doc.provider)
	credential, auth_type = provider_doc.get_credential()
	if doc.provider_batch_id:
		_adapter(doc.provider).cancel_batch(
			doc.provider_batch_id,
			credential=credential,
			auth_type=auth_type,
			api_base_url=provider_doc.api_base_url or "",
		)
	_fail(doc, _("Cancelled by {0}").format(frappe.session.user), status="Cancelled")
	return "Cancelled"


def _fail(doc, message: str, logs: list | None = None, status: str = "Failed"):
	logs = (
		logs
		if logs is not None
		else frappe.get_all("AI Call Log", {"batch": doc.name, "status": "Queued"}, pluck="name")
	)
	for log in logs:
		frappe.db.set_value(
			"AI Call Log",
			log,
			{"status": "Failed", "error_type": "Provider Error", "error_message": message},
			update_modified=False,
		)
	doc.db_set({"status": status, "error": message, "failed": len(logs), "ended_on": now_datetime()})
	frappe.db.commit()


def _apply_policy(doc):
	"""After delivery, the stored answers follow the usual logging rules."""
	settings = frappe.get_cached_doc("AI Settings")
	if not settings.enable_logging:
		frappe.db.sql("UPDATE `tabAI Call Log` SET output_text = '' WHERE batch = %s", doc.name)
	elif privacy.policy(doc.calling_app, settings).redact:
		for name, text in frappe.db.sql(
			"SELECT name, output_text FROM `tabAI Call Log` WHERE batch = %s", doc.name
		):
			frappe.db.set_value(
				"AI Call Log", name, "output_text", privacy.redact(text), update_modified=False
			)


def _adapter(provider_name: str):
	from ai_interface.providers import get_provider

	return get_provider(frappe.get_cached_doc("AI Provider", provider_name).provider_type)


@frappe.whitelist()
def check_now(name: str) -> str:
	"""The batch form's button: poll this batch at once instead of waiting for the scheduler."""
	frappe.only_for(("System Manager", "AI Manager"))
	return poll(name)

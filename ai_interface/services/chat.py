"""Full requests: system prompt, messages, a JSON answer, tools, caching and effort.

`generate()` sends one prompt and gets text back, which is enough for one-shot jobs. Agents and
assistants need more, so `run()` takes the whole request and still goes through everything a
call gets here: routing by capability, fallback, budgets, cost in two currencies, health and the
call log. Callers name what they need, never a vendor:

    from ai_interface.services.chat import run

    result = run(
        system=[{"text": PERSONA, "cache": True}, {"text": PLAYBOOK, "cache": True}],
        messages=[{"role": "user", "content": task_text}],
        output_schema=REPLY_SCHEMA,
        tools=["excom.erp_read"],          # registered by an app in its hooks (ai_tools)
        effort="high",
        calling_app="excom",
        reference_doctype="Excom Agent Task",
        reference_name=task.name,
        action="handle_reply",
    )
    result.data       # the validated JSON answer
    result.text       # the raw text
    result.usage      # tokens, cache tokens, cost in the provider's and the base currency

How it runs:
    1. Budget check, then the routing chain (capabilities: tools; plus what the adapter can do:
       system prompt, JSON, tools, server tools). Nothing is spent if nothing can serve it.
    2. The first turn walks the chain: retryable failures (rate limit, timeout, 5xx) move to the
       next provider, auth and configuration errors stop. Later turns stay on the provider that
       answered, because a tool conversation is in that vendor's own format.
    3. Tool calls are run and answered, at most `max_steps` turns and `time_budget` seconds.
    4. With `output_schema`, the answer is parsed and validated; one repair turn if it fails.
    5. Every turn is an AI Call Log row; turns after the first point at the first (`parent_call`).

Tools come from the caller (a dict with a `run` callable), from any app's `ai_tools` hook (a
name), or are vendor server tools (a dict with a `type`, for example web search). A hook entry
maps a tool name to a function returning its spec:

    # hooks.py of any app
    ai_tools = {"excom.erp_read": "excom.excom_ai.tools.erp_read.spec"}

    def spec():
        return {"name": "erp_read", "description": "...", "input_schema": {...},
                "run": run, "tier": "read", "permitted": lambda user: True}

Tool output is returned to the model as data. Callers that pass web pages or emails should fence
them as untrusted text in their own prompts.
"""

from __future__ import annotations

import json
import re
import time
import traceback

import frappe
from frappe import _

from ai_interface.providers import get_provider
from ai_interface.providers.base import CompletionRequest, ProviderRefusal
from ai_interface.services import ai_client, budget, privacy, router

DEFAULT_MAX_STEPS = 6
TOOL_RESULT_LIMIT = 20000  # characters of one tool result sent back to the model
TIERS = ("read", "draft", "communicate", "paid")


class AIRequestFailed(frappe.ValidationError):
	pass


class AIRefused(AIRequestFailed):
	pass


def run(
	messages: list | None = None,
	*,
	prompt: str | None = None,
	template: str | None = None,
	context: dict | None = None,
	system=None,
	output_schema: dict | None = None,
	tools: list | None = None,
	allowed_tiers: tuple = ("read",),
	max_steps: int = DEFAULT_MAX_STEPS,
	time_budget: int | None = None,
	effort: str | None = None,
	cache_ttl: str = "5m",
	calling_app: str = "",
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	module: str | None = None,
	action: str | None = None,
	needs: list | None = None,
	provider: str | None = None,
	model: str | None = None,
	function_type: str = "Generation",
	max_tokens: int | None = None,
	user: str | None = None,
	budget_scope: str | None = None,
	budget_caps: dict | None = None,
) -> frappe._dict:
	"""Run one full request to the end (synchronous: call it from a background job).

	messages / prompt / template+context: the conversation; a bare `prompt` is one user turn.
	system: a string, or a list of strings or {"text", "cache"} blocks, stable content first.
	tools: names from `ai_tools` hooks, caller tool dicts with a `run`, or server tool dicts.
	allowed_tiers: the tool tiers this run may use; a tool outside them is refused.
	budget_scope / budget_caps: charge the run to the caller's own scope, with its caps.
	"""
	settings = frappe.get_single("AI Settings")
	user = user or frappe.session.user
	system_blocks = _system_blocks(system)
	convo = _conversation(messages, prompt, template, context)
	client_tools, server_tools = _resolve_tools(tools or [], user, allowed_tiers)

	features = set()
	if system_blocks:
		features.add("system")
	if output_schema:
		features.add("json")
	if client_tools:
		features.add("tools")
	if server_tools:
		features.add("server_tools")

	if not module and reference_doctype:
		module = ai_client._module_for_doctype(reference_doctype)

	budget.check(settings, calling_app, user, scope=budget_scope, caps=budget_caps)

	requirements = list(needs or [])
	if (client_tools or server_tools) and "tools" not in requirements:
		requirements.append("tools")
	chain = router.build_chain(
		settings,
		provider=provider,
		model=model,
		template=template,
		function_type=function_type,
		calling_app=calling_app,
		needs=requirements,
		prompt=_estimate_text(system_blocks, convo),
	)
	chain = [step for step in chain if _adapter_for(step["provider"]).supports(features)]
	if not chain:
		frappe.throw(
			_("No configured provider can serve this request (it needs: {0}).").format(
				", ".join(sorted(features))
			),
			exc=router.RoutingError,
		)

	state = frappe._dict(
		settings=settings,
		user=user,
		attribution=dict(
			function_type=function_type,
			calling_app=calling_app,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			module=module,
			action=action,
			budget_scope=budget_scope,
		),
		parent=None,
		logs=[],
		usage=frappe._dict(
			input_tokens=0,
			output_tokens=0,
			cache_read_tokens=0,
			cache_write_tokens=0,
			cost=0.0,
			base_cost=0.0,
		),
		started=time.time(),
	)
	request = CompletionRequest(
		messages=list(convo),
		model="",
		max_tokens=int(max_tokens or settings.max_output_tokens or 16000),
		system=system_blocks,
		output_schema=output_schema,
		tools=[_wire_tool(t) for t in client_tools] + server_tools,
		effort=effort,
		cache_ttl=cache_ttl,
	)
	runners = {t["name"]: t for t in client_tools}
	tool_log = []

	response, pinned = _first_turn(chain, request, state)
	adapter = _adapter_for(pinned["provider"])
	turns = 1
	while response.tool_calls:
		if turns >= max_steps:
			raise AIRequestFailed(_("Stopped after {0} turns: the model kept calling tools.").format(turns))
		if time_budget and time.time() - state.started > time_budget:
			raise AIRequestFailed(_("Stopped: the time budget of {0} seconds ran out.").format(time_budget))
		results = []
		for call in response.tool_calls:
			content, is_error = _run_tool(runners.get(call.name), call, user, state)
			tool_log.append(
				{"name": call.name, "input": call.input, "is_error": is_error, "result": content[:500]}
			)
			results.append({"id": call.id, "content": content, "is_error": is_error})
		_append(request.messages, response.assistant_message)
		_append(request.messages, adapter.tool_results_message(results))
		response = _turn(pinned, request, state)
		turns += 1

	text = response.content or ""
	data = None
	if output_schema:
		data, problem = _parse_json(text, output_schema)
		if problem:
			# One repair turn, on the same provider.
			_append(request.messages, response.assistant_message or {"role": "assistant", "content": text})
			request.messages.append(
				{
					"role": "user",
					"content": "Your reply did not match the required JSON schema: "
					f"{problem}. Reply again with only the corrected JSON.",
				}
			)
			response = _turn(pinned, request, state)
			text = response.content or ""
			data, problem = _parse_json(text, output_schema)
			if problem:
				raise AIRequestFailed(_("The answer was not valid JSON for the schema: {0}").format(problem))

	return frappe._dict(
		text=text,
		data=data,
		stop_reason=response.stop_reason,
		provider=pinned["provider"],
		model=response.model or pinned["model"],
		log=state.parent,
		logs=state.logs,
		tool_calls=tool_log,
		usage=state.usage,
	)


# ---------------------------------------------------------------- turns


def _first_turn(chain, request, state):
	"""Walk the chain: move on only for retryable failures."""
	failures = []
	for index, step in enumerate(chain):
		try:
			return _turn(step, request, state), step
		except AIRequestFailed as e:
			failures.append(str(e))
			is_last = index == len(chain) - 1
			if is_last or getattr(e, "error_type", "") not in router.RETRYABLE:
				raise
	raise AIRequestFailed("\n".join(failures))


def _turn(step, request, state):
	"""One model call, logged. Raises AIRequestFailed (with .error_type) on failure."""
	settings = state.settings
	provider_doc = frappe.get_doc("AI Provider", step["provider"])
	adapter = _adapter_for(step["provider"])
	request.model = step["model"]
	base_currency = ai_client._base_currency(settings)
	log = ai_client._create_call_log(
		settings,
		status="Running",
		provider=step["provider"],
		model=step["model"],
		input_text=_transcript(request) if not state.parent else "",
		user=state.user,
		is_sync=1,
		currency=provider_doc.currency or "USD",
		base_currency=base_currency,
		parent_call=state.parent,
		step=len(state.logs) + 1,
		**state.attribution,
	)
	state.logs.append(log.name)
	if not state.parent:
		state.parent = log.name

	credential, auth_type = provider_doc.get_credential()
	start = time.time()
	try:
		response = adapter.complete(
			request,
			credential=credential,
			auth_type=auth_type,
			api_base_url=provider_doc.api_base_url or "",
			timeout=settings.api_call_timeout or 120,
		)
	except Exception as e:
		error_type = "Refused" if isinstance(e, ProviderRefusal) else ai_client._classify_error(e)
		log.db_set(
			{
				"status": "Failed",
				"error_type": error_type,
				"error_message": f"{e!s}\n\n{traceback.format_exc()}",
				"latency_ms": int((time.time() - start) * 1000),
			}
		)
		frappe.db.commit()
		if error_type != "Refused":
			ai_client._record_failure(step["provider"], e, error_type)
		exc_class = AIRefused if error_type == "Refused" else AIRequestFailed
		failure = exc_class(f"[{step['provider']} / {step['model']}] {error_type}: {e!s}")
		failure.error_type = error_type
		raise failure from e

	if response.stop_reason == "max_tokens" and not response.tool_calls:
		error_type = "Invalid Response"
		log.db_set(
			{
				"status": "Failed",
				"error_type": error_type,
				"stop_reason": response.stop_reason,
				"error_message": "The answer was cut off at max_tokens.",
			}
		)
		frappe.db.commit()
		failure = AIRequestFailed(_("The answer was cut off at max_tokens; raise max_tokens for this call."))
		failure.error_type = error_type
		raise failure

	cost = _cost(provider_doc, step["model"], response)
	rate = ai_client._exchange_rate(provider_doc, base_currency)
	keep = bool(settings.enable_logging)
	log.db_set(
		{
			"status": "Completed",
			"model": response.model or step["model"],
			"attempts": 1,
			"output_text": privacy.for_storage(
				response.content or json.dumps([c.__dict__ for c in response.tool_calls], default=str),
				state.attribution["calling_app"],
				settings,
			)
			if keep
			else "",
			"input_tokens": response.input_tokens,
			"output_tokens": response.output_tokens,
			"cache_read_tokens": response.cache_read_tokens,
			"cache_write_tokens": response.cache_write_tokens,
			"stop_reason": response.stop_reason,
			"cost": cost,
			"currency": provider_doc.currency or base_currency,
			"exchange_rate": rate,
			"base_cost": cost * rate,
			"latency_ms": int((time.time() - start) * 1000),
		}
	)
	frappe.db.commit()
	provider_doc.record_health(ok=True)

	u = state.usage
	u.input_tokens += response.input_tokens
	u.output_tokens += response.output_tokens
	u.cache_read_tokens += response.cache_read_tokens
	u.cache_write_tokens += response.cache_write_tokens
	u.cost += cost
	u.base_cost += cost * rate
	return response


def _cost(provider_doc, model, response) -> float:
	"""Token cost, cache tokens included (they are billed apart from input tokens)."""
	for m in provider_doc.models:
		if m.model_id == model:
			rate_in = m.cost_per_input_token or 0
			read = m.get("cost_per_cache_read_token") or rate_in * 0.1
			write = m.get("cost_per_cache_write_token") or rate_in * 1.25
			return (
				response.input_tokens * rate_in
				+ response.output_tokens * (m.cost_per_output_token or 0)
				+ response.cache_read_tokens * read
				+ response.cache_write_tokens * write
			)
	return 0.0


# ---------------------------------------------------------------- tools


def registered_tools() -> dict:
	"""Every tool apps register through the `ai_tools` hook: name -> dotted path of its spec()."""
	out = {}
	for name, paths in (frappe.get_hooks("ai_tools") or {}).items():
		out[name] = paths[-1] if isinstance(paths, list) else paths
	return out


def _resolve_tools(tools, user, allowed_tiers):
	client, server = [], []
	hooks = None
	for t in tools:
		if isinstance(t, str):
			hooks = hooks if hooks is not None else registered_tools()
			if t not in hooks:
				frappe.throw(_("Unknown AI tool: {0}").format(t))
			spec = frappe.get_attr(hooks[t])()
		elif isinstance(t, dict) and t.get("type"):
			server.append(t)
			continue
		else:
			spec = dict(t)
		tier = spec.get("tier", "read")
		if tier not in TIERS:
			frappe.throw(_("AI tool {0} has an unknown tier {1}").format(spec.get("name"), tier))
		if tier not in allowed_tiers:
			frappe.throw(
				_("AI tool {0} is a '{1}' tool, not allowed in this run").format(spec.get("name"), tier)
			)
		permitted = spec.get("permitted")
		if permitted and not permitted(user):
			frappe.throw(
				_("{0} may not use the AI tool {1}").format(user, spec.get("name")), frappe.PermissionError
			)
		if not callable(spec.get("run")):
			frappe.throw(_("AI tool {0} has no run function").format(spec.get("name")))
		client.append(spec)
	return client, server


def _wire_tool(spec):
	return {
		"name": spec["name"],
		"description": spec.get("description", ""),
		"input_schema": spec.get("input_schema") or {"type": "object", "properties": {}},
		"strict": bool(spec.get("strict")),
	}


def _run_tool(spec, call, user, state):
	"""Run one tool call. Errors go back to the model as an error result, not up the stack."""
	if not spec:
		return f"Unknown tool: {call.name}", True
	try:
		result = spec["run"](call.input or {}, frappe._dict(user=user, **state.attribution))
	except Exception as e:
		frappe.log_error(title=f"AI tool {call.name} failed", message=frappe.get_traceback())
		return f"Error: {e!s}"[:TOOL_RESULT_LIMIT], True
	if not isinstance(result, str):
		result = json.dumps(result, default=str, ensure_ascii=False)
	return result[:TOOL_RESULT_LIMIT], False


# ---------------------------------------------------------------- input and output


def _system_blocks(system) -> list:
	if not system:
		return []
	if isinstance(system, str):
		return [{"text": system, "cache": False}]
	blocks = []
	for b in system:
		if isinstance(b, str):
			blocks.append({"text": b, "cache": False})
		elif b.get("text"):
			blocks.append({"text": b["text"], "cache": bool(b.get("cache"))})
	return blocks


def _conversation(messages, prompt, template, context) -> list:
	if messages:
		return [dict(m) for m in messages]
	text = ai_client._render_prompt(prompt or "", template, context)
	if not text:
		frappe.throw(_("Nothing to send: give messages, a prompt or a template."))
	return [{"role": "user", "content": text}]


def _append(messages: list, item):
	if item is None:
		return
	if isinstance(item, list):
		messages.extend(item)
	else:
		messages.append(item)


def _estimate_text(system_blocks, convo) -> str:
	parts = [b["text"] for b in system_blocks]
	parts += [
		m["content"] if isinstance(m.get("content"), str) else json.dumps(m.get("content"), default=str)
		for m in convo
	]
	return "\n".join(parts)


def _transcript(request) -> str:
	"""What the first log row keeps as its input (subject to enable_logging)."""
	parts = [f"[system]\n{b['text']}" for b in request.system]
	for m in request.messages:
		content = (
			m["content"] if isinstance(m.get("content"), str) else json.dumps(m.get("content"), default=str)
		)
		parts.append(f"[{m.get('role')}]\n{content}")
	if request.output_schema:
		parts.append("[output schema]\n" + json.dumps(request.output_schema))
	if request.tools:
		parts.append("[tools] " + ", ".join(t.get("name") or t.get("type", "") for t in request.tools))
	return "\n\n".join(parts)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _parse_json(text: str, schema: dict):
	"""(data, None) when the text is JSON matching the schema, else (None, the problem)."""
	import jsonschema

	cleaned = _FENCE.sub("", (text or "").strip())
	try:
		data = json.loads(cleaned)
	except ValueError:
		start, end = cleaned.find("{"), cleaned.rfind("}")
		try:
			data = json.loads(cleaned[start : end + 1]) if start >= 0 and end > start else None
		except ValueError:
			data = None
		if data is None:
			return None, "the reply was not JSON"
	try:
		jsonschema.validate(data, schema)
	except jsonschema.ValidationError as e:
		path = "/".join(str(p) for p in e.absolute_path) or "(root)"
		return None, f"{path}: {e.message}"
	return data, None


def _adapter_for(provider_name: str):
	doc = frappe.get_cached_doc("AI Provider", provider_name)
	return get_provider(doc.provider_type)


# ---------------------------------------------------------------- in the background


def run_async(
	*, on_complete: str | None = None, queue: str = "long", job_context: dict | None = None, **kwargs
) -> str:
	"""Queue run() as a background job and return the job id.

	Everything must be serialisable: tools by hook name or as server tool dicts, not callables.
	When the job ends, the user gets a realtime event (`ai_run_complete` or `ai_run_failed`, with
	the job id and the first log's name), and `on_complete`, a dotted path, is called as
	on_complete(result=dict or None, error=str or None, context=job_context).
	"""
	for t in kwargs.get("tools") or []:
		if not isinstance(t, (str, dict)) or (isinstance(t, dict) and not t.get("type")):
			frappe.throw(_("In a background run, tools must be hook names or server tools."))
	user = kwargs.pop("user", None) or frappe.session.user
	job_id = f"ai_run::{frappe.generate_hash(length=12)}"
	frappe.enqueue(
		"ai_interface.services.chat._run_job",
		queue=queue,
		job_id=job_id,
		enqueue_after_commit=True,
		run_kwargs=kwargs,
		user=user,
		on_complete=on_complete,
		job_context=job_context or {},
		job_name=job_id,
	)
	return job_id


def _run_job(run_kwargs: dict, user: str, on_complete: str | None, job_context: dict, job_name: str):
	result, error = None, None
	try:
		result = run(user=user, **run_kwargs)
	except Exception as e:
		error = str(e)
		frappe.log_error(title="AI run failed", message=frappe.get_traceback())
	payload = {"job": job_name, "log": result.log if result else None, "error": error}
	frappe.publish_realtime("ai_run_failed" if error else "ai_run_complete", payload, user=user)
	if on_complete:
		try:
			frappe.get_attr(on_complete)(
				result=dict(result) if result else None, error=error, context=job_context
			)
		except Exception:
			frappe.log_error(title=f"AI run callback failed: {on_complete}", message=frappe.get_traceback())
	frappe.db.commit()

import os
import time
import traceback

import frappe
from frappe import _
from frappe.utils import flt

from ai_interface.providers import get_provider
from ai_interface.providers.base import ProviderResponse
from ai_interface.services import budget, router


def call_ai(
	prompt: str,
	*,
	images: list[str] | None = None,
	provider: str | None = None,
	model: str | None = None,
	template: str | None = None,
	context: dict | None = None,
	calling_app: str = "",
	function_type: str = "Generation",
	sync: bool = False,
	user: str | None = None,
	max_tokens: int | None = None,
	temperature: float = 0.7,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	module: str | None = None,
	action: str | None = None,
	needs: list[str] | None = None,
) -> str:
	"""Central AI call dispatcher.

	Async (default): Creates AI Call Log, enqueues execution, returns log name.
	Sync (sync=True): Executes inline, creates log, returns response text directly.

	Attribution (`reference_doctype`, `reference_name`, `module`, `action`) is
	optional but strongly recommended — it is what powers cost and failure
	breakdowns in the AI Command Center.

	`needs` states capabilities rather than a model name — e.g. ["vision"],
	["tools"] — so the call keeps working when the configured provider changes.
	Passing `images` implies "vision" without saying so.
	"""
	settings = frappe.get_single("AI Settings")
	user = user or frappe.session.user

	rendered_prompt = _render_prompt(prompt, template, context)

	if not module and reference_doctype:
		module = _module_for_doctype(reference_doctype)

	# Refused before anything is queued, so a blocked call costs nothing.
	budget.check(settings, calling_app)

	requirements = list(needs or [])
	if images and "vision" not in requirements:
		requirements.append("vision")

	chain = router.build_chain(
		settings,
		provider=provider,
		model=model,
		template=template,
		function_type=function_type,
		calling_app=calling_app,
		needs=requirements,
		prompt=rendered_prompt,
	)
	resolved_provider = chain[0]["provider"]
	resolved_model = chain[0]["model"]
	provider_doc = frappe.get_cached_doc("AI Provider", resolved_provider)

	log = _create_call_log(
		settings,
		status="Queued",
		function_type=function_type,
		calling_app=calling_app,
		provider=resolved_provider,
		model=resolved_model,
		prompt_template=template,
		input_text=rendered_prompt,
		user=user,
		is_sync=sync,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		module=module,
		action=action,
		currency=provider_doc.currency or "USD",
		base_currency=_base_currency(settings),
	)

	if sync:
		_execute_ai_call(
			log_name=log.name,
			rendered_prompt=rendered_prompt,
			images=images,
			provider_doc_name=provider_doc.name,
			model=resolved_model,
			max_tokens=max_tokens or settings.max_output_tokens or 4096,
			temperature=temperature,
			timeout=settings.api_call_timeout or 120,
			chain=chain,
		)
		log.reload()
		if log.status == "Failed":
			frappe.throw(log.error_message)
		return log.output_text

	frappe.enqueue(
		"ai_interface.services.ai_client._execute_ai_call",
		log_name=log.name,
		rendered_prompt=rendered_prompt,
		images=images,
		provider_doc_name=provider_doc.name,
		model=resolved_model,
		max_tokens=max_tokens or settings.max_output_tokens or 4096,
		temperature=temperature,
		timeout=settings.api_call_timeout or 120,
		chain=chain,
		queue="default",
		is_async=True,
	)
	return log.name


def _execute_ai_call(
	log_name: str,
	rendered_prompt: str,
	images: list[str] | None,
	provider_doc_name: str,
	model: str,
	max_tokens: int,
	temperature: float,
	timeout: int,
	chain: list[dict] | None = None,
):
	"""Background worker. Walks the routing chain until one provider answers.

	Only retryable failures move to the next entry — a rate limit, a timeout, a
	5xx. Auth and configuration errors stop immediately, because retrying a bad
	key against another provider just fails again more slowly.
	"""
	log = frappe.get_doc("AI Call Log", log_name)
	log.db_set("status", "Running")
	frappe.db.commit()

	attempts = chain or [{"provider": provider_doc_name, "model": model}]
	history = []

	for index, step in enumerate(attempts):
		start_time = time.time()
		try:
			provider_doc = frappe.get_doc("AI Provider", step["provider"])
			response = _attempt_call(
				provider_doc=provider_doc,
				rendered_prompt=rendered_prompt,
				images=images,
				model=step["model"],
				max_tokens=max_tokens,
				temperature=temperature,
				timeout=timeout,
			)
		except Exception as e:
			latency_ms = int((time.time() - start_time) * 1000)
			error_type = _classify_error(e)
			history.append(f"[{step['provider']} / {step['model']}] {error_type}: {e!s}")

			is_last = index == len(attempts) - 1
			if is_last or error_type not in router.RETRYABLE:
				log.db_set(
					{
						"status": "Failed",
						"provider": step["provider"],
						"model": step["model"],
						"attempts": index + 1,
						"error_type": error_type,
						"error_message": "\n".join(history) + f"\n\n{traceback.format_exc()}",
						"latency_ms": latency_ms,
					}
				)
				frappe.db.commit()
				frappe.publish_realtime(
					"ai_call_failed",
					{"log": log_name, "status": "Failed", "error": str(e)},
					user=log.user,
				)
				return
			continue

		latency_ms = int((time.time() - start_time) * 1000)
		cost = _calculate_cost(provider_doc, step["model"], response.input_tokens, response.output_tokens)
		retain_payloads = bool(frappe.db.get_single_value("AI Settings", "enable_logging"))

		base_currency = log.base_currency or _base_currency()
		rate = _exchange_rate(provider_doc, base_currency)

		log.db_set(
			{
				"status": "Completed",
				"provider": step["provider"],
				"model": response.model or step["model"],
				"attempts": index + 1,
				"output_text": response.content if retain_payloads else "",
				"input_tokens": response.input_tokens,
				"output_tokens": response.output_tokens,
				"cost": cost,
				# The provider that actually served the call sets the billing
				# currency. A fallback can hand the work to a vendor billing in
				# something else, and `cost` is computed from *its* rates — so
				# carrying the originally-planned currency here would label a
				# USD amount as rupees.
				"currency": provider_doc.currency or base_currency,
				"exchange_rate": rate,
				"base_cost": cost * rate,
				"base_currency": base_currency,
				"latency_ms": latency_ms,
				# Kept even on success: a call that only worked on the second
				# provider is a signal worth seeing, not noise to discard.
				"error_message": "\n".join(history) or None,
			}
		)
		frappe.db.commit()

		frappe.publish_realtime(
			"ai_call_complete",
			{"log": log_name, "status": "Completed"},
			user=log.user,
		)
		return


def _attempt_call(
	provider_doc,
	rendered_prompt: str,
	images: list[str] | None,
	model: str,
	max_tokens: int,
	temperature: float,
	timeout: int,
) -> ProviderResponse:
	"""One provider call. Raises on failure so the caller can decide to retry."""
	credential, auth_type = provider_doc.get_credential()
	provider_instance = get_provider(provider_doc.provider_type)
	messages = [{"role": "user", "content": rendered_prompt}]

	if images:
		if hasattr(provider_instance, "vision_from_paths"):
			return provider_instance.vision_from_paths(
				messages=messages,
				file_paths=_resolve_file_paths(images),
				model=model,
				max_tokens=max_tokens,
				timeout=timeout,
			)
		return provider_instance.vision(
			messages=messages,
			images=_load_images(images),
			model=model,
			max_tokens=max_tokens,
			credential=credential,
			auth_type=auth_type,
			api_base_url=provider_doc.api_base_url or "",
			timeout=timeout,
		)

	return provider_instance.chat(
		messages=messages,
		model=model,
		max_tokens=max_tokens,
		temperature=temperature,
		credential=credential,
		auth_type=auth_type,
		api_base_url=provider_doc.api_base_url or "",
		timeout=timeout,
	)


def _render_prompt(
	prompt: str,
	template_name: str | None,
	context: dict | None,
) -> str:
	"""Render prompt from template if provided, otherwise return raw prompt."""
	if not template_name:
		return prompt

	tmpl = frappe.get_doc("AI Prompt Template", template_name)
	if context:
		return frappe.render_template(tmpl.body, context)
	return tmpl.body


def _create_call_log(settings, **kwargs):
	"""Always persist the log row — the call contract returns its name, and the
	worker reloads it by name. `enable_logging` controls payload *retention*
	(prompt and response bodies), not whether the row exists.
	"""
	if not settings.enable_logging:
		kwargs["input_text"] = ""

	log = frappe.new_doc("AI Call Log")
	log.update(kwargs)
	log.insert(ignore_permissions=True)
	frappe.db.commit()
	return log


def _module_for_doctype(doctype: str) -> str | None:
	"""Best-effort module resolution from a doctype name."""
	try:
		return frappe.db.get_value("DocType", doctype, "module")
	except Exception:
		return None


# Status code is the vendor-neutral fact; text matching is the fallback for
# adapters that raise plain exceptions (SDKs, the Claude Code CLI).
STATUS_ERROR_TYPES = {
	400: "Provider Error",
	401: "Auth",
	403: "Auth",
	404: "Config Error",
	408: "Timeout",
	422: "Provider Error",
	429: "Rate Limit",
	500: "Provider Error",
	502: "Provider Error",
	503: "Provider Error",
	504: "Timeout",
	529: "Rate Limit",
}

ERROR_PATTERNS = (
	("Auth", ("authentication", "unauthorized", "invalid api key", "invalid x-api-key", "oauth", "api key")),
	("Rate Limit", ("rate limit", "too many requests", "overloaded", "quota")),
	("Timeout", ("timeout", "timed out", "deadline")),
	("Invalid Response", ("json", "could not parse", "unexpected response", "decode", "no choices")),
	(
		"Config Error",
		("no ai provider", "unknown provider type", "is disabled", "not configured", "no base url"),
	),
	("File Error", ("file not found", "access denied", "poppler", "no such file")),
	("Provider Error", ("api error", "connection", "connection refused", "name resolution")),
)


def _classify_error(exc: "Exception | str") -> str:
	"""Map a provider failure onto the AI Call Log error_type taxonomy.

	Prefers the HTTP status an adapter attached, because a vendor rewording its
	error text must not silently reclassify every failure on the dashboard.
	"""
	status = getattr(exc, "status_code", None)
	if status in STATUS_ERROR_TYPES:
		return STATUS_ERROR_TYPES[status]

	haystack = str(exc or "").lower()
	for label, needles in ERROR_PATTERNS:
		if any(n in haystack for n in needles):
			return label
	return "Unknown"


def _safe_resolve_path(url: str) -> str:
	"""Resolve a Frappe file URL to an absolute path, with traversal protection."""
	if url.startswith("/files/") or url.startswith("/private/files/"):
		file_path = frappe.get_site_path(url.lstrip("/"))
	else:
		file_path = frappe.get_site_path("public", url.lstrip("/"))

	site_root = os.path.realpath(frappe.get_site_path())
	resolved = os.path.realpath(file_path)
	if not resolved.startswith(site_root):
		frappe.throw(_("Access denied: file path outside site directory"))

	if not os.path.exists(resolved):
		frappe.throw(_("File not found: {0}").format(url))

	return resolved


DEFAULT_BASE_CURRENCY = "USD"


def _base_currency(settings=None) -> str:
	"""The one currency every call is normalised to for totalling."""
	value = (settings.base_currency if settings else None) or frappe.db.get_single_value(
		"AI Settings", "base_currency"
	)
	return value or frappe.db.get_default("currency") or DEFAULT_BASE_CURRENCY


def _exchange_rate(provider_doc, base_currency: str) -> float:
	"""Rate from the provider billing currency into the base currency.

	Snapshotted onto each log so a later rate change never rewrites past spend.
	A rate that cannot be resolved returns 0 rather than a silent 1.0 — treating
	₹100 as $100 is the exact error this stage exists to prevent, so an
	unconvertible call is left visibly unconverted for the dashboard to flag.
	"""
	from_currency = getattr(provider_doc, "currency", None) or base_currency
	if from_currency == base_currency:
		return 1.0

	manual = flt(getattr(provider_doc, "exchange_rate", 0))
	if manual > 0:
		return manual

	try:
		from erpnext.setup.utils import get_exchange_rate

		rate = flt(get_exchange_rate(from_currency, base_currency))
		if rate > 0:
			return rate
	except Exception:
		frappe.log_error(
			title="AI Interface: exchange rate lookup failed",
			message=f"{from_currency} -> {base_currency}\n{traceback.format_exc()}",
		)

	return 0.0


def _load_images(file_urls: list[str]) -> list[bytes]:
	"""Load image bytes from Frappe file URLs."""
	result = []
	for url in file_urls:
		file_path = _safe_resolve_path(url)
		with open(file_path, "rb") as f:
			result.append(f.read())
	return result


def _resolve_file_paths(file_urls: list[str]) -> list[str]:
	"""Resolve Frappe file URLs to absolute filesystem paths."""
	return [_safe_resolve_path(url) for url in file_urls]


def _calculate_cost(
	provider_doc,
	model: str,
	input_tokens: int,
	output_tokens: int,
) -> float:
	"""Calculate cost from provider model rates."""
	for m in provider_doc.models:
		if m.model_id == model:
			return input_tokens * (m.cost_per_input_token or 0) + output_tokens * (
				m.cost_per_output_token or 0
			)
	return 0.0

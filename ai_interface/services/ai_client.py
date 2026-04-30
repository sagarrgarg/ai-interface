import os
import time
import traceback

import frappe
from frappe import _

from ai_interface.providers import get_provider
from ai_interface.providers.base import ProviderResponse


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
) -> str:
	"""Central AI call dispatcher.

	Async (default): Creates AI Call Log, enqueues execution, returns log name.
	Sync (sync=True): Executes inline, creates log, returns response text directly.
	"""
	settings = frappe.get_single("AI Settings")
	user = user or frappe.session.user

	resolved_provider, resolved_model, provider_doc = _resolve_provider_model(
		provider, model, template, settings
	)

	rendered_prompt = _render_prompt(prompt, template, context)

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
):
	"""Background worker function. Executes the actual provider API call."""
	log = frappe.get_doc("AI Call Log", log_name)
	log.db_set("status", "Running")
	frappe.db.commit()

	provider_doc = frappe.get_doc("AI Provider", provider_doc_name)
	credential, auth_type = provider_doc.get_credential()
	provider_instance = get_provider(provider_doc.provider_type)

	start_time = time.time()
	try:
		messages = [{"role": "user", "content": rendered_prompt}]

		if images:
			if hasattr(provider_instance, "vision_from_paths"):
				file_paths = _resolve_file_paths(images)
				response: ProviderResponse = provider_instance.vision_from_paths(
					messages=messages,
					file_paths=file_paths,
					model=model,
					max_tokens=max_tokens,
					timeout=timeout,
				)
			else:
				image_bytes_list = _load_images(images)
				response: ProviderResponse = provider_instance.vision(
					messages=messages,
					images=image_bytes_list,
					model=model,
					max_tokens=max_tokens,
					credential=credential,
					auth_type=auth_type,
					api_base_url=provider_doc.api_base_url or "",
					timeout=timeout,
				)
		else:
			response: ProviderResponse = provider_instance.chat(
				messages=messages,
				model=model,
				max_tokens=max_tokens,
				temperature=temperature,
				credential=credential,
				auth_type=auth_type,
				api_base_url=provider_doc.api_base_url or "",
				timeout=timeout,
			)

		latency_ms = int((time.time() - start_time) * 1000)
		cost = _calculate_cost(provider_doc, model, response.input_tokens, response.output_tokens)

		log.db_set(
			{
				"status": "Completed",
				"output_text": response.content,
				"input_tokens": response.input_tokens,
				"output_tokens": response.output_tokens,
				"cost": cost,
				"latency_ms": latency_ms,
				"model": response.model,
			}
		)
		frappe.db.commit()

		frappe.publish_realtime(
			"ai_call_complete",
			{"log": log_name, "status": "Completed"},
			user=log.user,
		)

	except Exception as e:
		latency_ms = int((time.time() - start_time) * 1000)
		log.db_set(
			{
				"status": "Failed",
				"error_message": f"{e!s}\n\n{traceback.format_exc()}",
				"latency_ms": latency_ms,
			}
		)
		frappe.db.commit()

		frappe.publish_realtime(
			"ai_call_failed",
			{"log": log_name, "status": "Failed", "error": str(e)},
			user=log.user,
		)


def _resolve_provider_model(
	provider_name: str | None,
	model: str | None,
	template_name: str | None,
	settings,
) -> tuple:
	"""Resolve provider and model from caller override → template override → settings default."""
	resolved_provider = provider_name
	resolved_model = model

	if template_name:
		tmpl = frappe.get_doc("AI Prompt Template", template_name)
		if not resolved_provider and tmpl.provider_override:
			resolved_provider = tmpl.provider_override
		if not resolved_model and tmpl.model_override:
			resolved_model = tmpl.model_override

	if not resolved_provider:
		resolved_provider = settings.default_provider
	if not resolved_model:
		resolved_model = settings.default_model

	if not resolved_provider:
		frappe.throw(_("No AI provider configured. Set a default in AI Settings."))

	provider_doc = frappe.get_doc("AI Provider", resolved_provider)
	if not provider_doc.enabled:
		frappe.throw(_("AI Provider '{0}' is disabled.").format(resolved_provider))

	return resolved_provider, resolved_model, provider_doc


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
	log = frappe.new_doc("AI Call Log")
	log.update(kwargs)
	if settings.enable_logging:
		log.insert(ignore_permissions=True)
		frappe.db.commit()
	else:
		log.name = frappe.generate_hash(length=10)
	return log


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

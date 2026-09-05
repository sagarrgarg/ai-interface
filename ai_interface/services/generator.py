from ai_interface.services.ai_client import call_ai


def generate(
	prompt: str | None = None,
	template: str | None = None,
	context: dict | None = None,
	*,
	provider: str | None = None,
	model: str | None = None,
	calling_app: str = "",
	sync: bool = False,
	max_tokens: int | None = None,
	temperature: float = 0.7,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	module: str | None = None,
	action: str | None = None,
	needs: list[str] | None = None,
	**kwargs,
) -> str:
	"""General-purpose text generation.

	Use `prompt` for raw text, or `template` + `context` to resolve an AI Prompt Template.
	Returns AI Call Log name (async) or response text (sync).

	Pass `reference_doctype` / `reference_name` / `action` so the call is
	attributable in the AI Command Center dashboard.
	"""
	if not prompt and not template:
		import frappe

		frappe.throw("Either prompt or template must be provided.")

	return call_ai(
		prompt=prompt or "",
		template=template,
		context=context,
		provider=provider,
		model=model,
		calling_app=calling_app,
		function_type="Generation",
		sync=sync,
		max_tokens=max_tokens,
		temperature=temperature,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		module=module,
		action=action,
		needs=needs,
		**kwargs,
	)

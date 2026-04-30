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
	**kwargs,
) -> str:
	"""General-purpose text generation.

	Use `prompt` for raw text, or `template` + `context` to resolve an AI Prompt Template.
	Returns AI Call Log name (async) or response text (sync).
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
		**kwargs,
	)

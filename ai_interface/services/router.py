"""Capability routing.

Callers say what a call *needs* — vision, tools, a context window big enough for
the prompt — and the router picks a provider and model that can do it. Naming a
model in application code is what makes a provider un-swappable: point AI
Settings at a different vendor and every caller that hardcoded a Claude model id
starts failing. So the model name is resolved here, from configuration, at call
time.

Resolution order, most specific first:
    caller override -> prompt template override -> routing rules -> AI Settings

Everything that survives is then filtered by capability, and what comes out is a
*chain*: the first entry serves the call, the rest are fallbacks for retryable
failures.
"""

import frappe
from frappe import _
from frappe.utils import flt

# Roughly four characters to a token. Only used to catch prompts that obviously
# overflow a model's context, so precision would buy nothing here.
CHARS_PER_TOKEN = 4

# Failures worth trying another provider for. Auth and configuration errors are
# deliberately absent: retrying a bad key just spends money to fail again.
RETRYABLE = {"Rate Limit", "Timeout", "Provider Error"}


class RoutingError(frappe.ValidationError):
	pass


def build_chain(
	settings,
	*,
	provider: str | None,
	model: str | None,
	template: str | None,
	function_type: str,
	calling_app: str,
	needs: list[str] | None,
	prompt: str,
) -> list[dict]:
	"""Ordered list of {provider, model} to try, best first.

	Raises before anything is queued if nothing can serve the call — a clear
	error now beats a confusing provider rejection inside a background job.
	"""
	requirements = _requirements(needs, prompt)
	candidates = _candidate_providers(settings, provider, model, template, function_type, calling_app)

	if not candidates:
		frappe.throw(
			_("No AI provider configured. Add a routing rule or set a default in AI Settings."),
			exc=RoutingError,
		)

	chain, rejections = [], []
	for cand in candidates:
		try:
			resolved = _resolve_model(cand["provider"], cand.get("model"), requirements)
		except RoutingError as e:
			rejections.append(str(e))
			continue
		if not any(c["provider"] == resolved["provider"] and c["model"] == resolved["model"] for c in chain):
			chain.append(resolved)

	if not chain:
		frappe.throw(
			_("No model can serve this call.<br><br>{0}").format("<br>".join(rejections)),
			exc=RoutingError,
		)

	if not settings.get("enable_fallback", 1):
		return chain[:1]
	return chain


def _requirements(needs: list[str] | None, prompt: str) -> dict:
	req = {"vision": False, "tools": False, "min_context": 0}
	for need in needs or []:
		key = str(need).strip().lower()
		if key in ("vision", "image", "images"):
			req["vision"] = True
		elif key in ("tools", "tool", "function_calling"):
			req["tools"] = True
	req["min_context"] = int(len(prompt or "") / CHARS_PER_TOKEN)
	return req


def _candidate_providers(settings, provider, model, template, function_type, calling_app) -> list[dict]:
	"""Caller and template overrides win outright; rules and defaults follow."""
	if provider:
		return [{"provider": provider, "model": model}]

	if template:
		tmpl = frappe.get_cached_doc("AI Prompt Template", template)
		if tmpl.provider_override:
			return [{"provider": tmpl.provider_override, "model": model or tmpl.model_override}]
		if tmpl.model_override and not model:
			model = tmpl.model_override

	out = [
		{"provider": r.provider, "model": r.model}
		for r in _matching_rules(settings, function_type, calling_app)
	]

	if settings.default_provider:
		out.append({"provider": settings.default_provider, "model": model or settings.default_model})

	if model:
		# A caller-named model still applies to whatever provider is chosen.
		for entry in out:
			entry["model"] = entry["model"] or model

	return out


def _matching_rules(settings, function_type: str, calling_app: str) -> list:
	"""Rules that apply here, most specific first, then by priority.

	Specificity beats priority so a rule written for one app is never shadowed
	by a lower-priority catch-all.
	"""
	matches = []
	for rule in settings.get("routing_rules") or []:
		if not rule.enabled:
			continue
		if rule.function_type and rule.function_type != function_type:
			continue
		if rule.calling_app and rule.calling_app != calling_app:
			continue
		specificity = (1 if rule.calling_app else 0) + (1 if rule.function_type else 0)
		matches.append((-specificity, flt(rule.priority), rule.idx, rule))

	return [m[-1] for m in sorted(matches, key=lambda m: m[:3])]


def _resolve_model(provider_name: str, model: str | None, req: dict) -> dict:
	provider_doc = frappe.get_cached_doc("AI Provider", provider_name)
	if not provider_doc.enabled:
		raise RoutingError(_("Provider '{0}' is disabled.").format(provider_name))

	rows = [m for m in provider_doc.models if m.model_id]

	if model:
		match = next((m for m in rows if m.model_id == model), None)
		if not match:
			# Unlisted models are allowed through: the catalog can lag behind a
			# vendor release, and refusing a model the provider would accept is
			# worse than trusting an explicit instruction.
			return {"provider": provider_name, "model": model}
		_assert_capable(provider_name, match, req)
		return {"provider": provider_name, "model": match.model_id}

	capable = [m for m in rows if m.enabled and _capable(m, req)]
	if not capable:
		raise RoutingError(_("{0} has no enabled model that {1}.").format(provider_name, _describe(req)))

	capable.sort(key=lambda m: (flt(m.priority), flt(m.cost_per_input_token) + flt(m.cost_per_output_token)))
	return {"provider": provider_name, "model": capable[0].model_id}


def _capable(row, req: dict) -> bool:
	if req["vision"] and not row.supports_vision:
		return False
	if req["tools"] and not row.supports_tools:
		return False
	if req["min_context"] and row.context_window and row.context_window < req["min_context"]:
		return False
	return True


def _assert_capable(provider_name: str, row, req: dict):
	if req["vision"] and not row.supports_vision:
		raise RoutingError(
			_("Model '{0}' on {1} does not support images.").format(row.model_id, provider_name)
		)
	if req["tools"] and not row.supports_tools:
		raise RoutingError(
			_("Model '{0}' on {1} does not support tools.").format(row.model_id, provider_name)
		)
	if req["min_context"] and row.context_window and row.context_window < req["min_context"]:
		raise RoutingError(
			_("Prompt needs about {0} tokens but '{1}' holds {2}.").format(
				req["min_context"], row.model_id, row.context_window
			)
		)


def _describe(req: dict) -> str:
	parts = []
	if req["vision"]:
		parts.append(_("supports images"))
	if req["tools"]:
		parts.append(_("supports tools"))
	if req["min_context"]:
		parts.append(_("holds {0} tokens").format(req["min_context"]))
	return ", ".join(parts) or _("is enabled")

from ai_interface.providers.base import BaseProvider

PROVIDER_REGISTRY: dict[str, type[BaseProvider]] = {}


def register_provider(provider_type: str, provider_class: type[BaseProvider]):
	PROVIDER_REGISTRY[provider_type] = provider_class


def get_provider(provider_type: str) -> BaseProvider:
	if provider_type not in PROVIDER_REGISTRY:
		import frappe

		frappe.throw(f"Unknown provider type: {provider_type}")
	return PROVIDER_REGISTRY[provider_type]()


from ai_interface.providers.anthropic_provider import AnthropicProvider
from ai_interface.providers.claude_code_provider import ClaudeCodeProvider

register_provider("Anthropic", AnthropicProvider)
register_provider("Claude Code", ClaudeCodeProvider)

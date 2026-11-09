from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ProviderResponse:
	content: str
	input_tokens: int
	output_tokens: int
	model: str
	raw_response: dict = field(default_factory=dict)
	# Filled by complete(); plain chat()/vision() leave the defaults.
	stop_reason: str = ""
	tool_calls: list = field(default_factory=list)  # [ToolCall]
	cache_read_tokens: int = 0
	cache_write_tokens: int = 0
	# The assistant turn in the adapter's own wire format, appended back unchanged in a tool loop.
	assistant_message: dict | None = None


@dataclass
class ToolCall:
	id: str
	name: str
	input: dict


@dataclass
class CompletionRequest:
	"""One full request, the same for every vendor.

	system:  list of {"text": str, "cache": bool} blocks, stable content first.
	messages: user/assistant turns. Content is a string, or adapter-native content
	          when it came back from that same adapter (assistant_message, tool results).
	output_schema: JSON Schema the answer must match (the caller validates it).
	tools:   client tools {"name", "description", "input_schema"} and server tools
	         {"type": ...} passed through untouched.
	effort:  "low" | "medium" | "high" | "xhigh" | "max", where the vendor has it.
	cache_ttl: "5m" or "1h" for cached blocks.
	"""

	messages: list
	model: str
	max_tokens: int
	system: list = field(default_factory=list)
	output_schema: dict | None = None
	tools: list = field(default_factory=list)
	effort: str | None = None
	cache_ttl: str = "5m"


class ProviderRefusal(Exception):
	"""The model declined the request (a safety refusal). Not retried on another provider."""

	status_code = None


class ProviderHTTPError(Exception):
	"""Raised by HTTP adapters so failures carry a status code.

	Classifying an error by sniffing the exception text is guesswork that breaks
	the moment a vendor rewords a message. The status code is the vendor-neutral
	fact, so adapters raise this and let the caller map it.
	"""

	def __init__(self, message: str, status_code: int | None = None, body: str = ""):
		super().__init__(message)
		self.status_code = status_code
		self.body = body


class BaseProvider(ABC):
	"""Base for every adapter.

	`config` is populated by `get_provider()` from the AI Provider Type record —
	base URL, paths, auth header, wire quirks. Adapters that talk a fixed vendor
	protocol may ignore it entirely.
	"""

	config: dict

	def __init__(self):
		self.config = {}

	@abstractmethod
	def chat(
		self,
		messages: list[dict],
		model: str,
		max_tokens: int,
		temperature: float = 0.7,
		credential: str = "",
		auth_type: str = "API Key",
		api_base_url: str = "",
		timeout: int = 120,
	) -> ProviderResponse: ...

	@abstractmethod
	def vision(
		self,
		messages: list[dict],
		images: list[bytes],
		model: str,
		max_tokens: int,
		credential: str = "",
		auth_type: str = "API Key",
		api_base_url: str = "",
		timeout: int = 120,
	) -> ProviderResponse: ...

	@abstractmethod
	def fetch_models(
		self,
		credential: str = "",
		auth_type: str = "API Key",
		api_base_url: str = "",
	) -> list[dict]: ...

	# ---- full requests (system, messages, schema, tools, caching, effort)

	#: What complete() can do: "system", "json", "tools", "server_tools", "cache", "effort".
	#: The chat service skips a provider that lacks a feature the request needs.
	FEATURES: frozenset = frozenset()

	def supports(self, features) -> bool:
		return set(features or ()) <= set(self.FEATURES)

	def complete(
		self,
		request: "CompletionRequest",
		credential: str = "",
		auth_type: str = "API Key",
		api_base_url: str = "",
		timeout: int = 120,
	) -> ProviderResponse:
		"""One turn of a full request. Adapters that support it override this."""
		raise ProviderHTTPError(f"{type(self).__name__} does not support full requests (not configured)")

	def tool_results_message(self, results: list[dict]) -> dict:
		"""The message carrying tool results back, in this adapter's wire format.

		results: [{"id", "content": str, "is_error": bool}]
		"""
		raise ProviderHTTPError(f"{type(self).__name__} does not support tools (not configured)")

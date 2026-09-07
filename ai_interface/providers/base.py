from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ProviderResponse:
	content: str
	input_tokens: int
	output_tokens: int
	model: str
	raw_response: dict = field(default_factory=dict)


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

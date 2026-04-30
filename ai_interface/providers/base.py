from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ProviderResponse:
	content: str
	input_tokens: int
	output_tokens: int
	model: str
	raw_response: dict = field(default_factory=dict)


class BaseProvider(ABC):
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

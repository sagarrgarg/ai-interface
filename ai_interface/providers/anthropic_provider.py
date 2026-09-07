import base64

import anthropic

from ai_interface.providers.base import BaseProvider, ProviderResponse

CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


class AnthropicProvider(BaseProvider):
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
	) -> ProviderResponse:
		client = self._get_client(credential, auth_type, api_base_url, timeout)
		system_msg = None
		chat_messages = []
		for msg in messages:
			if msg["role"] == "system":
				system_msg = msg["content"]
			else:
				chat_messages.append(msg)

		system_msg = self._prepare_system(system_msg, auth_type)

		kwargs: dict = {
			"model": model,
			"max_tokens": max_tokens,
			"temperature": temperature,
			"messages": chat_messages,
		}
		if system_msg:
			kwargs["system"] = system_msg

		response = client.messages.create(**kwargs)
		return ProviderResponse(
			content=response.content[0].text,
			input_tokens=response.usage.input_tokens,
			output_tokens=response.usage.output_tokens,
			model=response.model,
			raw_response=response.model_dump(),
		)

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
	) -> ProviderResponse:
		client = self._get_client(credential, auth_type, api_base_url, timeout)

		image_content = []
		for img_bytes in images:
			media_type = self._detect_media_type(img_bytes)
			image_content.append(
				{
					"type": "image",
					"source": {
						"type": "base64",
						"media_type": media_type,
						"data": base64.b64encode(img_bytes).decode(),
					},
				}
			)

		system_msg = None
		user_text = ""
		for msg in messages:
			if msg["role"] == "system":
				system_msg = msg["content"]
			elif msg["role"] == "user":
				user_text = msg["content"]

		vision_messages = [
			{
				"role": "user",
				"content": [*image_content, {"type": "text", "text": user_text}],
			}
		]

		system_msg = self._prepare_system(system_msg, auth_type)

		kwargs: dict = {
			"model": model,
			"max_tokens": max_tokens,
			"messages": vision_messages,
		}
		if system_msg:
			kwargs["system"] = system_msg

		response = client.messages.create(**kwargs)
		return ProviderResponse(
			content=response.content[0].text,
			input_tokens=response.usage.input_tokens,
			output_tokens=response.usage.output_tokens,
			model=response.model,
			raw_response=response.model_dump(),
		)

	def fetch_models(
		self,
		credential: str = "",
		auth_type: str = "API Key",
		api_base_url: str = "",
	) -> list[dict]:
		"""Discover model ids from the Anthropic API.

		Ids and labels only — the API carries no pricing or context window, so
		those are left to the catalog prefill and the admin rather than invented
		here. An OAuth credential may not be allowed to list models; an empty list
		means "add them by hand", not "this vendor has none".
		"""
		client = self._get_client(credential, auth_type, api_base_url, timeout=30)
		response = client.models.list(limit=100)
		return [
			{"model_id": model.id, "label": getattr(model, "display_name", model.id)}
			for model in response.data
		]

	def _get_client(
		self, credential: str, auth_type: str, api_base_url: str, timeout: int
	) -> anthropic.Anthropic:
		if auth_type == "Auth Token":
			kwargs: dict = {
				"auth_token": credential,
				"timeout": timeout,
				"default_headers": {"anthropic-beta": "oauth-2025-04-20"},
			}
		else:
			kwargs: dict = {"api_key": credential, "timeout": timeout}
		if api_base_url:
			kwargs["base_url"] = api_base_url
		return anthropic.Anthropic(**kwargs)

	def _prepare_system(self, system_msg: str | None, auth_type: str) -> str | None:
		"""OAuth tokens require the Claude Code identifier as the system prompt prefix."""
		if auth_type != "Auth Token":
			return system_msg
		if not system_msg:
			return CLAUDE_CODE_SYSTEM_PREFIX
		if system_msg.startswith(CLAUDE_CODE_SYSTEM_PREFIX):
			return system_msg
		return f"{CLAUDE_CODE_SYSTEM_PREFIX}\n\n{system_msg}"

	def _detect_media_type(self, img_bytes: bytes) -> str:
		if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
			return "image/png"
		if img_bytes[:2] == b"\xff\xd8":
			return "image/jpeg"
		if img_bytes[:4] == b"GIF8":
			return "image/gif"
		if img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
			return "image/webp"
		return "image/jpeg"

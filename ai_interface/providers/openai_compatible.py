"""Adapter for any vendor speaking the OpenAI chat-completions wire format.

Covers OpenAI, Sarvam, Groq, Together, Mistral, DeepSeek, OpenRouter, Ollama and
vLLM without a line of vendor-specific code — everything that differs (base URL,
paths, auth header, rejected parameters) comes from the AI Provider Type record.

Built on `requests`, already a Frappe dependency, so onboarding a vendor never
means installing an SDK.
"""

import base64

import requests

from ai_interface.providers.base import BaseProvider, ProviderHTTPError, ProviderResponse

MEDIA_SIGNATURES = (
	(b"\x89PNG\r\n\x1a\n", "image/png"),
	(b"\xff\xd8", "image/jpeg"),
	(b"GIF8", "image/gif"),
	(b"%PDF", "application/pdf"),
)


class OpenAICompatibleProvider(BaseProvider):
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
		payload = {
			"model": model,
			"messages": messages,
			"max_tokens": max_tokens,
			"temperature": temperature,
		}
		data = self._post(self._chat_url(api_base_url), payload, credential, timeout)
		return self._to_response(data, model)

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
		system_msg = None
		user_text = ""
		for msg in messages:
			if msg["role"] == "system":
				system_msg = msg["content"]
			elif msg["role"] == "user":
				user_text = msg["content"]

		content: list[dict] = []
		for img_bytes in images:
			media_type = self._detect_media_type(img_bytes)
			encoded = base64.b64encode(img_bytes).decode()
			content.append(
				{
					"type": "image_url",
					"image_url": {"url": f"data:{media_type};base64,{encoded}"},
				}
			)
		content.append({"type": "text", "text": user_text})

		vision_messages: list[dict] = []
		if system_msg:
			vision_messages.append({"role": "system", "content": system_msg})
		vision_messages.append({"role": "user", "content": content})

		payload = {
			"model": model,
			"messages": vision_messages,
			"max_tokens": max_tokens,
		}
		data = self._post(self._chat_url(api_base_url), payload, credential, timeout)
		return self._to_response(data, model)

	def fetch_models(
		self,
		credential: str = "",
		auth_type: str = "API Key",
		api_base_url: str = "",
	) -> list[dict]:
		"""Discover model ids from the vendor.

		Returns ids and labels only. No vendor exposes pricing or context windows
		here, so those are left for the catalog prefill and the admin — never
		invented.
		"""
		models_path = self.config.get("models_path") or ""
		if not models_path:
			return []

		base = (api_base_url or self.config.get("base_url") or "").rstrip("/")
		url = f"{base}{models_path}"

		response = requests.get(url, headers=self._headers(credential), timeout=30)
		self._raise_for_status(response)

		payload = response.json()
		entries = payload.get("data") if isinstance(payload, dict) else payload
		if not isinstance(entries, list):
			raise ProviderHTTPError(f"Unexpected response from {url}: no model list found")

		models = []
		for entry in entries:
			if isinstance(entry, str):
				model_id, label = entry, entry
			elif isinstance(entry, dict):
				model_id = entry.get("id") or entry.get("model") or entry.get("name")
				label = entry.get("display_name") or entry.get("name") or model_id
			else:
				continue
			if model_id:
				models.append({"model_id": model_id, "label": label})

		return models

	def _chat_url(self, api_base_url: str) -> str:
		base = (api_base_url or self.config.get("base_url") or "").rstrip("/")
		if not base:
			raise ProviderHTTPError("No base URL configured for this provider.")
		path = self.config.get("chat_path") or "/v1/chat/completions"
		return f"{base}{path}"

	def _headers(self, credential: str) -> dict:
		header = self.config.get("auth_header") or "Authorization"
		prefix = self.config.get("auth_prefix") or ""
		return {
			header: f"{prefix}{credential}",
			"Content-Type": "application/json",
			"Accept": "application/json",
		}

	def _post(self, url: str, payload: dict, credential: str, timeout: int) -> dict:
		for key in self.config.get("unsupported_params") or []:
			payload.pop(key, None)

		response = requests.post(url, json=payload, headers=self._headers(credential), timeout=timeout)
		self._raise_for_status(response)

		try:
			return response.json()
		except ValueError as e:
			raise ProviderHTTPError(
				f"Could not decode JSON from {url}", response.status_code, response.text[:500]
			) from e

	def _raise_for_status(self, response):
		if response.status_code < 400:
			return
		detail = response.text[:500]
		raise ProviderHTTPError(
			f"API error {response.status_code} from {response.url}: {detail}",
			response.status_code,
			detail,
		)

	def _to_response(self, data: dict, requested_model: str) -> ProviderResponse:
		choices = data.get("choices") or []
		if not choices:
			raise ProviderHTTPError(f"Response contained no choices: {str(data)[:500]}")

		message = choices[0].get("message") or {}
		content = message.get("content")

		# Reasoning models may return content as a list of typed parts.
		if isinstance(content, list):
			content = "".join(p.get("text", "") for p in content if isinstance(p, dict))

		usage = data.get("usage") or {}
		return ProviderResponse(
			content=content or "",
			input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
			output_tokens=usage.get("completion_tokens") or usage.get("output_tokens") or 0,
			model=data.get("model") or requested_model,
			raw_response=data,
		)

	def _detect_media_type(self, img_bytes: bytes) -> str:
		for signature, media_type in MEDIA_SIGNATURES:
			if img_bytes[: len(signature)] == signature:
				return media_type
		if img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
			return "image/webp"
		return "image/jpeg"

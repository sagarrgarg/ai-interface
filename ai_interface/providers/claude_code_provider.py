import json
import subprocess

from ai_interface.providers.base import BaseProvider, ProviderResponse


class ClaudeCodeProvider(BaseProvider):
	"""Provider that routes through Claude Code CLI (claude -p).
	Uses the CLI's own auth (OAuth tokens via setup-token).
	"""

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
		prompt = self._messages_to_prompt(messages)
		return self._run_claude(prompt, model, timeout)

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
		import os
		import tempfile

		image_paths = []
		for img_bytes in images:
			ext = self._detect_ext(img_bytes)
			tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
			tmp.write(img_bytes)
			tmp.close()
			image_paths.append(tmp.name)

		try:
			user_prompt = self._messages_to_prompt(messages)
			file_refs = "\n".join(f"File: {p}" for p in image_paths)
			prompt = f"Read and analyze these files:\n{file_refs}\n\n{user_prompt}"
			return self._run_claude(prompt, model, timeout, allow_read=True)
		finally:
			for path in image_paths:
				try:
					os.unlink(path)
				except OSError:
					pass

	def vision_from_paths(
		self,
		messages: list[dict],
		file_paths: list[str],
		model: str,
		max_tokens: int,
		timeout: int = 120,
	) -> ProviderResponse:
		"""Read files directly by path — no bytes conversion needed."""
		user_prompt = self._messages_to_prompt(messages)
		file_refs = "\n".join(f"File: {p}" for p in file_paths)
		prompt = f"Read and analyze these files:\n{file_refs}\n\n{user_prompt}"
		return self._run_claude(prompt, model, timeout, allow_read=True)

	def fetch_models(
		self,
		credential: str = "",
		auth_type: str = "API Key",
		api_base_url: str = "",
	) -> list[dict]:
		"""The CLI exposes no model-listing endpoint.

		Which models a Claude Code install can reach depends on the subscription
		behind its OAuth token, so guessing here would be fiction. Models are
		entered by hand on the AI Provider.
		"""
		return []

	def _run_claude(
		self,
		prompt: str,
		model: str,
		timeout: int,
		allow_read: bool = False,
	) -> ProviderResponse:
		cmd = [
			"claude",
			"-p",
			prompt,
			"--model",
			model,
			"--output-format",
			"json",
		]
		if allow_read:
			cmd.extend(["--allowedTools", "Read"])

		result = subprocess.run(
			cmd,
			capture_output=True,
			text=True,
			timeout=timeout,
		)

		if result.returncode != 0:
			raise RuntimeError(f"Claude CLI failed: {result.stderr}")

		data = json.loads(result.stdout)

		if data.get("is_error"):
			raise RuntimeError(f"Claude CLI error: {data.get('result', 'Unknown error')}")

		usage = data.get("usage", {})
		return ProviderResponse(
			content=data.get("result", ""),
			input_tokens=usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
			output_tokens=usage.get("output_tokens", 0),
			model=model,
			raw_response=data,
		)

	def _messages_to_prompt(self, messages: list[dict]) -> str:
		parts = []
		for msg in messages:
			if msg["role"] == "system":
				parts.insert(0, f"[System: {msg['content']}]\n")
			elif msg["role"] == "user":
				parts.append(msg["content"])
		return "\n".join(parts)

	def _detect_ext(self, img_bytes: bytes) -> str:
		if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
			return ".png"
		if img_bytes[:2] == b"\xff\xd8":
			return ".jpg"
		if img_bytes[:4] == b"%PDF":
			return ".pdf"
		return ".png"

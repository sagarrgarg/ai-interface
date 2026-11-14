import json
import os
import shutil
import stat
import subprocess
import tempfile

from ai_interface.providers.base import BaseProvider, CompletionRequest, ProviderHTTPError, ProviderResponse

# Run like Harvey does: no tools, no MCP servers, no user or project settings, no slash
# commands, no saved session. A text-only call should never be able to touch the machine.
ISOLATION_FLAGS = [
	"--tools",
	"",
	"--strict-mcp-config",
	"--safe-mode",
	"--setting-sources",
	"",
	"--disable-slash-commands",
	"--no-session-persistence",
]
_isolation_ok: bool | None = None


def claude_bin() -> str | None:
	"""The Claude CLI to run: AI_INTERFACE_CLAUDE_BIN when set, else a `claude` on the PATH (one a person
	installed and signed in), else the copy bundled in the claude-agent-sdk package, which this app
	depends on, so it is there on any host pip installs the app on, Frappe Cloud included."""
	explicit = os.environ.get("AI_INTERFACE_CLAUDE_BIN")
	if explicit:
		return explicit if os.path.isfile(explicit) else None
	found = shutil.which("claude")
	if found:
		return found
	try:
		import importlib.util

		spec = importlib.util.find_spec("claude_agent_sdk")
	except (ImportError, ValueError):
		spec = None
	if not spec or not spec.origin:
		return None
	bundled = os.path.join(os.path.dirname(spec.origin), "_bundled", "claude")
	if not os.path.isfile(bundled):
		return None
	if not os.access(bundled, os.X_OK):
		try:  # some unpackers drop the executable bit
			os.chmod(bundled, os.stat(bundled).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
		except OSError:
			return None
	return bundled


def _claude() -> str:
	path = claude_bin()
	if not path:
		raise ProviderHTTPError(
			"The Claude CLI was not found: install claude-agent-sdk (an ai_interface dependency) or the claude CLI"
		)
	return path


def _cli_env(credential: str = "") -> dict:
	"""A clean environment for the CLI. A subscription token saved on the provider (from `claude
	setup-token`) signs it in where nobody can log in by hand, such as Frappe Cloud."""
	env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
	if credential:
		env["CLAUDE_CODE_OAUTH_TOKEN"] = credential
	return env


def _cli_supports_isolation() -> bool:
	"""Every isolation flag must be known to the installed CLI (checked once per process)."""
	global _isolation_ok
	if _isolation_ok is None:
		try:
			help_text = subprocess.run(
				[_claude(), "--help"], capture_output=True, text=True, timeout=30
			).stdout
			_isolation_ok = all(f in help_text for f in ISOLATION_FLAGS if f.startswith("--"))
		except Exception:
			_isolation_ok = False
	return _isolation_ok


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
		return self._run_claude(prompt, model, timeout, credential=credential)

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
			return self._run_claude(prompt, model, timeout, allow_read=True, credential=credential)
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

	FEATURES = frozenset({"system", "json"})

	def complete(
		self,
		request: CompletionRequest,
		credential: str = "",
		auth_type: str = "API Key",
		api_base_url: str = "",
		timeout: int = 120,
	) -> ProviderResponse:
		"""A full request as one prompt: the CLI takes text only. JSON is asked for in words
		and validated by the caller."""
		parts = []
		system = "\n\n".join(b["text"] for b in request.system if b.get("text"))
		if system:
			parts.append(f"[System instructions]\n{system}")
		for m in request.messages:
			content = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], default=str)
			parts.append(f"[{m['role']}]\n{content}")
		if request.output_schema:
			parts.append(
				"Reply with only a JSON object (no prose, no code fence) matching this JSON Schema:\n"
				+ json.dumps(request.output_schema)
			)
		if not _cli_supports_isolation():
			# Never fall back to a run with the CLI's default tools: a prompt carrying untrusted
			# text could make it read files on this server.
			raise ProviderHTTPError(
				"The installed Claude CLI cannot run isolated (missing flags); full requests are not configured"
			)
		response = self._run_claude("\n\n".join(parts), request.model, timeout, credential=credential)
		response.stop_reason = "end_turn"
		return response

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
		credential: str = "",
	) -> ProviderResponse:
		claude = _claude()
		if not allow_read and _cli_supports_isolation():
			# Prompt on stdin, from an empty folder, with a clean environment.
			cmd = [claude, "-p", "--model", model, "--output-format", "json", *ISOLATION_FLAGS]
			env = _cli_env(credential)
			env["CLAUDE_CODE_SAFE_MODE"] = "1"
			with tempfile.TemporaryDirectory(prefix="ai_interface_") as workdir:
				result = subprocess.run(
					cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=workdir, env=env
				)
		else:
			cmd = [claude, "-p", prompt, "--model", model, "--output-format", "json"]
			if allow_read:
				cmd.extend(["--allowedTools", "Read"])
			result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_cli_env(credential))

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

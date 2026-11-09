"""Full requests (services/chat.run) and the adapters' complete().

A scripted adapter is registered as a real AI Provider Type, so routing, fallback, budgets,
logging and cost all run for real; only the vendor is fake. The Anthropic, OpenAI-format and
Claude CLI adapters are then checked against mocked clients. Nothing touches the network.
"""

import json
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.providers.anthropic_provider import AnthropicProvider
from ai_interface.providers.base import (
	BaseProvider,
	CompletionRequest,
	ProviderHTTPError,
	ProviderRefusal,
	ProviderResponse,
	ToolCall,
)
from ai_interface.providers.openai_compatible import OpenAICompatibleProvider
from ai_interface.services import chat
from ai_interface.tests.utils import PREFIX, cleanup, make_provider, make_provider_type

APP = f"{PREFIX}chat"
TYPE_FAKE = f"{PREFIX} Scripted"
TYPE_PLAIN = f"{PREFIX} Plain"
P_MAIN = f"{PREFIX} Main"
P_BACKUP = f"{PREFIX} Backup"
P_PLAIN = f"{PREFIX} Plain Provider"

SCHEMA = {
	"type": "object",
	"properties": {"intent": {"type": "string"}, "reply": {"type": "string"}},
	"required": ["intent", "reply"],
	"additionalProperties": False,
}


class ScriptedAdapter(BaseProvider):
	"""Answers from a script: a list of responses or exceptions, per provider name."""

	FEATURES = frozenset({"system", "json", "tools", "server_tools", "cache", "effort"})
	script: ClassVar[dict] = {}
	seen: ClassVar[list] = []

	def chat(self, *a, **k):
		raise NotImplementedError

	def vision(self, *a, **k):
		raise NotImplementedError

	def fetch_models(self, *a, **k):
		return []

	def complete(self, request, credential="", auth_type="API Key", api_base_url="", timeout=120):
		key = api_base_url or "main"
		ScriptedAdapter.seen.append((key, json.loads(json.dumps(request.messages, default=str)), request))
		item = ScriptedAdapter.script[key].pop(0)
		if isinstance(item, Exception):
			raise item
		return item

	def tool_results_message(self, results):
		return {
			"role": "user",
			"content": [
				{"tool_result": r["id"], "content": r["content"], "is_error": r["is_error"]} for r in results
			],
		}


class PlainAdapter(ScriptedAdapter):
	FEATURES = frozenset({"system", "json"})  # no tools


def answer(text="", calls=None, stop="end_turn", cache_read=0, cache_write=0):
	return ProviderResponse(
		content=text,
		input_tokens=1000,
		output_tokens=100,
		model="m-1",
		stop_reason=stop,
		tool_calls=calls or [],
		cache_read_tokens=cache_read,
		cache_write_tokens=cache_write,
		assistant_message={"role": "assistant", "content": text or "(tool call)"},
	)


class TestChatRun(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup()
		make_provider_type(TYPE_FAKE, adapter_path="ai_interface.tests.test_chat.ScriptedAdapter")
		make_provider_type(TYPE_PLAIN, adapter_path="ai_interface.tests.test_chat.PlainAdapter")
		model = {"model_id": "m-1", "cost_in": 0.000004, "cost_out": 0.00002, "supports_tools": 1}
		make_provider(P_MAIN, TYPE_FAKE, models=[model], api_base_url="main")
		make_provider(P_BACKUP, TYPE_FAKE, models=[model], api_base_url="backup")
		make_provider(P_PLAIN, TYPE_PLAIN, models=[model], api_base_url="main")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cleanup()
		for name in (TYPE_FAKE, TYPE_PLAIN):
			if frappe.db.exists("AI Provider Type", name):
				frappe.delete_doc("AI Provider Type", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		ScriptedAdapter.script = {"main": [], "backup": []}
		ScriptedAdapter.seen = []

	def go(self, **kw):
		kw.setdefault("provider", P_MAIN)
		kw.setdefault("model", "m-1")
		kw.setdefault("calling_app", APP)
		return chat.run(**kw)

	# ---- the basics

	def test_system_messages_and_a_validated_json_answer(self):
		ScriptedAdapter.script["main"] = [answer('{"intent": "question", "reply": "Hola"}', cache_read=8000)]
		r = self.go(
			system=[{"text": "You are Harish.", "cache": True}, "Rules."],
			prompt="Price for 1000 pcs?",
			output_schema=SCHEMA,
			effort="high",
			cache_ttl="1h",
			action="handle_reply",
		)
		self.assertEqual(r.data, {"intent": "question", "reply": "Hola"})
		req = ScriptedAdapter.seen[0][2]
		self.assertEqual(
			req.system, [{"text": "You are Harish.", "cache": True}, {"text": "Rules.", "cache": False}]
		)
		self.assertEqual((req.effort, req.cache_ttl, req.output_schema), ("high", "1h", SCHEMA))
		log = frappe.get_doc("AI Call Log", r.log)
		self.assertEqual(
			(log.status, log.action, log.stop_reason, log.cache_read_tokens),
			("Completed", "handle_reply", "end_turn", 8000),
		)
		# 1000 in x 4e-6 + 100 out x 2e-5 + 8000 cache reads at 10% of the input rate
		self.assertAlmostEqual(log.cost, 0.004 + 0.002 + 8000 * 0.0000004, places=7)
		self.assertAlmostEqual(r.usage.cost, log.cost, places=7)

	def test_a_wrong_answer_gets_one_repair_turn(self):
		ScriptedAdapter.script["main"] = [
			answer('{"intent": "question"}'),
			answer('```json\n{"intent": "q", "reply": "ok"}\n```'),
		]
		r = self.go(prompt="x", output_schema=SCHEMA)
		self.assertEqual(r.data["reply"], "ok")
		self.assertEqual(len(r.logs), 2)
		self.assertEqual(frappe.db.get_value("AI Call Log", r.logs[1], "parent_call"), r.log)
		self.assertIn("did not match the required JSON schema", ScriptedAdapter.seen[1][1][-1]["content"])

	def test_wrong_twice_fails(self):
		ScriptedAdapter.script["main"] = [answer("no json here"), answer("still none")]
		with self.assertRaises(chat.AIRequestFailed):
			self.go(prompt="x", output_schema=SCHEMA)

	def test_cut_off_at_max_tokens_is_a_failure(self):
		ScriptedAdapter.script["main"] = [answer('{"intent": "q"', stop="max_tokens")]
		with self.assertRaises(chat.AIRequestFailed):
			self.go(prompt="x", output_schema=SCHEMA)

	# ---- tools

	def test_tools_are_run_and_answered(self):
		seen_inputs = []
		price_tool = {
			"name": "price_for",
			"description": "Price for an item and quantity",
			"tier": "read",
			"input_schema": {"type": "object", "properties": {"item": {"type": "string"}}},
			"run": lambda inp, ctx: seen_inputs.append((inp, ctx.reference_name)) or {"usd": 0.36},
		}
		ScriptedAdapter.script["main"] = [
			answer(
				calls=[ToolCall("c1", "price_for", {"item": "Pulse"}), ToolCall("c2", "nope", {})],
				stop="tool_use",
			),
			answer("Pulse is $0.36"),
		]
		r = self.go(
			prompt="price?", tools=[price_tool], reference_doctype="User", reference_name="Administrator"
		)
		self.assertEqual(r.text, "Pulse is $0.36")
		self.assertEqual(seen_inputs, [({"item": "Pulse"}, "Administrator")])
		results = ScriptedAdapter.seen[1][1][-1]["content"]
		self.assertEqual(results[0], {"tool_result": "c1", "content": '{"usd": 0.36}', "is_error": False})
		self.assertTrue(results[1]["is_error"], "an unknown tool is reported back as an error, not raised")
		self.assertEqual([t["name"] for t in r.tool_calls], ["price_for", "nope"])

	def test_a_failing_tool_is_reported_to_the_model(self):
		def boom(inp, ctx):
			raise ValueError("ERP is down")

		ScriptedAdapter.script["main"] = [
			answer(calls=[ToolCall("c1", "t", {})], stop="tool_use"),
			answer("sorry"),
		]
		self.go(prompt="x", tools=[{"name": "t", "tier": "read", "run": boom}])
		self.assertIn("ERP is down", ScriptedAdapter.seen[1][1][-1]["content"][0]["content"])

	def test_a_tool_loop_is_bounded(self):
		loop = [answer(calls=[ToolCall(f"c{i}", "t", {})], stop="tool_use") for i in range(5)]
		ScriptedAdapter.script["main"] = loop
		with self.assertRaises(chat.AIRequestFailed):
			self.go(prompt="x", tools=[{"name": "t", "tier": "read", "run": lambda i, c: "ok"}], max_steps=3)

	def test_tool_tiers_are_enforced(self):
		send = {"name": "send_mail", "tier": "communicate", "run": lambda i, c: "sent"}
		with self.assertRaises(frappe.ValidationError):
			self.go(prompt="x", tools=[send])
		ScriptedAdapter.script["main"] = [answer("ok")]
		self.go(prompt="x", tools=[send], allowed_tiers=("read", "communicate"))

	def test_tools_from_another_apps_hook(self):
		ScriptedAdapter.script["main"] = [
			answer(calls=[ToolCall("c1", "stock", {})], stop="tool_use"),
			answer("12 in stock"),
		]
		with patch.object(
			chat, "registered_tools", return_value={"erp.stock": "ai_interface.tests.test_chat.stock_tool"}
		):
			r = self.go(prompt="stock?", tools=["erp.stock"])
		self.assertEqual(r.text, "12 in stock")
		with self.assertRaises(frappe.ValidationError):
			self.go(prompt="x", tools=["nobody.registered.this"])

	def test_server_tools_pass_through(self):
		ScriptedAdapter.script["main"] = [answer("found it")]
		self.go(prompt="x", tools=[{"type": "web_search_20260209", "name": "web_search"}])
		self.assertEqual(
			ScriptedAdapter.seen[0][2].tools, [{"type": "web_search_20260209", "name": "web_search"}]
		)

	def test_a_paused_server_tool_turn_is_continued(self):
		ScriptedAdapter.script["main"] = [answer("searching", stop="pause_turn"), answer("done")]
		r = self.go(prompt="x", tools=[{"type": "web_search_20260209", "name": "web_search"}])
		self.assertEqual(r.text, "done")
		self.assertEqual(len(ScriptedAdapter.seen), 2)
		self.assertEqual(ScriptedAdapter.seen[1][1][-1], {"role": "assistant", "content": "searching"})

	# ---- routing, fallback, refusals

	def test_a_provider_that_cannot_do_tools_is_skipped(self):
		with self.assertRaises(frappe.ValidationError):
			self.go(
				provider=P_PLAIN, prompt="x", tools=[{"name": "t", "tier": "read", "run": lambda i, c: ""}]
			)

	def test_retryable_failure_moves_to_the_next_provider(self):
		ScriptedAdapter.script["main"] = [ProviderHTTPError("busy", 429)]
		ScriptedAdapter.script["backup"] = [answer("from backup")]
		with patch(
			"ai_interface.services.router.build_chain",
			return_value=[{"provider": P_MAIN, "model": "m-1"}, {"provider": P_BACKUP, "model": "m-1"}],
		):
			r = self.go(prompt="x")
		self.assertEqual((r.text, r.provider), ("from backup", P_BACKUP))
		self.assertEqual(frappe.db.get_value("AI Call Log", r.logs[0], "error_type"), "Rate Limit")

	def test_auth_failure_stops_the_chain(self):
		ScriptedAdapter.script["main"] = [ProviderHTTPError("bad key", 401)]
		ScriptedAdapter.script["backup"] = [answer("never")]
		with patch(
			"ai_interface.services.router.build_chain",
			return_value=[{"provider": P_MAIN, "model": "m-1"}, {"provider": P_BACKUP, "model": "m-1"}],
		):
			with self.assertRaises(chat.AIRequestFailed):
				self.go(prompt="x")
		self.assertEqual(ScriptedAdapter.script["backup"], [answer("never")])

	def test_refusal(self):
		ScriptedAdapter.script["main"] = [ProviderRefusal("declined (cyber)")]
		with self.assertRaises(chat.AIRefused):
			self.go(prompt="x")
		self.assertEqual(
			frappe.get_all(
				"AI Call Log", {"calling_app": APP}, ["error_type"], order_by="creation desc", limit=1
			)[0].error_type,
			"Refused",
		)

	def test_stored_prompt_is_masked_when_the_policy_says_so(self):
		from ai_interface.tests.utils import settings_for_test

		settings = settings_for_test(
			enable_logging=1, redact_contacts=1, max_output_tokens=4096, api_call_timeout=60
		)
		ScriptedAdapter.script["main"] = [answer("Reply to jane@shop.com on +61 7 3275 2588")]
		with patch("frappe.get_single", return_value=settings):
			r = chat.run(prompt="Customer jane@shop.com asks", provider=P_MAIN, model="m-1", calling_app=APP)
		self.assertIn(
			"jane@shop.com", ScriptedAdapter.seen[0][1][0]["content"], "the model gets the real text"
		)
		log = frappe.db.get_value("AI Call Log", r.log, ["input_text", "output_text"], as_dict=True)
		self.assertNotIn("jane@shop.com", log.input_text)
		self.assertEqual(log.output_text, "Reply to [email] on [phone]")

	def test_background_run_reports_back(self):
		queued = []
		with patch("frappe.enqueue", side_effect=lambda method, **kw: queued.append(kw)):
			job = chat.run_async(
				prompt="x",
				provider=P_MAIN,
				model="m-1",
				calling_app=APP,
				on_complete="ai_interface.tests.test_chat.remember",
				job_context={"task": "T-1"},
			)
		self.assertTrue(job.startswith("ai_run::"))
		with self.assertRaises(frappe.ValidationError):
			chat.run_async(prompt="x", tools=[{"name": "t", "run": print}])
		ScriptedAdapter.script["main"] = [answer("done")]
		kw = queued[0]
		chat._run_job(kw["run_kwargs"], kw["user"], kw["on_complete"], kw["job_context"], kw["run_job_id"])
		self.assertEqual(
			(CALLBACKS[-1]["result"]["text"], CALLBACKS[-1]["context"]), ("done", {"task": "T-1"})
		)
		ScriptedAdapter.script["main"] = [ProviderHTTPError("bad key", 401)]
		chat._run_job(kw["run_kwargs"], kw["user"], kw["on_complete"], kw["job_context"], kw["run_job_id"])
		self.assertIsNone(CALLBACKS[-1]["result"])
		self.assertIn("Auth", CALLBACKS[-1]["error"])


CALLBACKS = []


def remember(result=None, error=None, context=None):
	CALLBACKS.append({"result": result, "error": error, "context": context})


def stock_tool():
	return {"name": "stock", "description": "Stock on hand", "tier": "read", "run": lambda i, c: "12"}


# ---- adapters


def _block(**kw):
	return SimpleNamespace(**kw)


def _anthropic_response(content, stop="end_turn"):
	resp = MagicMock()
	resp.content = content
	resp.stop_reason = stop
	resp.model = "claude-opus-5-5"
	resp.usage = SimpleNamespace(
		input_tokens=10, output_tokens=5, cache_read_input_tokens=7, cache_creation_input_tokens=3
	)
	resp.model_dump.return_value = {}
	return resp


class TestAdapters(FrappeTestCase):
	def test_anthropic_reads_text_blocks_not_the_first_block(self):
		thinking = _block(type="thinking", thinking="", signature="sig")
		resp = _anthropic_response([thinking, _block(type="text", text="Hello")])
		client = MagicMock()
		client.messages.create.return_value = resp
		with patch.object(AnthropicProvider, "_get_client", return_value=client):
			out = AnthropicProvider().chat([{"role": "user", "content": "hi"}], "claude-opus-5-5", 100)
		self.assertEqual(out.content, "Hello")
		self.assertNotIn(
			"temperature", client.messages.create.call_args.kwargs, "current models reject temperature"
		)

	def test_anthropic_full_request(self):
		resp = _anthropic_response(
			[
				_block(type="text", text="calling"),
				_block(type="tool_use", id="t1", name="price_for", input={"item": "x"}),
			],
			stop="tool_use",
		)
		client = MagicMock()
		client.messages.create.return_value = resp
		req = CompletionRequest(
			messages=[{"role": "user", "content": "q"}],
			model="claude-opus-5-5",
			max_tokens=2000,
			system=[{"text": "persona", "cache": True}, {"text": "task", "cache": False}],
			output_schema=SCHEMA,
			effort="low",
			cache_ttl="1h",
			tools=[
				{"name": "price_for", "description": "d", "input_schema": {"type": "object"}, "strict": True},
				{"type": "web_fetch_20260209", "name": "web_fetch"},
			],
		)
		with patch.object(AnthropicProvider, "_get_client", return_value=client):
			out = AnthropicProvider().complete(req)
		kw = client.messages.create.call_args.kwargs
		self.assertEqual(
			kw["system"][0],
			{"type": "text", "text": "persona", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
		)
		self.assertNotIn("cache_control", kw["system"][1])
		self.assertEqual(
			kw["output_config"], {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
		)
		self.assertEqual(kw["tools"][0]["strict"], True)
		self.assertEqual(kw["tools"][1], {"type": "web_fetch_20260209", "name": "web_fetch"})
		self.assertEqual(out.tool_calls[0].name, "price_for")
		self.assertEqual((out.cache_read_tokens, out.cache_write_tokens, out.stop_reason), (7, 3, "tool_use"))
		self.assertIs(out.assistant_message["content"], resp.content, "blocks go back unchanged")
		msg = AnthropicProvider().tool_results_message(
			[{"id": "t1", "content": "1", "is_error": False}, {"id": "t2", "content": "x", "is_error": True}]
		)
		self.assertEqual(
			msg["content"][1], {"type": "tool_result", "tool_use_id": "t2", "content": "x", "is_error": True}
		)

	def test_anthropic_refusal(self):
		resp = _anthropic_response([], stop="refusal")
		resp.stop_details = SimpleNamespace(category="cyber")
		client = MagicMock()
		client.messages.create.return_value = resp
		with patch.object(AnthropicProvider, "_get_client", return_value=client):
			with self.assertRaises(ProviderRefusal):
				AnthropicProvider().complete(CompletionRequest(messages=[], model="m", max_tokens=10))

	def test_openai_format_full_request(self):
		adapter = OpenAICompatibleProvider()
		adapter.config = {"base_url": "https://x.invalid", "chat_path": "/v1/chat/completions"}
		reply = {
			"choices": [
				{
					"finish_reason": "tool_calls",
					"message": {
						"role": "assistant",
						"content": None,
						"tool_calls": [
							{
								"id": "c1",
								"type": "function",
								"function": {"name": "f", "arguments": '{"a": 1}'},
							}
						],
					},
				}
			],
			"usage": {
				"prompt_tokens": 5,
				"completion_tokens": 2,
				"prompt_tokens_details": {"cached_tokens": 4},
			},
		}
		with patch.object(OpenAICompatibleProvider, "_post", return_value=reply) as post:
			out = adapter.complete(
				CompletionRequest(
					messages=[{"role": "user", "content": "q"}],
					model="gpt",
					max_tokens=50,
					system=[{"text": "sys"}],
					output_schema=SCHEMA,
					tools=[
						{"name": "f", "input_schema": {"type": "object"}},
						{"type": "web_search_20260209"},
					],
				)
			)
		payload = post.call_args.args[1]
		self.assertEqual(payload["messages"][0], {"role": "system", "content": "sys"})
		self.assertEqual(payload["response_format"]["json_schema"]["schema"], SCHEMA)
		self.assertEqual(len(payload["tools"]), 1, "vendor server tools are not sent to other vendors")
		self.assertEqual(
			(out.stop_reason, out.tool_calls[0].input, out.cache_read_tokens), ("tool_use", {"a": 1}, 4)
		)
		self.assertEqual(out.input_tokens, 1, "cached tokens are not counted twice")
		self.assertEqual(
			adapter.tool_results_message([{"id": "c1", "content": "r"}]),
			[{"role": "tool", "tool_call_id": "c1", "content": "r"}],
		)

	def test_claude_cli_runs_isolated(self):
		from ai_interface.providers import claude_code_provider as cc

		done = SimpleNamespace(
			returncode=0, stdout=json.dumps({"result": '{"a": 1}', "usage": {}}), stderr=""
		)
		with (
			patch.object(cc, "_cli_supports_isolation", return_value=True),
			patch.object(cc.subprocess, "run", return_value=done) as run,
		):
			out = cc.ClaudeCodeProvider().complete(
				CompletionRequest(
					messages=[{"role": "user", "content": "q"}],
					model="sonnet",
					max_tokens=10,
					system=[{"text": "be brief"}],
					output_schema={"type": "object"},
				)
			)
		cmd = run.call_args.args[0]
		for flag in (
			"--tools",
			"--strict-mcp-config",
			"--safe-mode",
			"--setting-sources",
			"--disable-slash-commands",
			"--no-session-persistence",
		):
			self.assertIn(flag, cmd)
		self.assertNotIn("q", cmd, "the prompt goes on stdin, not the command line")
		self.assertIn("be brief", run.call_args.kwargs["input"])
		self.assertIn("JSON Schema", run.call_args.kwargs["input"])
		self.assertEqual(run.call_args.kwargs["env"]["CLAUDE_CODE_SAFE_MODE"], "1")
		self.assertEqual(out.content, '{"a": 1}')

	def test_claude_cli_refuses_to_run_unisolated(self):
		from ai_interface.providers import claude_code_provider as cc

		with (
			patch.object(cc, "_cli_supports_isolation", return_value=False),
			patch.object(cc.subprocess, "run") as run,
		):
			with self.assertRaises(ProviderHTTPError):
				cc.ClaudeCodeProvider().complete(
					CompletionRequest(
						messages=[{"role": "user", "content": "q"}], model="sonnet", max_tokens=10
					)
				)
		run.assert_not_called()

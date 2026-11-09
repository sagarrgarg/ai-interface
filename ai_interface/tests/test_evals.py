"""Evaluation sets: cases, checks, scores, the gate, and costs tagged to the run."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.services import chat, evals
from ai_interface.tests.test_chat import ScriptedAdapter, answer
from ai_interface.tests.utils import PREFIX, cleanup, make_provider, make_provider_type

SET = f"{PREFIX} classify replies"
SET_AI = f"{PREFIX} with model calls"
TYPE_EVAL = f"{PREFIX} Eval Scripted"
P_EVAL = f"{PREFIX} Eval Provider"


def classify(inp: dict) -> dict:
	"""A stand-in for an app's real classifier."""
	text = inp["text"].lower()
	if "price" in text:
		return {"intent": "question", "reply": "Our price list is attached, FOB India."}
	if "stop" in text:
		return {"intent": "unsubscribe", "reply": ""}
	return {"intent": "interested", "reply": "Thank you for your interest, we will send the catalogue."}


def classify_with_model(inp: dict) -> dict:
	return chat.run(
		prompt=inp["text"],
		provider=P_EVAL,
		model="m-1",
		calling_app=f"{PREFIX}evals",
		output_schema={"type": "object"},
	).data


_targets = patch.object(evals, "allowed_targets", return_value=["ai_interface.tests.test_evals.*"])


def setUpModule():
	_targets.start()


def tearDownModule():
	_targets.stop()


def make_set(name, target, cases, threshold=100):
	if frappe.db.exists("AI Eval Set", name):
		frappe.delete_doc("AI Eval Set", name, force=True, ignore_permissions=True)
	doc = frappe.get_doc(
		{
			"doctype": "AI Eval Set",
			"eval_name": name,
			"calling_app": f"{PREFIX}evals",
			"action": "classify_reply",
			"target_type": "Callable",
			"target": target,
			"pass_threshold": threshold,
		}
	)
	for c in cases:
		doc.append("cases", c)
	return doc.insert(ignore_permissions=True)


def cleanup_evals():
	for run in frappe.get_all("AI Eval Run", {"eval_set": ["like", PREFIX + "%"]}, pluck="name"):
		frappe.delete_doc("AI Eval Run", run, force=True, ignore_permissions=True)
	for s in frappe.get_all("AI Eval Set", {"name": ["like", PREFIX + "%"]}, pluck="name"):
		frappe.delete_doc("AI Eval Set", s, force=True, ignore_permissions=True)


class TestEvals(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup_evals()
		make_set(
			SET,
			"ai_interface.tests.test_evals.classify",
			[
				{
					"case_name": "price question",
					"input": '{"text": "What is your price for 3000 pcs?"}',
					"expected": '{"intent": "question"}',
					"checks": '[{"type": "contains", "path": "reply", "value": "fob"}, {"type": "max_words", "path": "reply", "value": 20},'
					' {"type": "not_contains", "path": "reply", "value": "EN71"}]',
				},
				{
					"case_name": "opt out",
					"input": '{"text": "STOP emailing me"}',
					"checks": '[{"type": "one_of", "path": "intent", "value": ["unsubscribe", "not_interested"]}]',
				},
				{
					"case_name": "wrong on purpose",
					"input": '{"text": "send me samples"}',
					"checks": '[{"type": "equals", "path": "intent", "value": "question"}, {"type": "regex", "path": "reply", "value": "\\\\$\\\\d"}]',
				},
				{"case_name": "switched off", "enabled": 0, "input": '{"text": "x"}', "expected": '"never"'},
			],
			threshold=60,
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cleanup_evals()
		frappe.db.commit()
		super().tearDownClass()

	def test_a_run_scores_the_cases(self):
		run = frappe.get_doc("AI Eval Run", evals.run_set(SET, notes="baseline"))
		self.assertEqual((run.total, run.passed, run.failed), (3, 2, 1), "the switched-off case is skipped")
		self.assertAlmostEqual(run.score, 66.67, places=1)
		self.assertEqual(run.status, "Passed", "threshold 60")
		failing = next(r for r in run.results if not r.passed)
		self.assertIn("equals intent", failing.failures)
		self.assertIn("regex reply", failing.failures)
		es = frappe.get_doc("AI Eval Set", SET)
		self.assertEqual((es.last_run, round(es.last_score)), (run.name, 67))

	def test_the_gate(self):
		self.assertTrue(evals.gate(SET))
		self.assertFalse(evals.gate(SET, min_score=90))

	def test_a_broken_target_fails_its_cases_not_the_run(self):
		make_set(
			f"{SET} broken",
			"ai_interface.tests.test_evals.no_such_function",
			[{"case_name": "c", "input": "{}"}],
		)
		run = frappe.get_doc("AI Eval Run", evals.run_set(f"{SET} broken"))
		self.assertEqual((run.status, run.passed), ("Failed", 0))
		self.assertIn("target failed", run.results[0].failures)

	def test_only_declared_targets_run(self):
		with self.assertRaises(frappe.PermissionError):
			make_set(f"{SET} rogue", "frappe.client.delete", [{"case_name": "c", "input": "{}"}])
		with patch.object(evals, "allowed_targets", return_value=["excom.evals.classify"]):
			self.assertRaises(frappe.PermissionError, evals.check_target, "excom.evals.classify_more")
			evals.check_target("excom.evals.classify")

	def test_a_failing_check_still_stops_tagging(self):
		make_set(
			f"{SET} bad check",
			"ai_interface.tests.test_evals.classify",
			[{"case_name": "c", "input": '{"text": "hi"}', "checks": '[{"type": "regex", "value": "("}]'}],
		)
		run = frappe.get_doc("AI Eval Run", evals.run_set(f"{SET} bad check"))
		self.assertEqual(run.passed, 0)
		self.assertIsNone(getattr(frappe.local, "ai_eval_run", None))

	def test_the_judge(self):
		make_set(
			f"{SET} judged",
			"ai_interface.tests.test_evals.classify",
			[
				{
					"case_name": "tone",
					"input": '{"text": "price?"}',
					"checks": '[{"type": "judge", "criteria": "Polite"}]',
				}
			],
		)
		with patch.object(
			chat, "run", return_value=frappe._dict(data={"pass": False, "reason": "too blunt"})
		) as judge:
			run = frappe.get_doc("AI Eval Run", evals.run_set(f"{SET} judged"))
		self.assertEqual(run.results[0].failures, "judge: too blunt")
		self.assertIn("Polite", judge.call_args.kwargs["prompt"])
		self.assertEqual(judge.call_args.kwargs["action"], "eval_judge")


class TestEvalCosts(FrappeTestCase):
	"""Calls a target makes are tagged to the run and costed per case."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup_evals()
		make_provider_type(TYPE_EVAL, adapter_path="ai_interface.tests.test_chat.ScriptedAdapter")
		make_provider(
			P_EVAL,
			TYPE_EVAL,
			models=[{"model_id": "m-1", "cost_in": 0.000004, "cost_out": 0.00002}],
			api_base_url="main",
		)
		ScriptedAdapter.script = {"main": []}
		ScriptedAdapter.seen = []
		make_set(
			SET_AI,
			"ai_interface.tests.test_evals.classify_with_model",
			[{"case_name": "one call", "input": '{"text": "hello"}', "expected": '{"intent": "question"}'}],
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cleanup_evals()
		cleanup()
		if frappe.db.exists("AI Provider Type", TYPE_EVAL):
			frappe.delete_doc("AI Provider Type", TYPE_EVAL, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_calls_are_tagged_and_costed(self):
		ScriptedAdapter.script["main"] = [answer('{"intent": "question"}')]
		run = frappe.get_doc("AI Eval Run", evals.run_set(SET_AI))
		self.assertEqual(run.status, "Passed")
		log = run.results[0].log
		self.assertEqual(frappe.db.get_value("AI Call Log", log, "eval_run"), run.name)
		self.assertGreater(run.results[0].base_cost, 0)
		self.assertAlmostEqual(run.base_cost, run.results[0].base_cost)
		self.assertIsNone(getattr(frappe.local, "ai_eval_logs", None), "tagging stops after the case")

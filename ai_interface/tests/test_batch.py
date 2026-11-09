"""Batches: submit, poll, answers on the right rows at the batch price, callback, policy after."""

from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.providers.base import ProviderHTTPError, ProviderRefusal, ProviderResponse
from ai_interface.services import batch
from ai_interface.tests.test_chat import ScriptedAdapter
from ai_interface.tests.utils import PREFIX, cleanup, make_provider, make_provider_type

APP = f"{PREFIX}batch"
TYPE_BATCH = f"{PREFIX} Batching"
TYPE_NOBATCH = f"{PREFIX} No Batching"
P_BATCH = f"{PREFIX} Batch Provider"
P_NOBATCH = f"{PREFIX} Live Only"
SCHEMA = {"type": "object", "properties": {"fit": {"type": "integer"}}, "required": ["fit"]}


class BatchAdapter(ScriptedAdapter):
	FEATURES = frozenset({"system", "json", "batch"})
	submitted: ClassVar[list] = []
	ended = False
	outcomes: ClassVar[dict] = {}  # batch_key -> response or exception

	def submit_batch(self, items, **kw):
		BatchAdapter.submitted = items
		return "pb_1"

	def batch_status(self, batch_id, **kw):
		return {"ended": BatchAdapter.ended, "status": "ended" if BatchAdapter.ended else "in_progress"}

	def batch_results(self, batch_id, **kw):
		for custom_id, _req in BatchAdapter.submitted:
			key = frappe.db.get_value("AI Call Log", custom_id, "batch_key")
			if key in BatchAdapter.outcomes:
				yield custom_id, BatchAdapter.outcomes[key]

	def cancel_batch(self, batch_id, **kw):
		BatchAdapter.cancelled = batch_id


def reply(text):
	return ProviderResponse(
		content=text, input_tokens=1000, output_tokens=100, model="m-1", stop_reason="end_turn"
	)


DONE = []


def on_done(name):
	DONE.append(name)


class TestBatch(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cleanup()
		make_provider_type(
			TYPE_BATCH, adapter_path="ai_interface.tests.test_batch.BatchAdapter", batch_discount_percent=50
		)
		make_provider(
			P_BATCH, TYPE_BATCH, models=[{"model_id": "m-1", "cost_in": 0.000004, "cost_out": 0.00002}]
		)
		make_provider_type(TYPE_NOBATCH, adapter_path="ai_interface.tests.test_chat.PlainAdapter")
		make_provider(P_NOBATCH, TYPE_NOBATCH, models=[{"model_id": "m-1"}])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		for name in frappe.get_all("AI Batch", {"calling_app": APP}, pluck="name"):
			frappe.delete_doc("AI Batch", name, force=True, ignore_permissions=True)
		cleanup()
		for t in (TYPE_BATCH, TYPE_NOBATCH):
			if frappe.db.exists("AI Provider Type", t):
				frappe.delete_doc("AI Provider Type", t, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		BatchAdapter.submitted, BatchAdapter.ended, BatchAdapter.outcomes = [], False, {}
		DONE.clear()

	def submit(self, **kw):
		return batch.submit(
			[
				{
					"key": "lead-1",
					"prompt": "profile A",
					"reference_doctype": "User",
					"reference_name": "Administrator",
				},
				{"key": "lead-2", "prompt": "profile B"},
				{"key": "lead-3", "prompt": "profile C"},
				{"key": "lead-4", "prompt": "profile D"},
			],
			system=[{"text": "You research buyers.", "cache": True}],
			output_schema=SCHEMA,
			calling_app=APP,
			action="profile_company",
			provider=P_BATCH,
			model="m-1",
			on_complete="ai_interface.tests.test_batch.on_done",
			budget_scope="Excom Agent:QA",
			**kw,
		)

	def test_a_batch_from_submission_to_callback(self):
		name = self.submit()
		doc = frappe.get_doc("AI Batch", name)
		self.assertEqual((doc.status, doc.provider_batch_id, doc.total), ("Submitted", "pb_1", 4))
		self.assertEqual(len(BatchAdapter.submitted), 4)
		self.assertEqual(
			BatchAdapter.submitted[0][1].system, [{"text": "You research buyers.", "cache": True}]
		)
		logs = frappe.get_all("AI Call Log", {"batch": name}, ["status", "batch_key", "budget_scope"])
		self.assertEqual({(l.status, l.budget_scope) for l in logs}, {("Queued", "Excom Agent:QA")})

		self.assertEqual(batch.poll(name), "Submitted", "not ended yet")
		self.assertEqual(DONE, [])

		BatchAdapter.ended = True
		BatchAdapter.outcomes = {
			"lead-1": reply('{"fit": 80}'),
			"lead-2": reply("not json"),
			"lead-3": ProviderRefusal("declined"),
			# lead-4: never answered
		}
		self.assertEqual(batch.poll(name), "Ended")
		doc.reload()
		self.assertEqual((doc.succeeded, doc.failed), (1, 3))
		self.assertEqual(DONE, [name])
		by_key = {r.key: r for r in batch.results(name)}
		self.assertEqual(by_key["lead-1"].data, {"fit": 80})
		self.assertEqual(by_key["lead-1"].reference_name, "Administrator")
		self.assertEqual(by_key["lead-2"].status, "Failed")
		self.assertEqual(frappe.db.get_value("AI Call Log", by_key["lead-3"].log, "error_type"), "Refused")
		self.assertIn("No result", by_key["lead-4"].error)
		cost = frappe.db.get_value("AI Call Log", by_key["lead-1"].log, "cost")
		self.assertAlmostEqual(cost, (1000 * 0.000004 + 100 * 0.00002) * 0.5, places=8, msg="half price")
		self.assertAlmostEqual(doc.base_cost, cost * 2, places=8, msg="the two answered requests are billed")

	def test_a_provider_without_batches_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			batch.submit([{"key": "x", "prompt": "y"}], calling_app=APP, provider=P_NOBATCH, model="m-1")

	def test_submission_failure_fails_every_request(self):
		with patch.object(BatchAdapter, "submit_batch", side_effect=ProviderHTTPError("down", 503)):
			with self.assertRaises(ProviderHTTPError):
				self.submit()
		name = frappe.get_all(
			"AI Batch", {"calling_app": APP}, pluck="name", order_by="creation desc", limit=1
		)[0]
		self.assertEqual(frappe.db.get_value("AI Batch", name, "status"), "Failed")
		self.assertEqual(set(frappe.get_all("AI Call Log", {"batch": name}, pluck="status")), {"Failed"})

	def test_a_stale_batch_is_failed(self):
		name = self.submit()
		frappe.db.set_value(
			"AI Batch", name, "submitted_on", frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-31)
		)
		self.assertEqual(batch.poll(name), "Failed")

	def test_cancel(self):
		name = self.submit()
		self.assertEqual(batch.cancel(name), "Cancelled")
		self.assertEqual(BatchAdapter.cancelled, "pb_1")
		self.assertEqual(frappe.db.get_value("AI Batch", name, "status"), "Cancelled")

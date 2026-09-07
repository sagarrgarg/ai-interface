"""The site assistant's query planner.

Security first: these tests exist mostly to prove the assistant cannot be talked
into reading something the asker could not already open, and cannot be talked
into reading a credential at all. No AI call is made — planning is stubbed, so
the validator and executor are exercised directly.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_interface.services import query_engine as qe

DOCTYPE = "AI Call Log"


class TestSecretFields(FrappeTestCase):
	def test_password_fieldtype_is_secret(self):
		self.assertTrue(qe.is_secret_field("new_password", "Password"))

	def test_credentials_typed_as_data_are_still_secret(self):
		# The bug this guards: on User, api_secret is a Password field but
		# api_key and reset_password_key are plain Data. A fieldtype check alone
		# let a live API key through.
		self.assertTrue(qe.is_secret_field("api_key", "Data"))
		self.assertTrue(qe.is_secret_field("reset_password_key", "Data"))
		self.assertTrue(qe.is_secret_field("access_token", "Data"))

	def test_ordinary_fields_are_not_flagged(self):
		for name in ("full_name", "key_account", "keywords", "monkey", "status"):
			with self.subTest(field=name):
				self.assertFalse(qe.is_secret_field(name, "Data"))

	def test_secret_fields_are_stripped_from_a_request(self):
		spec = qe.validate_spec(
			{"doctype": "User", "fields": ["name", "full_name", "api_key", "api_secret"]},
			user="Administrator",
			allowed=["User"],
		)
		self.assertEqual(spec["fields"], ["name", "full_name"])

	def test_filtering_on_a_secret_field_is_refused(self):
		# Stripping from the field list is not enough on its own: a filter can
		# extract a value one guess at a time without ever selecting it.
		with self.assertRaises(frappe.ValidationError):
			qe.validate_spec(
				{"doctype": "User", "filters": {"api_key": "abc"}},
				user="Administrator",
				allowed=["User"],
			)


class TestSpecValidation(FrappeTestCase):
	def spec(self, **kw):
		kw.setdefault("doctype", DOCTYPE)
		return qe.validate_spec(kw, user="Administrator", allowed=[DOCTYPE])

	def test_doctype_outside_the_allowed_list_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			qe.validate_spec({"doctype": "Sales Invoice"}, user="Administrator", allowed=[DOCTYPE])
		self.assertIn("do not have access", str(ctx.exception))

	def test_missing_doctype_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			qe.validate_spec({"fields": ["name"]}, user="Administrator", allowed=[DOCTYPE])

	def test_invented_fields_are_dropped(self):
		self.assertEqual(self.spec(fields=["status", "not_a_real_field"])["fields"], ["status"])

	def test_invented_filter_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.spec(filters={"not_a_real_field": 1})

	def test_invented_group_by_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.spec(group_by="not_a_real_field")

	def test_unsupported_aggregate_is_refused(self):
		# The allowlist is what keeps a function name out of the SQL string.
		with self.assertRaises(frappe.ValidationError):
			self.spec(aggregate={"function": "sleep", "field": "name"})

	def test_every_allowed_aggregate_validates(self):
		for fn in sorted(qe.AGGREGATES):
			with self.subTest(function=fn):
				out = self.spec(aggregate={"function": fn, "field": "latency_ms"})
				self.assertEqual(out["aggregate"]["function"], fn)

	def test_count_ignores_the_requested_field(self):
		out = self.spec(aggregate={"function": "count", "field": "whatever"})
		self.assertEqual(out["aggregate"]["field"], "name")

	def test_limit_is_capped(self):
		self.assertEqual(self.spec(limit=99999)["limit"], qe.MAX_ROWS)

	def test_absent_or_zero_limit_uses_the_default(self):
		# 0 means the model omitted it, so the sensible page size is the default
		# rather than a single row.
		self.assertEqual(self.spec(limit=0)["limit"], qe.DEFAULT_ROWS)
		self.assertEqual(self.spec()["limit"], qe.DEFAULT_ROWS)

	def test_negative_limit_cannot_reach_the_query(self):
		self.assertEqual(self.spec(limit=-10)["limit"], 1)

	def test_defaults_to_name_when_no_fields_survive(self):
		self.assertEqual(self.spec(fields=["nonsense"])["fields"], ["name"])


class TestExecution(FrappeTestCase):
	def test_count_runs_in_the_database(self):
		# Not "fetch rows and let the model count them" — that caps every total
		# at the page size, which is wrong for the commonest question there is.
		spec = qe.validate_spec(
			{"doctype": DOCTYPE, "aggregate": {"function": "count", "field": "name"}},
			user="Administrator", allowed=[DOCTYPE],
		)
		rows = qe.run_query(spec, user="Administrator")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["value"], frappe.db.count(DOCTYPE))

	def test_group_by_returns_a_label_per_group(self):
		spec = qe.validate_spec(
			{"doctype": DOCTYPE, "aggregate": {"function": "count", "field": "name"},
			 "group_by": "status"},
			user="Administrator", allowed=[DOCTYPE],
		)
		for row in qe.run_query(spec, user="Administrator"):
			self.assertIn("label", row)
			self.assertIn("value", row)


class TestSchemaScoping(FrappeTestCase):
	def test_denied_doctypes_never_appear(self):
		allowed = qe.accessible_doctypes("Administrator", use_cache=False)
		self.assertFalse(set(allowed) & qe.ALWAYS_DENIED)

	def test_child_and_single_doctypes_are_excluded(self):
		# A child table cannot be queried on its own and a Single has one row;
		# offering either to the model only invites a wrong query.
		allowed = set(qe.accessible_doctypes("Administrator", use_cache=False))
		self.assertNotIn("AI Provider Model", allowed)
		self.assertNotIn("AI Settings", allowed)

	def test_shortlist_matches_plurals_to_singular_doctypes(self):
		# "customers" must reach Customer, or the planner falls back to whatever
		# sorts first and confidently answers the wrong question.
		allowed = ["Customer", "Sales Order", "Access Log", "Account"]
		out = qe.shortlist_doctypes("How many customers do we have?", allowed, limit=3)
		self.assertIn("Customer", out)

	def test_shortlist_ignores_question_words(self):
		allowed = ["Customer", "Sales Order", "Account"]
		out = qe.shortlist_doctypes("how many total", allowed, limit=3)
		self.assertTrue(out)  # falls back rather than matching on "many"

	def test_shortlist_prefers_the_tighter_name(self):
		allowed = ["Item", "Item Price", "Item Group"]
		self.assertEqual(qe.shortlist_doctypes("how many items", allowed, limit=1), ["Item"])


class TestJSONParsing(FrappeTestCase):
	def test_plain_json(self):
		self.assertEqual(qe._parse_json('{"doctype": "X"}')["doctype"], "X")

	def test_fenced_json(self):
		self.assertEqual(qe._parse_json('```json\n{"doctype": "X"}\n```')["doctype"], "X")

	def test_json_with_surrounding_chatter(self):
		# Models add a sentence however firmly the prompt says not to.
		raw = 'Sure! Here is the query:\n{"doctype": "X"}\nHope that helps.'
		self.assertEqual(qe._parse_json(raw)["doctype"], "X")

	def test_unparseable_response_is_refused_clearly(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			qe._parse_json("I am afraid I cannot help with that.")
		self.assertIn("rephrasing", str(ctx.exception))

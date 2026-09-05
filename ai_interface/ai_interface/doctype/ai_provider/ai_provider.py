import frappe
import requests
from frappe import _
from frappe.model.document import Document

CATALOG_TIMEOUT = 30


class AIProvider(Document):
	def validate(self):
		self.apply_type_defaults()
		self.mark_manual_pricing()

	def apply_type_defaults(self):
		if not self.provider_type:
			return
		config = frappe.get_cached_doc("AI Provider Type", self.provider_type).as_config()
		if not self.currency:
			self.currency = config["currency"]

	def mark_manual_pricing(self):
		"""Any rate a human typed is stamped Manual so a re-fetch leaves it alone."""
		for m in self.models:
			priced = (m.cost_per_input_token or 0) or (m.cost_per_output_token or 0)
			if priced and not m.pricing_source:
				m.pricing_source = "Manual"
			elif not priced and m.pricing_source != "Manual":
				m.pricing_source = "Unpriced"

	def get_credential(self) -> tuple[str, str]:
		"""Returns (credential, auth_type).

		Provider types flagged No Credential Required authenticate themselves —
		a local CLI holding its own OAuth token, or an unauthenticated endpoint.
		"""
		if self.provider_type:
			config = frappe.get_cached_doc("AI Provider Type", self.provider_type).as_config()
			if config.get("no_credential"):
				return "", "None"
		fieldname = "auth_token" if self.auth_type == "Auth Token" else "api_key"
		label = "Auth Token" if self.auth_type == "Auth Token" else "API Key"

		# raise_exception=False: an unset key is an ordinary setup state, not a
		# crash. Report it in our own words rather than leaking Frappe's
		# "Password not found" out of a Fetch Models click.
		credential = self.get_password(fieldname, raise_exception=False)
		if not credential:
			frappe.throw(_("Enter the {0} for {1} before using it.").format(label, self.provider_name))

		return credential, label

	@frappe.whitelist()
	def fetch_models(self):
		"""Refresh the model catalog without destroying what the admin curated.

		Discovery gives ids. Pricing and capabilities come from the configured
		catalog for rows nobody has touched, and from the admin for everything
		else. A model the vendor no longer returns is disabled, never deleted —
		historical call logs still cost against it.
		"""
		frappe.only_for("System Manager")

		from ai_interface.providers import get_provider

		provider_instance = get_provider(self.provider_type)
		credential, auth_type = self.get_credential()

		discovered = provider_instance.fetch_models(
			credential=credential,
			auth_type=auth_type,
			api_base_url=self.api_base_url or "",
		)

		existing = {m.model_id: m for m in self.models if m.model_id}
		discovered_ids = {m["model_id"] for m in discovered}

		catalog = self._load_catalog(provider_instance.config, discovered_ids)

		added = 0
		for entry in discovered:
			row = existing.get(entry["model_id"])
			if row:
				row.enabled = 1
				if entry.get("label"):
					row.label = entry["label"]
			else:
				row = self.append("models", {"model_id": entry["model_id"], "label": entry.get("label")})
				added += 1
			self._prefill_from_catalog(row, catalog)

		retired = 0
		for model_id, row in existing.items():
			if model_id not in discovered_ids and discovered_ids:
				if row.enabled:
					retired += 1
				row.enabled = 0

		self.save()

		unpriced = len([m for m in self.models if m.enabled and m.pricing_source == "Unpriced"])
		self._report(len(discovered), added, retired, unpriced)

	def _load_catalog(self, config: dict, wanted: set) -> dict:
		"""Pull rates and capabilities from the configured JSON catalog.

		Entirely optional: an empty Pricing Source URL means no outbound call is
		made at all, and every rate is typed by hand.
		"""
		url = config.get("pricing_source_url")
		if not url or not wanted:
			return {}

		try:
			response = requests.get(url, timeout=CATALOG_TIMEOUT)
			response.raise_for_status()
			raw = response.json()
		except Exception as e:
			frappe.msgprint(
				_("Model catalog at {0} could not be read ({1}). Rates left for manual entry.").format(
					url, e
				),
				indicator="orange",
				alert=True,
			)
			return {}

		if not isinstance(raw, dict):
			return {}

		prefix = config.get("catalog_prefix") or ""
		catalog = {}
		for model_id in wanted:
			for key in (model_id, f"{prefix}{model_id}" if prefix else None):
				if key and isinstance(raw.get(key), dict):
					catalog[model_id] = raw[key]
					break
		return catalog

	def _prefill_from_catalog(self, row, catalog: dict):
		if row.pricing_source == "Manual":
			return

		entry = catalog.get(row.model_id)
		if not entry:
			if not row.pricing_source:
				row.pricing_source = "Unpriced"
			return

		row.cost_per_input_token = entry.get("input_cost_per_token") or 0
		row.cost_per_output_token = entry.get("output_cost_per_token") or 0
		row.context_window = entry.get("max_input_tokens") or entry.get("max_tokens") or 0
		row.supports_vision = 1 if entry.get("supports_vision") else 0
		row.supports_tools = 1 if entry.get("supports_function_calling") else 0
		row.pricing_source = (
			"Catalog" if (row.cost_per_input_token or row.cost_per_output_token) else "Unpriced"
		)

	def _report(self, discovered: int, added: int, retired: int, unpriced: int):
		if not discovered:
			frappe.msgprint(
				_("{0} exposes no model listing. Add the models you want in the table below.").format(
					self.provider_name
				),
				indicator="blue",
			)
			return

		parts = [_("{0} models from {1}: {2} new").format(discovered, self.provider_name, added)]
		if retired:
			parts.append(_("{0} no longer offered (disabled)").format(retired))
		frappe.msgprint(", ".join(parts), indicator="green", alert=True)

		if unpriced:
			frappe.msgprint(
				_(
					"{0} enabled model(s) have no rate — their calls will log a cost of 0. "
					"Enter the rates in the table below."
				).format(unpriced),
				indicator="orange",
			)

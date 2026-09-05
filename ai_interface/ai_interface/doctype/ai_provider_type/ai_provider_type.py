import frappe
from frappe import _
from frappe.model.document import Document


class AIProviderType(Document):
	def validate(self):
		self.validate_adapter()
		self.normalise_paths()

	def validate_adapter(self):
		"""Fail at save time, not at call time.

		A bad adapter path used to surface as a failed AI call hours later; the
		import is cheap, so resolve it while the admin is still looking at the
		form.
		"""
		from ai_interface.providers.base import BaseProvider

		try:
			cls = frappe.get_attr(self.adapter_path)
		except Exception as e:
			frappe.throw(_("Adapter path {0} could not be imported: {1}").format(self.adapter_path, e))

		if not (isinstance(cls, type) and issubclass(cls, BaseProvider)):
			frappe.throw(
				_("Adapter path {0} must point to a BaseProvider subclass.").format(self.adapter_path)
			)

	def normalise_paths(self):
		if self.default_base_url:
			self.default_base_url = self.default_base_url.rstrip("/")
		for field in ("chat_path", "models_path"):
			value = (self.get(field) or "").strip()
			if value and not value.startswith("/"):
				value = "/" + value
			self.set(field, value)

	def as_config(self) -> dict:
		"""The slice of this record an adapter needs at call time."""
		return {
			"type_name": self.type_name,
			"base_url": self.default_base_url or "",
			"chat_path": self.chat_path or "/v1/chat/completions",
			"models_path": self.models_path or "",
			"no_credential": bool(self.no_credential),
			"auth_header": self.auth_header or "Authorization",
			"auth_prefix": self.auth_prefix or "",
			"currency": self.default_currency or "USD",
			"pricing_source_url": self.pricing_source_url or "",
			"catalog_prefix": self.catalog_prefix or "",
			"unsupported_params": self.get_unsupported_params(),
		}

	def get_unsupported_params(self) -> list[str]:
		raw = (self.unsupported_params or "").replace(",", "\n")
		return [p.strip() for p in raw.split("\n") if p.strip()]

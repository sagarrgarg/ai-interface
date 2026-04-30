import frappe
from frappe import _
from frappe.model.document import Document


class AIProvider(Document):
	def get_credential(self) -> tuple[str, str]:
		"""Returns (credential, auth_type). For Claude Code, returns empty credential."""
		if self.provider_type == "Claude Code":
			return "", "Claude Code"
		if self.auth_type == "Auth Token":
			return self.get_password("auth_token"), "Auth Token"
		return self.get_password("api_key"), "API Key"

	@frappe.whitelist()
	def fetch_models(self):
		frappe.only_for("System Manager")

		from ai_interface.providers import get_provider

		provider_instance = get_provider(self.provider_type)
		credential, auth_type = self.get_credential()

		models = provider_instance.fetch_models(
			credential=credential,
			auth_type=auth_type,
			api_base_url=self.api_base_url or "",
		)

		self.models = []
		for m in models:
			self.append("models", m)
		self.save()

		frappe.msgprint(
			_("Fetched {0} models from {1}.").format(len(models), self.provider_name),
			alert=True,
		)

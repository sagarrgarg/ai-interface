"""Provider resolution.

There is deliberately no hardcoded registry here. A provider type is an **AI
Provider Type** record naming a dotted path to a BaseProvider subclass, so a new
vendor is a form entry — and another app can register its own adapter with a
fixture row without editing this file.
"""

import frappe
from frappe import _

from ai_interface.providers.base import BaseProvider


def get_provider(provider_type: str) -> BaseProvider:
	"""Instantiate the adapter for a provider type, carrying its config."""
	doc = _get_type_doc(provider_type)

	try:
		cls = frappe.get_attr(doc.adapter_path)
	except Exception as e:
		frappe.throw(
			_("Adapter {0} for provider type '{1}' could not be loaded: {2}").format(
				doc.adapter_path, provider_type, e
			)
		)

	if not (isinstance(cls, type) and issubclass(cls, BaseProvider)):
		frappe.throw(
			_("Adapter {0} for provider type '{1}' is not a BaseProvider subclass.").format(
				doc.adapter_path, provider_type
			)
		)

	instance = cls()
	instance.config = doc.as_config()
	return instance


def get_provider_config(provider_type: str) -> dict:
	return _get_type_doc(provider_type).as_config()


def _get_type_doc(provider_type: str):
	if not provider_type:
		frappe.throw(_("No provider type set on this AI Provider."))

	if not frappe.db.exists("AI Provider Type", provider_type):
		frappe.throw(_("Unknown provider type: {0}").format(provider_type))

	doc = frappe.get_cached_doc("AI Provider Type", provider_type)
	if not doc.enabled:
		frappe.throw(_("Provider type '{0}' is disabled.").format(provider_type))

	return doc

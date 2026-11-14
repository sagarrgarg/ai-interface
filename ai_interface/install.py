"""What a new site needs from the start.

`bench install-app` marks every patch in patches.txt as already applied without running it, so the
records those patches seed — the provider types, the AI Manager and AI User roles, the assistant and
health-check defaults, the ready-made providers — never reached a freshly installed site: its AI Provider form offered no
provider type to choose. after_install runs the same idempotent steps.

after_migrate restores the default provider types only on a site that has none at all (one installed
before this hook existed); a type an administrator deleted on purpose is not brought back.
"""

import frappe

from ai_interface.patches.v1_0 import (
	assistant_defaults,
	create_ai_manager_role,
	create_ai_user_role,
	create_default_provider_types,
	create_default_providers,
	health_check_defaults,
)


def after_install():
	create_ai_manager_role.execute()
	create_ai_user_role.execute()
	create_default_provider_types.execute()
	create_default_providers.execute()  # switched off, no keys: the user adds a key and turns one on
	assistant_defaults.execute()
	health_check_defaults.execute()
	frappe.db.commit()


def after_migrate():
	create_ai_manager_role.execute()
	create_ai_user_role.execute()
	if not frappe.db.count("AI Provider Type"):
		create_default_provider_types.execute()
	frappe.db.commit()

app_name = "ai_interface"
app_title = "Ai Interface"
app_publisher = "Sagar Ratan Garg"
app_description = "AI Interface for other apps"
app_email = "sagar1ratan1garg1@gmail.com"
app_license = "mit"

required_apps = ["frappe", "erpnext"]

# Patches never run on a fresh install: these seed what a new site needs (provider types, roles, defaults)
after_install = "ai_interface.install.after_install"
after_migrate = "ai_interface.install.after_migrate"

export_python_type_annotations = True

default_log_clearing_doctypes = {
	"AI Call Log": 90,
}

# The site assistant widget. Loaded on every desk page, but renders nothing
# until the server confirms this user is allowed to use it.
app_include_js = "/assets/ai_interface/js/assistant.js"
app_include_css = "/assets/ai_interface/css/assistant.css"

has_permission = {
	"AI Chat Conversation": "ai_interface.ai_interface.doctype.ai_chat_conversation.ai_chat_conversation.has_permission",
}

permission_query_conditions = {
	"AI Chat Conversation": "ai_interface.ai_interface.doctype.ai_chat_conversation.ai_chat_conversation.get_permission_query_conditions",
}

# Apps declare the callables their eval sets may run (services/evals.py):
# ai_eval_targets = ["myapp.evals.classify_reply", "myapp.evals.*"]

scheduler_events = {
	"cron": {
		# Collect batches the provider has finished (services/batch.py).
		"*/5 * * * *": [
			"ai_interface.services.batch.poll_open_batches",
		],
	},
	"hourly": [
		"ai_interface.services.ai_client.check_provider_health",
	],
	"daily": [
		"ai_interface.ai_interface.doctype.ai_chat_conversation.ai_chat_conversation.clear_old_conversations",
		# Empty prompt and answer text past its retention; rows and cost stay (services/privacy.py).
		"ai_interface.services.privacy.purge_payloads",
	],
}

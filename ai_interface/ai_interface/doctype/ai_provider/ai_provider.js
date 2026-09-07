frappe.ui.form.on("AI Provider", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.page.set_indicator(...indicator_for(frm.doc.health_status));
	},

	fetch_models_button(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Please save the document before fetching models."));
			return;
		}
		frm.call("fetch_models").then(() => frm.reload_doc());
	},

	test_connection_button(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Please save the document before testing the connection."));
			return;
		}
		// A real billed call, so say so while it runs rather than looking frozen.
		frappe.dom.freeze(__("Calling {0}...", [frm.doc.provider_name]));
		frm.call("test_connection")
			.always(() => frappe.dom.unfreeze())
			.then(() => frm.reload_doc());
	},
});

function indicator_for(status) {
	const map = {
		Healthy: ["Healthy", "green"],
		Degraded: ["Degraded", "orange"],
		Down: ["Down", "red"],
	};
	return map[status] || [__("Not tested"), "gray"];
}

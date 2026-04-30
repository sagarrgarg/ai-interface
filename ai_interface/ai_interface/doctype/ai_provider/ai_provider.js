frappe.ui.form.on("AI Provider", {
	fetch_models_button(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Please save the document before fetching models."));
			return;
		}
		frm.call("fetch_models").then(() => {
			frm.reload_doc();
		});
	},
});

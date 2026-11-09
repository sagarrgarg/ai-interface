frappe.ui.form.on("AI Eval Set", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Run now"), () => {
			frappe.prompt(
				{ fieldname: "notes", fieldtype: "Small Text", label: __("What changed?") },
				(v) =>
					frm.call({ method: "ai_interface.services.evals.start", args: { eval_set: frm.doc.name, notes: v.notes || "" } }).then((r) => {
						frappe.show_alert({ message: __("Eval run {0} started", [r.message]), indicator: "blue" });
						frappe.set_route("Form", "AI Eval Run", r.message);
					}),
				__("Run eval set")
			);
		});
		if (frm.doc.last_run) frm.add_custom_button(__("Last run"), () => frappe.set_route("Form", "AI Eval Run", frm.doc.last_run));
	},
});

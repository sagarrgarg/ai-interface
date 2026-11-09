frappe.ui.form.on("AI Batch", {
	refresh(frm) {
		if (frm.doc.status === "Submitted") {
			frm.add_custom_button(__("Check now"), () =>
				frm.call({ method: "ai_interface.services.batch.check_now", args: { name: frm.doc.name } }).then(() => frm.reload_doc())
			);
			frm.add_custom_button(__("Cancel batch"), () =>
				frappe.confirm(__("Cancel this batch? Requests not yet answered are dropped."), () =>
					frm.call({ method: "ai_interface.services.batch.cancel", args: { name: frm.doc.name } }).then(() => frm.reload_doc())
				)
			);
		}
		frm.add_custom_button(__("Requests"), () => frappe.set_route("List", "AI Call Log", { batch: frm.doc.name }));
	},
});

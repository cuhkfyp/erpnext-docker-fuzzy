frappe.ui.form.on("CCD Tiered One-to-Many Batch", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Open Validation"), () => {
			frappe.set_route("Form", "CCD Tiered One-to-Many Validation", frm.doc.validation);
		});
		frm.add_custom_button(__("View Components"), () => {
			frappe.set_route("List", "CCD Match Component Review", {
				tiered_automation_batch: frm.doc.name,
			});
		});
	},
});

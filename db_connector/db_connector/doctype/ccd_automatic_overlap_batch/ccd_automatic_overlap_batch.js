frappe.ui.form.on("CCD Automatic Overlap Batch", {
	refresh(frm) {
		frm.add_custom_button(__("Refresh Progress"), () => frm.reload_doc());
	},
});

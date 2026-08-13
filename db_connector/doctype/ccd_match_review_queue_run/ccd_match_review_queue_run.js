frappe.ui.form.on("CCD Match Review Queue Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("View Review Candidates"), () => {
			frappe.set_route("List", "CCD Match Review Candidate", {
				queue_run: frm.doc.name,
			});
		}, __("Review"));
	},
});

frappe.ui.form.on("CCD Match Review Batch", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("View Assigned Candidates"), () => {
			frappe.set_route("List", "CCD Match Review Candidate", { assigned_review_batch: frm.doc.name });
		});
	},
});

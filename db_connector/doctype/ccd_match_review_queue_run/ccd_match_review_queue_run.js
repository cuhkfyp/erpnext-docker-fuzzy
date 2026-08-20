frappe.ui.form.on("CCD Match Review Queue Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("View Review Candidates"), () => {
			frappe.set_route("List", "CCD Match Review Candidate", {
				queue_run: frm.doc.name,
			});
		}, __("Review"));
		frm.add_custom_button(__("View Review Batches"), () => {
			frappe.set_route("List", "CCD Match Review Batch", { queue_run: frm.doc.name });
		}, __("Optional Assignment"));
		if (frm.doc.status === "Ready" && frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("Create Review Batch"), () => {
				frappe.prompt(
					[
						{ fieldname: "batch_size", fieldtype: "Int", label: __("Candidates to assign"), default: 100, reqd: 1 },
						{ fieldname: "selection_method", fieldtype: "Select", label: __("Selection Method"), options: "Highest Priority\nSource Balanced\nRisk Targeted", default: "Highest Priority", reqd: 1 },
						{ fieldname: "assignee", fieldtype: "Link", label: __("Default Assignee (optional)"), options: "User" },
						{ fieldname: "due_at", fieldtype: "Datetime", label: __("Due At (optional)") },
					],
					(values) => frappe.call({
						method: "db_connector.api_identity_review_batch.create_review_batch",
						args: { queue_run: frm.doc.name, ...values },
						freeze: true,
						callback(response) {
							if (response.message?.batch) frappe.set_route("Form", "CCD Match Review Batch", response.message.batch);
						},
					}),
					__("Create bounded optional Review Batch"),
				);
			}, __("Optional Assignment"));
		}
	},
});

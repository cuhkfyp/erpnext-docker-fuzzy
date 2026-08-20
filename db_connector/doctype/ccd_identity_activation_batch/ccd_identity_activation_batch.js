frappe.ui.form.on("CCD Identity Activation Batch", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;
		if (frm.doc.status === "Reviewed") {
			frm.add_custom_button(__("Approve Batch"), () => {
				frappe.confirm(
					__("Approve this frozen, component-atomic batch? Approval alone creates no Identity Memberships."),
					() => frappe.call({
						method: "db_connector.api_identity_activation.approve_activation_batch",
						args: { batch_name: frm.doc.name },
						callback: () => frm.reload_doc(),
					}),
				);
			});
		}
		if (frm.doc.status === "Approved") {
			frm.add_custom_button(__("Apply Approved Batch"), () => {
				frappe.confirm(
					__("Run fresh safety checks and create reversible Identity Decisions, Groups, and Memberships? This is blocked while Live Identity Materialization is disabled or paused."),
					() => frappe.call({
						method: "db_connector.api_identity_activation.apply_activation_batch",
						args: { batch_name: frm.doc.name },
						freeze: true,
						callback: () => frm.reload_doc(),
					}),
				);
			});
		}
		if (frm.doc.status === "Failed") {
			frm.add_custom_button(__("Revalidate for Retry"), () => {
				frappe.confirm(
					__("Re-run the frozen component and safety checks before returning this batch to Approved?"),
					() => frappe.call({
						method: "db_connector.api_identity_activation.revalidate_failed_activation_batch",
						args: { batch_name: frm.doc.name },
						freeze: true,
						callback: () => frm.reload_doc(),
					}),
				);
			});
		}
	},
});

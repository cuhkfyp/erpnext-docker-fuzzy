frappe.ui.form.on("CCD Match Evaluation Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Review Pairs"), () => {
			frappe.set_route("List", "CCD Match Evaluation Pair", {
				evaluation_run: frm.doc.name,
			});
		});
		if (!frappe.user.has_role("System Manager")) return;
		if (frm.doc.status === "Reviewing") {
			frm.add_custom_button(__("Finalize Evaluation"), () => {
				frappe.call({
					method: "db_connector.api_fuzzy_evaluation.finalize_evaluation",
					args: { run_name: frm.doc.name },
					freeze: true,
					callback: () => frm.reload_doc(),
				});
			});
		}
		if (frm.doc.status === "Awaiting Management Approval") {
			for (const decision of ["Approved", "Rejected"]) {
				frm.add_custom_button(__(decision), () => {
					frappe.call({
						method: "db_connector.api_fuzzy_evaluation.set_evaluation_approval",
						args: { run_name: frm.doc.name, decision },
						callback: () => frm.reload_doc(),
					});
				}, __("Management Decision"));
			}
		}
	},
});

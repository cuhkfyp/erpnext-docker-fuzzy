frappe.ui.form.on("CCD Match Evaluation Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Review Pairs"), () => {
			frappe.set_route("List", "CCD Match Evaluation Pair", {
				evaluation_run: frm.doc.name,
			});
		});
		if (!frappe.user.has_role("System Manager")) return;
		if (frm.doc.run_purpose === "Splink Automatic Validation") {
			add_splink_validation_actions(frm);
			return;
		}
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

function add_splink_validation_actions(frm) {
	if (frm.doc.status === "Reviewing") {
		frm.add_custom_button(__("Finalize Frozen Validation"), () => frappe.call({
			method: "db_connector.api_splink_automation.finalize_splink_automatic_validation",
			args: { run_name: frm.doc.name },
			freeze: true,
			callback: () => frm.reload_doc(),
		}), __("Splink Automation"));
	}
	if (frm.doc.status === "Awaiting Management Approval") {
		for (const decision of ["Approved", "Rejected"]) {
			frm.add_custom_button(__(decision), () => frappe.prompt(
				[
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Management reason"), reqd: 1 },
					{ fieldname: "confirm_run_name", fieldtype: "Data", label: __("Type the exact Validation Run ID"), description: frm.doc.name, reqd: 1 },
				],
				(values) => frappe.call({
					method: "db_connector.api_splink_automation.approve_splink_automatic_validation",
					args: { run_name: frm.doc.name, decision, ...values },
					callback: () => frm.reload_doc(),
				}),
				__(`${decision} Splink validation`),
			), __("Splink Management Decision"));
		}
	}
	if (frm.doc.status === "Completed" && frm.doc.approval_status === "Approved") {
		frm.add_custom_button(__("Preview Human-Label Activation"), () => frappe.call({
			method: "db_connector.api_splink_automation.preview_validation_label_activation",
			args: { run_name: frm.doc.name },
			freeze: true,
			callback(response) {
				if (response.message?.batch) frappe.set_route("Form", "CCD Splink Automation Batch", response.message.batch);
			},
		}), __("Splink Automation"));
		frm.add_custom_button(__("Create Capped Splink Rollout"), () => frappe.call({
			method: "db_connector.api_splink_automation.create_capped_splink_rollout",
			args: { run_name: frm.doc.name },
			freeze: true,
			callback(response) {
				if (response.message?.batch) frappe.set_route("Form", "CCD Splink Automation Batch", response.message.batch);
			},
		}), __("Splink Automation"));
		if (frm.doc.holdout_qc_complete_count >= frm.doc.holdout_eligible_count && frm.doc.holdout_eligible_count >= 45) {
			frm.add_custom_button(__("Record Scheduled Splink Decision"), () => frappe.prompt(
				[
					{ fieldname: "decision", fieldtype: "Select", label: __("Decision"), options: "Approved\nRejected", reqd: 1 },
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Management reason"), reqd: 1 },
					{ fieldname: "confirm_run_name", fieldtype: "Data", label: __("Type the exact Validation Run ID"), description: frm.doc.name, reqd: 1 },
				],
				(values) => frappe.call({
					method: "db_connector.api_splink_automation.set_scheduled_splink_approval",
					args: { run_name: frm.doc.name, ...values },
					callback: () => frm.reload_doc(),
				}),
				__("Second management approval"),
			), __("Splink Management Decision"));
		}
	}
}

frappe.ui.form.on("CCD Splink Automation Batch", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;
		if (frm.doc.status === "Previewed") {
			frm.add_custom_button(__("Approve and Queue Background Apply"), () => frappe.prompt(
				[
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Approval reason"), reqd: 1 },
					{ fieldname: "confirm_batch_name", fieldtype: "Data", label: __("Type the exact Batch ID"), description: frm.doc.name, reqd: 1 },
				],
				(values) => frappe.call({
					method: "db_connector.api_splink_automation.approve_and_queue_splink_batch",
					args: { batch_name: frm.doc.name, ...values },
					callback: () => frm.reload_doc(),
				}),
				__("Approve frozen Splink batch"),
			), __("Splink Automation"));
		}
		if (frm.doc.status === "Failed") {
			frm.add_custom_button(__("Retry Background Apply"), () => frappe.prompt(
				[
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Retry reason"), reqd: 1 },
					{ fieldname: "confirm_batch_name", fieldtype: "Data", label: __("Type the exact Batch ID"), description: frm.doc.name, reqd: 1 },
				],
				(values) => frappe.call({
					method: "db_connector.api_splink_automation.retry_splink_batch",
					args: { batch_name: frm.doc.name, ...values },
					callback: () => frm.reload_doc(),
				}),
				__("Retry frozen Splink batch"),
			), __("Splink Automation"));
		}
	},
});

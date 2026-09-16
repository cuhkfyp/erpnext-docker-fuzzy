frappe.ui.form.on("CCD Identity QC Investigation", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status !== "Open" || !frappe.user.has_role("System Manager")) return;
		frm.add_custom_button(__("Resolve Governed Investigation"), () => {
			frappe.prompt(
				[
					{
						fieldname: "resolution_action",
						fieldtype: "Select",
						label: __("Resolution Action"),
						options: ["Relationship Corrected", "Relationship Revalidated", "QC Review Error", "Policy Disabled"],
						reqd: 1,
					},
					{ fieldname: "notes", fieldtype: "Small Text", label: __("Resolution Notes"), reqd: 1 },
					{
						fieldname: "confirm_investigation_name",
						fieldtype: "Data",
						label: __("Type the exact Investigation ID to confirm"),
						description: frm.doc.name,
						reqd: 1,
					},
				],
				(values) => frappe.call({
					method: "db_connector.api_identity_qc.resolve_qc_investigation",
					args: { investigation_name: frm.doc.name, ...values },
					freeze: true,
					callback: () => frm.reload_doc(),
				}),
				__("Resolve QC Investigation"),
			);
		}, __("Quality Control"));
	},
});

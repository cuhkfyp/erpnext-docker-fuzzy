frappe.ui.form.on("CCD Match Canary Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("View Recommendations"), () => {
			frappe.set_route("List", "CCD Match Recommendation", {
				canary_run: frm.doc.name,
			});
		});
		if (!frappe.user.has_role("System Manager")) return;
		if (frm.doc.status === "Ready") {
			frm.add_custom_button(__("Activate Recommendations"), () => {
				frappe.confirm(
					__("Activate only the passing reversible recommendations? This does not merge records or set Is Matched."),
					() => frappe.call({
						method: "db_connector.api_fuzzy_canary.activate_canary",
						args: { run_name: frm.doc.name },
						freeze: true,
						callback: () => frm.reload_doc(),
					}),
				);
			});
		}
		if (frm.doc.status === "Active") {
			frm.add_custom_button(__("Reverse Canary"), () => {
				frappe.prompt(
					[{ fieldname: "reason", fieldtype: "Small Text", label: __("Reversal Reason"), reqd: 1 }],
					(values) => frappe.call({
						method: "db_connector.api_fuzzy_canary.reverse_canary",
						args: { run_name: frm.doc.name, reason: values.reason },
						freeze: true,
						callback: () => frm.reload_doc(),
					}),
					__("Reverse Canary"),
				);
			});
		}
	},
});

frappe.ui.form.on("CCD Match Recommendation", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;
		if (["Proposed", "Active"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Reverse Recommendation"), () => {
				frappe.prompt(
					[{ fieldname: "reason", fieldtype: "Small Text", label: __("Reversal Reason"), reqd: 1 }],
					(values) => frappe.call({
						method: "db_connector.api_fuzzy_canary.reverse_recommendation",
						args: { recommendation_name: frm.doc.name, reason: values.reason },
						callback: () => frm.reload_doc(),
					}),
					__("Reverse Recommendation"),
				);
			});
		}
	},
});

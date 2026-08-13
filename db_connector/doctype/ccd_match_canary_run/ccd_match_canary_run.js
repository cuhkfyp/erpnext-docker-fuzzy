frappe.ui.form.on("CCD Match Canary Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("View All Recommendations"), () => {
			frappe.set_route("List", "CCD Match Recommendation", {
				canary_run: frm.doc.name,
			});
		}, __("Review"));
		if (frm.doc.exception_component_count) {
			frm.add_custom_button(__("Review Exception Components"), () => {
				frappe.set_route("List", "CCD Match Component Review", {
					canary_run: frm.doc.name,
				});
			}, __("Review"));
		}
		if (frm.doc.qc_sample_count) {
			frm.add_custom_button(__("Review Random QC Sample"), () => {
				frappe.set_route("List", "CCD Match Recommendation", {
					canary_run: frm.doc.name,
					qc_selected: 1,
				});
			}, __("Review"));
		}
		if (!frappe.user.has_role("System Manager")) return;
		if (["Ready", "Active"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Create Splink Review Queue"), () => {
				frappe.confirm(
					__("Create a separate human-review queue for eligible candidate pairs at or above the approved maximum-F1 Splink cutoff? Tiered High pairs and previously reviewed pairs are excluded. This does not link, merge, or update CCD Master."),
					() => frappe.call({
						method: "db_connector.api_fuzzy_review_queue.enqueue_review_queue",
						args: { canary_name: frm.doc.name },
						freeze: true,
						callback(response) {
							if (response.message && response.message.run) {
								frappe.set_route("Form", "CCD Match Review Queue Run", response.message.run);
							}
						},
					}),
				);
			}, __("Review"));
		}
		if (frm.doc.status === "Ready") {
			frm.add_custom_button(__("Approve Recommendations"), () => {
				frappe.confirm(
					__("Approve only the safety-gated Proposed recommendation records? This changes recommendation status and appends audit events only. It does not link or merge CCD records, set Is Matched, or populate Matching Score. All exceptions remain inactive."),
					() => frappe.call({
						method: "db_connector.api_fuzzy_canary.approve_canary_recommendations",
						args: { run_name: frm.doc.name },
						freeze: true,
						callback: () => frm.reload_doc(),
					}),
				);
			}, __("Recommendation Status Only"));
		}
		if (frm.doc.status === "Active") {
			frm.add_custom_button(__("Withdraw Approved Recommendations"), () => {
				frappe.prompt(
					[{ fieldname: "reason", fieldtype: "Small Text", label: __("Withdrawal Reason"), reqd: 1 }],
					(values) => frappe.call({
						method: "db_connector.api_fuzzy_canary.reverse_canary",
						args: { run_name: frm.doc.name, reason: values.reason },
						freeze: true,
						callback: () => frm.reload_doc(),
					}),
					__("Withdraw Approved Recommendations"),
				);
			}, __("Recommendation Status Only"));
		}
	},
});

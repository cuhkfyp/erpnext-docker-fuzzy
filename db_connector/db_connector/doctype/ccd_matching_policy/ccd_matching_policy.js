frappe.ui.form.on("CCD Matching Policy", {
	refresh(frm) {
		if (!frm.is_new() && frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("Create Draft Revision"), () => {
				frappe.prompt(
					[
						{ fieldname: "target_policy", fieldtype: "Data", label: __("New Policy Version"), reqd: 1 },
						{
							fieldname: "phone_evidence_strategy",
							fieldtype: "Select",
							label: __("Phone Evidence Strategy"),
							options: "Configured Field Only\nAll Valid Phone Fields",
							default: frm.doc.phone_evidence_strategy || "Configured Field Only",
							reqd: 1,
						},
					],
					(values) => frappe.call({
						method: "db_connector.api_fuzzy_evaluation.create_policy_revision",
						args: {
							source_policy: frm.doc.name,
							target_policy: values.target_policy,
							phone_evidence_strategy: values.phone_evidence_strategy,
						},
						freeze: true,
						callback(response) {
							if (response.message?.policy) {
								frappe.set_route("Form", "CCD Matching Policy", response.message.policy);
							}
						},
					}),
					__("Create Governed Draft Policy Revision"),
				);
			}, __("Governance"));
		}
		if (frm.is_new() || !["Draft", "Pilot"].includes(frm.doc.status)) return;
		if (frm.doc.status === "Draft" && frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("Promote to Pilot"), () => {
				frappe.confirm(
					__("Promote this unchanged evaluated policy to a recommendation-only Pilot?"),
					() => frappe.call({
						method: "db_connector.api_fuzzy_canary.promote_policy_to_pilot",
						args: { policy_name: frm.doc.name },
						callback: () => frm.reload_doc(),
					}),
				);
			}, __("Canary"));
		}
		if (frm.doc.status === "Pilot" && frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("Start Recommendation Canary"), () => {
				frappe.call({
					method: "db_connector.api_fuzzy_canary.enqueue_canary",
					args: { policy_name: frm.doc.name },
					freeze: true,
					callback(response) {
						if (response.message?.run) {
							if (response.message.already_running) {
								frappe.show_alert({
									message: __("A recommendation canary is already running; opening it."),
									indicator: "blue",
								});
							}
							frappe.set_route("Form", "CCD Match Canary Run", response.message.run);
						}
					},
				});
			}, __("Canary"));
		}
		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Import Registration Mappings"), () => {
				frappe.confirm(
					__("Replace this Draft policy's source profiles from current CCD Registration field mappings?"),
					() => frappe.call({
						method: "db_connector.api_fuzzy_evaluation.sync_policy_source_profiles",
						args: { policy_name: frm.doc.name },
						freeze: true,
						callback: () => frm.reload_doc(),
					}),
				);
			});
		}
		frm.add_custom_button(__("Start Shadow Evaluation"), () => {
			frappe.prompt(
				[
					{ fieldname: "sample_size", fieldtype: "Int", label: __("Sample Size"), default: 500, reqd: 1 },
					{ fieldname: "double_review_count", fieldtype: "Int", label: __("Double Review Count"), default: 100, reqd: 1 },
				],
				(values) => frappe.call({
					method: "db_connector.api_fuzzy_evaluation.enqueue_evaluation",
					args: {
						policy_name: frm.doc.name,
						sample_size: values.sample_size,
						double_review_count: values.double_review_count,
					},
					callback(response) {
						if (response.message?.run) {
							frappe.set_route("Form", "CCD Match Evaluation Run", response.message.run);
						}
					},
				}),
				__("Start Recommendation-Only Run"),
			);
		});
	},
});

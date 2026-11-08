frappe.listview_settings["CCD Hybrid Exploratory Batch"] = {
	onload(listview) {
		if (!frappe.user.has_role("System Manager")) return;
		listview.page.set_primary_action(
			__("Create / Reopen Governed Batch"),
			() => prompt_hybrid_exploratory_batch_from_list(),
			"add",
		);
	},
};

function prompt_hybrid_exploratory_batch_from_list() {
	frappe.prompt(
		[
			{
				fieldname: "rule_version",
				fieldtype: "Select",
				label: __("Development research rule"),
				options: "hybrid-name-support-v1\nhybrid-research-v1",
				default: "hybrid-name-support-v1",
				reqd: 1,
				description: __("Name-support v1 uses the 0.95+ score band plus name evidence; research v1 keeps the original stronger corroboration routes."),
			},
			{
				fieldname: "minimum_score",
				fieldtype: "Float",
				label: __("Research minimum Splink score"),
				default: 0.95,
				reqd: 1,
				description: __("Creates or reopens the deterministic 100-pair batch. The score must remain below the frozen ultra-high cutoff."),
			},
		],
		(values) => frappe.call({
			method: "db_connector.api_hybrid_exploratory_review.start_hybrid_exploratory_batch",
			args: {
				minimum_score: values.minimum_score,
				rule_version: values.rule_version,
			},
			freeze: true,
			freeze_message: __("Creating or reopening the governed exploratory batch…"),
			callback(response) {
				const batch = response.message?.batch;
				if (batch) frappe.set_route("Form", "CCD Hybrid Exploratory Batch", batch);
			},
		}),
		__("Create / Reopen Governed Exploratory Batch"),
	);
}

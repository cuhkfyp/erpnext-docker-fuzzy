frappe.ui.form.on("CCD Match Review Queue Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("View Review Candidates"), () => {
			frappe.set_route("List", "CCD Match Review Candidate", {
				queue_run: frm.doc.name,
			});
		}, __("Review"));
		frm.add_custom_button(__("View Review Batches"), () => {
			frappe.set_route("List", "CCD Match Review Batch", { queue_run: frm.doc.name });
		}, __("Optional Assignment"));
		if (frm.doc.status === "Ready" && frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("Preview Validation Inventory"), () => {
				frappe.call({
					method: "db_connector.api_splink_automation.preview_splink_automatic_validation",
					args: { queue_run: frm.doc.name },
					freeze: true,
					callback(response) {
						const result = response.message || {};
						frappe.msgprint({
							title: __("Splink validation inventory"),
							indicator: result.ready_to_create ? "green" : "orange",
							message: `<pre>${frappe.utils.escape_html(JSON.stringify(result, null, 2))}</pre>`,
						});
					},
				});
			}, __("Splink Automation"));
			frm.add_custom_button(__("Create Splink Automatic Validation"), () => {
				open_splink_validation_prompt(frm);
			}, __("Splink Automation"));
			frm.add_custom_button(__("Create Review Batch"), () => {
				frappe.prompt(
					[
						{ fieldname: "batch_size", fieldtype: "Int", label: __("Candidates to assign"), default: 100, reqd: 1 },
						{ fieldname: "selection_method", fieldtype: "Select", label: __("Selection Method"), options: "Highest Priority\nSource Balanced\nRisk Targeted", default: "Highest Priority", reqd: 1 },
						{ fieldname: "assignee", fieldtype: "Link", label: __("Default Assignee (optional)"), options: "User" },
						{ fieldname: "due_at", fieldtype: "Datetime", label: __("Due At (optional)") },
					],
					(values) => frappe.call({
						method: "db_connector.api_identity_review_batch.create_review_batch",
						args: { queue_run: frm.doc.name, ...values },
						freeze: true,
						callback(response) {
							if (response.message?.batch) frappe.set_route("Form", "CCD Match Review Batch", response.message.batch);
						},
					}),
					__("Create bounded optional Review Batch"),
				);
			}, __("Optional Assignment"));
		}
	},
});

function open_splink_validation_prompt(frm) {
	frappe.call({
		method: "db_connector.api_splink_automation.preview_splink_automatic_validation",
		args: { queue_run: frm.doc.name },
		freeze: true,
		callback(response) {
			const inventory = response.message || {};
			if (!inventory.ready_to_create) {
				frappe.msgprint({
					title: __("Validation cohort is not ready"),
					indicator: "orange",
					message: `<pre>${frappe.utils.escape_html(JSON.stringify(inventory, null, 2))}</pre>`,
				});
				return;
			}
			const actual = inventory.actual || {};
			const allocation = inventory.source_allocations || {};
			frappe.prompt(
				[
					{
						fieldname: "governance_summary",
						fieldtype: "HTML",
						options:
							`<p><strong>${__("Frozen cutoff")}</strong>: ${Number(inventory.cutoff || 0).toFixed(12)}<br>`
							+ `<strong>${__("Wilson lower target")}</strong>: ${(Number(inventory.precision_lower_target || 0) * 100).toFixed(2)}%<br>`
							+ `<strong>${__("Ultra-high / isolated / eligible")}</strong>: ${Number(actual.ultra_high_edges || 0)} / ${Number(actual.isolated_pairs || 0)} / ${Number(actual.eligible_isolated_pairs || 0)}</p>`
							+ `<pre>${frappe.utils.escape_html(JSON.stringify(allocation, null, 2))}</pre>`,
					},
					{
						fieldname: "deterministic_seed",
						fieldtype: "Data",
						label: __("Governance-approved 64-character hexadecimal seed"),
						default: secure_hex_seed(),
						reqd: 1,
						description: __("Record and use this seed once. Do not try alternatives to obtain a preferred cohort."),
					},
					{ fieldname: "confirm_queue_run", fieldtype: "Data", label: __("Type the exact Queue ID to confirm"), description: frm.doc.name, reqd: 1 },
				],
				(values) => frappe.call({
					method: "db_connector.api_splink_automation.create_splink_automatic_validation",
					args: { queue_run: frm.doc.name, ...values },
					freeze: true,
					callback(result) {
						if (result.message?.validation_run) frappe.set_route("Form", "CCD Match Evaluation Run", result.message.validation_run);
					},
				}),
				__("Freeze blinded 165 / 50 cohorts"),
			);
		},
	});
}

function secure_hex_seed() {
	const bytes = new Uint8Array(32);
	window.crypto.getRandomValues(bytes);
	return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

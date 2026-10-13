frappe.ui.form.on("CCD Match Evaluation Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Review Pairs"), () => {
			frappe.set_route("List", "CCD Match Evaluation Pair", {
				evaluation_run: frm.doc.name,
			});
		});
		if (frm.doc.run_purpose === "Hybrid Prospective Validation") {
			add_hybrid_validation_actions(frm);
			return;
		}
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

function add_hybrid_validation_actions(frm) {
	render_hybrid_validation_progress(frm);
	frm.dashboard.set_headline_alert(
		__("Development-only prospective evidence. This workflow cannot write identity state, activate automation, or authorize UAT/Production."),
		"orange",
	);
	const manager = frappe.user.has_role("System Manager");
	const reviewer = manager || frappe.user.has_role("CCD Match Reviewer") || frappe.user.has_role("CCD Match Sensitive Reviewer");

	if (["Profiling", "Generating Candidates"].includes(frm.doc.status)) {
		setTimeout(() => {
			if (window.cur_frm === frm) frm.reload_doc();
		}, 4000);
	}
	if (frm.doc.status === "Queued" && manager) {
		frm.add_custom_button(__("Freeze 165 Validation + 50 Holdout"), () => {
			frappe.prompt(
				[
					{
						fieldname: "deterministic_seed",
						fieldtype: "Data",
						label: __("Governance-approved 64-character hexadecimal seed"),
						default: frm.doc.validation_seed || secure_hex_seed(),
						reqd: 1,
						description: __("Record and use this seed once. Do not try alternative seeds to obtain a preferred cohort."),
					},
					{
						fieldname: "confirm_run_name",
						fieldtype: "Data",
						label: __("Type the exact Validation Run ID"),
						description: frm.doc.name,
						reqd: 1,
					},
				],
				(values) => frappe.call({
					method: "db_connector.api_hybrid_validation.start_freeze_hybrid_validation",
					args: { run_name: frm.doc.name, ...values },
					freeze: true,
					freeze_message: __("Starting the background cohort freeze…"),
					callback: () => frm.reload_doc(),
				}),
				__("Freeze unseen prospective cohorts"),
			);
		}, __("Hybrid Validation"));
	}
	if (frm.doc.status === "Reviewing") {
		if (reviewer) {
			frm.add_custom_button(__("Review Next Validation Pair"), () => frappe.call({
				method: "db_connector.api_hybrid_validation.get_next_hybrid_validation_pair",
				args: { run_name: frm.doc.name },
				callback(response) {
					const pair = response.message?.pair;
					if (pair) frappe.set_route("Form", "CCD Match Evaluation Pair", pair);
					else frappe.msgprint(__("You have no remaining ordinary review in this validation cohort."));
				},
			}), __("Hybrid Validation"));
		}
		if (manager) {
			frm.add_custom_button(__("Finalize Prospective Validation"), () => frappe.call({
				method: "db_connector.api_hybrid_validation.finalize_hybrid_prospective_validation",
				args: { run_name: frm.doc.name },
				freeze: true,
				callback: () => frm.reload_doc(),
			}), __("Hybrid Validation"));
		}
	}
	if (frm.doc.status === "Awaiting Management Approval" && manager) {
		for (const decision of ["Approved", "Rejected"]) {
			frm.add_custom_button(__(decision), () => frappe.prompt(
				[
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Development evidence decision reason"), reqd: 1 },
					{ fieldname: "confirm_run_name", fieldtype: "Data", label: __("Type the exact Validation Run ID"), description: frm.doc.name, reqd: 1 },
				],
				(values) => frappe.call({
					method: "db_connector.api_hybrid_validation.record_hybrid_development_decision",
					args: { run_name: frm.doc.name, decision, ...values },
					callback: () => frm.reload_doc(),
				}),
				__(`${decision} Development evidence`),
			), __("Hybrid Management Decision"));
		}
	}
	if (frm.doc.status === "Failed" && manager) {
		frm.add_custom_button(__("Retry Background Job"), () => frappe.call({
			method: "db_connector.api_hybrid_validation.retry_hybrid_validation_job",
			args: { run_name: frm.doc.name },
			callback: () => frm.reload_doc(),
		}), __("Hybrid Validation"));
	}
}

function secure_hex_seed() {
	const bytes = new Uint8Array(32);
	window.crypto.getRandomValues(bytes);
	return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function parse_json(value, fallback = {}) {
	try {
		return JSON.parse(value || "") || fallback;
	} catch (_error) {
		return fallback;
	}
}

function render_hybrid_validation_progress(frm) {
	const field = frm.get_field("hybrid_progress_html");
	if (!field?.$wrapper) return;
	const profile = parse_json(frm.doc.profile_json);
	const allocation = parse_json(frm.doc.source_allocation_json);
	const available = allocation.available || profile.eligible_by_source || {};
	const validation = allocation.validation || profile.source_allocations?.validation || {};
	const holdout = allocation.holdout || profile.source_allocations?.holdout || {};
	const sources = Array.from(new Set([...Object.keys(available), ...Object.keys(validation), ...Object.keys(holdout)])).sort();
	const sourceRows = sources.length
		? sources.map((source) => `<tr><td>${frappe.utils.escape_html(source)}</td><td>${Number(available[source] || 0)}</td><td>${Number(validation[source] || 0)}</td><td>${Number(holdout[source] || 0)}</td></tr>`).join("")
		: `<tr><td colspan="4" class="text-muted">${__("The background profile has not completed yet.")}</td></tr>`;
	field.$wrapper.html(
		`<p><strong>${__("Frozen proposal")}</strong>: 0.95 ≤ ${__("Splink score")} &lt; ${Number(frm.doc.frozen_automatic_cutoff || 0).toFixed(12)}; ` +
		`${__("eligible unseen pairs")}: ${Number(frm.doc.candidate_count || 0)}; ${__("excluded exploratory pairs")}: ${Number(frm.doc.hybrid_excluded_exploratory_count || 0)}.</p>` +
		`<table class="table table-bordered table-condensed"><thead><tr><th>${__("Source pair")}</th><th>${__("Available")}</th><th>${__("Validation")}</th><th>${__("Holdout")}</th></tr></thead><tbody>${sourceRows}</tbody></table>` +
		`<p>${__("Validation progress")}: ${Number(frm.doc.validation_valid_count || 0)} ${__("valid")}, ${Number(frm.doc.validation_unresolved_count || 0)} ${__("unresolved")}, ${Number(frm.doc.validation_stale_count || 0)} ${__("stale")}; ` +
		`${__("Same")}: ${Number(frm.doc.validation_same_count || 0)}, ${__("Different")}: ${Number(frm.doc.validation_different_count || 0)}, ${__("Wilson 95% lower")}: ${Number(frm.doc.validation_wilson_lower || 0).toFixed(2)}%.</p>` +
		`<p class="text-muted"><strong>${__("The 50-pair holdout stays reserved and hidden from this validation review. It is not materialized by this workflow.")}</strong></p>`,
	);
}

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

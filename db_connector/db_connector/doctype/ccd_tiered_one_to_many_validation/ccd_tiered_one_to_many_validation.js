frappe.ui.form.on("CCD Tiered One-to-Many Validation", {
	refresh(frm) {
		if (frm.is_new()) return;
	render_validation_dashboard(frm);
		add_review_navigation(frm);
		if (!(frappe.user_roles || []).includes("System Manager")) return;
		add_manager_actions(frm);
	},
});

function add_review_navigation(frm) {
	frm.add_custom_button(__("View Validation Components"), () => {
		frappe.set_route("List", "CCD Match Component Review", {
			tiered_one_to_many_validation: frm.doc.name,
			tiered_governance_cohort: "Validation",
		});
	}, __("Review"));
	frm.add_custom_button(__("View Holdout Components"), () => {
		frappe.set_route("List", "CCD Match Component Review", {
			tiered_one_to_many_validation: frm.doc.name,
			tiered_governance_cohort: "Blinded Holdout",
		});
	}, __("Review"));
	frm.add_custom_button(__("Open Unified QC Workbench"), () => {
		frappe.set_route("query-report", "CCD Unified QC Workbench", {
			channel: "Tiered One-to-Many",
			work_state: "Pending",
		});
	}, __("Review"));
	if (frm.doc.status === "Reviewing") {
		frm.add_custom_button(__("Review Next Validation Component"), () => {
			open_next_component(frm.doc.name, "get_next_validation_component");
		}, __("Review"));
	}
	if (frm.doc.status === "Holdout Reviewing") {
		frm.add_custom_button(__("Review Next Holdout Component"), () => {
			open_next_component(frm.doc.name, "get_next_holdout_component");
		}, __("Review"));
	}
	if (frm.doc.status === "Authorized") {
		frm.add_custom_button(__("Review Next Ongoing QC Component"), () => {
			open_next_component(frm.doc.name, "get_next_ongoing_qc_component");
		}, __("Review"));
	}
}

function add_manager_actions(frm) {
	if (frm.doc.status === "Prepared") {
		frm.add_custom_button(__("Freeze 165 Validation + 50 Holdout"), () => {
			frappe.confirm(
				__("Use the single recorded seed once and reserve the deterministic component cohorts? This cannot be re-rolled."),
				() => frappe.call({
					method: "db_connector.api_tiered_one_to_many.freeze_validation_cohorts",
					args: { validation_name: frm.doc.name },
					freeze: true,
					callback: () => frm.reload_doc(),
				}),
			);
		}, __("One-to-Many Validation"));
	}
	if (frm.doc.status === "Reviewing") {
		frm.add_custom_button(__("Finalize Prospective Validation"), () => {
			frappe.call({
				method: "db_connector.api_tiered_one_to_many.finalize_validation",
				args: { validation_name: frm.doc.name },
				freeze: true,
				callback: () => frm.reload_doc(),
			});
		}, __("One-to-Many Validation"));
	}
	if (frm.doc.status === "Awaiting Management Approval") {
		for (const decision of ["Approve", "Reject"]) {
			frm.add_custom_button(__(decision), () => {
				prompt_decision(frm, decision, "record_validation_decision");
			}, __("Validation Decision"));
		}
	}
	if (frm.doc.status === "Approved") {
		frm.add_custom_button(__("Create Blinded Holdout Rollout"), () => {
			frappe.confirm(
				__("Automatically apply the frozen blinded holdout components, then require two independent QC reviews on every applied holdout component?"),
				() => frappe.call({
					method: "db_connector.api_tiered_one_to_many.start_holdout_rollout",
					args: { validation_name: frm.doc.name },
					callback(response) {
						frappe.show_alert({ message: __("Holdout rollout queued: {0}", [response.message?.batch || ""]), indicator: "blue" });
						frm.reload_doc();
					},
				}),
			);
		}, __("Blinded Holdout"));
	}
	if (frm.doc.status === "Holdout Reviewing") {
		frm.add_custom_button(__("Finalize Holdout QC"), () => {
			frappe.call({
				method: "db_connector.api_tiered_one_to_many.finalize_holdout_qc",
				args: { validation_name: frm.doc.name },
				freeze: true,
				callback: () => frm.reload_doc(),
			});
		}, __("Blinded Holdout"));
	}
	if (frm.doc.status === "Holdout Awaiting Management Approval") {
		for (const decision of ["Approve", "Reject"]) {
			frm.add_custom_button(__(decision), () => {
				prompt_decision(frm, decision, "record_holdout_decision");
			}, __("Holdout Decision"));
		}
	}
	if (frm.doc.status === "Authorized") {
		frm.add_custom_button(__("Apply Final Validation Decisions"), () => {
			frappe.confirm(
				__("Queue component-atomic activation of the already-final two-reviewer Validation decisions? This does not create additional QC work."),
				() => frappe.call({
					method: "db_connector.api_tiered_one_to_many.start_validation_label_activation",
					args: { validation_name: frm.doc.name },
					callback(response) {
						frappe.show_alert({ message: `${__("Status")}: ${response.message?.status || ""}`, indicator: "blue" });
						frm.reload_doc();
					},
				}),
			);
		}, __("One-to-Many Activation"));
	}
}

function open_next_component(validationName, methodName) {
	frappe.call({
		method: `db_connector.api_tiered_one_to_many.${methodName}`,
		args: { validation_name: validationName },
		callback(response) {
			const review = response.message?.review;
			if (review) {
				frappe.set_route("Form", "CCD Match Component Review", review);
				return;
			}
			frappe.msgprint(__("No component currently needs your review in this cohort."));
		},
	});
}

function prompt_decision(frm, decision, methodName) {
	frappe.prompt(
		[
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Management decision reason"), reqd: 1 },
			{ fieldname: "confirm_validation_name", fieldtype: "Data", label: __("Type {0} exactly", [frm.doc.name]), reqd: 1 },
		],
		(values) => frappe.call({
			method: `db_connector.api_tiered_one_to_many.${methodName}`,
			args: { validation_name: frm.doc.name, decision, ...values },
			callback: () => frm.reload_doc(),
		}),
		__(`${decision} One-to-Many Evidence`),
	);
}

function render_validation_dashboard(frm) {
	frappe.call({
		method: "db_connector.api_tiered_one_to_many.get_validation_dashboard",
		args: { validation_name: frm.doc.name },
		callback(response) {
			const result = response.message || {};
			const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
			const wrapper = frm.fields_dict.progress_html?.$wrapper;
			if (!wrapper) return;
			wrapper.html(
				`<div class="alert alert-info"><b>${__("Governed one-to-many workflow")}</b><br>` +
				`${__("Status")}: ${esc(result.status)}<br>` +
				`${__("Canary / maximum component size")}: ${esc(result.canary_run)} / ${esc(result.maximum_component_size)}<br>` +
				`${__("Eligible population")}: ${esc(result.eligible_component_count)}<br>` +
				`${__("Validation complete / All Same / non-All-Same")}: ${esc(result.validation_complete_count)} / ${esc(result.validation_all_same_count)} / ${esc(result.validation_non_all_same_count)}<br>` +
				`${__("Validation Wilson lower")}: ${esc(result.validation_wilson_lower)}%<br>` +
				`${__("Holdout applied / QC complete")}: ${esc(result.holdout_applied_count)} / ${esc(result.holdout_qc_complete_count)}<br>` +
				`${__("Ongoing sampled QC selected / complete / failures")}: ${esc(result.ongoing_qc_selected_count)} / ${esc(result.ongoing_qc_complete_count)} / ${esc(result.ongoing_qc_failure_count)}` +
				`${result.error_summary ? `<br><span class="text-danger">${esc(result.error_summary)}</span>` : ""}</div>`,
			);
		},
	});
}

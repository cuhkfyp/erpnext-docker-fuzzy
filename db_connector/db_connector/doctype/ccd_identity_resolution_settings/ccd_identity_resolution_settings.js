frappe.ui.form.on("CCD Identity Resolution Settings", {
	refresh(frm) {
		enable_wrapped_page_actions(frm);
		if (!frappe.user.has_role("System Manager")) return;
		add_unified_qc_workbench(frm);
		add_qc_control(frm);
		add_tiered_control(frm);
		add_tiered_one_to_many_research(frm);
		add_automatic_overlap_control(frm);
		add_breaker_control(frm);
		add_splink_control(frm);
		add_hybrid_research_control(frm);
		add_hybrid_holdout_control(frm);
		add_hybrid_automation_control(frm);
		add_integrity_control(frm);
		add_unified_person_control(frm);
		add_notification_control(frm);
		frm.add_custom_button(__("Preview Automatic Tiered Run"), () => preview_automatic(frm), __("Automation"));
		if (frm.doc.automatic_tiered_enabled) {
			frm.add_custom_button(
				__("Run One Automatic Cycle Now"),
				() => frappe.confirm(
					__("Run one bounded automatic Tiered cycle using the current limits and fresh safety checks?"),
					() => start_automatic_tiered_cycle(frm),
				),
				__("Automation"),
			);
		}
	},
});

function enable_wrapped_page_actions(frm) {
	frm.page?.wrapper?.addClass("ccd-identity-resolution-settings-page");

	const style_id = "ccd-identity-resolution-settings-page-actions";
	if (document.getElementById(style_id)) return;

	const style = document.createElement("style");
	style.id = style_id;
	style.textContent = `
		.ccd-identity-resolution-settings-page .page-head .page-head-content {
			height: auto;
			min-height: var(--page-head-height);
			flex-wrap: wrap;
			align-items: flex-start;
			row-gap: var(--margin-xs, 4px);
		}

		.ccd-identity-resolution-settings-page .page-head .page-actions {
			min-width: 0;
			max-width: 100%;
			flex: 1 1 720px;
			flex-wrap: wrap;
			align-items: flex-start;
			row-gap: var(--margin-xs, 4px);
		}

		.ccd-identity-resolution-settings-page .page-head .custom-actions {
			min-width: 0;
			max-width: 100%;
			flex: 1 1 600px;
			flex-wrap: wrap;
			justify-content: flex-end;
			align-items: flex-start;
			row-gap: var(--margin-xs, 4px);
		}

		.ccd-identity-resolution-settings-page .page-head .custom-actions > .custom-btn-group {
			flex: 0 0 auto;
		}

		.ccd-identity-resolution-settings-page .page-head .standard-actions {
			flex: 0 0 auto;
			align-items: flex-start;
		}
	`;
	document.head.appendChild(style);
}

function add_tiered_one_to_many_research(frm) {
	frm.set_df_property(
		"maximum_tiered_one_to_many_component_size",
		"label",
		__("Maximum Tiered One-to-Many Component Size"),
	);
	frm.set_df_property(
		"maximum_tiered_one_to_many_component_size",
		"description",
		__("Live manager-selected ceiling for both research and the next authorized one-to-many automation cycle. Raising it does not require another validation cohort; every component still receives current conflict, exclusion, overlap, and staleness checks."),
	);
	frm.set_df_property(
		"tiered_one_to_many_components_per_run",
		"description",
		__("Positive manager-selected cap for the next component-atomic bulk cycle. It can be changed while automation is enabled; an already-created batch keeps its frozen selection."),
	);
	add_one_to_many_qc_capacity_control(frm);
	frm.add_custom_button(
		__("Preview Tiered One-to-Many Research"),
		() => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the component-size setting before running the preview."));
				return;
			}
			frappe.call({
				method: "db_connector.api_fuzzy_canary.preview_tiered_one_to_many_research",
				freeze: true,
				callback(response) {
					render_tiered_one_to_many_research(response.message || {});
				},
			});
		},
		__("Tiered One-to-Many Research"),
	);
	frm.add_custom_button(
		__("Create One-to-Many Validation Proposal"),
		() => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the component-size setting before preparing validation."));
				return;
			}
			frappe.confirm(
				__("Create one persistent Development-only proposal and generate one governance seed? The seed will be recorded and cannot be re-rolled inside the proposal."),
				() => frappe.call({
					method: "db_connector.api_tiered_one_to_many.create_validation_proposal",
					args: { maximum_component_size: frm.doc.maximum_tiered_one_to_many_component_size },
					freeze: true,
					callback(response) {
						if (response.message?.validation) {
							frappe.set_route("Form", "CCD Tiered One-to-Many Validation", response.message.validation);
						}
					},
				}),
			);
		},
		__("Tiered One-to-Many Research"),
	);

	const enabled = Boolean(frm.doc.automatic_tiered_one_to_many_enabled);
	const action = enabled ? __("Disable One-to-Many Automation") : __("Enable One-to-Many Automation");
	const phrase = enabled ? "DISABLE ONE-TO-MANY AUTOMATION" : "ENABLE ONE-TO-MANY AUTOMATION";
	frm.add_custom_button(action, () => frappe.prompt(
		[
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Control-change reason"), reqd: 1 },
			{ fieldname: "confirm_phrase", fieldtype: "Data", label: __("Type {0} exactly", [phrase]), reqd: 1 },
		],
		(values) => frappe.call({
			method: "db_connector.api_tiered_one_to_many.set_one_to_many_automation",
			args: { enabled: enabled ? 0 : 1, ...values },
			callback: () => frm.reload_doc(),
		}),
		action,
	), __("Tiered One-to-Many Automation"));

	frm.add_custom_button(__("Preview One-to-Many Bulk Cycle"), () => {
		frappe.call({
			method: "db_connector.api_tiered_one_to_many.preview_bulk_cycle",
			callback(response) {
				const result = response.message || {};
				frappe.msgprint({
					title: __("Zero-write one-to-many bulk preview"),
					indicator: (result.blockers || []).length ? "orange" : "green",
					message: `<p>${__("Eligible / selected components")}: ${result.eligible_component_count || 0} / ${result.selected_component_count || 0}</p>` +
						`<p>${__("Validated / effective maximum component size")}: ${result.validated_component_size_maximum || 0} / ${result.effective_component_size_maximum || 0}</p>` +
						(result.component_scope_expanded_by_manager ? `<p class="text-warning"><strong>${__("The manager-selected live ceiling extends beyond the original prospective cohort. Fresh component safety checks still apply; no additional labeling cohort is required.")}</strong></p>` : "") +
						`<p>${__("QC capacity mode")}: <strong>${esc(result.qc_capacity_mode || "Capacity Gated")}</strong><br>` +
						`${__("Requested / assignable / risk-accepted unassigned QC")}: ${result.requested_qc_sample_count || 0} / ${result.assignable_qc_sample_count || 0} / ${result.unassigned_qc_sample_count || 0}<br>` +
						`${__("Shared weekly QC remaining")}: ${result.shared_weekly_qc_remaining || 0}</p>` +
						`<p>${__("Blockers")}: ${(result.blockers || []).length ? result.blockers.map(esc).join("<br>") : __("None")}</p>` +
						(result.qc_capacity_gate_required ? "" : `<p class="text-warning"><strong>${__("Management accepted non-blocking QC risk. Materialization may proceed when QC capacity is unavailable.")}</strong></p>`) +
						`<p><strong>${__("No records were written.")}</strong></p>`,
				});
			},
		});
	}, __("Tiered One-to-Many Automation"));

	if (enabled) {
		frm.add_custom_button(__("Run One One-to-Many Cycle Now"), () => {
			frappe.confirm(
				__("Apply one bounded component-atomic cycle? Deterministic QC will be assigned when capacity exists; an audited non-blocking mode may proceed without it."),
				() => frappe.call({
					method: "db_connector.api_tiered_one_to_many.start_bulk_cycle",
					callback(response) {
						const result = response.message || {};
						frappe.msgprint({
							title: __("One-to-Many cycle"),
							indicator: result.status === "Queued" ? "blue" : "orange",
							message: `${__("Status")}: ${esc(result.status)}<br>${__("Batch")}: ${esc(result.batch || "")}`,
						});
						frm.reload_doc();
					},
				}),
			);
		}, __("Tiered One-to-Many Automation"));
	}
}

function add_automatic_overlap_control(frm) {
	frm.set_df_property(
		"automatic_overlap_components_per_run",
		"description",
		__("Positive per-cycle cap. It can be changed while recovery is enabled; every created batch keeps its own frozen selection."),
	);
	const enabled = Boolean(frm.doc.automatic_overlap_enabled);
	const action = enabled
		? __("Disable Automatic Overlap Recovery")
		: __("Enable Automatic Overlap Recovery");
	const phrase = enabled
		? "DISABLE AUTOMATIC OVERLAP RECOVERY"
		: "ENABLE AUTOMATIC OVERLAP RECOVERY";
	frm.add_custom_button(action, () => frappe.prompt(
		[
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Control-change reason"), reqd: 1 },
			{ fieldname: "confirm_phrase", fieldtype: "Data", label: __("Type {0} exactly", [phrase]), reqd: 1 },
		],
		(values) => frappe.call({
			method: "db_connector.api_automatic_overlap.set_automatic_overlap",
			args: { enabled: enabled ? 0 : 1, ...values },
			callback: () => frm.reload_doc(),
		}),
		action,
	), __("Automatic Overlap Recovery"));

	frm.add_custom_button(
		__("Preview Complete-Group Recovery"),
		() => preview_automatic_overlap(frm),
		__("Automatic Overlap Recovery"),
	);
	if (enabled) {
		frm.add_custom_button(
			__("Run One Overlap Recovery Cycle Now"),
			() => frappe.confirm(
				__("Queue one bounded component-atomic recovery cycle? It expands complete active identity groups. Birthday disagreement and missing or partial identifiers are not hard conflicts; active exclusions and contradictory complete validated HKIDs remain blocked."),
				() => run_automatic_overlap_now(frm),
			),
			__("Automatic Overlap Recovery"),
		);
	}
}

function overlap_seed_link(item) {
	if (!item.seed_document) return __("None");
	if (item.seed_doctype === "CCD Match Component Review") {
		return `<a href="/app/ccd-match-component-review/${encodeURIComponent(item.seed_document)}">${esc(item.seed_document)}</a>`;
	}
	return esc(item.seed_document);
}

function preview_automatic_overlap(frm) {
	frappe.call({
		method: "db_connector.api_automatic_overlap.preview_automatic_overlap_cycle",
		freeze: true,
		freeze_message: __("Expanding current complete identity-group closures without writing…"),
		callback(response) {
			const result = response.message || {};
			const selected = (result.selected_components || []).map((item) =>
				`<li><strong>${esc(item.channel)}</strong> — ${overlap_seed_link(item)} — ${esc(item.initial_record_count)} → ${esc(item.expanded_record_count)} ${__("records")}</li>`,
			).join("");
			const blockers = format_map(result.control_blocker_counts);
			frappe.msgprint({
				title: __("Zero-write complete-group overlap recovery preview"),
				indicator: result.would_queue_now ? "green" : "orange",
				wide: true,
				message:
					`<p>${__("Source overlap exceptions / structurally eligible / selected")}: ${esc(result.source_exception_count || 0)} / ${esc(result.structurally_eligible_count || 0)} / ${esc(result.selected_component_count || 0)}</p>` +
					`<p>${__("Available by authorized signal")}: ${esc(format_map(result.available_by_channel))}<br>` +
					`${__("Eligible by authorized signal")}: ${esc(format_map(result.eligible_by_channel))}<br>` +
					`${__("Selected by authorized signal")}: ${esc(format_map(result.selected_by_channel))}</p>` +
					`<p>${__("Per-cycle component cap / expanded-scope record cap")}: ${esc(result.maximum_components || 0)} / ${esc(result.maximum_scope_records || 0)}<br>` +
					`${__("Non-blocking post-apply QC sample")}: ${esc(result.qc_sample_percent || 0)}%</p>` +
					`<p>${__("Control blockers")}: ${esc(blockers)}<br>` +
					`${__("Excluded or deferred reasons")}: ${esc(format_map(result.excluded_reason_counts))}</p>` +
					(selected ? `<p><strong>${__("Frozen-order selection preview")}</strong></p><ol>${selected}</ol>` : "") +
					`<p class="small">${__("A precise birthday disagreement is not a hard conflict in this path. A complete contradictory validated HKID, active Different exclusion, stale source fingerprint, or over-limit closure remains blocked.")}</p>` +
					`<p><strong>${__("No identity or candidate state was changed.")}</strong></p>`,
			});
		},
	});
}

function run_automatic_overlap_now(frm) {
	if (frm.__ccd_overlap_cycle_starting) return;
	frm.__ccd_overlap_cycle_starting = true;
	frappe.call({
		method: "db_connector.api_automatic_overlap.start_automatic_overlap_cycle",
		freeze: true,
		freeze_message: __("Freezing and queueing the complete-group recovery batch…"),
		callback(response) {
			frm.__ccd_overlap_cycle_starting = false;
			const result = response.message || {};
			if (!result.batch) {
				frappe.msgprint({
					title: __("Automatic overlap recovery"),
					indicator: result.status === "No Eligible Components" ? "green" : "orange",
					message: `<p>${__("Status")}: ${esc(result.status || "")}</p>` +
						`<p>${__("Selected components")}: ${esc(result.selected_component_count || 0)}</p>`,
				});
				frm.reload_doc();
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Automatic complete-group overlap recovery"),
				fields: [{ fieldname: "automatic_overlap_progress_html", fieldtype: "HTML" }],
			});
			dialog.show();
			update_automatic_overlap_progress(dialog, result);
			poll_automatic_overlap_progress(frm, result.batch, dialog);
		},
		error() {
			frm.__ccd_overlap_cycle_starting = false;
		},
	});
}

function update_automatic_overlap_progress(dialog, result) {
	const field = dialog.get_field("automatic_overlap_progress_html");
	if (!field?.$wrapper) return;
	const batch = result.batch || "";
	const batchLink = batch
		? `<a href="/app/ccd-automatic-overlap-batch/${encodeURIComponent(batch)}">${esc(batch)}</a>`
		: __("None");
	const terminal = ["Applied", "Applied with Exceptions", "Failed"].includes(result.status);
	const itemRows = (result.items || []).map((item) =>
		`<tr><td>${esc(item.channel)}</td><td>${overlap_seed_link(item)}</td><td>${esc(item.initial_records)} → ${esc(item.expanded_records)}</td><td>${esc(item.status)}</td><td>${esc(item.error || "")}</td></tr>`,
	).join("");
	field.$wrapper.html(
		`<div class="alert alert-${result.status === "Applied" ? "success" : (result.status === "Failed" ? "danger" : "info")}">` +
			`<p><strong>${__("Status")}: ${esc(result.status || "Queued")}</strong><br>${__("Batch")}: ${batchLink}</p>` +
			`<p>${__("Selected / Tiered High / Ultra-High Splink")}: ${esc(result.selected || result.selected_component_count || 0)} / ${esc(result.tiered || result.tiered_component_count || 0)} / ${esc(result.splink || result.splink_component_count || 0)}<br>` +
			`${__("Applied / exceptions / stale")}: ${esc(result.applied || 0)} / ${esc(result.exceptions || 0)} / ${esc(result.stale || 0)}<br>` +
			`${__("QC requested / assigned / risk-accepted unassigned")}: ${esc(result.qc_requested || 0)} / ${esc(result.qc_assigned || 0)} / ${esc(result.qc_unassigned || 0)}</p>` +
			(itemRows ? `<div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr><th>${__("Signal")}</th><th>${__("Seed")}</th><th>${__("Records")}</th><th>${__("Outcome")}</th><th>${__("Reason")}</th></tr></thead><tbody>${itemRows}</tbody></table></div>` : "") +
			(result.error ? `<p class="text-danger">${esc(result.error)}</p>` : "") +
			(terminal ? "" : `<p>${__("The long worker is continuing independently. You may close this dialog or leave the page.")}</p>`) +
		`</div>`,
	);
}

function poll_automatic_overlap_progress(frm, batch, dialog) {
	frappe.call({
		method: "db_connector.api_automatic_overlap.get_automatic_overlap_batch_progress",
		args: { batch_name: batch },
		callback(response) {
			const result = response.message || {};
			update_automatic_overlap_progress(dialog, result);
			if (["Queued", "Applying"].includes(result.status)) {
				setTimeout(() => poll_automatic_overlap_progress(frm, batch, dialog), 4000);
				return;
			}
			frm.reload_doc();
		},
		error() {
			setTimeout(() => poll_automatic_overlap_progress(frm, batch, dialog), 4000);
		},
	});
}

function add_one_to_many_qc_capacity_control(frm) {
	frappe.call({
		method: "db_connector.api_tiered_one_to_many.get_one_to_many_qc_capacity_mode",
		callback(response) {
			const mode = response.message || {};
			const gate_required = mode.capacity_gate_required !== false;
			const action = gate_required
				? __("Accept Non-Blocking One-to-Many QC Risk")
				: __("Restore One-to-Many QC Capacity Gate");
			const phrase = gate_required
				? "ACCEPT NON-BLOCKING ONE-TO-MANY QC RISK"
				: "RESTORE GATED ONE-TO-MANY QC";
			frm.set_df_property(
				"tiered_one_to_many_qc_percent",
				"description",
				gate_required
					? __("Capacity-gated: the complete deterministic QC sample must fit before a bulk cycle can write identities.")
					: __("NON-BLOCKING RISK ACCEPTED: available QC is assigned best-effort; capacity shortage does not block reversible identity writes. Event: {0}", [mode.event || ""]),
			);
			frm.add_custom_button(action, () => frappe.prompt(
				[
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Management risk decision reason"), reqd: 1 },
					{ fieldname: "confirm_phrase", fieldtype: "Data", label: __("Type {0} exactly", [phrase]), reqd: 1 },
				],
				(values) => frappe.call({
					method: "db_connector.api_tiered_one_to_many.set_one_to_many_qc_capacity_gate",
					args: { enabled: gate_required ? 0 : 1, ...values },
					callback: () => frm.reload_doc(),
				}),
				action,
			), __("Tiered One-to-Many Automation"));
		},
	});
}

function render_tiered_one_to_many_research(result) {
	const sizes = Object.entries(result.within_limit_size_counts || {})
		.map(([size, count]) => `${esc(size)}: ${esc(count)}`)
		.join(", ") || __("None");
	const evidence = Object.entries(result.within_limit_evidence_band_counts || {})
		.map(([band, count]) => `${esc(band)}: ${esc(count)}`)
		.join("<br>") || __("None");
	const examples = (result.examples || []).map((item) => {
		const review = item.component_review
			? `<a href="/app/ccd-match-component-review/${encodeURIComponent(item.component_review)}">${esc(item.component_review)}</a>`
			: __("No review document");
		return `<li>${review} — ${esc(item.component_size)} ${__("records")}, ${esc(item.edge_count)} ${__("edges")}, ${esc(item.source_pattern)}${item.hard_trusted_identifier_conflict ? ` — <strong>${__("trusted identifier conflict")}</strong>` : ""}</li>`;
	}).join("");
	frappe.msgprint({
		title: __("Zero-write Tiered one-to-many research"),
		indicator: (result.hard_trusted_identifier_conflict_count || 0) ? "orange" : "blue",
		wide: true,
		message:
			`<p>${__("Canary")}: ${esc(result.run)} (${esc(result.run_status)})</p>` +
			`<p>${__("Configured maximum component size")}: ${esc(result.maximum_component_size)}</p>` +
			`<p>${__("All one-to-many components / edges / records")}: ${esc(result.total_component_count)} / ${esc(result.total_edge_count)} / ${esc(result.total_record_count)}</p>` +
			`<p>${__("Within limit components / edges / records")}: ${esc(result.within_limit_component_count)} / ${esc(result.within_limit_edge_count)} / ${esc(result.within_limit_record_count)}</p>` +
			`<p>${__("Within-limit component sizes")}: ${sizes}</p>` +
			`<p>${__("Above-limit components")}: ${esc(result.above_limit_component_count)}</p>` +
			`<p>${__("Unreviewed research candidates")}: ${esc(result.unreviewed_research_candidate_count)}</p>` +
			`<p>${__("Complete trusted-identifier conflicts")}: ${esc(result.hard_trusted_identifier_conflict_count)}</p>` +
			`<p><strong>${__("Evidence bands (research only)")}</strong><br>${evidence}</p>` +
			`<p>${esc(result.notice)}</p>` +
			(examples ? `<p><strong>${__("Deterministic examples")}</strong></p><ol>${examples}</ol>` : "") +
			`<p><strong>${__("No records were written.")}</strong></p>`,
	});
}

function add_unified_qc_workbench(frm) {
	frm.add_custom_button(
		__("Open Unified QC Workbench"),
		() => frappe.set_route("query-report", "CCD Unified QC Workbench"),
		__("Quality Control"),
	);
}

function add_splink_control(frm) {
	const desired = frm.doc.automatic_splink_enabled ? 0 : 1;
	const action = desired ? __("Enable Automatic Splink") : __("Disable Automatic Splink");
	const phrase = desired ? "ENABLE AUTOMATIC SPLINK" : "DISABLE AUTOMATIC SPLINK";
	frm.add_custom_button(action, () => frappe.prompt(
		[
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Control-change reason"), reqd: 1 },
			{ fieldname: "confirm_phrase", fieldtype: "Data", label: __("Type {0} exactly", [phrase]), reqd: 1 },
		],
		(values) => frappe.call({
			method: "db_connector.api_splink_automation.set_automatic_splink",
			args: { enabled: desired, ...values },
			callback: () => frm.reload_doc(),
		}),
		action,
	), __("Splink Automation"));
	const paused = !!frm.doc.splink_automation_paused;
	const breakerAction = paused ? __("Resume Splink Automation") : __("Pause Splink Automation");
	const breakerPhrase = paused ? "RESUME SPLINK AUTOMATION" : "PAUSE SPLINK AUTOMATION";
	frm.add_custom_button(breakerAction, () => frappe.prompt(
		[
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Circuit-breaker reason"), reqd: 1 },
			{ fieldname: "confirm_phrase", fieldtype: "Data", label: __("Type {0} exactly", [breakerPhrase]), reqd: 1 },
		],
		(values) => frappe.call({
			method: paused
				? "db_connector.api_splink_automation.resume_splink_automation"
				: "db_connector.api_splink_automation.pause_splink_automation",
			args: values,
			callback: () => frm.reload_doc(),
		}),
		breakerAction,
	), __("Splink Automation"));
	frm.add_custom_button(
		__("Preview Automatic Splink Run"),
		() => preview_automatic_splink(frm),
		__("Splink Automation"),
	);
	frm.add_custom_button(
		__("Preview Incomplete Ultra-High Components"),
		() => preview_incomplete_splink(frm),
		__("Splink Automation"),
	);
	const incompleteDesired = frm.doc.automatic_splink_incomplete_enabled ? 0 : 1;
	const incompleteAction = incompleteDesired
		? __("Enable Incomplete Ultra-High Automation")
		: __("Disable Incomplete Ultra-High Automation");
	const incompletePhrase = incompleteDesired
		? "ENABLE INCOMPLETE ULTRA HIGH SPLINK"
		: "DISABLE INCOMPLETE ULTRA HIGH SPLINK";
	frm.add_custom_button(incompleteAction, () => frappe.prompt(
		[
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Control-change reason"), reqd: 1 },
			{ fieldname: "confirm_phrase", fieldtype: "Data", label: __("Type {0} exactly", [incompletePhrase]), reqd: 1 },
		],
		(values) => frappe.call({
			method: "db_connector.api_splink_automation.set_incomplete_splink_automation",
			args: { enabled: incompleteDesired, ...values },
			callback: () => frm.reload_doc(),
		}),
		incompleteAction,
	), __("Splink Automation"));
	if (frm.doc.automatic_splink_enabled) {
		frm.add_custom_button(
			__("Run One Automatic Splink Cycle Now"),
			() => frappe.confirm(
				__("Queue one bounded automatic Splink cycle using the current component-size limit and fresh safety checks?"),
				() => run_automatic_splink_now(frm),
			),
			__("Splink Automation"),
		);
	}
}

function add_hybrid_automation_control(frm) {
	const field = frm.get_field("hybrid_automation_status_html");
	if (!field?.$wrapper) return;
	field.$wrapper.html(`<div class="text-muted">${__("Loading Development Hybrid automation controls…")}</div>`);
	frappe.call({
		method: "db_connector.api_hybrid_automation.get_hybrid_automation_dashboard",
		callback(response) {
			const result = response.message || {};
			render_hybrid_automation_dashboard(field.$wrapper, result);
			const latest = result.latest_approved_run || "";
			if (result.can_authorize && latest) {
				frm.add_custom_button(
					__("Authorize Approved Hybrid Evidence"),
					() => frappe.prompt(
						[
							{ fieldname: "reason", fieldtype: "Small Text", label: __("Development authorization reason"), reqd: 1 },
							{ fieldname: "confirm_run_name", fieldtype: "Data", label: __("Type the exact Hybrid Validation Run ID"), description: latest, reqd: 1 },
						],
						(values) => frappe.call({
							method: "db_connector.api_hybrid_automation.authorize_hybrid_development_automation",
							args: { run_name: latest, ...values },
							callback: () => frm.reload_doc(),
						}),
						__("Authorize Development Hybrid evidence"),
					),
					__("Development Hybrid Automation"),
				);
			}
			frm.add_custom_button(
				__("Preview Development Hybrid Cycle"),
				() => preview_automatic_hybrid(frm),
				__("Development Hybrid Automation"),
			);
			if (!result.authorized_run) return;
			const desired = result.enabled ? 0 : 1;
			const action = desired ? __("Enable Development Hybrid") : __("Disable Development Hybrid");
			const phrase = desired ? "ENABLE DEVELOPMENT HYBRID AUTOMATION" : "DISABLE DEVELOPMENT HYBRID AUTOMATION";
			frm.add_custom_button(action, () => prompt_hybrid_control(
				"db_connector.api_hybrid_automation.set_automatic_hybrid",
				action,
				phrase,
				{ enabled: desired },
				frm,
			), __("Development Hybrid Automation"));

			const paused = !!result.paused;
			const pauseAction = paused ? __("Resume Development Hybrid") : __("Pause Development Hybrid");
			const pausePhrase = paused ? "RESUME DEVELOPMENT HYBRID AUTOMATION" : "PAUSE DEVELOPMENT HYBRID AUTOMATION";
			frm.add_custom_button(pauseAction, () => prompt_hybrid_control(
				paused
					? "db_connector.api_hybrid_automation.resume_hybrid_automation"
					: "db_connector.api_hybrid_automation.pause_hybrid_automation",
				pauseAction,
				pausePhrase,
				{},
				frm,
			), __("Development Hybrid Automation"));

			if (result.enabled) {
				frm.add_custom_button(
					__("Run One Development Hybrid Cycle Now"),
					() => frappe.confirm(
						__("Queue one bounded Development-only Hybrid cycle? The background worker will repeat all current-state safety checks."),
						() => run_automatic_hybrid_now(frm),
					),
					__("Development Hybrid Automation"),
				);
				const scheduleDesired = result.scheduling_enabled ? 0 : 1;
				const scheduleAction = scheduleDesired
					? __("Enable Daily Development Hybrid")
					: __("Disable Daily Development Hybrid");
				const schedulePhrase = scheduleDesired
					? "ENABLE SCHEDULED DEVELOPMENT HYBRID"
					: "DISABLE SCHEDULED DEVELOPMENT HYBRID";
				frm.add_custom_button(scheduleAction, () => prompt_hybrid_control(
					"db_connector.api_hybrid_automation.set_hybrid_scheduling",
					scheduleAction,
					schedulePhrase,
					{ enabled: scheduleDesired },
					frm,
				), __("Development Hybrid Automation"));
			}
		},
	});
}

function prompt_hybrid_control(method, title, phrase, extraArgs, frm) {
	frappe.prompt(
		[
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Control-change reason"), reqd: 1 },
			{ fieldname: "confirm_phrase", fieldtype: "Data", label: __("Type {0} exactly", [phrase]), reqd: 1 },
		],
		(values) => frappe.call({
			method,
			args: { ...extraArgs, ...values },
			callback: () => frm.reload_doc(),
		}),
		title,
	);
}

function render_hybrid_automation_dashboard(wrapper, result) {
	if (!result.available) {
		wrapper.html(`<div class="alert alert-warning">${__("No approved Development Hybrid evidence is available.")}</div>`);
		return;
	}
	const blockers = result.authorization_blockers || [];
	const batch = result.last_batch
		? `<a href="/app/ccd-splink-automation-batch/${encodeURIComponent(result.last_batch)}">${esc(result.last_batch)}</a>`
		: __("None");
	wrapper.html(
		`<div class="alert alert-${blockers.length ? "warning" : "info"}">` +
			`<p><strong>${__("Development-only lower-score Hybrid automation")}</strong></p>` +
			`<p>${__("Latest approved evidence")}: ${esc(result.latest_approved_run || "None")}<br>` +
			`${__("Authorized run")}: ${esc(result.authorized_run || "None")}<br>` +
			`${__("Materialization control / daily schedule / paused")}: ${result.enabled ? __("Enabled") : __("Disabled")} / ${result.scheduling_enabled ? __("Enabled") : __("Disabled")} / ${result.paused ? __("Yes") : __("No")}</p>` +
			`<p>${__("Maximum isolated components per cycle")}: ${esc(result.component_limit || 1)}<br>` +
			`${__("Last status / batch")}: ${esc(result.last_status || "None")} / ${batch}</p>` +
			(blockers.length ? `<p class="text-danger">${__("Authorization blockers")}: ${blockers.map(esc).join("<br>")}</p>` : "") +
			(result.pause_reason ? `<p class="text-danger">${__("Pause reason")}: ${esc(result.pause_reason)}</p>` : "") +
			(result.last_error ? `<p class="text-danger">${__("Last error")}: ${esc(result.last_error)}</p>` : "") +
			`<p class="small">${__("This authorization is Development-only. It does not lower or replace the independent 0.999148140 ultra-high Splink authorization.")}</p>` +
		`</div>`,
	);
}

function preview_automatic_hybrid(frm) {
	frappe.call({
		method: "db_connector.api_hybrid_automation.preview_automatic_hybrid_cycle",
		freeze: true,
		callback(response) {
			const result = response.message || {};
			const blockers = result.operational_blockers || [];
			frappe.msgprint({
				title: __("Zero-write Development Hybrid preview"),
				indicator: blockers.length ? "orange" : "green",
				message: `<p>${__("Authorized run")}: ${esc(result.validation_run || "")}</p>` +
					`<p>${__("Frozen score range")}: ${esc(result.minimum_score || 0)} ≤ score &lt; ${esc(result.upper_cutoff || 0)}</p>` +
					`<p>${__("Per-cycle component limit")}: ${esc(result.component_limit || 0)}</p>` +
					`<p>${__("Eligible / selected isolated components")}: ${esc(result.eligible_component_count || 0)} / ${esc(result.selected_component_count || 0)}</p>` +
					`<p>${__("Selected by route")}: ${esc(format_map(result.selected_by_route))}</p>` +
					`<p>${__("Selected by score band")}: ${esc(format_map(result.selected_by_score_band))}</p>` +
					`<p>${__("Selected by source")}: ${esc(format_map(result.selected_by_source))}</p>` +
					`<p>${__("Selected candidates")}: ${(result.selected_candidates || []).map(esc).join(", ") || __("None")}</p>` +
					`<p>${__("Operational blockers")}: ${blockers.length ? blockers.map(esc).join("<br>") : __("None")}</p>` +
					`<p><strong>${__("No records were changed.")}</strong></p>`,
			});
		},
	});
}

function run_automatic_hybrid_now(frm) {
	if (frm.__ccd_hybrid_cycle_starting) return;
	frm.__ccd_hybrid_cycle_starting = true;
	frappe.call({
		method: "db_connector.api_hybrid_automation.run_automatic_hybrid_now",
		freeze: true,
		freeze_message: __("Selecting and queueing the bounded Development Hybrid batch…"),
		callback(response) {
			frm.__ccd_hybrid_cycle_starting = false;
			const result = response.message || {};
			const batch = result.batch || "";
			if (!batch) {
				frappe.msgprint({
					title: __("Development Hybrid cycle"),
					indicator: result.status === "No Eligible Work" ? "green" : "orange",
					message: `<p>${__("Status")}: ${esc(result.status || "")}</p>` +
						`<p>${__("Selected isolated components")}: ${esc(result.selected_component_count || result.selected_components || 0)}</p>`,
				});
				frm.reload_doc();
				return;
			}
			const progress = new frappe.ui.Dialog({
				title: __("Development Hybrid cycle"),
				fields: [{ fieldname: "hybrid_batch_status_html", fieldtype: "HTML" }],
			});
			progress.show();
			update_hybrid_batch_progress(progress, {
				batch,
				status: result.status || "Queued",
				selected_components: result.selected_component_count || result.selected_components || 0,
			});
			poll_hybrid_batch_status(frm, batch, progress);
		},
		error() {
			frm.__ccd_hybrid_cycle_starting = false;
		},
	});
}

function update_hybrid_batch_progress(dialog, result) {
	const field = dialog.get_field("hybrid_batch_status_html");
	if (!field?.$wrapper) return;
	const batch = result.batch || "";
	const batchLink = batch
		? `<a href="/app/ccd-splink-automation-batch/${encodeURIComponent(batch)}">${esc(batch)}</a>`
		: __("None");
	const terminal = ["Applied", "Applied with Exceptions", "Failed", "Stale", "Superseded"].includes(result.status);
	const alert = result.status === "Applied"
		? "success"
		: (["Failed", "Stale"].includes(result.status) ? "danger" : "info");
	field.$wrapper.html(
		`<div class="alert alert-${alert}">` +
			`<p><strong>${__("Status")}: ${esc(result.status || __("Unknown"))}</strong></p>` +
			`<p>${__("Batch")}: ${batchLink}<br>` +
			`${__("Selected isolated components")}: ${esc(result.selected_components || 0)}</p>` +
			(terminal
				? `<p>${__("Applied")}: ${esc(String(result.applied ?? 0))}<br>` +
					`${__("Exceptions")}: ${esc(String(result.exceptions ?? 0))}<br>` +
					`${__("Stale")}: ${esc(String(result.stale ?? 0))}</p>`
				: `<p>${result.status === "Applying" ? __("The long worker is applying component-atomic writes…") : __("Waiting for the long-queue worker…")}</p>`) +
			(result.error ? `<p class="text-danger">${esc(result.error)}</p>` : "") +
			`<p class="text-muted small">${__("The batch continues independently if you close this dialog or leave this page.")}</p>` +
		`</div>`,
	);
}

function poll_hybrid_batch_status(frm, batch, dialog) {
	frappe.call({
		method: "db_connector.api_hybrid_automation.get_hybrid_batch_status",
		args: { batch_name: batch },
		callback(response) {
			const result = response.message || {};
			update_hybrid_batch_progress(dialog, result);
			if (["Queued", "Applying"].includes(result.status)) {
				setTimeout(() => poll_hybrid_batch_status(frm, batch, dialog), 4000);
				return;
			}
			frm.reload_doc();
		},
		error() {
			update_hybrid_batch_progress(dialog, {
				batch,
				status: __("Status check interrupted; retrying"),
			});
			setTimeout(() => poll_hybrid_batch_status(frm, batch, dialog), 4000);
		},
	});
}

function format_map(values) {
	const entries = Object.entries(values || {});
	return entries.length ? entries.map(([key, value]) => `${key}: ${value}`).join(", ") : __("None");
}

function preview_automatic_splink(frm) {
	frappe.call({
		method: "db_connector.api_splink_automation.preview_automatic_splink_cycle",
		freeze: true,
		callback(response) {
			const result = response.message || {};
			const blockers = result.operational_blockers || [];
			frappe.msgprint({
				title: __("Zero-write automatic Splink preview"),
				indicator: blockers.length ? "orange" : "green",
				message: `<p>${__("Maximum component size")}: ${esc(result.maximum_component_size || 0)}</p>` +
					`<p>${__("Candidate edges")}: ${esc(result.candidate_count || 0)}</p>` +
					`<p>${__("Complete components available by size")}: ${esc(format_size_counts(result.available_complete_size_counts))}</p>` +
					`<p>${__("Incomplete automation enabled")}: ${result.incomplete_automation_enabled ? __("Yes") : __("No")}</p>` +
					`<p>${__("Corroborated incomplete components by size")}: ${esc(format_size_counts(result.corroborated_incomplete_size_counts))}</p>` +
					`<p>${__("Selected components by size")}: ${esc(format_size_counts(result.selected_size_counts))}</p>` +
					`<p>${__("Selected components / candidate edges")}: ${esc(result.selected_component_count || 0)} / ${esc(result.selected_candidate_count || 0)}</p>` +
					`<p>${__("Incomplete components excluded")}: ${esc(result.incomplete_component_count || 0)}</p>` +
					`<p>${__("Complete components above maximum excluded")}: ${esc(result.excluded_above_maximum_count || 0)}</p>` +
					`<p>${__("Operational blockers")}: ${blockers.length ? blockers.map(esc).join("<br>") : __("None")}</p>` +
					`<p><strong>${__("No records were changed.")}</strong></p>`,
			});
		},
	});
}

function preview_incomplete_splink(frm) {
	frappe.call({
		method: "db_connector.api_splink_automation.preview_incomplete_splink_components",
		freeze: true,
		callback(response) {
			const result = response.message || {};
			const blockers = result.blockers || [];
			frappe.msgprint({
				title: __("Zero-write incomplete ultra-high preview"),
				indicator: blockers.length ? "orange" : "green",
				message: `<p>${__("Rule version")}: ${esc(result.rule_version || "")}</p>` +
					`<p>${__("Automation enabled")}: ${result.enabled ? __("Yes") : __("No")}</p>` +
					`<p>${__("Maximum component size")}: ${esc(result.maximum_component_size || 0)}</p>` +
					`<p>${__("Incomplete components by size")}: ${esc(format_size_counts(result.incomplete_size_counts))}</p>` +
					`<p>${__("Eligible corroborated components by size")}: ${esc(format_size_counts(result.eligible_size_counts))}</p>` +
					`<p>${__("Eligible / excluded")}: ${esc(result.eligible_component_count || 0)} / ${esc(result.excluded_component_count || 0)}</p>` +
					`<p>${__("Exclusion reasons")}: ${esc(format_map(result.exclusion_reason_counts))}</p>` +
					`<p>${__("Blockers")}: ${blockers.length ? blockers.map(esc).join("<br>") : __("None")}</p>` +
					`<p>${esc(result.notice || "")}</p>` +
					`<p><strong>${__("No records were changed.")}</strong></p>`,
			});
		},
	});
}

function run_automatic_splink_now(frm) {
	if (frm.__ccd_splink_cycle_starting) return;
	frm.__ccd_splink_cycle_starting = true;
	frappe.call({
		method: "db_connector.api_splink_automation.run_automatic_splink_now",
		freeze: true,
		freeze_message: __("Selecting and queueing the bounded Splink batch…"),
		callback(response) {
			frm.__ccd_splink_cycle_starting = false;
			const result = response.message || {};
			const batch = result.batch || "";
			const batchLink = batch
				? `<a href="/app/ccd-splink-automation-batch/${encodeURIComponent(batch)}">${esc(batch)}</a>`
				: __("None");
			frappe.msgprint({
				title: __("Automatic Splink cycle"),
				indicator: result.status === "Queued" ? "green" : "orange",
				message: `<p>${__("Status")}: ${esc(result.status || "")}</p>` +
					`<p>${__("Batch")}: ${batchLink}</p>` +
					`<p>${__("Maximum component size")}: ${esc(result.maximum_component_size || 0)}</p>` +
					`<p>${__("Selected components by size")}: ${esc(format_size_counts(result.selected_size_counts))}</p>` +
					`<p>${__("Selected components")}: ${esc(result.selected_component_count || result.selected_components || 0)}</p>` +
					`<p class="text-muted">${__("A queued batch continues on the long worker after this dialog closes.")}</p>`,
			});
			frm.reload_doc();
		},
		error() {
			frm.__ccd_splink_cycle_starting = false;
		},
	});
}

function format_size_counts(counts) {
	const entries = Object.entries(counts || {});
	return entries.length
		? entries.map(([size, count]) => `${size}: ${count}`).join(", ")
		: __("None");
}

function add_hybrid_research_control(frm) {
	frm.add_custom_button(
		__("Preview Lower-Score Hybrid Research"),
		() => frappe.prompt(
			[
				hybrid_research_rule_field(),
				{
					fieldname: "minimum_score",
					fieldtype: "Float",
					label: __("Research minimum Splink score"),
					default: 0.95,
					reqd: 1,
					description: __("Zero-write only. Must remain below the frozen ultra-high cutoff."),
				},
			],
			(values) => preview_hybrid_research(values.minimum_score, values.rule_version),
			__("Lower-Score Hybrid Research"),
		),
		__("Splink Automation"),
	);
	frm.add_custom_button(
		__("Open Stratified Exploratory Sample"),
		() => frappe.prompt(
			[
				hybrid_research_rule_field(),
				{
					fieldname: "minimum_score",
					fieldtype: "Float",
					label: __("Research minimum Splink score"),
					default: 0.95,
					reqd: 1,
					description: __("Builds the fixed 100-pair, deterministic, zero-write sample."),
				},
			],
			(values) => open_hybrid_exploratory_sample(values.minimum_score, values.rule_version),
			__("Deterministic Hybrid Exploratory Sample"),
		),
		__("Splink Automation"),
	);
	frm.add_custom_button(
		__("Create Persistent Exploratory Review Batch"),
		() => frappe.prompt(
			[
				hybrid_research_rule_field(),
				{
					fieldname: "minimum_score",
					fieldtype: "Float",
					label: __("Research minimum Splink score"),
					default: 0.95,
					reqd: 1,
					description: __("Freezes the deterministic 100-pair sample for reopenable exploratory review."),
				},
			],
			(values) => frappe.confirm(
				__("Create or reopen the persistent exploratory batch? Labels remain separate from candidate and identity decisions."),
				() => start_hybrid_exploratory_batch(values.minimum_score, values.rule_version),
			),
			__("Persistent Hybrid Exploratory Review"),
		),
		__("Splink Automation"),
	);
}

function hybrid_research_rule_field() {
	return {
		fieldname: "rule_version",
		fieldtype: "Select",
		label: __("Development research rule"),
		options: "hybrid-name-support-v1\nhybrid-research-v1",
		default: "hybrid-name-support-v1",
		reqd: 1,
		description: __("Name-support v1 tests score plus name evidence only. Research v1 preserves the original stronger deterministic-corroboration routes."),
	};
}

function add_hybrid_holdout_control(frm) {
	const field = frm.get_field("hybrid_development_status_html");
	if (!field?.$wrapper) return;
	field.$wrapper.html(`<div class="text-muted">${__("Loading the latest approved Development hybrid evidence…")}</div>`);
	frappe.call({
		method: "db_connector.api_hybrid_validation.get_hybrid_holdout_dashboard",
		callback(response) {
			const result = response.message || {};
			render_hybrid_holdout_dashboard(field.$wrapper, result);
			if (!result.available) return;
			const run = result.run;
			frm.add_custom_button(
				__("Open Hybrid Validation Run"),
				() => frappe.set_route("Form", "CCD Match Evaluation Run", run),
				__("Development Hybrid Holdout"),
			);
			if (result.can_create) {
				frm.add_custom_button(
					__("Create Blinded Hybrid Holdout QC"),
					() => frappe.confirm(
						__("Use exactly the 50 already-reserved pairs and create persistent masked QC after current-state checks? No identity records will be written."),
						() => call_hybrid_holdout_action(frm, "start_hybrid_holdout_qc", run),
					),
					__("Development Hybrid Holdout"),
				);
			}
			if (result.can_review) {
				frm.add_custom_button(
					__("Review Next Hybrid Holdout Pair"),
					() => frappe.call({
						method: "db_connector.api_hybrid_validation.get_next_hybrid_holdout_pair",
						args: { run_name: run },
						callback(nextResponse) {
							const pair = nextResponse.message?.pair;
							if (pair) frappe.set_route("Form", "CCD Match Evaluation Pair", pair);
							else frappe.msgprint(__("You have no remaining ordinary holdout review."));
						},
					}),
					__("Development Hybrid Holdout"),
				);
				frm.add_custom_button(
					__("View Hybrid Holdout Pairs"),
					() => frappe.set_route("List", "CCD Match Evaluation Pair", {
						evaluation_run: run,
						automation_cohort: "Blinded Rollout Holdout",
					}),
					__("Development Hybrid Holdout"),
				);
				frm.add_custom_button(
					__("Finalize Hybrid Holdout QC"),
					() => call_hybrid_holdout_action(frm, "finalize_hybrid_holdout_qc", run),
					__("Development Hybrid Holdout"),
				);
			}
			if (result.can_retry) {
				frm.add_custom_button(
					__("Retry Hybrid Holdout Build"),
					() => call_hybrid_holdout_action(frm, "retry_hybrid_holdout_job", run),
					__("Development Hybrid Holdout"),
				);
			}
			if (result.can_decide) {
				for (const decision of ["Approved", "Rejected"]) {
					frm.add_custom_button(
						__(`${decision} Hybrid Holdout Evidence`),
						() => prompt_settings_hybrid_holdout_decision(frm, run, decision),
						__("Development Hybrid Holdout"),
					);
				}
			}
			if (result.status === "Building") {
				setTimeout(() => {
					if (window.cur_frm === frm) frm.reload_doc();
				}, 4000);
			}
		},
	});
}

function call_hybrid_holdout_action(frm, method, run) {
	frappe.call({
		method: `db_connector.api_hybrid_validation.${method}`,
		args: { run_name: run },
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}

function prompt_settings_hybrid_holdout_decision(frm, run, decision) {
	frappe.prompt(
		[
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Development holdout decision reason"), reqd: 1 },
			{ fieldname: "confirm_run_name", fieldtype: "Data", label: __("Type the exact Validation Run ID"), description: run, reqd: 1 },
		],
		(values) => frappe.call({
			method: "db_connector.api_hybrid_validation.record_hybrid_holdout_development_decision",
			args: { run_name: run, decision, ...values },
			callback: () => frm.reload_doc(),
		}),
		__(`${decision} Development holdout evidence`),
	);
}

function render_hybrid_holdout_dashboard(wrapper, result) {
	if (!result.available) {
		wrapper.html(`<div class="alert alert-warning">${esc(result.message || __("No approved Development hybrid validation exists."))}</div>`);
		return;
	}
	const holdout = result.metrics?.holdout || {};
	const combined = result.metrics?.combined_authorization_gate || {};
	const runLink = `<a href="/app/ccd-match-evaluation-run/${encodeURIComponent(result.run)}">${esc(result.run)}</a>`;
	wrapper.html(
		`<div class="alert alert-info">` +
			`<p><strong>${__("Development evidence only — zero identity writes")}</strong></p>` +
			`<p>${__("Validation run")}: ${runLink}<br>${__("Holdout status")}: ${esc(result.status)}<br>${__("Second Development decision")}: ${esc(result.second_approval_status)}</p>` +
			`<p>${__("Reserved / eligible / reviewed")}: ${esc(String(result.reserved_target ?? 0))} / ${esc(String(result.eligible ?? 0))} / ${esc(String(result.complete ?? 0))}<br>` +
			`${__("Same / Different / current-state exclusions")}: ${esc(String(result.same ?? 0))} / ${esc(String(result.different ?? 0))} / ${esc(String(result.ineligible ?? 0))}</p>` +
			(holdout.valid !== undefined
				? `<p>${__("Holdout precision / Wilson lower")}: ${(Number(holdout.precision || 0) * 100).toFixed(2)}% / ${(Number((holdout.wilson_95 || [0])[0]) * 100).toFixed(2)}%<br>` +
					`${__("Combined valid / Different / Wilson lower")}: ${esc(String(combined.valid ?? 0))} / ${esc(String(combined.different ?? 0))} / ${(Number((combined.wilson_95 || [0])[0]) * 100).toFixed(2)}%</p>`
				: "") +
			(result.error ? `<p class="text-danger">${esc(result.error)}</p>` : "") +
			`<p class="small">${__("Approval here records Development evidence only. It does not add an automation session, lower the 0.999148140 authorization, materialize identities, or authorize UAT/Production.")}</p>` +
		`</div>`,
	);
}

function start_hybrid_exploratory_batch(minimumScore, ruleVersion) {
	frappe.call({
		method: "db_connector.api_hybrid_exploratory_review.start_hybrid_exploratory_batch",
		args: { minimum_score: minimumScore, rule_version: ruleVersion },
		freeze: true,
		freeze_message: __("Creating the persistent exploratory request…"),
		callback(response) {
			const batch = response.message?.batch;
			if (batch) frappe.set_route("Form", "CCD Hybrid Exploratory Batch", batch);
		},
	});
}

function preview_hybrid_research(minimumScore, ruleVersion) {
	frappe.call({
		method: "db_connector.api_hybrid_research.preview_lower_score_hybrid_research",
		args: { minimum_score: minimumScore, rule_version: ruleVersion },
		freeze: true,
		freeze_message: __("Classifying lower-score pairs without writing identity state…"),
		callback(response) {
			const result = response.message || {};
			const routes = format_count_map(result.corroboration_route_counts);
			const birthdays = format_count_map(result.birthday_evidence_counts);
			const bands = format_count_map(result.score_band_counts);
			const exclusions = format_count_map(result.state_exclusion_counts);
			const examples = format_hybrid_examples(result.examples_by_route);
			frappe.msgprint({
				title: __("Zero-write lower-score hybrid research"),
				indicator: "blue",
				wide: true,
				message: `<p>${__("Queue")}: ${esc(result.queue_run || "")}</p>` +
					`<p>${__("Frozen research rule")}: ${esc(result.rule_version || "")}</p>` +
					`<p>${__("Research score range")}: ${esc(result.research_minimum_score)} ≤ score &lt; ${esc(result.frozen_ultra_high_cutoff)}</p>` +
					`<p>${__("Scanned / corroborated / untouched edges")}: ${esc(result.scanned_candidate_count || 0)} / ${esc(result.corroborated_candidate_count || 0)} / ${esc(result.untouched_corroborated_edge_count || 0)}</p>` +
					`<p>${__("Isolated pairs / operationally clear isolated pairs")}: ${esc(result.isolated_pair_count || 0)} / ${esc(result.operationally_clear_isolated_pair_count || 0)}</p>` +
					`<p>${__("Incomplete or multi-record components")}: ${esc(result.incomplete_or_multi_record_component_count || 0)}</p>` +
					`<p><strong>${__("Corroboration routes")}</strong><br>${routes}</p>` +
					`<p><strong>${__("Birthday evidence")}</strong><br>${birthdays}</p>` +
					`<p><strong>${__("Score bands")}</strong><br>${bands}</p>` +
					`<p><strong>${__("Excluded states")}</strong><br>${exclusions}</p>` +
					`${examples}` +
					`<p class="text-muted"><strong>${__("Research only: no candidate, authorization, automation control, or identity record was changed.")}</strong></p>`,
			});
		},
	});
}

function format_count_map(counts) {
	const entries = Object.entries(counts || {});
	return entries.length
		? entries.map(([key, count]) => `${esc(key)}: ${esc(count)}`).join("<br>")
		: __("None");
}

function format_hybrid_examples(examplesByRoute) {
	const entries = Object.entries(examplesByRoute || {});
	if (!entries.length) return `<p><strong>${__("Examples")}</strong><br>${__("None")}</p>`;
	const rows = entries.map(([route, examples]) => {
		const links = (examples || []).map((item) =>
			`<a href="/app/ccd-match-review-candidate/${encodeURIComponent(item.candidate)}">${esc(item.candidate)}</a> (${esc(item.score)})`
		).join(", ");
		return `${esc(route)}: ${links || __("None")}`;
	});
	return `<p><strong>${__("Highest-score operationally clear examples")}</strong><br>${rows.join("<br>")}</p>`;
}

function open_hybrid_exploratory_sample(minimumScore, ruleVersion) {
	frappe.call({
		method: "db_connector.api_hybrid_research.preview_lower_score_hybrid_research",
		args: {
			minimum_score: minimumScore,
			exploratory_sample_size: 100,
			rule_version: ruleVersion,
		},
		freeze: true,
		freeze_message: __("Building the deterministic stratified sample without writing candidate state…"),
		callback(response) {
			show_hybrid_exploratory_sample(response.message || {});
		},
	});
}

function show_hybrid_exploratory_sample(result) {
	const sample = result.exploratory_sample || {};
	const rows = sample.rows || [];
	if (!rows.length) {
		frappe.msgprint({
			title: __("No exploratory sample available"),
			indicator: "orange",
			message: __("No operationally clear corroborated pair is available at this score floor."),
		});
		return;
	}
	const pageSize = 20;
	let page = 0;
	const dialog = new frappe.ui.Dialog({
		title: __("Deterministic Lower-Score Hybrid Sample"),
		size: "extra-large",
		fields: [
			{ fieldname: "sample_summary", fieldtype: "HTML" },
			{ fieldname: "sample_rows", fieldtype: "HTML" },
		],
	});
	const summary = dialog.get_field("sample_summary").$wrapper;
	summary.html(
		`<div class="alert alert-blue">` +
			`<p><strong>${__("Exploratory only")}</strong>: ${__("opening or downloading this sample changes no candidate, review, authorization, or identity state.")}</p>` +
			`<p>${__("Queue / score range")}: ${esc(result.queue_run || "")} — ${esc(result.research_minimum_score)} ≤ score &lt; ${esc(result.frozen_ultra_high_cutoff)}</p>` +
			`<p>${__("Selected / eligible / target")}: ${esc(sample.selected_size || 0)} / ${esc(sample.population_size || 0)} / ${esc(sample.target_size || 0)}</p>` +
			`<p>${__("Rare routes included in full")}: ${esc((sample.rare_routes_included_in_full || []).join(", ") || __("None"))}</p>` +
			`<p><strong>${__("Sample route coverage")}</strong> ${__("(one pair may satisfy more than one route)")}<br>${format_count_map(sample.route_counts)}</p>` +
			`<p><strong>${__("Score bands")}</strong><br>${format_count_map(sample.score_band_counts)}</p>` +
			`<p><strong>${__("Source pairs")}</strong><br>${format_count_map(sample.source_pair_counts)}</p>` +
			`<p><strong>${__("Birthday evidence")}</strong><br>${format_count_map(sample.birthday_evidence_counts)}</p>` +
			`<p class="small text-muted" style="overflow-wrap:anywhere">${__("Rule version")}: ${esc(sample.rule_version || "")}<br>` +
			`${__("Derived seed")}: ${esc(sample.derived_seed || "")}<br>` +
			`${__("Eligible manifest digest")}: ${esc(sample.eligible_manifest_digest || "")}</p>` +
			`<p><strong>${__("Open candidates in new tabs for exploratory inspection. These inspections cannot be counted as prospective validation.")}</strong></p>` +
		`</div>`,
	);

	function render_page() {
		const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
		page = Math.max(0, Math.min(page, pageCount - 1));
		const visible = rows.slice(page * pageSize, (page + 1) * pageSize);
		const tableRows = visible.map((item) =>
			`<tr>` +
				`<td>${esc(item.sample_order)}</td>` +
				`<td><a target="_blank" rel="noopener" href="/app/ccd-match-review-candidate/${encodeURIComponent(item.candidate)}">${esc(item.candidate)}</a></td>` +
				`<td>${esc(item.primary_route)}</td>` +
				`<td>${esc(item.score)}</td>` +
				`<td>${esc(item.score_band)}</td>` +
				`<td>${esc(item.source_pair)}</td>` +
				`<td>${esc(item.birthday_status)}</td>` +
			`</tr>`
		).join("");
		const wrapper = dialog.get_field("sample_rows").$wrapper;
		wrapper.html(
			`<div class="mb-3">` +
				`<button class="btn btn-xs btn-default hybrid-sample-prev" ${page === 0 ? "disabled" : ""}>${__("Previous")}</button> ` +
				`<span>${__("Page")} ${page + 1} / ${pageCount}</span> ` +
				`<button class="btn btn-xs btn-default hybrid-sample-next" ${page + 1 >= pageCount ? "disabled" : ""}>${__("Next")}</button> ` +
				`<button class="btn btn-xs btn-default hybrid-sample-download">${__("Download CSV")}</button>` +
			`</div>` +
			`<div style="max-height:55vh;overflow:auto">` +
				`<table class="table table-bordered table-condensed">` +
					`<thead><tr><th>#</th><th>${__("Candidate")}</th><th>${__("Primary route")}</th><th>${__("Score")}</th><th>${__("Score band")}</th><th>${__("Source pair")}</th><th>${__("Birthday")}</th></tr></thead>` +
					`<tbody>${tableRows}</tbody>` +
				`</table>` +
			`</div>`,
		);
		wrapper.find(".hybrid-sample-prev").on("click", () => { page -= 1; render_page(); });
		wrapper.find(".hybrid-sample-next").on("click", () => { page += 1; render_page(); });
		wrapper.find(".hybrid-sample-download").on("click", () => download_hybrid_sample_csv(result, sample));
	}
	dialog.show();
	render_page();
}

function download_hybrid_sample_csv(result, sample) {
	const headers = ["sample_order", "candidate", "left_record", "right_record", "primary_route", "all_routes", "score", "score_band", "source_pair", "birthday_status", "queue_run", "rule_version", "derived_seed", "eligible_manifest_digest"];
	const lines = [headers.map(hybrid_csv_cell).join(",")];
	for (const row of (sample.rows || [])) {
		lines.push([
			row.sample_order,
			row.candidate,
			row.left_record,
			row.right_record,
			row.primary_route,
			(row.routes || []).join("|"),
			row.score,
			row.score_band,
			row.source_pair,
			row.birthday_status,
			result.queue_run,
			sample.rule_version,
			sample.derived_seed,
			sample.eligible_manifest_digest,
		].map(hybrid_csv_cell).join(","));
	}
	const blob = new Blob([`\ufeff${lines.join("\r\n")}`], { type: "text/csv;charset=utf-8" });
	const url = URL.createObjectURL(blob);
	const anchor = document.createElement("a");
	anchor.href = url;
	anchor.download = `hybrid-exploratory-sample-${result.queue_run || "queue"}.csv`;
	document.body.appendChild(anchor);
	anchor.click();
	anchor.remove();
	URL.revokeObjectURL(url);
}

function hybrid_csv_cell(value) {
	let text = String(value ?? "");
	if (/^[=+\-@]/.test(text)) text = `'${text}`;
	return `"${text.replaceAll('"', '""')}"`;
}

function add_unified_person_control(frm) {
	frm.add_custom_button(
		__("Unified Person Reconciliation / Status"),
		() => frappe.call({
			method: "db_connector.api_unified_person.get_unified_person_backfill_status",
			freeze: true,
			callback(response) {
				show_unified_person_status(frm, response.message || {});
			},
		}),
		__("Identity Integrity"),
	);
	frm.add_custom_button(
		__("Audit Unified Person Integrity"),
		() => frappe.call({
			method: "db_connector.api_unified_person.get_unified_person_integrity_report",
			freeze: true,
			callback(response) {
				const result = response.message || {};
				const rows = Object.entries(result.active_issue_counts || {})
					.map(([key, value]) => `<tr><td>${esc(key.replaceAll("_", " "))}</td><td>${esc(value)}</td></tr>`)
					.join("");
				frappe.msgprint({
					title: __("Unified Person integrity audit"),
					indicator: result.active_issue_count ? "orange" : "green",
					message: `<p><b>${__("Active issues")}</b>: ${esc(result.active_issue_count || 0)}<br>` +
						`<b>${__("Issued people")}</b>: ${esc(result.unified_person_count || 0)}<br>` +
						`<b>${__("Active memberships")}</b>: ${esc(result.active_membership_count || 0)}</p>` +
						`<table class="table table-bordered"><tbody>${rows}</tbody></table>` +
						`<p><strong>${__("This audit changed no records.")}</strong></p>`,
				});
			},
		}),
		__("Identity Integrity"),
	);
}

function show_unified_person_status(frm, status) {
	if (status.status === "Running" || status.status === "Failed") {
		show_unified_person_progress(frm, status);
		return;
	}
	if (status.status === "Catch-up Required") {
		frappe.call({
			method: "db_connector.api_unified_person.preview_unified_person_backfill",
			freeze: true,
			callback(response) {
				show_unified_person_preview(frm, response.message || {});
			},
		});
		return;
	}
	if (status.status === "Completed") {
		const result = status.result || status;
		frappe.msgprint({
			title: __("Unified Person backfill completed"),
			indicator: result.integrity_active_issue_count ? "orange" : "green",
			message: `<p>${__("Backfill run")}: ${esc(status.backfill_run)}</p>` +
				`<p>${__("Issued people")}: ${esc(result.issued_person_count || status.issued_person_count || 0)}</p>` +
				`<p>${__("Active memberships")}: ${esc(result.active_membership_count || status.created_membership_count || 0)}</p>` +
				`<p>${__("CCD Master modified unchanged")}: ${result.ccd_master_modified_unchanged ? __("Yes") : __("No")}</p>`,
		});
		return;
	}
	frappe.call({
		method: "db_connector.api_unified_person.preview_unified_person_backfill",
		freeze: true,
		callback(response) {
			show_unified_person_preview(frm, response.message || {});
		},
	});
}

function unified_person_run_link(run_name) {
	if (!run_name) return "";
	const href = `/app/ccd-unified-person-backfill-run/${encodeURIComponent(run_name)}`;
	return `<a href="${href}" target="_blank" rel="noopener">${esc(run_name)}</a>`;
}

function show_unified_person_progress(frm, initial_status) {
	let timer = null;
	const dialog = new frappe.ui.Dialog({
		title: __("Unified Person reconciliation progress"),
		fields: [{ fieldname: "progress_html", fieldtype: "HTML" }],
		primary_action_label: __("Queue / Resume in Background"),
		primary_action() {
			const run_name = dialog.__status?.backfill_run;
			if (!run_name) return;
			frappe.call({
				method: "db_connector.api_unified_person.enqueue_unified_person_reconciliation",
				args: { backfill_run: run_name, max_batches: 25 },
				freeze: true,
				callback() {
					refresh_status();
					if (!timer) timer = setInterval(refresh_status, 3000);
				},
			});
		},
	});

	function render(status) {
		dialog.__status = status;
		const indicator = status.status === "Completed"
			? "alert-success"
			: (status.status === "Failed" ? "alert-danger" : "alert-info");
		const queued = status.background_job_enqueued
			? __("Running on the long worker")
			: __("Not currently queued; use Queue / Resume in Background");
		dialog.fields_dict.progress_html.$wrapper.html(
			`<div class="alert ${indicator}">` +
			`<p><b>${__("Run")}</b>: ${unified_person_run_link(status.backfill_run)}<br>` +
			`<b>${__("Purpose")}</b>: ${esc(status.run_purpose || "Catch-up Reconciliation")}<br>` +
			`<b>${__("Status")}</b>: ${esc(status.status || "")}<br>` +
			`<b>${__("Worker")}</b>: ${esc(queued)}</p>` +
			`<p><b>${__("Remaining records")}</b>: ${esc(status.remaining_record_count || 0)}<br>` +
			`<b>${__("Processed / active memberships")}</b>: ${esc(status.processed_record_count || 0)}<br>` +
			`<b>${__("Committed batches")}</b>: ${esc(status.batch_count || 0)}<br>` +
			`<b>${__("Last committed batch")}</b>: ${esc(status.last_batch_at || "—")}</p>` +
			(status.error_summary ? `<pre class="small text-danger">${esc(status.error_summary)}</pre>` : "") +
			`<p class="text-muted">${__("The background job is restartable and continues if this dialog or page is closed.")}</p></div>`,
		);
		if (status.status === "Completed") {
			dialog.get_primary_btn().hide();
			if (timer) clearInterval(timer);
			timer = null;
		} else {
			dialog.get_primary_btn().show();
		}
	}

	function refresh_status() {
		frappe.call({
			method: "db_connector.api_unified_person.get_unified_person_backfill_status",
			callback(response) {
				render(response.message || {});
			},
		});
	}

	dialog.$wrapper.on("hidden.bs.modal", () => {
		if (timer) clearInterval(timer);
		timer = null;
		frm.reload_doc();
	});
	dialog.show();
	render(initial_status || {});
	if (initial_status?.background_job_enqueued) timer = setInterval(refresh_status, 3000);
}

function show_unified_person_preview(frm, preview) {
	const source_rows = (preview.missing_source_counts || [])
		.map((row) => `<tr><td>${esc(row.ccd_reg_source || "(blank)")}</td><td>${esc(row.missing_record_count || 0)}</td></tr>`)
		.join("");
	const dialog = new frappe.ui.Dialog({
		title: __("Zero-write Unified Person reconciliation preview"),
		fields: [
			{
				fieldname: "summary",
				fieldtype: "HTML",
				options: `<div class="alert alert-info"><p><b>${__("CCD Masters")}</b>: ${esc(preview.ccd_master_count || 0)}<br>` +
					`<b>${__("Existing active memberships")}</b>: ${esc(preview.existing_active_membership_count || 0)}<br>` +
					`<b>${__("Missing assignments to reconcile")}</b>: ${esc(preview.remaining_ccd_master_count || 0)}</p>` +
					(source_rows ? `<table class="table table-bordered"><thead><tr><th>${__("CCD Registration Source")}</th><th>${__("Missing")}</th></tr></thead><tbody>${source_rows}</tbody></table>` : "") +
					`<p><b>${__("Frozen scope fingerprint")}</b>:<br><code>${esc(preview.scope_fingerprint)}</code></p>` +
					`<p><strong>${__("This preview changed no records.")}</strong></p></div>`,
			},
			{ fieldname: "batch_size", fieldtype: "Int", label: __("Records per committed batch"), default: 5000, reqd: 1 },
			{ fieldname: "confirm_scope_fingerprint", fieldtype: "Data", label: __("Type the exact scope fingerprint to confirm"), reqd: 1 },
		],
		primary_action_label: __("Start Background Reconciliation"),
		primary_action(values) {
			if (values.confirm_scope_fingerprint !== preview.scope_fingerprint) {
				frappe.msgprint(__("The confirmation must exactly match the preview fingerprint."));
				return;
			}
			frappe.call({
				method: "db_connector.api_unified_person.start_unified_person_backfill",
				args: values,
				freeze: true,
				callback(response) {
					const result = response.message || {};
					if (result.status === "Completed") {
						dialog.hide();
						frappe.msgprint(__("No Unified Person assignments remain to reconcile."));
						return;
					}
					frappe.call({
						method: "db_connector.api_unified_person.enqueue_unified_person_reconciliation",
						args: { backfill_run: result.backfill_run, max_batches: 25 },
						freeze: true,
						callback() {
							dialog.hide();
							frappe.call({
								method: "db_connector.api_unified_person.get_unified_person_backfill_status",
								callback(status_response) {
									show_unified_person_progress(frm, status_response.message || result);
								},
							});
						},
					});
				},
			});
		},
	});
	dialog.show();
}

function add_notification_control(frm) {
	if (!frm.doc.automation_notifications_enabled) return;
	frm.add_custom_button(
		__("Send Test Notification"),
		() => frappe.call({
			method: "db_connector.api_identity_notifications.send_test_automation_notification",
			freeze: true,
			callback(response) {
				const result = response.message || {};
				frappe.msgprint({
					title: __("Automation email notification"),
					indicator: result.status === "Queued" ? "green" : "orange",
					message: `<p>${__("Status")}: ${esc(result.status)}</p>` +
						`<p>${__("Recipients")}: ${esc(result.recipient_count || 0)}</p>`,
				});
			},
		}),
		__("Automation"),
	);
}

function add_integrity_control(frm) {
	frm.add_custom_button(
		__("Preview Orphan Lifecycle Repair"),
		() => frappe.call({
			method: "db_connector.api_identity_retirement.preview_orphan_retirement",
			freeze: true,
			callback(response) {
				show_orphan_repair_preview(frm, response.message || {});
			},
		}),
		__("Identity Integrity"),
	);
}

function show_orphan_repair_preview(frm, result) {
	const counts = result.active_issue_counts || {};
	const enabled = Object.entries(result.controls_enabled || {})
		.filter(([, value]) => value)
		.map(([name]) => name);
	const countRows = Object.entries(counts)
		.map(([name, value]) => `<tr><td>${esc(name.replaceAll("_", " "))}</td><td>${esc(value)}</td></tr>`)
		.join("");
	const dialog = new frappe.ui.Dialog({
		title: __("Zero-write orphan lifecycle preview"),
		fields: [
			{
				fieldname: "summary",
				fieldtype: "HTML",
				options:
					`<div class="alert ${result.active_issue_count ? "alert-warning" : "alert-success"}">` +
					`<p><b>${__("Missing CCD Masters")}</b>: ${esc(result.missing_ccd_master_count || 0)}<br>` +
					`<b>${__("Active integrity issues")}</b>: ${esc(result.active_issue_count || 0)}<br>` +
					`<b>${__("Planned writes")}</b>: ${esc(result.planned_write_count || 0)}</p>` +
					`<p><b>${__("Frozen scope fingerprint")}</b>:<br><code>${esc(result.scope_fingerprint)}</code></p>` +
					`<p><b>${__("Enabled safety controls")}</b>: ${enabled.length ? enabled.map(esc).join(", ") : __("None")}</p>` +
					`<p><strong>${__("This preview changed no records.")}</strong></p></div>` +
					`<table class="table table-bordered"><thead><tr><th>${__("Lifecycle action")}</th><th>${__("Count")}</th></tr></thead><tbody>${countRows}</tbody></table>`,
			},
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Repair reason"), reqd: 1 },
			{
				fieldname: "confirm_scope_fingerprint",
				fieldtype: "Data",
				label: __("Type the exact scope fingerprint to confirm"),
				reqd: 1,
			},
		],
		primary_action_label: __("Apply Audited Lifecycle Repair"),
		primary_action(values) {
			if (values.confirm_scope_fingerprint !== result.scope_fingerprint) {
				frappe.msgprint(__("The confirmation must exactly match the preview fingerprint."));
				return;
			}
			if (enabled.length) {
				frappe.msgprint(__("Disable all materialization and automation controls first."));
				return;
			}
			frappe.call({
				method: "db_connector.api_identity_retirement.apply_orphan_retirement",
				args: values,
				freeze: true,
				callback(response) {
					dialog.hide();
					const applied = response.message || {};
					frappe.msgprint(__("Retirement run {0}: {1}", [applied.retirement_run || "", applied.status || ""]));
					frm.reload_doc();
				},
			});
		},
	});
	if (!result.planned_write_count) dialog.get_primary_btn().prop("disabled", true);
	dialog.show();
}

function esc(value) {
	return frappe.utils.escape_html(String(value || ""));
}

function confirm_control(frm, title, method, args, enabled) {
	frappe.prompt(
		[
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
			{
				fieldname: "confirm_settings_name",
				fieldtype: "Data",
				label: __("Type the exact Settings ID to confirm"),
				description: frm.doc.name,
				reqd: 1,
			},
		],
		(values) => {
			const requestArgs = { ...args, ...values };
			if (enabled !== null) requestArgs.enabled = enabled ? 1 : 0;
			frappe.call({
				method,
				args: requestArgs,
				freeze: true,
				callback: () => frm.reload_doc(),
			});
		},
		title,
	);
}

function add_qc_control(frm) {
	const enabled = Boolean(frm.doc.automatic_qc_assignment_enabled);
	frm.add_custom_button(
		enabled ? __("Stop Automatic QC Assignment") : __("Enable Automatic QC Assignment"),
		() => confirm_control(
			frm,
			enabled ? __("Stop automatic QC assignment") : __("Enable automatic QC assignment"),
			"db_connector.api_identity_qc.set_automatic_qc_assignment",
			{},
			!enabled,
		),
		__("Automation"),
	);
}

function add_tiered_control(frm) {
	const enabled = Boolean(frm.doc.automatic_tiered_enabled);
	frm.add_custom_button(
		enabled ? __("Stop Automatic Tiered") : __("Enable Automatic Tiered"),
		() => confirm_control(
			frm,
			enabled ? __("Stop automatic Tiered materialization") : __("Enable automatic Tiered materialization"),
			"db_connector.api_identity_automation.set_automatic_tiered_enabled",
			{},
			!enabled,
		),
		__("Automation"),
	);
}

function add_breaker_control(frm) {
	if (!frm.doc.automation_paused) {
		frm.add_custom_button(
			__("Emergency Pause Tiered"),
			() => confirm_control(
				frm,
				__("Emergency pause Tiered automation"),
				"db_connector.api_identity_qc.pause_tiered_automation",
				{},
				null,
			),
			__("Automation"),
		);
		return;
	}
	frm.add_custom_button(__("Preview Governed Resume"), () => {
		frappe.call({
			method: "db_connector.api_identity_qc.preview_resume_tiered_automation",
			callback(response) {
				const result = response.message || {};
				const blockers = result.blockers || [];
				frappe.msgprint({
					title: __("Zero-write governed resume preview"),
					indicator: blockers.length ? "orange" : "green",
					message: `<p>${__("Paused scope")}: ${esc(result.pause_scope)}</p>` +
						`<p>${__("Pause reason")}: ${esc(result.pause_reason)}</p>` +
						`<p>${__("Blockers")}: ${blockers.length ? blockers.map(esc).join("<br>") : __("None")}</p>` +
						`<p><strong>${__("No records were changed.")}</strong></p>`,
				});
				if (!blockers.length) {
					confirm_control(
						frm,
						__("Resume Tiered automation"),
						"db_connector.api_identity_qc.resume_tiered_automation",
						{},
						null,
					);
				}
			},
		});
	}, __("Automation"));
}

function preview_automatic(frm) {
	frappe.call({
		method: "db_connector.api_identity_automation.preview_automatic_tiered_run",
		freeze: true,
		callback(response) {
			const result = response.message || {};
			frappe.msgprint({
				title: __("Zero-write automatic Tiered preview"),
				indicator: (result.operational_blockers || []).length ? "orange" : "green",
				message: `<p>${__("Complete components")}: ${result.selected_component_count || 0}</p>` +
					`<p>${__("Recommendations")}: ${result.selected_recommendation_count || 0}</p>` +
					`<p>${__("Planned groups / memberships")}: ${result.planned_identity_group_count || 0} / ${result.planned_membership_count || 0}</p>` +
					`<p>${__("Unsafe components skipped")}: ${result.skipped_unsafe_component_count || 0}</p>` +
					`<p>${__("Operational blockers")}: ${(result.operational_blockers || []).length ? result.operational_blockers.map(esc).join("<br>") : __("None")}</p>` +
					`<p><strong>${__("No records were written.")}</strong></p>`,
			});
		},
	});
}

function start_automatic_tiered_cycle(frm) {
	if (frm.__ccd_automatic_cycle_starting) return;
	frm.__ccd_automatic_cycle_starting = true;
	const progress = new frappe.ui.Dialog({
		title: __("Running Automatic Tiered Cycle"),
		fields: [{ fieldname: "operation_status_html", fieldtype: "HTML" }],
	});
	progress.show();
	update_automatic_cycle_progress(progress, __("Queueing the governed cycle…"));
	frappe.call({
		method: "db_connector.api_identity_automation.start_automatic_tiered_cycle",
		callback(response) {
			frm.__ccd_automatic_cycle_starting = false;
			const token = response.message?.operation_token;
			if (!token) {
				progress.hide();
				frappe.msgprint(__("No operation token was returned. Check the last automatic status before retrying."));
				return;
			}
			poll_automatic_tiered_cycle(frm, token, progress);
		},
		error() {
			frm.__ccd_automatic_cycle_starting = false;
			progress.hide();
		},
	});
}

function update_automatic_cycle_progress(dialog, message) {
	const field = dialog.get_field("operation_status_html");
	if (!field?.$wrapper) return;
	field.$wrapper.html(
		`<p>${esc(message)}</p>`
		+ `<p class="text-muted small">${__("The cycle continues on the long queue even if you close this dialog. Its configured safety checks and control revision still apply.")}</p>`,
	);
}

function poll_automatic_tiered_cycle(frm, operationToken, dialog) {
	frappe.call({
		method: "db_connector.api_identity_automation.get_automatic_tiered_operation",
		args: { operation_token: operationToken },
		callback(response) {
			const operation = response.message || {};
			if (["Queued", "Running"].includes(operation.status)) {
				update_automatic_cycle_progress(
					dialog,
					operation.status === "Queued"
						? __("Waiting for the long-queue worker…")
						: __("Selecting, validating, and applying the bounded cycle…"),
				);
				setTimeout(() => poll_automatic_tiered_cycle(frm, operationToken, dialog), 4000);
				return;
			}
			dialog.hide();
			if (operation.status === "Completed") {
				show_result(operation.result || {});
				frm.reload_doc();
				return;
			}
			frappe.msgprint({
				title: operation.status === "Failed" ? __("Automatic Tiered cycle failed") : __("Automatic Tiered result unavailable"),
				indicator: operation.status === "Failed" ? "red" : "orange",
				message: esc(operation.error || __("Check the last automatic status and batch before retrying.")),
			});
			frm.reload_doc();
		},
		error() {
			update_automatic_cycle_progress(dialog, __("The status check was interrupted. Retrying…"));
			setTimeout(() => poll_automatic_tiered_cycle(frm, operationToken, dialog), 4000);
		},
	});
}

function show_result(result) {
	frappe.msgprint({
		title: __("Automatic Tiered cycle"),
		indicator: result.status === "Applied" ? "green" : "orange",
		message: `<p>${__("Status")}: ${esc(result.status)}</p>` +
			`<p>${__("Batch")}: ${esc(result.batch)}</p>` +
			`<p>${__("Groups / Memberships")}: ${result.created_groups || 0} / ${result.created_memberships || 0}</p>`,
	});
}

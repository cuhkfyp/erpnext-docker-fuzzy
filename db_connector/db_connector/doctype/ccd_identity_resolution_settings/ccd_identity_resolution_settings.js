frappe.ui.form.on("CCD Identity Resolution Settings", {
	refresh(frm) {
		if (!frappe.user.has_role("System Manager")) return;
		add_qc_control(frm);
		add_tiered_control(frm);
		add_breaker_control(frm);
		add_integrity_control(frm);
		add_unified_person_control(frm);
		add_notification_control(frm);
		frm.add_custom_button(__("Preview Automatic Tiered Run"), () => preview_automatic(frm), __("Automation"));
		if (frm.doc.automatic_tiered_enabled) {
			frm.add_custom_button(
				__("Run One Automatic Cycle Now"),
				() => frappe.confirm(
					__("Run one bounded automatic Tiered cycle using the current limits and fresh safety checks?"),
					() => frappe.call({
						method: "db_connector.api_identity_automation.run_automatic_tiered_now",
						freeze: true,
						callback(response) {
							show_result(response.message || {});
							frm.reload_doc();
						},
					}),
				),
				__("Automation"),
			);
		}
	},
});

function add_unified_person_control(frm) {
	frm.add_custom_button(
		__("Unified Person Backfill / Status"),
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
	if (status.status === "Running") {
		frappe.confirm(
			__("Backfill {0} is running. Process the next 10 bounded batches now? Remaining records: {1}.", [status.backfill_run, status.remaining_record_count || 0]),
			() => frappe.call({
				method: "db_connector.api_unified_person.run_unified_person_backfill_batches",
				args: { backfill_run: status.backfill_run, max_batches: 10 },
				freeze: true,
				callback(response) {
					const result = response.message || {};
					frappe.msgprint({
						title: __("Unified Person backfill"),
						indicator: result.status === "Completed" ? "green" : "blue",
						message: `<p>${__("Status")}: ${esc(result.status)}</p>` +
							`<p>${__("Remaining records")}: ${esc(result.remaining_record_count || 0)}</p>` +
							`<p>${__("Committed batches")}: ${esc(result.batch_count || 0)}</p>`,
					});
					frm.reload_doc();
				},
			}),
		);
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

function show_unified_person_preview(frm, preview) {
	const dialog = new frappe.ui.Dialog({
		title: __("Zero-write Unified Person backfill preview"),
		fields: [
			{
				fieldname: "summary",
				fieldtype: "HTML",
				options: `<div class="alert alert-info"><p><b>${__("CCD Masters")}</b>: ${esc(preview.ccd_master_count || 0)}<br>` +
					`<b>${__("Expected Unified People")}</b>: ${esc(preview.expected_unified_person_count || 0)}<br>` +
					`<b>${__("Existing active memberships")}</b>: ${esc(preview.existing_active_membership_count || 0)}</p>` +
					`<p><b>${__("Frozen scope fingerprint")}</b>:<br><code>${esc(preview.scope_fingerprint)}</code></p>` +
					`<p><strong>${__("This preview changed no records.")}</strong></p></div>`,
			},
			{ fieldname: "batch_size", fieldtype: "Int", label: __("Records per bounded batch"), default: 2000, reqd: 1 },
			{ fieldname: "confirm_scope_fingerprint", fieldtype: "Data", label: __("Type the exact scope fingerprint to confirm"), reqd: 1 },
		],
		primary_action_label: __("Start Restartable Backfill"),
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
					dialog.hide();
					const result = response.message || {};
					frappe.msgprint(__("Backfill run {0} is {1}. Use Unified Person Backfill / Status to process bounded batches.", [result.backfill_run || "", result.status || ""]));
					frm.reload_doc();
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

function show_result(result) {
	frappe.msgprint({
		title: __("Automatic Tiered cycle"),
		indicator: result.status === "Applied" ? "green" : "orange",
		message: `<p>${__("Status")}: ${esc(result.status)}</p>` +
			`<p>${__("Batch")}: ${esc(result.batch)}</p>` +
			`<p>${__("Groups / Memberships")}: ${result.created_groups || 0} / ${result.created_memberships || 0}</p>`,
	});
}

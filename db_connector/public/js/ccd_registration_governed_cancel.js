const REGISTRATION_RETIREMENT_API = "db_connector.api_identity_retirement";
const AGENT_SYNC_API = "db_connector.api_agent_sync";
const REGISTRATION_RETIREMENT_POLL_MS = 4000;

function retirement_progress_dialog(title, initial_message) {
	const dialog = new frappe.ui.Dialog({
		title,
		fields: [
			{
				fieldname: "operation_status_html",
				fieldtype: "HTML",
			},
		],
	});
	dialog.show();
	update_retirement_progress(dialog, initial_message);
	return dialog;
}

function update_retirement_progress(dialog, message) {
	const field = dialog.get_field("operation_status_html");
	if (!field || !field.$wrapper) return;
	field.$wrapper.html(
		`<p>${frappe.utils.escape_html(message)}</p>`
		+ `<p class="text-muted small">${__("The operation continues in the background even if you close this dialog.")}</p>`,
	);
}

function show_retirement_operation_error(operation) {
	frappe.msgprint({
		title: __("Identity Retirement Operation Failed"),
		message: frappe.utils.escape_html(
			operation.error || __("The operation expired before its result could be read."),
		),
		indicator: "red",
	});
}

function load_registration_retirement_readiness(frm, callback) {
	frappe.call({
		method: `${REGISTRATION_RETIREMENT_API}.get_registration_retirement_readiness`,
		args: { registration_name: frm.doc.name },
		callback(response) {
			callback(response.message || {});
		},
	});
}

function show_agent_stop_preparation(frm, readiness) {
	const exact_confirmation = readiness.required_stop_confirmation
		|| `AGENT STOPPED AND HELD: ${frm.doc.name}`;
	const dialog = new frappe.ui.Dialog({
		title: __("Prepare Agent Stop for Retirement"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<div class="alert alert-warning">
					<p><strong>${__("Required order")}</strong></p>
					<ol>
						<li>${__("On the client machine, stop the CCD agent service/process and make sure it will not auto-restart.")}</li>
						<li>${__("Place this server-side hold. It blocks new sync leases and CCD Master writes for this source.")}</li>
						<li>${__("Run the governed identity-retirement preview and cancellation.")}</li>
						<li>${__("Create/submit the amended Registration, install its current configuration, then start the agent again.")}</li>
					</ol>
					<p>${__("The server cannot terminate a process on the remote client machine. This confirmation records that a System Manager stopped it.")}</p>
					<p><strong>${__("Type exactly")}</strong><br><code>${frappe.utils.escape_html(exact_confirmation)}</code></p>
				</div>`,
			},
			{
				fieldname: "note",
				fieldtype: "Small Text",
				label: __("Retirement preparation note"),
				reqd: 1,
			},
			{
				fieldname: "stop_confirmation",
				fieldtype: "Data",
				label: __("Agent-stop confirmation"),
				reqd: 1,
			},
		],
		primary_action_label: __("Place Server Retirement Hold"),
		primary_action(values) {
			if (values.stop_confirmation !== exact_confirmation) {
				frappe.throw(__("The typed agent-stop confirmation does not match."));
			}
			dialog.get_primary_btn().prop("disabled", true);
			frappe.call({
				method: `${REGISTRATION_RETIREMENT_API}.place_registration_retirement_hold`,
				args: {
					registration_name: frm.doc.name,
					stop_confirmation: values.stop_confirmation,
					note: values.note,
				},
				callback(response) {
					const result = response.message || {};
					dialog.hide();
					if (result.active_sync_lease) {
						frappe.msgprint({
							title: __("Retirement Hold Is Draining an Earlier Sync"),
							message: __("The server hold is active, so no new sync can start. An earlier run still owns the source lease. Wait for it to release or expire, then click Cancel with Identity Retirement again."),
							indicator: "orange",
						});
						frm.reload_doc();
						return;
					}
					frm.reload_doc().then(() => start_registration_retirement_preview(frm));
				},
				error() {
					dialog.get_primary_btn().prop("disabled", false);
				},
			});
		},
	});
	dialog.show();
}

function start_guarded_registration_retirement(frm) {
	load_registration_retirement_readiness(frm, (readiness) => {
		if (!readiness.hold_active) {
			show_agent_stop_preparation(frm, readiness);
			return;
		}
		if (readiness.active_sync_lease) {
			frappe.msgprint({
				title: __("Earlier Agent Sync Still Draining"),
				message: __("The retirement hold is active and prevents a new sync, but the earlier source lease has not been released yet. Retry after it finishes or expires."),
				indicator: "orange",
			});
			return;
		}
		start_registration_retirement_preview(frm);
	});
}

function show_release_retirement_hold(frm, readiness) {
	const exact_confirmation = readiness.required_release_confirmation
		|| `RELEASE RETIREMENT HOLD: ${frm.doc.name}`;
	const dialog = new frappe.ui.Dialog({
		title: __("Release Agent Retirement Hold"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__("Use this only when the source retirement has been abandoned. The unchanged submitted Registration may be synchronized again after release.")}</p>
					<p><strong>${__("Type exactly")}</strong><br><code>${frappe.utils.escape_html(exact_confirmation)}</code></p>`,
			},
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Hold-release reason"),
				reqd: 1,
			},
			{
				fieldname: "release_confirmation",
				fieldtype: "Data",
				label: __("Release confirmation"),
				reqd: 1,
			},
		],
		primary_action_label: __("Release Hold"),
		primary_action(values) {
			if (values.release_confirmation !== exact_confirmation) {
				frappe.throw(__("The typed release confirmation does not match."));
			}
			dialog.get_primary_btn().prop("disabled", true);
			frappe.call({
				method: `${REGISTRATION_RETIREMENT_API}.release_registration_retirement_hold`,
				args: {
					registration_name: frm.doc.name,
					release_confirmation: values.release_confirmation,
					reason: values.reason,
				},
				callback() {
					dialog.hide();
					frappe.show_alert({ message: __("Retirement hold released."), indicator: "green" });
					frm.reload_doc();
				},
				error() {
					dialog.get_primary_btn().prop("disabled", false);
				},
			});
		},
	});
	dialog.show();
}

function poll_retirement_operation(operation_token, dialog, on_completed) {
	frappe.call({
		method: `${REGISTRATION_RETIREMENT_API}.get_registration_retirement_operation`,
		args: { operation_token },
		callback(response) {
			const operation = response.message || {};
			if (operation.status === "Queued" || operation.status === "Running") {
				update_retirement_progress(
					dialog,
					operation.progress_message || (operation.status === "Queued"
						? __("Waiting for the long-running worker…")
						: __("Calculating or applying the governed identity retirement…")),
				);
				setTimeout(
					() => poll_retirement_operation(operation_token, dialog, on_completed),
					REGISTRATION_RETIREMENT_POLL_MS,
				);
				return;
			}
			dialog.hide();
			if (operation.status === "Completed") {
				on_completed(operation);
				return;
			}
			show_retirement_operation_error(operation);
		},
		error() {
			update_retirement_progress(
				dialog,
				__("The status check was interrupted. Retrying…"),
			);
			setTimeout(
				() => poll_retirement_operation(operation_token, dialog, on_completed),
				REGISTRATION_RETIREMENT_POLL_MS,
			);
		},
	});
}

function show_registration_retirement_confirmation(frm, preview) {
	const readiness = preview.agent_retirement || {};
	const dialog = new frappe.ui.Dialog({
		title: __("Confirm Governed Registration Cancellation"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p><strong>${__("Agent retirement hold")}</strong>: ${readiness.ready ? __("Ready") : __("Not ready")}</p><p>${__(
					"This will retire identity state before deleting {0} CCD Master records.",
					[preview.target_ccd_master_count],
				)}</p><p><strong>${__("Scope fingerprint")}</strong><br><code>${frappe.utils.escape_html(
					preview.scope_fingerprint,
				)}</code></p>`,
			},
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Cancellation and retirement reason"),
				reqd: 1,
			},
			{
				fieldname: "fingerprint",
				fieldtype: "Data",
				label: __("Type the exact scope fingerprint"),
				reqd: 1,
			},
		],
		primary_action_label: __("Retire Source and Cancel"),
		primary_action(values) {
			if (values.fingerprint !== preview.scope_fingerprint) {
				frappe.throw(__("The typed fingerprint does not match the preview."));
			}
			dialog.get_primary_btn().prop("disabled", true);
			frappe.call({
				method: `${REGISTRATION_RETIREMENT_API}.start_registration_cancellation_with_retirement`,
				args: {
					registration_name: frm.doc.name,
					confirm_scope_fingerprint: values.fingerprint,
					reason: values.reason,
				},
				callback(response) {
					const operation = response.message || {};
					if (!operation.operation_token) {
						dialog.get_primary_btn().prop("disabled", false);
						return;
					}
					dialog.hide();
					const progress = retirement_progress_dialog(
						__("Governed Identity Retirement"),
						__("The retirement and cancellation were queued…"),
					);
					poll_retirement_operation(
						operation.operation_token,
						progress,
						() => {
							frappe.show_alert({
								message: __("Registration cancelled through governed retirement."),
								indicator: "green",
							});
							frm.reload_doc();
						},
					);
				},
				error() {
					dialog.get_primary_btn().prop("disabled", false);
				},
			});
		},
	});
	dialog.show();
}

function start_registration_retirement_preview(frm) {
	frappe.call({
		method: `${REGISTRATION_RETIREMENT_API}.start_registration_cancellation_preview`,
		args: { registration_name: frm.doc.name },
		freeze: true,
		freeze_message: __("Queueing identity-retirement impact preview…"),
		callback(response) {
			const operation = response.message || {};
			if (!operation.operation_token) return;
			const progress = retirement_progress_dialog(
				__("Identity Retirement Impact Preview"),
				__("The impact preview was queued…"),
			);
			poll_retirement_operation(
				operation.operation_token,
				progress,
				(completed_operation) => {
					if (!completed_operation.preview) {
						show_retirement_operation_error({
							error: __("The preview completed without a result."),
						});
						return;
					}
					show_registration_retirement_confirmation(
						frm,
						completed_operation.preview,
					);
				},
			);
		},
	});
}

function show_master_baseline_adoption_confirmation(frm, preview) {
	const exact_confirmation = preview.required_confirmation || "";
	const dialog = new frappe.ui.Dialog({
		title: __("Adopt Exact Existing Staging and Master Baseline"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<div class="alert alert-warning">
					<p><strong>${__("Zero-write recovery")}</strong></p>
					<p>${__("This authorizes the current client agent to rebuild its local staging and CCD Master delta caches only after proving that every incoming mapped value equals both existing server populations.")}</p>
					<p>${__("Staging and Master inserts, updates, and deletions remain prohibited. Keep the client agent stopped until authorization completes; then start only the current agent.")}</p>
					<ul>
						<li>${__("Source")}: <code>${frappe.utils.escape_html(preview.source_id || "")}</code></li>
						<li>${__("Staging / Master rows")}: ${preview.staging_count || 0} / ${preview.master_count || 0}</li>
						<li>${__("Field mappings")}: ${preview.mapping_count || 0}</li>
						<li>${__("Mapping fingerprint")}: <code>${frappe.utils.escape_html(preview.mapping_fingerprint || "")}</code></li>
						<li>${__("Source-key fingerprint")}: <code>${frappe.utils.escape_html(preview.source_key_fingerprint || "")}</code></li>
					</ul>
					<p><strong>${__("Type exactly")}</strong><br><code style="overflow-wrap:anywhere">${frappe.utils.escape_html(exact_confirmation)}</code></p>
				</div>`,
			},
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Baseline-adoption reason"),
				reqd: 1,
			},
			{
				fieldname: "confirmation",
				fieldtype: "Small Text",
				label: __("Exact authorization confirmation"),
				reqd: 1,
			},
		],
		primary_action_label: __("Authorize Zero-Write Adoption"),
		primary_action(values) {
			if (String(values.confirmation || "").trim() !== exact_confirmation) {
				frappe.throw(__("The typed baseline-adoption confirmation does not match."));
			}
			dialog.get_primary_btn().prop("disabled", true);
			frappe.call({
				method: `${AGENT_SYNC_API}.authorize_master_baseline_adoption`,
				type: "POST",
				args: {
					registration: frm.doc.name,
					scope_fingerprint: preview.scope_fingerprint,
					confirmation: values.confirmation,
					reason: values.reason,
				},
				callback(response) {
					const result = response.message || {};
					dialog.hide();
					frappe.msgprint({
						title: __("Existing Staging and Master Baseline Authorized"),
						message: __("Authorization covers {0} exact existing rows. Start only the current client agent. It will first confirm staging, then Master, and rebuild both local caches without changing server rows or identity state.", [result.expected_count || 0]),
						indicator: "green",
					});
					frm.reload_doc();
				},
				error() {
					dialog.get_primary_btn().prop("disabled", false);
				},
			});
		},
	});
	dialog.show();
}

function start_master_baseline_adoption_preview(frm) {
	frappe.call({
		method: `${AGENT_SYNC_API}.preview_master_baseline_adoption`,
		args: { registration: frm.doc.name },
		freeze: true,
		freeze_message: __("Verifying exact staging and Master source scope…"),
		callback(response) {
			const preview = response.message || {};
			if (preview.scope_fingerprint) {
				show_master_baseline_adoption_confirmation(frm, preview);
			}
		},
	});
}

frappe.ui.form.on("CCD Registration", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || !frappe.user_roles.includes("System Manager")) {
			return;
		}
		frm.add_custom_button(
			__("Cancel with Identity Retirement"),
			() => start_guarded_registration_retirement(frm),
			__("Actions"),
		);
		if (frm.doc.agent_retirement_hold) {
			frm.add_custom_button(
				__("Release Agent Retirement Hold"),
				() => load_registration_retirement_readiness(
					frm,
					(readiness) => show_release_retirement_hold(frm, readiness),
				),
				__("Actions"),
			);
		}
		if (
			frm.doc.agent_sync_state === "Reconciliation Required"
			&& !["Authorized", "In Progress"].includes(
				frm.doc.agent_baseline_adoption_state,
			)
		) {
			frm.add_custom_button(
				__("Adopt Existing Staging and Master Baseline"),
				() => start_master_baseline_adoption_preview(frm),
				__("Actions"),
			);
		}
	},
});

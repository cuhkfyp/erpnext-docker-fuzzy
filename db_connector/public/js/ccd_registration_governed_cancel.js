const REGISTRATION_RETIREMENT_API = "db_connector.api_identity_retirement";
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

function poll_retirement_operation(operation_token, dialog, on_completed) {
	frappe.call({
		method: `${REGISTRATION_RETIREMENT_API}.get_registration_retirement_operation`,
		args: { operation_token },
		callback(response) {
			const operation = response.message || {};
			if (operation.status === "Queued" || operation.status === "Running") {
				update_retirement_progress(
					dialog,
					operation.status === "Queued"
						? __("Waiting for the long-running worker…")
						: __("Calculating or applying the governed identity retirement…"),
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
	const dialog = new frappe.ui.Dialog({
		title: __("Confirm Governed Registration Cancellation"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${__(
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

frappe.ui.form.on("CCD Registration", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || !frappe.user_roles.includes("System Manager")) {
			return;
		}
		frm.add_custom_button(
			__("Cancel with Identity Retirement"),
			() => start_registration_retirement_preview(frm),
			__("Actions"),
		);
	},
});

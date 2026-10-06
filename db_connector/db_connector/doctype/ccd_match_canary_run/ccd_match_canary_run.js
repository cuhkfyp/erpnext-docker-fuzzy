frappe.ui.form.on("CCD Match Canary Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		add_review_navigation(frm);
		if (!frappe.user.has_role("System Manager")) return;
		add_manager_actions(frm);
	},
});

function add_review_navigation(frm) {
	frm.add_custom_button(__("View All Recommendations"), () => {
		frappe.set_route("List", "CCD Match Recommendation", { canary_run: frm.doc.name });
	}, __("Review"));
	if (frm.doc.exception_component_count) {
		frm.add_custom_button(__("Review Exception Components"), () => {
			frappe.set_route("List", "CCD Match Component Review", { canary_run: frm.doc.name });
		}, __("Review"));
	}
	if (frm.doc.qc_sample_count) {
		frm.add_custom_button(__("Review Assigned QC"), () => {
			frappe.set_route("List", "CCD Match Recommendation", {
				canary_run: frm.doc.name,
				qc_selected: 1,
				qc_assigned_at: ["is", "set"],
			});
		}, __("Review"));
	}
	frm.add_custom_button(__("View Activation Batches"), () => {
		frappe.set_route("List", "CCD Identity Activation Batch", { canary_run: frm.doc.name });
	}, __("Identity Rollout"));
}

function add_manager_actions(frm) {
	if (["Ready", "Active"].includes(frm.doc.status)) {
		frm.add_custom_button(__("Create Splink Review Queue"), () => {
			frappe.confirm(
				__("Create the optional full Review Pool ranked by the approved Splink cutoff? This assigns no human work and creates no identity links."),
				() => frappe.call({
					method: "db_connector.api_fuzzy_review_queue.enqueue_review_queue",
					args: { canary_name: frm.doc.name },
					freeze: true,
					callback(response) {
						if (response.message?.run) frappe.set_route("Form", "CCD Match Review Queue Run", response.message.run);
					},
				}),
			);
		}, __("Review"));

		frm.add_custom_button(__("Preview Approve All"), () => {
			start_approve_all_preview(frm);
		}, __("Identity Rollout"));

		frm.add_custom_button(__("Create Pilot Wave"), () => create_wave(frm), __("Identity Rollout"));
		frm.add_custom_button(__("Create Approve-All Batch"), () => create_all_batch(frm), __("Identity Rollout"));
		frm.add_custom_button(__("Assign Next QC Cases"), () => {
			frappe.prompt(
				[{ fieldname: "count", fieldtype: "Int", label: __("QC cases"), default: 10, reqd: 1 }],
				(values) => frappe.call({
					method: "db_connector.api_identity_qc.assign_qc_cases",
					args: { run_name: frm.doc.name, count: values.count },
					freeze: true,
					callback(response) {
						const result = response.message || {};
						frm.reload_doc().then(
							() => show_qc_assignment_result(result),
							() => show_qc_assignment_result(result),
						);
					},
				}),
				__("Assign asynchronous QC work"),
			);
		}, __("Quality Control"));
	}
}

function start_approve_all_preview(frm) {
	if (frm.__ccd_approve_all_preview_starting) return;
	frm.__ccd_approve_all_preview_starting = true;
	const progress = new frappe.ui.Dialog({
		title: __("Previewing Approve All"),
		fields: [{ fieldname: "operation_status_html", fieldtype: "HTML" }],
	});
	progress.show();
	update_approve_all_preview_progress(progress, {
		status: "Queued",
		stage: "queued",
		processed_components: 0,
		total_components: 0,
	});
	frappe.call({
		method: "db_connector.api_identity_activation.start_approve_all_preview",
		args: { run_name: frm.doc.name },
		callback(response) {
			frm.__ccd_approve_all_preview_starting = false;
			const token = response.message?.operation_token;
			if (!token) {
				progress.hide();
				frappe.msgprint(__("No operation token was returned. No records were written."));
				return;
			}
			poll_approve_all_preview(token, progress);
		},
		error() {
			frm.__ccd_approve_all_preview_starting = false;
			progress.hide();
		},
	});
}

function update_approve_all_preview_progress(dialog, operation) {
	const field = dialog.get_field("operation_status_html");
	if (!field?.$wrapper) return;
	const processed = Number(operation.processed_components || 0);
	const total = Number(operation.total_components || 0);
	let message = __("Waiting for the background worker…");
	if (operation.stage === "selecting_components") {
		message = __("Selecting all currently available Proposed components…");
	} else if (operation.stage === "checking_safety") {
		message = total
			? __("Safety checked {0} of {1} components.", [processed, total])
			: __("Checking component safety…");
	}
	field.$wrapper.html(
		`<p>${frappe.utils.escape_html(message)}</p>`
		+ `<p class="text-muted small">${__("This zero-write preview continues on the long queue if you close this dialog. It does not create a batch or identity links.")}</p>`,
	);
}

function poll_approve_all_preview(operationToken, dialog) {
	frappe.call({
		method: "db_connector.api_identity_activation.get_approve_all_preview",
		args: { operation_token: operationToken },
		callback(response) {
			const operation = response.message || {};
			if (["Queued", "Running"].includes(operation.status)) {
				update_approve_all_preview_progress(dialog, operation);
				setTimeout(() => poll_approve_all_preview(operationToken, dialog), 4000);
				return;
			}
			dialog.hide();
			if (operation.status === "Completed" && operation.result) {
				show_approve_all_preview_result(operation.result);
				return;
			}
			frappe.msgprint({
				title: operation.status === "Failed" ? __("Approve-all preview failed") : __("Approve-all preview result unavailable"),
				indicator: operation.status === "Failed" ? "red" : "orange",
				message: frappe.utils.escape_html(
					operation.error || __("The result could not be confirmed. No identity or batch records were written."),
				),
			});
		},
		error() {
			update_approve_all_preview_progress(dialog, {
				status: "Running",
				stage: "checking_safety",
			});
			setTimeout(() => poll_approve_all_preview(operationToken, dialog), 4000);
		},
	});
}

function show_approve_all_preview_result(result) {
	const unsafe = (result.components || []).filter((component) => !component.safe);
	const unsafeHtml = unsafe.length
		? `<p><strong>${__("Unsafe component details")}</strong></p><ul>${unsafe.map((component) => {
			const recommendation = (component.recommendation_names || [])[0] || "";
			const link = recommendation
				? `<a href="/app/ccd-match-recommendation/${encodeURIComponent(recommendation)}">${frappe.utils.escape_html(recommendation)}</a>`
				: frappe.utils.escape_html(component.component_fingerprint || "");
			return `<li>${link}: ${frappe.utils.escape_html((component.conflicts || []).join(", "))}</li>`;
		}).join("")}</ul><p>${__("Open a linked recommendation and use Prepare Overlap Resolution Batch when the listed failure is structural identity overlap.")}</p>`
		: "";
	const omitted = Number(result.omitted_component_detail_count || 0);
	const omittedHtml = omitted
		? `<p>${__("{0} additional unsafe component details were omitted from this dialog.", [omitted])}</p>`
		: "";
	frappe.msgprint({
		title: __("Zero-write activation preview"),
		indicator: result.unsafe_component_count ? "orange" : "green",
		message: `<p>${__("Complete components")}: ${result.selected_component_count || 0}</p>` +
			`<p>${__("Recommendations")}: ${result.selected_recommendation_count || 0}</p>` +
			`<p>${__("Planned groups / memberships")}: ${result.planned_identity_group_count || 0} / ${result.planned_membership_count || 0}</p>` +
			`<p>${__("Unsafe components")}: ${result.unsafe_component_count || 0}</p>` +
			unsafeHtml + omittedHtml +
			`<p><strong>${__("No records were written.")}</strong></p>`,
	});
}

function show_qc_assignment_result(result) {
	const assigned = Number(result.assigned || 0);
	const replenished = Number(result.replenished || 0);
	const recommendations = Array.isArray(result.recommendations) ? result.recommendations : [];
	const caseLinks = recommendations.length
		? `<ul>${recommendations.map((recommendation) => {
			const caseId = String(recommendation || "");
			return `<li><a href="/app/ccd-match-recommendation/${encodeURIComponent(caseId)}">${frappe.utils.escape_html(caseId)}</a></li>`;
		}).join("")}</ul>`
		: `<p>${__("No eligible QC cases were available.")}</p>`;
	const dueAt = result.due_at
		? frappe.utils.escape_html(String(result.due_at))
		: __("Not set");

	frappe.msgprint({
		title: assigned ? __("QC cases assigned") : __("No QC cases assigned"),
		indicator: assigned ? "green" : "orange",
		message:
			`<p><strong>${__("Assigned")}: ${assigned}</strong></p>` +
			`<p>${__("Replenished")}: ${replenished}<br>${__("Due at")}: ${dueAt}</p>` +
			`<p><strong>${__("Newly assigned cases")}</strong></p>${caseLinks}`,
	});
}

function create_wave(frm) {
	frappe.prompt(
		[
			{ fieldname: "component_limit", fieldtype: "Int", label: __("Complete components"), default: 100, reqd: 1 },
			{ fieldname: "is_demonstration", fieldtype: "Check", label: __("Synthetic demonstration batch only"), default: 0 },
		],
		(values) => create_activation_batch(frm, "Explicit Wave", values.component_limit, values.is_demonstration),
		__("Create component-atomic Pilot Wave"),
	);
}

function create_all_batch(frm) {
	frappe.confirm(
		__("Freeze every currently available Proposed component into one reviewed Activation Batch? Deliberately held components remain Proposed and are excluded. Creating the batch does not create identity links."),
		() => create_activation_batch(frm, "Approve All Eligible", null, 0),
	);
}

function create_activation_batch(frm, selectionMethod, componentLimit, demonstration) {
	if (frm.__ccd_batch_creation_starting) return;
	frm.__ccd_batch_creation_starting = true;
	const progress = new frappe.ui.Dialog({
		title: __("Creating Activation Batch"),
		fields: [{ fieldname: "operation_status_html", fieldtype: "HTML" }],
	});
	progress.show();
	update_activation_creation_progress(progress, __("Queueing the component safety preview…"));
	frappe.call({
		method: "db_connector.api_identity_activation.start_activation_batch_creation",
		args: {
			run_name: frm.doc.name,
			selection_method: selectionMethod,
			component_limit: componentLimit,
			is_pilot_wave: selectionMethod === "Explicit Wave" ? 1 : 0,
			is_demonstration: demonstration || 0,
		},
		callback(response) {
			frm.__ccd_batch_creation_starting = false;
			const token = response.message?.operation_token;
			if (!token) {
				progress.hide();
				frappe.msgprint(__("No operation token was returned. Check the Activation Batch list before retrying."));
				return;
			}
			poll_activation_batch_creation(token, progress);
		},
		error() {
			frm.__ccd_batch_creation_starting = false;
			progress.hide();
		},
	});
}

function update_activation_creation_progress(dialog, message, operation = {}) {
	const field = dialog.get_field("operation_status_html");
	if (!field?.$wrapper) return;
	const processed = Number(operation.processed_components || 0);
	const total = Number(operation.total_components || 0);
	const detail = total
		? `<p>${__("Components checked")}: ${processed} / ${total}</p>`
		: "";
	field.$wrapper.html(
		`<p>${frappe.utils.escape_html(message)}</p>`
		+ detail
		+ `<p class="text-muted small">${__("The safety preview continues in the background even if you close this dialog. Creating a batch does not approve or apply it.")}</p>`,
	);
}

function poll_activation_batch_creation(operationToken, dialog) {
	frappe.call({
		method: "db_connector.api_identity_activation.get_activation_batch_creation",
		args: { operation_token: operationToken },
		callback(response) {
			const operation = response.message || {};
			if (["Queued", "Running"].includes(operation.status)) {
				let message = operation.status === "Queued"
					? __("Waiting for the background worker…")
					: __("Selecting the available components…");
				if (operation.stage === "checking_safety") {
					message = __("Checking the selected components…");
				} else if (operation.stage === "writing_batch") {
					message = __("Writing the reviewed Activation Batch…");
				}
				update_activation_creation_progress(
					dialog,
					message,
					operation,
				);
				setTimeout(() => poll_activation_batch_creation(operationToken, dialog), 4000);
				return;
			}
			dialog.hide();
			if (operation.status === "Completed" && operation.batch) {
				frappe.show_alert({ message: __("Activation Batch created."), indicator: "green" });
				frappe.set_route("Form", "CCD Identity Activation Batch", operation.batch);
				return;
			}
			frappe.msgprint({
				title: operation.status === "Failed" ? __("Activation Batch creation failed") : __("Activation Batch result unavailable"),
				indicator: operation.status === "Failed" ? "red" : "orange",
				message: frappe.utils.escape_html(
					operation.error || __("The result could not be confirmed. Check the Activation Batch list before retrying."),
				),
			});
		},
		error() {
			update_activation_creation_progress(dialog, __("The status check was interrupted. Retrying…"));
			setTimeout(() => poll_activation_batch_creation(operationToken, dialog), 4000);
		},
	});
}

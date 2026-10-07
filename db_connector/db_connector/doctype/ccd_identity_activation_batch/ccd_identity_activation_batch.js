frappe.ui.form.on("CCD Identity Activation Batch", {
	refresh(frm) {
		const canPreviewOverlap = frappe.user.has_role("System Manager") &&
			["Reviewed", "Approved"].includes(frm.doc.status);
		if (frm.fields_dict.items && frm.fields_dict.items.grid) {
			frm.fields_dict.items.grid.update_docfield_property(
				"resolve_overlap",
				"hidden",
				canPreviewOverlap ? 0 : 1,
			);
			frm.fields_dict.items.grid.refresh();
		}
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;
		if (frm.doc.status === "Reviewed") {
			frm.add_custom_button(__("Approve Batch"), () => {
				frappe.confirm(
					__("Approve this frozen, component-atomic batch? Approval alone creates no Identity Memberships."),
					() => frappe.call({
						method: "db_connector.api_identity_activation.approve_activation_batch",
						args: { batch_name: frm.doc.name },
						callback: () => frm.reload_doc(),
					}),
				);
			});
		}
		const hasStructuralOverlap = (frm.doc.items || []).some((item) => item.status === "Exception");
		if (frm.doc.status === "Approved" && !hasStructuralOverlap && !frm.doc.is_automatic) {
			frm.add_custom_button(__("Apply Approved Batch"), () => {
				frappe.confirm(
					__("Queue fresh safety checks and create reversible Identity Decisions, Groups, and Memberships? Apply remains one governed transaction and is blocked while Live Identity Materialization is disabled or paused."),
					() => start_activation_batch_apply(frm),
				);
			});
		}
		if (frm.doc.status === "Failed" && !frm.doc.is_automatic) {
			frm.add_custom_button(__("Revalidate for Retry"), () => {
				frappe.confirm(
					__("Re-run the frozen component and safety checks before returning this batch to Approved?"),
					() => frappe.call({
						method: "db_connector.api_identity_activation.revalidate_failed_activation_batch",
						args: { batch_name: frm.doc.name },
						freeze: true,
						callback: () => frm.reload_doc(),
					}),
				);
			});
		}
	},
});

function start_activation_batch_apply(frm) {
	if (frm.__ccd_batch_apply_starting) return;
	frm.__ccd_batch_apply_starting = true;
	const progress = new frappe.ui.Dialog({
		title: __("Applying Activation Batch"),
		fields: [{ fieldname: "operation_status_html", fieldtype: "HTML" }],
	});
	progress.show();
	update_activation_apply_progress(progress, __("Queueing the governed Apply…"));
	frappe.call({
		method: "db_connector.api_identity_activation.start_activation_batch_apply",
		args: { batch_name: frm.doc.name },
		callback(response) {
			frm.__ccd_batch_apply_starting = false;
			const operation = response.message || {};
			if (operation.status === "Completed" && operation.already_applied) {
				progress.hide();
				frappe.show_alert({ message: __("Activation Batch is already Applied."), indicator: "green" });
				frm.reload_doc();
				return;
			}
			if (!operation.operation_token) {
				progress.hide();
				frappe.msgprint(__("No operation token was returned. Inspect this batch before retrying."));
				return;
			}
			poll_activation_batch_apply(operation.operation_token, progress, frm);
		},
		error() {
			frm.__ccd_batch_apply_starting = false;
			progress.hide();
		},
	});
}

function update_activation_apply_progress(dialog, message, operation = {}) {
	const field = dialog.get_field("operation_status_html");
	if (!field?.$wrapper) return;
	const processed = Number(operation.processed_components || 0);
	const total = Number(operation.total_components || 0);
	const detail = total
		? `<p>${__("Components materialized")}: ${processed} / ${total}</p>`
		: "";
	field.$wrapper.html(
		`<p>${frappe.utils.escape_html(message)}</p>`
		+ detail
		+ `<p class="text-muted small">${__("Apply continues on the long queue if you close this dialog. No partial identity state is committed: the entire governed transaction succeeds or rolls back.")}</p>`,
	);
}

function poll_activation_batch_apply(operationToken, dialog, frm) {
	frappe.call({
		method: "db_connector.api_identity_activation.get_activation_batch_apply",
		args: { operation_token: operationToken },
		callback(response) {
			const operation = response.message || {};
			if (["Queued", "Running"].includes(operation.status)) {
				let message = operation.status === "Queued"
					? __("Waiting for the long-queue worker…")
					: __("Loading and checking the frozen components…");
				if (operation.stage === "materializing_components") {
					message = __("Creating governed identity decisions, groups, and memberships…");
				} else if (operation.stage === "finalizing") {
					message = __("Finalizing recommendation and canary counters…");
				} else if (operation.stage === "committing") {
					message = __("Committing the complete Activation Batch…");
				}
				update_activation_apply_progress(dialog, message, operation);
				setTimeout(() => poll_activation_batch_apply(operationToken, dialog, frm), 4000);
				return;
			}
			dialog.hide();
			if (operation.status === "Completed") {
				const result = operation.result || {};
				frappe.show_alert({
					message: __("Activation Batch applied: {0} groups and {1} memberships.", [
						Number(result.created_groups || 0),
						Number(result.created_memberships || 0),
					]),
					indicator: "green",
				}, 10);
				frm.reload_doc();
				return;
			}
			frappe.msgprint({
				title: operation.status === "Failed" ? __("Activation Batch Apply failed") : __("Activation Batch Apply result unavailable"),
				indicator: operation.status === "Failed" ? "red" : "orange",
				message: frappe.utils.escape_html(
					operation.error || __("The result could not be confirmed. Inspect this batch before retrying."),
				),
			});
			frm.reload_doc();
		},
		error() {
			update_activation_apply_progress(dialog, __("The status check was interrupted. Retrying…"));
			setTimeout(() => poll_activation_batch_apply(operationToken, dialog, frm), 4000);
		},
	});
}

frappe.ui.form.on("CCD Identity Activation Item", {
	review_component(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.call({
			method: "db_connector.api_identity_activation.get_activation_batch_component",
			args: { batch_name: frm.doc.name, item_name: row.name },
			freeze: true,
			freeze_message: __("Loading the frozen component…"),
			callback(response) {
				show_activation_component(response.message || {});
			},
		});
	},
	resolve_overlap(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!(frappe.user_roles || []).includes("System Manager")) return;
		if (!["Reviewed", "Approved"].includes(frm.doc.status)) {
			frappe.msgprint(__("Only a Reviewed or Approved frozen Activation Batch can preview an overlap."));
			return;
		}
		if (!["Planned", "Failed", "Exception"].includes(row.status)) {
			frappe.msgprint(__("Only an unapplied Activation Item can start overlap resolution."));
			return;
		}
		window.db_connector_identity_overlap.open(
			"CCD Identity Activation Item",
			row.name,
			() => frm.reload_doc(),
		);
	},
});

function activation_esc(value) {
	return frappe.utils.escape_html(String(value || ""));
}

function activation_record_heading(side) {
	const text = `${side.alias || "Record"} — ${side.source || "Unknown source"}`;
	if (!side.record_id) return activation_esc(text);
	return `<a href="/app/ccd-master/${encodeURIComponent(side.record_id)}" target="_blank" rel="noopener">${activation_esc(text)}</a>`;
}

function activation_comparison_label(value) {
	const labels = {
		exact: __("Exact"),
		missing: __("Missing"),
		disagree: __("Different"),
		close: __("Close"),
		phonetic: __("Phonetic"),
		weak: __("Weak"),
		not_compared: __("Not compared"),
	};
	return labels[value] || value;
}

function activation_pair_table(pair) {
	const rows = (pair.attributes || []).map((attribute) =>
		`<tr><td>${activation_esc(attribute.attribute)}</td>` +
		`<td>${activation_esc(attribute.left)}</td><td>${activation_esc(attribute.right)}</td>` +
		`<td>${activation_esc(activation_comparison_label(attribute.comparison))}</td></tr>`
	).join("");
	const recommendation_url = `/app/ccd-match-recommendation/${encodeURIComponent(pair.recommendation)}`;
	const stale = pair.stale
		? `<span class="indicator-pill red">${__("Stale")}</span>`
		: `<span class="indicator-pill green">${__("Current")}</span>`;
	const reasons = (pair.reason_codes || []).length
		? `<div class="text-muted mb-2">${__("Recommendation reasons")}: ${activation_esc(pair.reason_codes.join(", "))}</div>`
		: "";
	const safety = (pair.safety_reasons || []).length
		? `<div class="text-muted mb-2">${__("Safety reasons")}: ${activation_esc(pair.safety_reasons.join(", "))}</div>`
		: "";
	return `<div class="mb-4"><h5>` +
		`<a href="${recommendation_url}" target="_blank" rel="noopener">${activation_esc(pair.recommendation)}</a> ` +
		`${stale}</h5><div class="text-muted mb-2">${__("Recommendation status")}: ${activation_esc(pair.status)}</div>` +
		`${reasons}${safety}<div class="table-responsive"><table class="table table-bordered">` +
		`<thead><tr><th>${__("Evidence")}</th><th>${activation_record_heading(pair.left)}</th>` +
		`<th>${activation_record_heading(pair.right)}</th><th>${__("Comparison")}</th></tr></thead>` +
		`<tbody>${rows}</tbody></table></div></div>`;
}

function show_activation_component(payload) {
	const retired = payload.historical_source_retired
		? `<div class="alert alert-danger">${__("Historical source retired. Deleted CCD Masters are shown only as frozen aliases and are not linked to recreated records.")}</div>`
		: "";
	const privacy = payload.sensitive_values_visible
		? `<div class="alert alert-warning">${__("Sensitive values and CCD Master links are visible because your role permits them.")}</div>`
		: `<div class="alert alert-info">${__("Identity values are masked. A Sensitive Reviewer or System Manager can see the full permitted values.")}</div>`;
	const demonstration = payload.is_demonstration
		? `<div class="alert alert-danger"><strong>${__("Synthetic demonstration label")}</strong>: ` +
			`${__("Apply is still a real write; this flag only labels resulting audit events as demonstration events.")}</div>`
		: "";
	const records = (payload.records || []).map((record) =>
		`<li>${activation_record_heading(record)}</li>`
	).join("");
	const summary = `<div class="alert alert-secondary"><strong>${__("Frozen complete component")}</strong><br>` +
		`${__("Source pair(s)")}: ${activation_esc((payload.source_pairs || []).join(", ") || "—")}<br>` +
		`${__("Records")}: ${activation_esc(payload.record_count)} &nbsp; ` +
		`${__("Recommendations")}: ${activation_esc(payload.recommendation_count)} &nbsp; ` +
		`${__("Item status")}: ${activation_esc(payload.item_status)}<br>` +
		`${__("The selected record pairs are frozen. Evidence is reloaded from the current CCD records and is marked Stale if a record changed after the canary snapshot.")}</div>`;
	const html = retired + privacy + demonstration + summary +
		`<h5>${__("Selected CCD records")}</h5><ul>${records}</ul>` +
		`<h5>${__("Frozen selected pair(s) and current evidence")}</h5>` +
		(payload.recommendations || []).map(activation_pair_table).join("");
	const dialog = new frappe.ui.Dialog({
		title: __("Review Selected Complete Component"),
		size: "extra-large",
		fields: [{ fieldname: "component_html", fieldtype: "HTML" }],
	});
	dialog.fields_dict.component_html.$wrapper.html(html);
	dialog.show();
}

frappe.ui.form.on("CCD Match Component Review", {
	refresh(frm) {
		if (frm.is_new()) return;
		load_component_evidence(frm);
	},
});

function component_esc(value) {
	return frappe.utils.escape_html(String(value || ""));
}

function component_record_label(record) {
	const label = `${record.alias} — ${record.source || "Unknown source"}`;
	if (!record.record_id) return component_esc(label);
	return `<a href="/app/ccd-master/${encodeURIComponent(record.record_id)}" target="_blank" rel="noopener">${component_esc(label)}</a>`;
}

function render_component(frm, payload) {
	const privacy = payload.sensitive_values_visible
		? '<div class="alert alert-warning">' + __("Sensitive values are visible because your role permits them.") + "</div>"
		: '<div class="alert alert-info">' + __("All identity values are masked. Equality is preserved for comparison; privileged reviewers can see full permitted values.") + "</div>";
	const stale = payload.stale
		? '<div class="alert alert-danger">' + __("At least one source record changed after the snapshot. This component is closed; create a new canary.") + "</div>"
		: "";
	const headers = (payload.attributes || []).map((attribute) => `<th>${component_esc(attribute)}</th>`).join("");
	const records = (payload.records || []).map((record) => {
		const values = (payload.attributes || []).map((attribute) =>
			`<td>${component_esc((record.attributes || {})[attribute])}</td>`
		).join("");
		return `<tr><td>${component_record_label(record)}</td>${values}</tr>`;
	}).join("");
	const edges = (payload.candidate_pairs || []).map((pair) =>
		`<span class="badge badge-light mr-2">${component_esc(pair.left)} ↔ ${component_esc(pair.right)}</span>`
	).join("");
	const groups = (payload.final_groups || []).length
		? `<div class="alert alert-success"><b>${__("Final grouping")}</b>: ` +
			payload.final_groups.map((group) => component_esc(group.join(" = "))).join("; ") + "</div>"
		: "";
	const materialization = payload.materialization_status && payload.materialization_status !== "Not Final"
		? `<div class="alert alert-secondary"><b>${__("Identity materialization")}</b>: ${component_esc(payload.materialization_status)}` +
			`${payload.identity_decision ? ` — <a href="/app/ccd-identity-decision/${encodeURIComponent(payload.identity_decision)}">${__("open decision")}</a>` : ""}` +
			`${payload.materialization_error ? `<br>${component_esc(payload.materialization_error)}` : ""}</div>`
		: "";
	frm.fields_dict.evidence_html.$wrapper.html(
		privacy + stale + groups + materialization +
		`<p><b>${__("Review status")}</b>: ${component_esc(payload.status)} ` +
		`${payload.final_decision ? `— ${component_esc(payload.final_decision)}` : ""}</p>` +
		`<p><b>${__("Tiered High candidate edges")}</b>: ${edges || __("None")}</p>` +
		`<div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr>` +
		`<th>${__("Record / Source")}</th>${headers}</tr></thead><tbody>${records}</tbody></table></div>`
	);
}

function load_component_evidence(frm) {
	frappe.call({
		method: "db_connector.api_fuzzy_canary.get_component_evidence",
		args: { review_name: frm.doc.name },
		callback(response) {
			const payload = response.message || {};
			render_component(frm, payload);
			if (payload.can_submit) add_component_buttons(frm, payload, false);
			if (payload.can_adjudicate) add_component_buttons(frm, payload, true);
			if (payload.can_materialize) {
				frm.add_custom_button(__("Retry Identity Materialization"), () => frappe.call({
					method: "db_connector.api_identity_human.materialize_component_review",
					args: { review_name: frm.doc.name },
					freeze: true,
					callback: () => frm.reload_doc(),
				}), __("Identity Resolution"));
			}
		},
	});
}

function add_component_buttons(frm, payload, adjudication) {
	const group = adjudication ? __("Component Adjudication") : __("Component Review");
	for (const decision of ["All Same", "Partial Match", "All Different"]) {
		frm.add_custom_button(
			__(decision),
			() => collect_component_decision(frm, payload, decision, adjudication),
			group,
		);
	}
	if (!adjudication) {
		frm.add_custom_button(
			__("Unsure"),
			() => collect_component_decision(frm, payload, "Unsure", false),
			group,
		);
	}
}

function collect_component_decision(frm, payload, decision, adjudication) {
	if (decision === "Partial Match") {
		const dialog = new frappe.ui.Dialog({
			title: adjudication ? __("Adjudicate Partial Match") : __("Submit Partial Match"),
			fields: [
				{
					fieldname: "same_pairs",
					fieldtype: "MultiCheck",
					label: __("Select every pair that represents the same person"),
					options: (payload.pair_options || []).map((option) => ({
						label: option.label,
						value: option.value,
					})),
					columns: 2,
				},
				{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes"), reqd: adjudication ? 1 : 0 },
			],
			primary_action_label: __("Submit"),
			primary_action(values) {
				if (!(values.same_pairs || []).length) {
					frappe.msgprint(__("Select at least one Same pair."));
					return;
				}
				dialog.hide();
				send_component_decision(frm, decision, values.same_pairs, values.notes, adjudication);
			},
		});
		dialog.show();
		return;
	}
	frappe.prompt(
		[{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes"), reqd: adjudication ? 1 : 0 }],
		(values) => send_component_decision(frm, decision, [], values.notes, adjudication),
		adjudication ? __(`Adjudicate: ${decision}`) : __(`Submit: ${decision}`),
	);
}

function send_component_decision(frm, decision, same_pairs, notes, adjudication) {
	frappe.call({
		method: adjudication
			? "db_connector.api_fuzzy_canary.adjudicate_component_review"
			: "db_connector.api_fuzzy_canary.submit_component_review",
		args: {
			review_name: frm.doc.name,
			decision,
			same_pairs_json: JSON.stringify(same_pairs || []),
			notes: notes || "",
		},
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}

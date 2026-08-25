frappe.ui.form.on("CCD Match Recommendation", {
	refresh(frm) {
		if (frm.is_new()) return;
		load_recommendation_evidence(frm);
		if (!frappe.user.has_role("System Manager")) return;
		if (
			frm.doc.identity_decision &&
			frm.doc.rollout_state === "Applied" &&
			frm.doc.status === "Approved"
		) {
			frm.add_custom_button(
				__("Correct Complete Identity Component"),
				() => window.db_connector_identity_correction.open(
					frm.doc.identity_decision,
					() => frm.reload_doc(),
				),
				__("Identity Resolution"),
			);
		}
		if (frm.doc.status === "Proposed" && !frm.doc.identity_decision) {
			frm.add_custom_button(
				__("Prepare Overlap Resolution Batch"),
				() => frappe.confirm(
					__("Freeze this complete Tiered component into a reviewed batch only if its sole safety problem is existing identity overlap? This creates no identity links."),
					() => frappe.call({
						method: "db_connector.api_identity_activation.create_overlap_resolution_batch",
						args: { recommendation_name: frm.doc.name },
						freeze: true,
						callback(response) {
							if (response.message?.batch) {
								frappe.set_route("Form", "CCD Identity Activation Batch", response.message.batch);
							}
						},
					}),
				),
				__("Identity Resolution"),
			);
			frm.add_custom_button(__("Withdraw Recommendation"), () => {
				frappe.prompt(
					[{ fieldname: "reason", fieldtype: "Small Text", label: __("Withdrawal Reason"), reqd: 1 }],
					(values) => frappe.call({
						method: "db_connector.api_fuzzy_canary.reverse_recommendation",
						args: { recommendation_name: frm.doc.name, reason: values.reason },
						callback: () => frm.reload_doc(),
					}),
					__("Withdraw Recommendation"),
				);
			});
			if (frm.doc.rollout_state === "Held") {
				frm.add_custom_button(__("Release Complete Component Hold"), () => frappe.call({
					method: "db_connector.api_identity_activation.release_component_hold",
					args: { recommendation_name: frm.doc.name },
					callback: () => frm.reload_doc(),
				}));
			} else {
				frm.add_custom_button(__("Hold Complete Component for Later/Demo"), () => {
					frappe.prompt(
						[{ fieldname: "reason", fieldtype: "Small Text", label: __("Hold Reason"), reqd: 1 }],
						(values) => frappe.call({
							method: "db_connector.api_identity_activation.hold_component",
							args: { recommendation_name: frm.doc.name, reason: values.reason },
							callback: () => frm.reload_doc(),
						}),
						__("Hold complete component"),
					);
				});
			}
		}
	},
});

function esc(value) {
	return frappe.utils.escape_html(String(value || ""));
}

function record_heading(side) {
	const text = `${side.alias} — ${side.source || "Unknown source"}`;
	if (!side.record_id) return esc(text);
	const route = `/app/ccd-master/${encodeURIComponent(side.record_id)}`;
	return `<a href="${route}" target="_blank" rel="noopener">${esc(text)}</a>`;
}

function comparison_label(value) {
	const labels = {
		exact: __("Exact"),
		missing: __("Missing"),
		disagree: __("Different"),
		close: __("Close"),
		phonetic: __("Phonetic"),
		weak: __("Weak"),
	};
	return labels[value] || value;
}

function render_recommendation_evidence(frm, payload) {
	const privacy = payload.sensitive_values_visible
		? '<div class="alert alert-warning">' + __("Sensitive values are visible because your role permits them.") + "</div>"
		: '<div class="alert alert-info">' + __("Identity values are masked. A Sensitive Reviewer or System Manager can see the full permitted values.") + "</div>";
	const stale = payload.stale
		? '<div class="alert alert-danger">' + __("The source record changed after this canary snapshot. Do not review this stale pair.") + "</div>"
		: "";
	const rows = (payload.attributes || []).map((row) =>
		`<tr><td>${esc(row.attribute)}</td><td>${esc(row.left)}</td>` +
		`<td>${esc(row.right)}</td><td>${esc(comparison_label(row.comparison))}</td></tr>`
	).join("");
	const component = payload.component_review
		? `<div class="alert alert-warning">${__("This pair belongs to a multi-record exception. Review the complete component, not this edge alone.")} ` +
			`<a href="/app/ccd-match-component-review/${encodeURIComponent(payload.component_review)}">${__("Open component review")}</a></div>`
		: "";
	const qc = payload.qc_selected
		? `<div class="alert alert-secondary"><b>${__("Random QC sample")}</b>: ${esc(payload.qc_review_status || "Unreviewed")}` +
			`${payload.qc_final_label ? ` — ${esc(payload.qc_final_label)}` : ""}` +
			`${payload.qc_due_at ? `<br>${__("Due")}: ${esc(payload.qc_due_at)}` : ""}` +
			`${payload.qc_failure_action ? `<br><strong>${__("Circuit breaker")}</strong>: ${esc(payload.qc_failure_action)}` : ""}</div>`
		: "";
	frm.fields_dict.evidence_html.$wrapper.html(
		privacy + stale + component + qc +
		`<div class="table-responsive"><table class="table table-bordered"><thead><tr>` +
		`<th>${__("Evidence")}</th><th>${record_heading(payload.left)}</th>` +
		`<th>${record_heading(payload.right)}</th><th>${__("Comparison")}</th>` +
		`</tr></thead><tbody>${rows}</tbody></table></div>`
	);
}

function load_recommendation_evidence(frm) {
	frappe.call({
		method: "db_connector.api_fuzzy_canary.get_recommendation_evidence",
		args: { recommendation_name: frm.doc.name },
		callback(response) {
			const payload = response.message || {};
			render_recommendation_evidence(frm, payload);
			if (payload.can_submit_qc) add_qc_buttons(frm);
			if (payload.can_adjudicate_qc) add_qc_adjudication_buttons(frm);
		},
	});
}

function add_qc_buttons(frm) {
	for (const label of ["Same", "Different", "Unsure"]) {
		frm.add_custom_button(__(label), () => submit_qc(frm, label), __("QC Review"));
	}
}

function add_qc_adjudication_buttons(frm) {
	for (const label of ["Same", "Different"]) {
		frm.add_custom_button(
			__(`Adjudicate ${label}`),
			() => submit_qc(frm, label, true),
			__("QC Adjudication"),
		);
	}
}

function submit_qc(frm, label, adjudication = false) {
	frappe.prompt(
		[{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes"), reqd: adjudication ? 1 : 0 }],
		(values) => frappe.call({
			method: adjudication
				? "db_connector.api_fuzzy_canary.adjudicate_recommendation_qc"
				: "db_connector.api_fuzzy_canary.submit_recommendation_qc",
			args: { recommendation_name: frm.doc.name, label, notes: values.notes || "" },
			callback: () => frm.reload_doc(),
		}),
		adjudication ? __(`Adjudicate as ${label}`) : __(`Submit ${label}`),
	);
}

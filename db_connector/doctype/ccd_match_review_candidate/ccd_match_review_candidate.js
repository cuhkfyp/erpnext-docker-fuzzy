frappe.ui.form.on("CCD Match Review Candidate", {
	refresh(frm) {
		if (frm.is_new()) return;
		load_candidate_evidence(frm);
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

function render_candidate_evidence(frm, payload) {
	const privacy = payload.sensitive_values_visible
		? '<div class="alert alert-warning">' + __("Sensitive values are visible because your role permits them.") + "</div>"
		: '<div class="alert alert-info">' + __("Identity values are masked. A Sensitive Reviewer or System Manager can see the full permitted values.") + "</div>";
	const purpose = '<div class="alert alert-secondary"><b>' + __("Model tier: Review") + "</b> — " +
		__("This pair is prioritized by Splink for human review only. It is not an automatic High match and this screen cannot link or merge records.") + "</div>";
	const stale = payload.stale
		? '<div class="alert alert-danger">' + __("A source record changed after the frozen snapshot. Do not review this stale pair.") + "</div>"
		: "";
	const score = Object.prototype.hasOwnProperty.call(payload, "probabilistic_score")
		? `<div class="alert alert-light">${__("System Manager audit view")}: ${__("Splink probability")} ${esc(payload.probabilistic_score)}; ` +
			`${__("approved cutoff")} ${esc(payload.review_threshold)}; ${__("blocking routes")} ${esc(payload.blocking_routes)}</div>`
		: "";
	const materialization = payload.materialization_status && payload.materialization_status !== "Not Final"
		? `<div class="alert alert-secondary"><b>${__("Identity materialization")}</b>: ${esc(payload.materialization_status)}` +
			`${payload.identity_decision ? ` — <a href="/app/ccd-identity-decision/${encodeURIComponent(payload.identity_decision)}">${__("open decision")}</a>` : ""}` +
			`${payload.correction_decision ? ` — <a href="/app/ccd-identity-decision/${encodeURIComponent(payload.correction_decision)}">${__("open correction")}</a>` : ""}` +
			`${payload.materialization_error ? `<br>${esc(payload.materialization_error)}` : ""}</div>`
		: "";
	const rows = (payload.attributes || []).map((row) =>
		`<tr><td>${esc(row.attribute)}</td><td>${esc(row.left)}</td>` +
		`<td>${esc(row.right)}</td><td>${esc(comparison_label(row.comparison))}</td></tr>`
	).join("");
	frm.fields_dict.evidence_html.$wrapper.html(
		privacy + purpose + stale + score + materialization +
		`<div class="table-responsive"><table class="table table-bordered"><thead><tr>` +
		`<th>${__("Evidence")}</th><th>${record_heading(payload.left)}</th>` +
		`<th>${record_heading(payload.right)}</th><th>${__("Comparison")}</th>` +
		`</tr></thead><tbody>${rows}</tbody></table></div>`
	);
}

function load_candidate_evidence(frm) {
	frappe.call({
		method: "db_connector.api_fuzzy_review_queue.get_candidate_evidence",
		args: { candidate_name: frm.doc.name },
		callback(response) {
			const payload = response.message || {};
			render_candidate_evidence(frm, payload);
			if (payload.can_submit) add_review_buttons(frm);
			if (payload.can_adjudicate) add_adjudication_buttons(frm);
			if (payload.can_materialize) {
				if ((frappe.user_roles || []).includes("System Manager")) {
					frm.add_custom_button(
						__("Preview Combined Identity Component"),
						() => window.db_connector_identity_overlap.open(
							"CCD Match Review Candidate",
							frm.doc.name,
							() => frm.reload_doc(),
						),
						__("Identity Resolution"),
					);
				}
				frm.add_custom_button(__("Retry Identity Materialization"), () => frappe.call({
					method: "db_connector.api_identity_human.materialize_review_candidate",
					args: { candidate_name: frm.doc.name },
					freeze: true,
					callback: () => frm.reload_doc(),
				}), __("Identity Resolution"));
			}
			if (
				payload.can_reverse_materialization &&
				(frappe.user_roles || []).includes("System Manager")
			) {
				frm.add_custom_button(
					__("Correct Applied Same to Different"),
					() => preview_splink_same_correction(frm),
					__("Identity Resolution"),
				);
			}
			if (
				(frappe.user_roles || []).includes("System Manager") &&
				payload.materialization_status === "Applied" &&
				payload.identity_decision
			) {
				frm.add_custom_button(
					__("Correct Complete Identity Component"),
					() => window.db_connector_identity_correction.open(
						payload.identity_decision,
						() => frm.reload_doc(),
					),
					__("Identity Resolution"),
				);
			}
		},
	});
}

function preview_splink_same_correction(frm) {
	frappe.call({
		method: "db_connector.api_identity_human.preview_reverse_splink_same",
		args: { candidate_name: frm.doc.name },
		freeze: true,
		callback(response) {
			const preview = response.message || {};
			const planned = preview.planned || {};
			const switch_warning = preview.materialization_enabled
				? `<div class="alert alert-danger">${__("Materialization is enabled. Disable it before applying this correction.")}</div>`
				: `<div class="alert alert-success">${__("Materialization is disabled, as required for this correction.")}</div>`;
			const membership_rows = (preview.memberships || []).map((row) =>
				`<li>${esc(row.ccd_master)} — ${esc(row.membership)} (${esc(row.status)})</li>`
			).join("");
			const dialog = new frappe.ui.Dialog({
				title: __("Correct Applied Same Decision"),
				fields: [
					{
						fieldname: "warning",
						fieldtype: "HTML",
						options:
							`<div class="alert alert-warning"><b>${__("This is an audited identity correction, not a record deletion.")}</b><br>` +
							`${__("The two live memberships and their group will end; the old Same decision will be superseded; a new Different decision and one fingerprint-scoped exclusion will be created. CCD Master records are not merged or deleted.")}</div>` +
							switch_warning +
							`<p><b>${__("Candidate")}</b>: ${esc(preview.candidate)}<br>` +
							`<b>${__("Original decision")}</b>: ${esc(preview.original_identity_decision)}<br>` +
							`<b>${__("Identity group")}</b>: ${esc(preview.identity_group)}<br>` +
							`<b>${__("Planned result")}</b>: ${esc(planned.ended_memberships)} ${__("memberships ended")}, ` +
							`${esc(planned.new_exclusions)} ${__("Different exclusion created")}</p><ul>${membership_rows}</ul>`,
					},
					{
						fieldname: "reason",
						fieldtype: "Small Text",
						label: __("Correction reason"),
						reqd: 1,
						description: __("Explain how the false Same decision was discovered."),
					},
					{
						fieldname: "is_demonstration",
						fieldtype: "Check",
						label: __("Development / demonstration correction"),
						default: 1,
					},
					{
						fieldname: "confirm_candidate_name",
						fieldtype: "Data",
						label: __("Type the exact Candidate ID to confirm"),
						reqd: 1,
						description: esc(preview.candidate),
					},
				],
				primary_action_label: __("Apply Audited Correction"),
				primary_action(values) {
					if (values.confirm_candidate_name !== preview.candidate) {
						frappe.msgprint(__("The confirmation must exactly match Candidate {0}.", [preview.candidate]));
						return;
					}
					if (preview.materialization_enabled) {
						frappe.msgprint(__("Disable Materialization, then open this preview again."));
						return;
					}
					frappe.call({
						method: "db_connector.api_identity_human.reverse_applied_splink_same",
						args: {
							candidate_name: preview.candidate,
							reason: values.reason,
							confirm_candidate_name: values.confirm_candidate_name,
							is_demonstration: values.is_demonstration ? 1 : 0,
						},
						freeze: true,
						callback(result) {
							dialog.hide();
							const outcome = result.message || {};
							frappe.msgprint(__("Candidate {0} is now {1}. Correction decision: {2}.", [
								preview.candidate,
								outcome.status || __("Reversed"),
								outcome.correction_decision || "",
							]));
							frm.reload_doc();
						},
					});
				},
			});
			dialog.show();
		},
	});
}

function add_review_buttons(frm) {
	for (const label of ["Same", "Different", "Unsure"]) {
		frm.add_custom_button(__(label), () => submit_review(frm, label), __("Human Review"));
	}
}

function add_adjudication_buttons(frm) {
	for (const label of ["Same", "Different"]) {
		frm.add_custom_button(
			__(`Adjudicate ${label}`),
			() => submit_review(frm, label, true),
			__("Adjudication"),
		);
	}
}

function submit_review(frm, label, adjudication = false) {
	frappe.prompt(
		[{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes"), reqd: adjudication ? 1 : 0 }],
		(values) => frappe.call({
			method: adjudication
				? "db_connector.api_fuzzy_review_queue.adjudicate_candidate_review"
				: "db_connector.api_fuzzy_review_queue.submit_candidate_review",
			args: { candidate_name: frm.doc.name, label, notes: values.notes || "" },
			callback: () => frm.reload_doc(),
		}),
		adjudication ? __(`Adjudicate as ${label}`) : __(`Submit ${label}`),
	);
}

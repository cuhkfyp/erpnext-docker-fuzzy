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
				frm.add_custom_button(__("Retry Identity Materialization"), () => frappe.call({
					method: "db_connector.api_identity_human.materialize_review_candidate",
					args: { candidate_name: frm.doc.name },
					freeze: true,
					callback: () => frm.reload_doc(),
				}), __("Identity Resolution"));
			}
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

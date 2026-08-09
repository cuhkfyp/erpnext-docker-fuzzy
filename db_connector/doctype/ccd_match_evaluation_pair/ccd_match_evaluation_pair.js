frappe.ui.form.on("CCD Match Evaluation Pair", {
	refresh(frm) {
		if (frm.is_new()) return;
		load_evidence(frm);
		if (!frm.doc.stale) {
			for (const label of ["Same", "Different", "Unsure"]) {
				frm.add_custom_button(__(label), () => submit_label(frm, label), __("Review"));
			}
		}
		if (frm.doc.review_status === "Positive Confirmation Required") {
			frm.dashboard.set_headline_alert(
				__("A second independent Same confirmation is required before finalization."),
				"orange",
			);
		}
		if (
			!frm.doc.stale &&
			frm.doc.review_status === "Needs Adjudication" &&
			frappe.user.has_role("System Manager")
		) {
			for (const label of ["Same", "Different"]) {
				frm.add_custom_button(
					__(`Adjudicate ${label}`),
					() => adjudicate(frm, label),
					__("Adjudication"),
				);
			}
		}
	},
});

function load_evidence(frm) {
	frappe.call({
		method: "db_connector.api_fuzzy_evaluation.get_pair_evidence",
		args: { pair_name: frm.doc.name },
		callback(response) {
			const payload = response.message || {};
			const rows = Object.entries(payload.attributes || {}).map(([field, values]) =>
				`<tr><td>${frappe.utils.escape_html(field)}</td>` +
				`<td>${frappe.utils.escape_html(String(values.left || ""))}</td>` +
				`<td>${frappe.utils.escape_html(String(values.right || ""))}</td></tr>`
			);
			const warning = payload.stale
				? '<div class="alert alert-warning">This pair is stale and cannot be used for calibration.</div>'
				: "";
			frm.fields_dict.evidence_html.$wrapper.html(
				`${warning}<table class="table table-bordered"><thead><tr>` +
				"<th>Attribute</th><th>Left record</th><th>Right record</th>" +
				`</tr></thead><tbody>${rows.join("")}</tbody></table>`
			);
		},
	});
}

function adjudicate(frm, label) {
	frappe.prompt(
		[{ fieldname: "notes", fieldtype: "Small Text", label: __("Adjudication Notes"), reqd: 1 }],
		(values) => frappe.call({
			method: "db_connector.api_fuzzy_evaluation.adjudicate_review",
			args: { pair_name: frm.doc.name, label, notes: values.notes },
			callback: () => frm.reload_doc(),
		}),
		__(`Adjudicate as ${label}`),
	);
}

function submit_label(frm, label) {
	frappe.prompt(
		[{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes") }],
		(values) => frappe.call({
			method: "db_connector.api_fuzzy_evaluation.submit_review",
			args: { pair_name: frm.doc.name, label, notes: values.notes || "" },
			callback: () => frm.reload_doc(),
		}),
		__(`Submit ${label} label`),
	);
}

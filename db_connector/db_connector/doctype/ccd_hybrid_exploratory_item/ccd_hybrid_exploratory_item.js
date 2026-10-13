frappe.ui.form.on("CCD Hybrid Exploratory Item", {
	refresh(frm) {
		if (frm.is_new()) return;
		load_exploratory_evidence(frm);
		if (frm.doc.status === "Unreviewed") {
			for (const label of ["Same", "Different", "Unsure"]) {
				frm.add_custom_button(
					__(label),
					() => submit_exploratory_label(frm, label),
					__("Exploratory Review"),
				);
			}
		}
		frm.add_custom_button(__("Back to Batch"), () => {
			frappe.set_route("Form", "CCD Hybrid Exploratory Batch", frm.doc.exploratory_batch);
		});
	},
});

function load_exploratory_evidence(frm) {
	frappe.call({
		method: "db_connector.api_hybrid_exploratory_review.get_hybrid_exploratory_item_evidence",
		args: { item_name: frm.doc.name },
		callback(response) {
			const payload = response.message || {};
			const rows = (payload.attributes || []).map((item) =>
				`<tr><td>${esc_exploratory(item.attribute)}</td>` +
				`<td>${esc_exploratory(item.left)}</td>` +
				`<td>${esc_exploratory(item.right)}</td>` +
				`<td>${esc_exploratory(item.comparison)}</td></tr>`
			).join("");
			const warning = payload.stale
				? `<div class="alert alert-warning">${__("This frozen exploratory pair is stale and cannot be labeled.")}</div>`
				: `<div class="alert alert-blue"><strong>${__("Exploratory only")}</strong>: ${__("this label will not change the Review Candidate or identity state.")}</div>`;
			frm.fields_dict.evidence_html.$wrapper.html(
				`${warning}<table class="table table-bordered"><thead><tr>` +
				`<th>${__("Evidence")}</th><th>${esc_exploratory(payload.left?.source || __("Left"))}</th>` +
				`<th>${esc_exploratory(payload.right?.source || __("Right"))}</th><th>${__("Comparison")}</th>` +
				`</tr></thead><tbody>${rows}</tbody></table>`,
			);
		},
	});
}

function submit_exploratory_label(frm, label) {
	frappe.prompt(
		[{ fieldname: "notes", fieldtype: "Small Text", label: __("Exploratory notes") }],
		(values) => frappe.call({
			method: "db_connector.api_hybrid_exploratory_review.submit_hybrid_exploratory_label",
			args: { item_name: frm.doc.name, label, notes: values.notes || "" },
			freeze: true,
			callback(response) {
				if (response.message?.status === "Stale") {
					frappe.show_alert({
						message: __("This item became stale and was not given an exploratory label."),
						indicator: "orange",
					});
				}
				const next = response.message?.next_item;
				if (next) frappe.set_route("Form", "CCD Hybrid Exploratory Item", next);
				else frappe.set_route("Form", "CCD Hybrid Exploratory Batch", frm.doc.exploratory_batch);
			},
		}),
		__(`Record exploratory ${label}`),
	);
}

function esc_exploratory(value) {
	return frappe.utils.escape_html(String(value ?? ""));
}

frappe.ui.form.on("CCD Master", {
	refresh(frm) {
		label_legacy_matching_fields(frm);
		load_identity_resolution(frm);
	},
});

function label_legacy_matching_fields(frm) {
	const labels = {
		match_ct: __("Legacy Fuzzy Match Count"),
		is_matched: __("Legacy Is Matched?"),
		match_table: __("Legacy Fuzzy Matching"),
		btn_match: __("Run Legacy Fuzzy Matching"),
	};
	for (const [fieldname, label] of Object.entries(labels)) {
		if (frm.fields_dict[fieldname]) frm.set_df_property(fieldname, "label", label);
	}
}

function identity_esc(value) {
	return frappe.utils.escape_html(String(value || ""));
}

function identity_member_label(member) {
	const base = `${member.alias}${member.source ? ` — ${member.source}` : ""}`;
	if (!member.record_id) return identity_esc(base);
	const href = `/app/ccd-master/${encodeURIComponent(member.record_id)}`;
	return `<a href="${href}" target="_blank" rel="noopener">${identity_esc(base)}</a>`;
}

function render_identity_resolution(frm, payload) {
	const wrapper = frm.fields_dict.ccd_identity_resolution_html?.$wrapper;
	if (!wrapper) return;
	if (payload.status === "Unlinked") {
		wrapper.html(
			'<div class="alert alert-info">' +
			__("Unlinked. No CCD record is physically merged; approved identity links will appear here.") +
			"</div>",
		);
		return;
	}
	const warning = payload.status === "Needs Revalidation"
		? '<div class="alert alert-danger">' + __("Governed identity evidence changed. This membership needs manager revalidation and cannot support new automatic decisions.") + "</div>"
		: "";
	const duplicate = payload.same_source_duplicate_warning
		? '<div class="alert alert-warning">' + __("This human-confirmed group contains more than one record from the same governed source.") + "</div>"
		: "";
	const privacy = payload.sensitive_values_visible
		? '<div class="alert alert-warning">' + __("Your role permits links to the participating CCD records.") + "</div>"
		: '<div class="alert alert-info">' + __("Members are shown as masked aliases. Sensitive Reviewers and System Managers can open the source records.") + "</div>";
	const rows = (payload.members || []).map((member) =>
		`<tr><td>${identity_member_label(member)}${member.is_this_record ? ` <strong>(${__("this record")})</strong>` : ""}</td>` +
		`<td>${identity_esc(member.status)}</td></tr>`
	).join("");
	const groupLink = payload.identity_group
		? `<a href="/app/ccd-identity-group/${encodeURIComponent(payload.identity_group)}">${identity_esc(payload.identity_group)}</a>`
		: "";
	wrapper.html(
		warning + duplicate + privacy +
		`<p><strong>${__("Identity status")}</strong>: ${identity_esc(payload.status)}</p>` +
		`<p><strong>${__("Identity Group")}</strong>: ${groupLink}</p>` +
		`<p><strong>${__("Decision origin")}</strong>: ${identity_esc(payload.decision_origin)} — ${identity_esc(payload.policy_version)}</p>` +
		`<div class="table-responsive"><table class="table table-bordered"><thead><tr><th>${__("Member")}</th><th>${__("Status")}</th></tr></thead><tbody>${rows}</tbody></table></div>` +
		'<div class="text-muted">' + __("Relationship links are reversible. CCD Master source documents remain unchanged.") + "</div>",
	);
}

function load_identity_resolution(frm) {
	const wrapper = frm.fields_dict.ccd_identity_resolution_html?.$wrapper;
	if (!wrapper || frm.is_new()) return;
	frappe.call({
		method: "db_connector.api_identity_resolution.get_identity_resolution",
		args: { ccd_master_name: frm.doc.name },
		callback(response) {
			render_identity_resolution(frm, response.message || {});
		},
		error() {
			wrapper.html('<div class="text-muted">' + __("Identity Resolution details are not available for your role.") + "</div>");
		},
	});
}

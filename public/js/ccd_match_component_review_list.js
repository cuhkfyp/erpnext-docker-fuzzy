const CCD_COMPONENT_BULK_LIMIT = 25;

frappe.listview_settings["CCD Match Component Review"] = {
	onload(listview) {
		if (!frappe.user_roles.includes("System Manager")) return;
		listview.page.add_actions_menu_item(
			__("Preview Selected Identity Materialization"),
			() => preview_selected_component_materialization(listview),
		);
	},
};

function component_bulk_esc(value) {
	return frappe.utils.escape_html(String(value || ""));
}

function selected_component_names(listview) {
	const names = listview.get_checked_items(true);
	if (!names.length) {
		frappe.msgprint(__("Select one or more Component Review rows first."));
		return [];
	}
	if (names.length > CCD_COMPONENT_BULK_LIMIT) {
		frappe.msgprint(
			__("Select at most {0} components per operation. You selected {1}.", [
				CCD_COMPONENT_BULK_LIMIT,
				names.length,
			]),
		);
		return [];
	}
	return names;
}

function preview_selected_component_materialization(listview) {
	const names = selected_component_names(listview);
	if (!names.length) return;
	frappe.call({
		method: "db_connector.api_identity_human.preview_component_materializations",
		args: { review_names: JSON.stringify(names) },
		freeze: true,
		freeze_message: __("Running identity safety preview..."),
		callback(response) {
			show_component_materialization_preview(listview, names, response.message || {});
		},
	});
}

function component_review_link(name) {
	const href = `/app/ccd-match-component-review/${encodeURIComponent(name)}`;
	return `<a href="${href}" target="_blank" rel="noopener">${component_bulk_esc(name)}</a>`;
}

function show_component_materialization_preview(listview, names, payload) {
	const rows = (payload.rows || []).map((row) => {
		const outcome = row.eligible
			? `<span class="indicator-pill green">${__("Ready")}</span>`
			: `<span class="indicator-pill red">${component_bulk_esc(row.error || row.conflicts?.join(", ") || __("Blocked"))}</span>`;
		return `<tr>` +
			`<td>${component_review_link(row.review)}</td>` +
			`<td>${component_bulk_esc(row.final_decision)}</td>` +
			`<td>${component_bulk_esc(row.materialization_status)}</td>` +
			`<td>${component_bulk_esc(row.record_count)}</td>` +
			`<td>${component_bulk_esc(row.group_count)}</td>` +
			`<td>${component_bulk_esc(row.membership_count)}</td>` +
			`<td>${component_bulk_esc(row.exclusion_count)}</td>` +
			`<td>${outcome}</td>` +
			`</tr>`;
	}).join("");
	const totals = payload.totals || {};
	const switch_notice = payload.materialization_enabled
		? '<div class="alert alert-warning">' +
			__("Materialization is enabled. Applying will create the live identity objects shown below.") +
			"</div>"
		: '<div class="alert alert-info">' +
			__("This preview made no changes. Materialization is disabled; enable it in Identity Resolution Settings before applying.") +
			"</div>";
	const summary = `<p>${__("The checked rows define the exact operation size: {0} component(s), with a maximum of {1}.", [payload.selected_count || names.length, payload.max_components || CCD_COMPONENT_BULK_LIMIT])}</p>` +
		`<p><strong>${__("Planned totals")}</strong>: ` +
		`${component_bulk_esc(totals.record_count)} ${__("records")}, ` +
		`${component_bulk_esc(totals.group_count)} ${__("groups")}, ` +
		`${component_bulk_esc(totals.membership_count)} ${__("memberships")}, ` +
		`${component_bulk_esc(totals.exclusion_count)} ${__("Different relationships")}.</p>`;
	const table = `<div class="table-responsive"><table class="table table-bordered table-sm">` +
		`<thead><tr><th>${__("Component Review")}</th><th>${__("Decision")}</th>` +
		`<th>${__("Current status")}</th><th>${__("Records")}</th><th>${__("Groups")}</th>` +
		`<th>${__("Memberships")}</th><th>${__("Different")}</th><th>${__("Preview")}</th></tr></thead>` +
		`<tbody>${rows}</tbody></table></div>`;

	const dialog = new frappe.ui.Dialog({
		title: __("Selected Identity Materialization Preview"),
		size: "extra-large",
		fields: [{ fieldname: "preview_html", fieldtype: "HTML" }],
	});
	dialog.fields_dict.preview_html.$wrapper.html(switch_notice + summary + table);
	if (payload.materialization_enabled && payload.all_eligible) {
		dialog.set_primary_action(__("Materialize Selected ({0})", [names.length]), () => {
			frappe.confirm(
				__("Apply exactly these {0} component(s)? The operation is atomic: if one no longer passes, none are committed.", [names.length]),
				() => apply_selected_component_materialization(listview, dialog, names),
			);
		});
	}
	dialog.show();
}

function apply_selected_component_materialization(listview, dialog, names) {
	frappe.call({
		method: "db_connector.api_identity_human.materialize_component_reviews",
		args: { review_names: JSON.stringify(names) },
		freeze: true,
		freeze_message: __("Materializing selected identity decisions..."),
		callback(response) {
			const result = response.message || {};
			dialog.hide();
			frappe.msgprint({
				title: __("Identity Materialization Applied"),
				indicator: "green",
				message: __("Applied {0} component(s): {1} groups, {2} memberships, and {3} Different relationships created.", [
					result.selected_count || 0,
					result.created_groups || 0,
					result.created_memberships || 0,
					result.created_exclusions || 0,
				]),
			});
			listview.clear_checked_items();
			listview.refresh();
		},
	});
}

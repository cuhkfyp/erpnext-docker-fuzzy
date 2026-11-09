frappe.ui.form.on("CCD Tiered One-to-Many Batch", {
	refresh(frm) {
		clearTimeout(frm.__one_to_many_progress_timer);
		if (frm.is_new()) return;
		load_batch_progress(frm);
		frm.add_custom_button(__("Refresh Progress"), () => load_batch_progress(frm));
		frm.add_custom_button(__("Open Validation"), () => {
			frappe.set_route("Form", "CCD Tiered One-to-Many Validation", frm.doc.validation);
		});
		frm.add_custom_button(__("View Components"), () => {
			frappe.set_route("List", "CCD Match Component Review", {
				tiered_automation_batch: frm.doc.name,
			});
		});
	},
});

function load_batch_progress(frm) {
	clearTimeout(frm.__one_to_many_progress_timer);
	frappe.call({
		method: "db_connector.api_tiered_one_to_many.get_one_to_many_batch_progress",
		args: { batch_name: frm.doc.name },
		callback(response) {
			const progress = response.message || {};
			render_batch_progress(frm, progress);
			if (["Queued", "Applying"].includes(progress.status)) {
				frm.__one_to_many_progress_timer = setTimeout(
					() => load_batch_progress(frm),
					2000,
				);
			}
		},
	});
}

function render_batch_progress(frm, progress) {
	const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
	const selected = Number(progress.selected_component_count || 0);
	const completed = Number(progress.completed_component_count || 0);
	const applied = Number(progress.applied_component_count || 0);
	const exceptions = Number(progress.exception_component_count || 0);
	const pending = Number(progress.pending_component_count || 0);
	const percent = Math.max(0, Math.min(100, Number(progress.percent_complete || 0)));
	const outcome_class = {
		Applied: "green",
		Exception: "orange",
		Queued: "blue",
		Pending: "gray",
	};
	const rows = (progress.items || []).map((item, index) => {
		const component = esc(item.component_review);
		const link = `<a href="/app/ccd-match-component-review/${encodeURIComponent(item.component_review)}">${component}</a>`;
		const color = outcome_class[item.outcome] || "gray";
		const reason = item.reason
			? `<div class="text-danger small"><strong>${__("Reason")}:</strong> ${esc(item.reason)}</div>`
			: "";
		return `<tr>` +
			`<td>${index + 1}</td>` +
			`<td>${link}</td>` +
			`<td><span class="indicator-pill ${color}">${esc(item.outcome)}</span>${reason}</td>` +
			`</tr>`;
	}).join("");

	frm.dashboard.parent.find(".ccd-one-to-many-batch-progress").remove();
	frm.dashboard.add_section(
		`<div class="mb-3">` +
			`<p><strong>${__("Status")}:</strong> ${esc(progress.status)} &nbsp; ` +
			`<strong>${__("Completed")}:</strong> ${completed} / ${selected} &nbsp; ` +
			`<strong>${__("Applied")}:</strong> ${applied} &nbsp; ` +
			`<strong>${__("Exceptions")}:</strong> ${exceptions} &nbsp; ` +
			`<strong>${__("Pending")}:</strong> ${pending}</p>` +
			`<div class="progress" style="height: 18px;">` +
				`<div class="progress-bar" role="progressbar" style="width: ${percent}%;" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100">${percent}%</div>` +
			`</div>` +
		`</div>` +
		`<div class="table-responsive"><table class="table table-bordered table-sm">` +
			`<thead><tr><th>#</th><th>${__("Component Review")}</th><th>${__("Outcome / Reason")}</th></tr></thead>` +
			`<tbody>${rows || `<tr><td colspan="3" class="text-muted">${__("No frozen components")}</td></tr>`}</tbody>` +
		`</table></div>`,
		__("Live Batch Progress"),
		"custom ccd-one-to-many-batch-progress",
	);

	frm.doc.status = progress.status;
	frm.doc.applied_component_count = applied;
	frm.doc.exception_component_count = exceptions;
	frm.doc.qc_selected_count = Number(progress.qc_selected_count || 0);
	[
		"status",
		"applied_component_count",
		"exception_component_count",
		"qc_selected_count",
	].forEach((fieldname) => frm.refresh_field(fieldname));
}

frappe.ui.form.on("CCD Unified Person Backfill Run", {
	refresh(frm) {
		stop_unified_person_progress_poll(frm);
		render_unified_person_run_progress(frm, frm.doc);

		frm.add_custom_button(__("Refresh Progress"), () => {
			refresh_unified_person_run_progress(frm, true);
		});

		if (["Running", "Failed"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Queue / Resume in Background"), () => {
				frappe.call({
					method: "db_connector.api_unified_person.enqueue_unified_person_reconciliation",
					args: { backfill_run: frm.doc.name, max_batches: 25 },
					freeze: true,
					callback() {
						refresh_unified_person_run_progress(frm, true);
					},
				});
			}, __("Actions"));
		}

		frm.add_custom_button(__("Identity Resolution Settings"), () => {
			frappe.set_route(
				"Form",
				"CCD Identity Resolution Settings",
				"CCD Identity Resolution Settings",
			);
		}, __("Actions"));

		if (frm.doc.status === "Running") {
			frm.__unified_person_progress_timer = setInterval(() => {
				if (window.cur_frm !== frm) {
					stop_unified_person_progress_poll(frm);
					return;
				}
				refresh_unified_person_run_progress(frm, false);
			}, 3000);
		}
	},
});

function stop_unified_person_progress_poll(frm) {
	if (frm.__unified_person_progress_timer) {
		clearInterval(frm.__unified_person_progress_timer);
		frm.__unified_person_progress_timer = null;
	}
}

function frozen_reconciliation_total(doc) {
	try {
		const preview = JSON.parse(doc.preview_json || "{}");
		return Number(preview.remaining_ccd_master_count || 0);
	} catch (error) {
		return 0;
	}
}

function display_count(value) {
	return Number(value || 0).toLocaleString();
}

function render_unified_person_run_progress(frm, status) {
	const total = frozen_reconciliation_total(frm.doc);
	const remaining = Number(status.remaining_record_count || 0);
	const completed = Math.max(0, total - remaining);
	const percent = total ? Math.min(100, (completed / total) * 100) : 0;
	const batches = Number(status.batch_count || 0);
	const last_batch = status.last_batch_at || __("Not committed yet");
	const worker_active = Boolean(status.background_job_enqueued);

	let message = __(
		"{0} / {1} records reconciled; {2} committed batch(es); last commit: {3}.",
		[display_count(completed), display_count(total), batches, last_batch],
	);
	if (status.status === "Running" && batches === 0 && worker_active) {
		message += " " + __(
			"The first batch is active. Each batch is atomic, so counters update only after its commit.",
		);
	}
	if (status.status === "Running" && !worker_active) {
		message += " " + __(
			"No background worker currently owns this run; use Queue / Resume in Background.",
		);
	}

	frm.dashboard.show_progress(__("Unified Person reconciliation"), percent, message);
	if (status.status === "Completed") {
		frm.dashboard.set_headline_alert(
			__("Reconciliation completed: {0} records across {1} committed batches.", [
				display_count(total),
				batches,
			]),
			"green",
		);
		stop_unified_person_progress_poll(frm);
	} else if (status.status === "Failed") {
		frm.dashboard.set_headline_alert(
			__("Reconciliation failed after {0} committed batches. Review Error, then use Queue / Resume in Background.", [batches]),
			"red",
		);
		stop_unified_person_progress_poll(frm);
	} else {
		frm.dashboard.set_headline_alert(
			worker_active
				? __("Background reconciliation is active. This page refreshes every three seconds.")
				: __("Reconciliation is recorded as Running but is not currently queued."),
			worker_active ? "blue" : "orange",
		);
	}
}

function refresh_unified_person_run_progress(frm, show_alert) {
	if (frm.__unified_person_progress_request) return;
	frm.__unified_person_progress_request = true;
	frappe.call({
		method: "db_connector.api_unified_person.get_unified_person_backfill_status",
		callback(response) {
			const status = response.message || {};
			if (status.backfill_run !== frm.doc.name) {
				stop_unified_person_progress_poll(frm);
				return;
			}
			[
				"status",
				"processed_record_count",
				"issued_person_count",
				"created_membership_count",
				"remaining_record_count",
				"batch_count",
				"last_batch_at",
				"completed_at",
				"completed_by",
				"error_summary",
			].forEach((fieldname) => {
				if (status[fieldname] !== undefined) {
					frm.doc[fieldname] = status[fieldname];
					frm.refresh_field(fieldname);
				}
			});
			status.background_job_enqueued = Boolean(status.background_job_enqueued);
			render_unified_person_run_progress(frm, status);
			if (show_alert) {
				frappe.show_alert({
					message: __("Reconciliation progress refreshed"),
					indicator: "blue",
				});
			}
		},
		always() {
			frm.__unified_person_progress_request = false;
		},
	});
}

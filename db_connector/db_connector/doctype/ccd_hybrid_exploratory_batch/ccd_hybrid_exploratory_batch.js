frappe.ui.form.on("CCD Hybrid Exploratory Batch", {
	refresh(frm) {
		if (frm.is_new()) {
			frm.disable_save();
			frm.set_intro(
				__("Do not create this document manually. Use the governed action so the authorized queue, deterministic seed, provenance, and exact sample are frozen by the server."),
				"blue",
			);
			frm.add_custom_button(
				__("Create / Reopen Governed Batch"),
				() => prompt_hybrid_exploratory_batch_from_form(),
				__("Exploratory Review"),
			);
			if (!frm.__hybrid_batch_prompted) {
				frm.__hybrid_batch_prompted = true;
				setTimeout(() => prompt_hybrid_exploratory_batch_from_form(), 0);
			}
			return;
		}
		render_progress(frm);
		frm.add_custom_button(__("View Sample Items"), () => {
			frappe.set_route("List", "CCD Hybrid Exploratory Item", {
				exploratory_batch: frm.doc.name,
			});
		});
		if (["Reviewing", "Completed", "Completed with Stale Items"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Review Next Unreviewed"), () => {
				frappe.call({
					method: "db_connector.api_hybrid_exploratory_review.get_next_hybrid_exploratory_item",
					args: { batch_name: frm.doc.name },
					callback(response) {
						const item = response.message?.item;
						if (item) frappe.set_route("Form", "CCD Hybrid Exploratory Item", item);
						else frappe.msgprint(__("No unreviewed exploratory item remains."));
					},
				});
			}, __("Exploratory Review"));
		}
		if (frm.doc.status === "Failed" && frappe.user.has_role("System Manager")) {
			frm.add_custom_button(__("Retry Build"), () => {
				frappe.call({
					method: "db_connector.api_hybrid_exploratory_review.retry_hybrid_exploratory_batch",
					args: { batch_name: frm.doc.name },
					callback: () => frm.reload_doc(),
				});
			}, __("Exploratory Review"));
		}
		if (frm.doc.status === "Building") {
			frm.dashboard.set_headline_alert(
				__("The deterministic sample is being frozen on the long worker. This page may be closed safely."),
				"blue",
			);
			setTimeout(() => {
				if (window.cur_frm === frm) frm.reload_doc();
			}, 4000);
		}
	},
});

function prompt_hybrid_exploratory_batch_from_form() {
	frappe.prompt(
		[
			{
				fieldname: "minimum_score",
				fieldtype: "Float",
				label: __("Research minimum Splink score"),
				default: 0.95,
				reqd: 1,
				description: __("Creates or reopens the deterministic 100-pair batch. The score must remain below the frozen ultra-high cutoff."),
			},
		],
		(values) => create_hybrid_exploratory_batch_from_form(values.minimum_score),
		__("Create / Reopen Governed Exploratory Batch"),
	);
}

function create_hybrid_exploratory_batch_from_form(minimumScore) {
	frappe.call({
		method: "db_connector.api_hybrid_exploratory_review.start_hybrid_exploratory_batch",
		args: { minimum_score: minimumScore },
		freeze: true,
		freeze_message: __("Creating or reopening the governed exploratory batch…"),
		callback(response) {
			const batch = response.message?.batch;
			if (batch) frappe.set_route("Form", "CCD Hybrid Exploratory Batch", batch);
		},
	});
}

function render_progress(frm) {
	const field = frm.get_field("progress_html");
	if (!field?.$wrapper) return;
	const sample = Number(frm.doc.sample_count || 0);
	const reviewed = Number(frm.doc.reviewed_count || 0);
	const percent = sample ? Math.round((reviewed / sample) * 100) : 0;
	field.$wrapper.html(
		`<div class="progress" style="height:20px"><div class="progress-bar" role="progressbar" style="width:${percent}%">${percent}%</div></div>` +
		`<p>${__("Reviewed")}: ${reviewed} / ${sample}; ${__("Remaining")}: ${Number(frm.doc.remaining_count || 0)}; ` +
		`${__("Same")}: ${Number(frm.doc.same_count || 0)}; ${__("Different")}: ${Number(frm.doc.different_count || 0)}; ` +
		`${__("Unsure")}: ${Number(frm.doc.unsure_count || 0)}; ${__("Stale")}: ${Number(frm.doc.stale_count || 0)}</p>` +
		`<p class="text-muted"><strong>${__("Exploratory labels are isolated from candidate decisions and cannot authorize identity materialization.")}</strong></p>`,
	);
}

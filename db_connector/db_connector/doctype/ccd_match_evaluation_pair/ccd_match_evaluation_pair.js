frappe.ui.form.on("CCD Match Evaluation Pair", {
	refresh(frm) {
		if (frm.is_new()) return;
		configure_governed_navigation(frm);
		load_evidence(frm);
		if (
			!frm.doc.stale &&
			["Unreviewed", "Partially Reviewed", "Positive Confirmation Required"].includes(
				frm.doc.review_status,
			)
		) {
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
				? `<div class="alert alert-warning">${payload.historical_source_retired
					? __("Historical source retired. This pair remains as audit history and cannot be used for calibration.")
					: __("This pair is stale and cannot be used for calibration.")}</div>`
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
			callback: () => continue_governed_review_or_reload(frm, "adjudication"),
		}),
		__(`Adjudicate as ${label}`),
	);
}

function continue_governed_review_or_reload(frm, reviewMode = "ordinary") {
	frappe.db.get_value("CCD Match Evaluation Run", frm.doc.evaluation_run, "run_purpose")
		.then((response) => {
			const runPurpose = response.message?.run_purpose;
			if (runPurpose === "Splink Automatic Validation") {
				navigate_splink_validation(frm, false, reviewMode, true);
				return;
			}
			if (runPurpose !== "Hybrid Prospective Validation") {
				frm.reload_doc();
				return;
			}
			frappe.call({
				method: "db_connector.api_hybrid_validation.get_next_hybrid_review_pair",
				args: { pair_name: frm.doc.name },
				callback(nextResponse) {
					const pair = nextResponse.message?.pair;
					if (pair) frappe.set_route("Form", "CCD Match Evaluation Pair", pair);
					else frappe.set_route("Form", "CCD Match Evaluation Run", frm.doc.evaluation_run);
				},
			});
		})
		.catch(() => frm.reload_doc());
}

function configure_governed_navigation(frm) {
	if (!frm.__ccd_native_navigate_records) {
		frm.__ccd_native_navigate_records = frm.navigate_records.bind(frm);
		frm.navigate_records = (previous) => {
			if (frm.__ccd_run_purpose === "Splink Automatic Validation") {
				const mode = frm.doc.review_status === "Needs Adjudication"
					&& frappe.user.has_role("System Manager")
					? "adjudication"
					: "ordinary";
				navigate_splink_validation(frm, Boolean(previous), mode, false);
				return;
			}
			frm.__ccd_native_navigate_records(previous);
		};
	}
	frappe.db.get_value("CCD Match Evaluation Run", frm.doc.evaluation_run, "run_purpose")
		.then((response) => {
			frm.__ccd_run_purpose = response.message?.run_purpose || "";
		});
}

function navigate_splink_validation(frm, previous, reviewMode, returnToRunWhenDone) {
	frappe.call({
		method: "db_connector.api_splink_automation.get_next_splink_validation_pair",
		args: {
			run_name: frm.doc.evaluation_run,
			current_pair: frm.doc.name,
			previous: previous ? 1 : 0,
			review_mode: reviewMode,
		},
		callback(response) {
			const pair = response.message?.pair;
			if (pair) {
				frappe.set_route("Form", "CCD Match Evaluation Pair", pair);
				return;
			}
			frappe.show_alert({
				message: reviewMode === "adjudication"
					? __("No further Splink pairs require adjudication.")
					: __("You have completed your ordinary reviews in this Splink cohort."),
				indicator: "green",
			});
			if (returnToRunWhenDone) {
				frappe.set_route("Form", "CCD Match Evaluation Run", frm.doc.evaluation_run);
			}
		},
	});
}

function submit_label(frm, label) {
	frappe.prompt(
		[{ fieldname: "notes", fieldtype: "Small Text", label: __("Notes") }],
		(values) => frappe.call({
			method: "db_connector.api_fuzzy_evaluation.submit_review",
			args: { pair_name: frm.doc.name, label, notes: values.notes || "" },
			callback: () => continue_governed_review_or_reload(frm),
		}),
		__(`Submit ${label} label`),
	);
}

(function () {
	"use strict";

	function esc(value) {
		return frappe.utils.escape_html(String(value || ""));
	}

	function correction_api(method, args, callback) {
		frappe.call({
			method: `db_connector.api_identity_correction.${method}`,
			args,
			freeze: true,
			freeze_message: __("Checking the complete identity component..."),
			callback,
		});
	}

	function partition_fields(context) {
		const group_for_record = {};
		let group_number = 0;
		for (const group of context.current_groups || []) {
			if ((group || []).length < 2) continue;
			group_number += 1;
			for (const record_id of group) group_for_record[record_id] = `Group ${group_number}`;
		}
		const options = [__("Separate")];
		for (let index = 1; index <= (context.records || []).length; index += 1) {
			options.push(`Group ${index}`);
		}
		return (context.records || []).map((record, index) => ({
			fieldname: `partition_${index}`,
			fieldtype: "Select",
			label: `${record.record_id} — ${record.source || __("Unknown source")}`,
			options: options.join("\n"),
			default: group_for_record[record.record_id] || __("Separate"),
			reqd: 1,
			description: `<a href="/app/ccd-master/${encodeURIComponent(record.record_id)}" target="_blank" rel="noopener">${__("Open CCD Master")}</a>`,
		}));
	}

	function groups_from_values(context, values) {
		const named_groups = {};
		const singletons = [];
		(context.records || []).forEach((record, index) => {
			const label = values[`partition_${index}`];
			if (!label || label === __("Separate")) {
				singletons.push([record.record_id]);
				return;
			}
			if (!named_groups[label]) named_groups[label] = [];
			named_groups[label].push(record.record_id);
		});
		return singletons.concat(Object.keys(named_groups).sort().map((key) => named_groups[key]));
	}

	function group_text(groups) {
		return (groups || []).map((group) => group.length > 1
			? group.map(esc).join(" = ")
			: `${esc(group[0])} (${__("separate")})`
		).join("<br>");
	}

	function warning_text(code) {
		const labels = {
			complete_hkid_conflict_governance_override: __("A proposed Same group contains conflicting complete HKIDs."),
			same_source_duplicates_governance_override: __("A proposed Same group contains more than one record from the same governed source."),
			current_membership_needs_revalidation: __("At least one current Membership already needs revalidation."),
			current_group_needs_revalidation: __("At least one current Identity Group already needs revalidation."),
			scope_contains_already_superseded_decision_provenance: __("The expanded scope includes historical decision provenance that is already superseded."),
		};
		return labels[code] || code;
	}

	function show_apply_dialog(preview, on_complete) {
		const planned = preview.planned || {};
		const warnings = preview.warnings || [];
		const materialization_notice = preview.materialization_enabled
			? `<div class="alert alert-danger">${__("Materialization is enabled. Disable it before Apply.")}</div>`
			: `<div class="alert alert-success">${__("Materialization is disabled, as required.")}</div>`;
		const warning_html = warnings.length
			? `<div class="alert alert-danger"><b>${__("Safety warnings requiring explicit confirmation")}</b><ul>${warnings.map((item) => `<li>${esc(warning_text(item))}</li>`).join("")}</ul></div>`
			: "";
		const fields = [
			{
				fieldname: "summary",
				fieldtype: "HTML",
				options:
					`<div class="alert alert-warning"><b>${__("This replaces relationship objects; it never merges or deletes CCD Master records.")}</b></div>` +
					materialization_notice + warning_html +
					`<p><b>${__("Source decision")}</b>: ${esc(preview.source_identity_decision)} (${esc(preview.source_origin)})<br>` +
					`<b>${__("Replacement type")}</b>: ${esc(planned.replacement_decision_type)}<br>` +
					`<b>${__("Will end")}</b>: ${esc(planned.ended_groups)} ${__("groups")}, ${esc(planned.ended_memberships)} ${__("memberships")}<br>` +
					`<b>${__("Will supersede")}</b>: ${esc(planned.superseded_decisions)} ${__("decisions")}, ${esc(planned.superseded_exclusions)} ${__("exclusions")}<br>` +
					`<b>${__("Will create")}</b>: ${esc(planned.new_groups)} ${__("groups")}, ${esc(planned.new_memberships)} ${__("memberships")}, ${esc(planned.new_exclusions)} ${__("exclusions")}</p>` +
					`<p><b>${__("Replacement partition")}</b><br>${group_text(preview.replacement_groups)}</p>`,
			},
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Correction reason"),
				reqd: 1,
				description: __("Explain how the prior identity decision was found to be wrong and what evidence supports this replacement."),
			},
			{
				fieldname: "is_demonstration",
				fieldtype: "Check",
				label: __("Development / demonstration correction"),
				default: 1,
			},
		];
		if (warnings.length) {
			fields.push({
				fieldname: "confirm_safety_warnings",
				fieldtype: "Check",
				label: __("I explicitly accept every safety warning above"),
			});
		}
		fields.push({
			fieldname: "confirm_source_decision",
			fieldtype: "Data",
			label: __("Type the exact source Identity Decision ID"),
			description: esc(preview.source_identity_decision),
			reqd: 1,
		});

		const dialog = new frappe.ui.Dialog({
			title: __("Apply Complete Identity Correction"),
			fields,
			primary_action_label: __("Apply Audited Correction"),
			primary_action(values) {
				if (preview.materialization_enabled) {
					frappe.msgprint(__("Disable Materialization and run the preview again."));
					return;
				}
				if (values.confirm_source_decision !== preview.source_identity_decision) {
					frappe.msgprint(__("The confirmation must exactly match the source Identity Decision ID."));
					return;
				}
				if (warnings.length && !values.confirm_safety_warnings) {
					frappe.msgprint(__("Explicitly confirm every displayed safety warning."));
					return;
				}
				frappe.call({
					method: "db_connector.api_identity_correction.apply_complete_component_correction",
					args: {
						source_decision: preview.source_identity_decision,
						replacement_groups_json: JSON.stringify(preview.replacement_groups || []),
						expected_scope_fingerprint: preview.scope_fingerprint,
						reason: values.reason,
						confirm_source_decision: values.confirm_source_decision,
						confirm_safety_warnings: values.confirm_safety_warnings ? 1 : 0,
						is_demonstration: values.is_demonstration ? 1 : 0,
					},
					freeze: true,
					freeze_message: __("Applying the audited complete-component correction..."),
					callback(response) {
						dialog.hide();
						const result = response.message || {};
						frappe.msgprint(
							`${__("Correction applied")}: <a href="/app/ccd-identity-correction/${encodeURIComponent(result.correction || "")}">${esc(result.correction)}</a><br>` +
							`${__("Replacement decision")}: <a href="/app/ccd-identity-decision/${encodeURIComponent(result.replacement_identity_decision || "")}">${esc(result.replacement_identity_decision)}</a>`,
						);
						if (on_complete) on_complete(result);
					},
				});
			},
		});
		dialog.show();
	}

	function open(source_decision, on_complete) {
		correction_api("get_complete_component_correction_context", { source_decision }, (response) => {
			const context = response.message || {};
			const materialization_notice = context.materialization_enabled
				? `<div class="alert alert-danger">${__("Materialization is enabled. You may design and preview the replacement, but Apply will remain blocked until it is disabled.")}</div>`
				: `<div class="alert alert-success">${__("Materialization is disabled, as required before Apply.")}</div>`;
			const dialog = new frappe.ui.Dialog({
				title: __("Design Complete Identity Replacement"),
				fields: [
					{
						fieldname: "instructions",
						fieldtype: "HTML",
						options: materialization_notice +
							`<p>${__("Assign records that are the same person to the same Group label. Leave each unrelated record as Separate. The operation always includes the complete current identity scope.")}</p>` +
							`<p><b>${__("Source decision")}</b>: ${esc(context.source_identity_decision)} (${esc(context.source_origin)})<br>` +
							`<b>${__("Bounded scope")}</b>: ${esc((context.records || []).length)} ${__("records")}; ${esc(context.current_group_count)} ${__("current groups")}; ${esc(context.current_exclusion_count)} ${__("current exclusions")}</p>`,
					},
					{ fieldtype: "Section Break", label: __("Replacement partition") },
					...partition_fields(context),
				],
				primary_action_label: __("Preview Replacement"),
				primary_action(values) {
					const replacement_groups = groups_from_values(context, values);
					correction_api(
						"preview_complete_component_correction",
						{
							source_decision: context.source_identity_decision,
							replacement_groups_json: JSON.stringify(replacement_groups),
						},
						(preview_response) => {
							const preview = preview_response.message || {};
							if (!preview.changed) {
								frappe.msgprint(__("The replacement partition is identical to the current identity state."));
								return;
							}
							dialog.hide();
							show_apply_dialog(preview, on_complete);
						},
					);
				},
			});
			dialog.show();
		});
	}

	window.db_connector_identity_correction = { open };
})();

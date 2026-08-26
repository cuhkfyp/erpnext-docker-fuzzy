(function () {
	"use strict";

	function esc(value) {
		return frappe.utils.escape_html(String(value || ""));
	}

	function api(method, args, callback) {
		frappe.call({
			method: `db_connector.api_identity_overlap.${method}`,
			args,
			freeze: true,
			freeze_message: __("Expanding the complete identity scope..."),
			callback,
		});
	}

	function groupText(groups) {
		return (groups || []).map((group) => (group || []).length > 1
			? group.map(esc).join(" = ")
			: `${esc((group || [""])[0])} (${__("separate")})`
		).join("<br>");
	}

	function partitionFields(context) {
		const groupForRecord = {};
		(context.default_groups || []).forEach((group, index) => {
			if ((group || []).length < 2) return;
			for (const recordId of group) groupForRecord[recordId] = `Group ${index + 1}`;
		});
		const options = [__("Separate")];
		for (let index = 1; index <= (context.records || []).length; index += 1) {
			options.push(`Group ${index}`);
		}
		return (context.records || []).map((record, index) => ({
			fieldname: `overlap_partition_${index}`,
			fieldtype: "Select",
			label: `${record.record_id} — ${record.source || __("Unknown source")}`,
			options: options.join("\n"),
			default: groupForRecord[record.record_id] || __("Separate"),
			reqd: 1,
			description: `<a href="/app/ccd-master/${encodeURIComponent(record.record_id)}" target="_blank" rel="noopener">${__("Open CCD Master")}</a>`,
		}));
	}

	function groupsFromValues(context, values) {
		if (values.overlap_decision_mode === "All Same") {
			return [(context.records || []).map((record) => record.record_id)];
		}
		if (values.overlap_decision_mode === "All Different") {
			return (context.records || []).map((record) => [record.record_id]);
		}
		const namedGroups = {};
		const singletons = [];
		(context.records || []).forEach((record, index) => {
			const label = values[`overlap_partition_${index}`];
			if (!label || label === __("Separate")) {
				singletons.push([record.record_id]);
				return;
			}
			if (!namedGroups[label]) namedGroups[label] = [];
			namedGroups[label].push(record.record_id);
		});
		return singletons.concat(Object.keys(namedGroups).sort().map((key) => namedGroups[key]));
	}

	function documentLink(scope) {
		const routes = {
			"CCD Match Review Candidate": "ccd-match-review-candidate",
			"CCD Match Component Review": "ccd-match-component-review",
			"CCD Identity Activation Item": "ccd-identity-activation-batch",
			"CCD Match Recommendation": "ccd-match-recommendation",
		};
		if (scope.doctype === "CCD Identity Activation Item" && scope.activation_batch) {
			return `<a href="/app/ccd-identity-activation-batch/${encodeURIComponent(scope.activation_batch)}" target="_blank" rel="noopener">${esc(scope.document)}</a>`;
		}
		const route = routes[scope.doctype];
		if (!route || String(scope.document || "").includes(";")) return esc(scope.document);
		return `<a href="/app/${route}/${encodeURIComponent(scope.document)}" target="_blank" rel="noopener">${esc(scope.document)}</a>`;
	}

	function scopeRows(scopes) {
		return (scopes || []).map((scope) =>
			`<tr><td>${esc(scope.origin)}</td><td>${documentLink(scope)}</td>` +
			`<td>${esc(scope.result || scope.status)}</td><td>${esc((scope.records || []).join(", "))}</td>` +
			`<td>${scope.probability === null || scope.probability === undefined ? "—" : esc(scope.probability)}</td></tr>`
		).join("");
	}

	function recordLink(recordId) {
		return `<a href="/app/ccd-master/${encodeURIComponent(recordId)}" target="_blank" rel="noopener">${esc(recordId)}</a>`;
	}

	function identityGroupLink(groupName) {
		return `<a href="/app/ccd-identity-group/${encodeURIComponent(groupName)}" target="_blank" rel="noopener">${esc(groupName)}</a>`;
	}

	function identityDecisionLink(decisionName) {
		return `<a href="/app/ccd-identity-decision/${encodeURIComponent(decisionName)}" target="_blank" rel="noopener">${esc(decisionName)}</a>`;
	}

	function activeGroupRows(groups) {
		return (groups || []).map((group) =>
			`<tr><td>${identityGroupLink(group.identity_group)}</td>` +
			`<td>${(group.records || []).map(recordLink).join("<br>")}</td>` +
			`<td>${identityDecisionLink(group.originating_decision)}</td></tr>`
		).join("");
	}

	function activeExclusionRows(exclusions) {
		return (exclusions || []).map((item) =>
			`<tr><td>${recordLink(item.left_record)} ≠ ${recordLink(item.right_record)}</td>` +
			`<td>${identityDecisionLink(item.originating_decision)}</td><td>${esc(item.status)}</td></tr>`
		).join("");
	}

	function activeGroupOverlapRows(overlaps) {
		return (overlaps || []).map((item) =>
			`<tr><td>${esc(item.pending_origin)} — ${esc(item.pending_document)}</td>` +
			`<td>${(item.pending_records || []).map(recordLink).join("<br>")}</td>` +
			`<td>${identityGroupLink(item.identity_group)}</td>` +
			`<td>${(item.identity_group_records || []).map(recordLink).join("<br>")}</td>` +
			`<td><strong>${(item.shared_records || []).map(recordLink).join("<br>")}</strong></td></tr>`
		).join("");
	}

	function recordEvidenceMatrix(context) {
		const records = context.record_evidence || [];
		if (!records.length) return `<p>${__("No record evidence is available.")}</p>`;
		const headers = records.map((record) => {
			const groups = (record.current_identity_groups || []).length
				? (record.current_identity_groups || []).map(identityGroupLink).join("<br>")
				: `<span class="text-muted">${__("Not currently linked")}</span>`;
			return `<th>${recordLink(record.record_id)}<br><small>${esc(record.source || __("Unknown source"))}</small><br>` +
				`<small>${__("Current group")}: ${groups}</small></th>`;
		}).join("");
		const rows = (context.evidence_attributes || []).map((attribute) =>
			`<tr><th>${esc(attribute)}</th>${records.map((record) =>
				`<td>${esc((record.values || {})[attribute] || "—")}</td>`
			).join("")}</tr>`
		).join("");
		return `<div class="table-responsive"><table class="table table-bordered table-sm">` +
			`<thead><tr><th>${__("Evidence")}</th>${headers}</tr></thead><tbody>${rows}</tbody></table></div>`;
	}

	function warningLabel(code) {
		const labels = {
			splits_active_identity_group: __("The final partition splits an active Identity Group."),
			overrides_finalized_different_constraint: __("The final partition overrides an active or finalized Different constraint."),
			overrides_finalized_pending_decision: __("The final partition contradicts at least one included finalized pending decision."),
			merges_multiple_active_identity_groups: __("The operation replaces and combines multiple active Identity Groups."),
			current_membership_needs_revalidation: __("At least one current Membership needs revalidation."),
			current_group_needs_revalidation: __("At least one current Identity Group needs revalidation."),
			complete_hkid_conflict_governance_override: __("A proposed Same group contains conflicting complete HKIDs."),
			same_source_duplicates_governance_override: __("A proposed Same group contains multiple records from the same governed source."),
		};
		return labels[code] || code;
	}

	function hardConflictLabel(code, context) {
		if (code === "identity_automation_circuit_breaker_paused") {
			const details = [context.pause_scope, context.pause_reason].filter(Boolean).map(esc).join(" — ");
			return `${__("The identity QC circuit breaker is paused. Resolve and deliberately clear the pause before applying any combined component.")}${details ? `<br><small>${details}</small>` : ""}`;
		}
		return esc(code);
	}

	function showApply(preview, onComplete) {
		if ((preview.hard_conflicts || []).length) {
			frappe.msgprint({
				title: __("Combined component is not eligible"),
				indicator: "red",
				message: (preview.hard_conflicts || []).map((item) => hardConflictLabel(item, preview)).join("<br>"),
			});
			return;
		}
		const warnings = preview.warnings || [];
		const planned = preview.planned || {};
		const noChange = Boolean(preview.already_represented);
		const approvalPending = Boolean(preview.seed_requires_approval && !preview.seed_approved);
		const switchNotice = approvalPending
			? `<div class="alert alert-info"><b>${__("Review-only result")}</b>: ${__("This frozen Activation Batch is still Reviewed. Nothing can be applied until a System Manager explicitly approves the batch and reopens this preview.")}</div>`
			: noChange
			? `<div class="alert alert-success">${__("The requested identity state is already represented. Apply records an audited no-change outcome and creates no Identity Decision, Group, Membership, or Exclusion.")}</div>`
			: preview.materialization_enabled
				? `<div class="alert alert-success">${__("Live Identity Materialization is enabled.")}</div>`
				: `<div class="alert alert-danger">${__("Enable Live Identity Materialization and preview again before Apply.")}</div>`;
		const warningHtml = warnings.length
			? `<div class="alert alert-danger"><b>${__("Warnings requiring explicit confirmation")}</b><ul>${warnings.map((item) => `<li>${esc(warningLabel(item))}</li>`).join("")}</ul></div>`
			: "";
		const fields = [
			{
				fieldname: "summary",
				fieldtype: "HTML",
				options:
					`<div class="alert alert-warning"><b>${__("This changes reversible relationship objects only. CCD Master records are never merged or deleted.")}</b></div>` +
					switchNotice + warningHtml +
					`<p><b>${__("Seed")}</b>: ${esc(preview.seed_document)} (${esc(preview.seed_origin)})<br>` +
					`<b>${__("Final result")}</b>: ${esc(planned.replacement_decision_type)}<br>` +
					`<b>${__("Will end")}</b>: ${esc(planned.ended_groups)} ${__("groups")}, ${esc(planned.ended_memberships)} ${__("memberships")}<br>` +
					`<b>${__("Will supersede")}</b>: ${esc(planned.superseded_decisions)} ${__("decisions")}, ${esc(planned.superseded_exclusions)} ${__("exclusions")}<br>` +
					`<b>${__("Will create")}</b>: ${esc(planned.new_groups)} ${__("groups")}, ${esc(planned.new_memberships)} ${__("memberships")}, ${esc(planned.new_exclusions)} ${__("exclusions")}</p>` +
					`<p><b>${__("Final partition")}</b><br>${groupText(preview.replacement_groups)}</p>`,
			},
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Resolution reason"),
				reqd: 1,
				description: __("Explain the evidence and why this complete partition is correct."),
			},
			{
				fieldname: "is_demonstration",
				fieldtype: "Check",
				label: __("Development / demonstration resolution"),
				default: 0,
			},
		];
		if (approvalPending) {
			const reviewDialog = new frappe.ui.Dialog({
				title: __("Preview Final Atomic Result — Approval Required"),
				fields: [fields[0]],
				primary_action_label: __("Close Preview"),
				primary_action() {
					reviewDialog.hide();
				},
			});
			reviewDialog.show();
			return;
		}
		if (warnings.length) {
			fields.push({
				fieldname: "confirm_safety_warnings",
				fieldtype: "Check",
				label: __("I explicitly accept every warning above"),
			});
		}
		fields.push({
			fieldname: "confirm_seed_document",
			fieldtype: "Data",
			label: __("Type the exact seed document ID"),
			description: esc(preview.seed_document),
			reqd: 1,
		});

		const dialog = new frappe.ui.Dialog({
			title: noChange ? __("Record Already-Represented Outcome") : __("Apply Combined Identity Resolution"),
			fields,
			primary_action_label: noChange ? __("Record Audited No-Change") : __("Apply Atomic Resolution"),
			primary_action(values) {
				if (values.confirm_seed_document !== preview.seed_document) {
					frappe.msgprint(__("The confirmation must exactly match the seed document ID."));
					return;
				}
				if (!noChange && !preview.materialization_enabled) {
					frappe.msgprint(__("Enable Live Identity Materialization and run the preview again."));
					return;
				}
				if (warnings.length && !values.confirm_safety_warnings) {
					frappe.msgprint(__("Explicitly confirm every displayed warning."));
					return;
				}
				frappe.call({
					method: "db_connector.api_identity_overlap.apply_combined_component_resolution",
					args: {
						seed_doctype: preview.seed_doctype,
						seed_document: preview.seed_document,
						replacement_groups_json: JSON.stringify(preview.replacement_groups || []),
						expected_scope_fingerprint: preview.scope_fingerprint,
						reason: values.reason,
						confirm_seed_document: values.confirm_seed_document,
						confirm_safety_warnings: values.confirm_safety_warnings ? 1 : 0,
						is_demonstration: values.is_demonstration ? 1 : 0,
					},
					freeze: true,
					freeze_message: __("Applying the atomic combined-component resolution..."),
					callback(response) {
						dialog.hide();
						const result = response.message || {};
						frappe.msgprint(
							`${__("Resolution")}: <a href="/app/ccd-identity-overlap-resolution/${encodeURIComponent(result.resolution || "")}">${esc(result.resolution)}</a><br>` +
							`${__("Status")}: ${esc(result.status)}${result.identity_decision ? `<br>${__("Identity Decision")}: <a href="/app/ccd-identity-decision/${encodeURIComponent(result.identity_decision)}">${esc(result.identity_decision)}</a>` : ""}`,
						);
						if (onComplete) onComplete(result);
					},
				});
			},
		});
		dialog.show();
	}

	function open(seedDoctype, seedDocument, onComplete) {
		api("get_combined_component_context", {
			seed_doctype: seedDoctype,
			seed_document: seedDocument,
		}, (response) => {
			const context = response.message || {};
			if ((context.hard_conflicts || []).length) {
				frappe.msgprint({
					title: __("Combined component is stale or incomplete"),
					indicator: "red",
					message: (context.hard_conflicts || []).map((item) => hardConflictLabel(item, context)).join("<br>"),
				});
				return;
			}
			const included = scopeRows(context.included_pending_scopes || []);
			const adjacent = scopeRows(context.adjacent_unreviewed_scopes || []);
			const adjacentNote = context.adjacent_unreviewed_truncated
				? `<div class="alert alert-warning">${__("Only the first {0} of {1} adjacent unresolved scopes are displayed.", [(context.adjacent_unreviewed_scopes || []).length, context.adjacent_unreviewed_count])}</div>`
				: "";
			const overlapRows = activeGroupOverlapRows(context.active_group_overlaps || []);
			const groupRows = activeGroupRows(context.active_identity_groups || []);
			const exclusionRows = activeExclusionRows(context.active_exclusions || []);
			const approvalNote = context.seed_requires_approval && !context.seed_approved
				? `<div class="alert alert-info"><b>${__("Batch status")}: ${esc(context.activation_batch_status)}</b>. ${__("You can inspect the entire overlap and preview a final partition now. Applying remains blocked until the batch is explicitly Approved.")}</div>`
				: "";
			const privacyNote = context.sensitive_values_visible
				? `<div class="alert alert-warning">${__("Sensitive identity values are visible because this workflow is restricted to System Managers.")}</div>`
				: `<div class="alert alert-info">${__("Identity values are masked for your current role.")}</div>`;
			const dialog = new frappe.ui.Dialog({
				title: __("Preview Combined Identity Component"),
				size: "extra-large",
				fields: [
					{
						fieldname: "instructions",
						fieldtype: "HTML",
						options:
							`<div class="alert alert-success"><b>${__("Zero-write preview")}</b>: ${__("No Identity Decision, Group, Membership, Exclusion, or source status is changed on this screen.")}</div>` +
							approvalNote +
							`<p>${__("Assign records representing the same person to the same Group. Separate records remain singletons. The scope already includes every touched active group, active Different exclusion, and connected finalized pending decision.")}</p>` +
							`<p><b>${__("Seed")}</b>: ${esc(context.seed_document)} (${esc(context.seed_origin)})<br>` +
							`<b>${__("Complete scope")}</b>: ${esc((context.records || []).length)} ${__("records")}; ${esc((context.included_pending_scopes || []).length)} ${__("finalized pending scopes")}; ${esc(context.adjacent_unreviewed_count)} ${__("adjacent unresolved scopes")}</p>` +
							`<p><b>${__("Current live partition")}</b><br>${groupText(context.current_groups || [])}</p>` +
							`<p><b>${__("Suggested complete partition")}</b><br>${groupText(context.default_groups || [])}</p>` +
							`<h5>${__("Why this component is an overlap")}</h5><div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr><th>${__("Pending decision")}</th><th>${__("Pending records")}</th><th>${__("Existing Identity Group")}</th><th>${__("Existing group members")}</th><th>${__("Shared record")}</th></tr></thead><tbody>${overlapRows || `<tr><td colspan="5">${__("No active Identity Group overlap; inspect exclusions and connected scopes below.")}</td></tr>`}</tbody></table></div>` +
							`<h5>${__("Current active Identity Groups in scope")}</h5><div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr><th>${__("Identity Group")}</th><th>${__("Active members")}</th><th>${__("Originating Decision")}</th></tr></thead><tbody>${groupRows || `<tr><td colspan="3">${__("None")}</td></tr>`}</tbody></table></div>` +
							`<h5>${__("Active Different exclusions in scope")}</h5><div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr><th>${__("Different pair")}</th><th>${__("Originating Decision")}</th><th>${__("Status")}</th></tr></thead><tbody>${exclusionRows || `<tr><td colspan="3">${__("None")}</td></tr>`}</tbody></table></div>` +
							`<h5>${__("Included authoritative pending scopes")}</h5><div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr><th>${__("Route")}</th><th>${__("Document")}</th><th>${__("Result")}</th><th>${__("Records")}</th><th>${__("Splink probability")}</th></tr></thead><tbody>${included}</tbody></table></div>` +
							`<h5>${__("Adjacent unresolved evidence—not included")}</h5>${adjacentNote}<div class="table-responsive"><table class="table table-bordered table-sm"><thead><tr><th>${__("Route")}</th><th>${__("Document")}</th><th>${__("Status")}</th><th>${__("Records")}</th><th>${__("Splink probability")}</th></tr></thead><tbody>${adjacent || `<tr><td colspan="5">${__("None")}</td></tr>`}</tbody></table></div>` +
							`<h5>${__("Complete-scope identity evidence — side by side")}</h5>${privacyNote}${recordEvidenceMatrix(context)}`,
					},
					{ fieldtype: "Section Break", label: __("Final complete partition") },
					{
						fieldname: "overlap_decision_mode",
						fieldtype: "Select",
						label: __("Decision mode"),
						options: ["Suggested / Custom Partition", "All Same", "All Different"].join("\n"),
						default: "Suggested / Custom Partition",
						reqd: 1,
						description: __("For Partial Match, keep Suggested / Custom Partition and assign every same-person record to the same numbered Group. Separate means a singleton. If the final complete state is already live, the next preview becomes an audited no-change result."),
					},
					...partitionFields(context),
				],
				primary_action_label: __("Preview Final Atomic Result"),
				primary_action(values) {
					const groups = groupsFromValues(context, values);
					api("preview_combined_component_resolution", {
						seed_doctype: context.seed_doctype,
						seed_document: context.seed_document,
						replacement_groups_json: JSON.stringify(groups),
					}, (previewResponse) => {
						dialog.hide();
						showApply(previewResponse.message || {}, onComplete);
					});
				},
			});
			dialog.show();
		});
	}

	window.db_connector_identity_overlap = { open };
})();

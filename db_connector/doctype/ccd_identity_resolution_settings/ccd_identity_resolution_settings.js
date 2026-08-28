frappe.ui.form.on("CCD Identity Resolution Settings", {
	refresh(frm) {
		if (!frappe.user.has_role("System Manager")) return;
		add_qc_control(frm);
		add_tiered_control(frm);
		add_breaker_control(frm);
		frm.add_custom_button(__("Preview Automatic Tiered Run"), () => preview_automatic(frm), __("Automation"));
		if (frm.doc.automatic_tiered_enabled) {
			frm.add_custom_button(
				__("Run One Automatic Cycle Now"),
				() => frappe.confirm(
					__("Run one bounded automatic Tiered cycle using the current limits and fresh safety checks?"),
					() => frappe.call({
						method: "db_connector.api_identity_automation.run_automatic_tiered_now",
						freeze: true,
						callback(response) {
							show_result(response.message || {});
							frm.reload_doc();
						},
					}),
				),
				__("Automation"),
			);
		}
	},
});

function esc(value) {
	return frappe.utils.escape_html(String(value || ""));
}

function confirm_control(frm, title, method, args, enabled) {
	frappe.prompt(
		[
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
			{
				fieldname: "confirm_settings_name",
				fieldtype: "Data",
				label: __("Type the exact Settings ID to confirm"),
				description: frm.doc.name,
				reqd: 1,
			},
		],
		(values) => {
			const requestArgs = { ...args, ...values };
			if (enabled !== null) requestArgs.enabled = enabled ? 1 : 0;
			frappe.call({
				method,
				args: requestArgs,
				freeze: true,
				callback: () => frm.reload_doc(),
			});
		},
		title,
	);
}

function add_qc_control(frm) {
	const enabled = Boolean(frm.doc.automatic_qc_assignment_enabled);
	frm.add_custom_button(
		enabled ? __("Stop Automatic QC Assignment") : __("Enable Automatic QC Assignment"),
		() => confirm_control(
			frm,
			enabled ? __("Stop automatic QC assignment") : __("Enable automatic QC assignment"),
			"db_connector.api_identity_qc.set_automatic_qc_assignment",
			{},
			!enabled,
		),
		__("Automation"),
	);
}

function add_tiered_control(frm) {
	const enabled = Boolean(frm.doc.automatic_tiered_enabled);
	frm.add_custom_button(
		enabled ? __("Stop Automatic Tiered") : __("Enable Automatic Tiered"),
		() => confirm_control(
			frm,
			enabled ? __("Stop automatic Tiered materialization") : __("Enable automatic Tiered materialization"),
			"db_connector.api_identity_automation.set_automatic_tiered_enabled",
			{},
			!enabled,
		),
		__("Automation"),
	);
}

function add_breaker_control(frm) {
	if (!frm.doc.automation_paused) {
		frm.add_custom_button(
			__("Emergency Pause Tiered"),
			() => confirm_control(
				frm,
				__("Emergency pause Tiered automation"),
				"db_connector.api_identity_qc.pause_tiered_automation",
				{},
				null,
			),
			__("Automation"),
		);
		return;
	}
	frm.add_custom_button(__("Preview Governed Resume"), () => {
		frappe.call({
			method: "db_connector.api_identity_qc.preview_resume_tiered_automation",
			callback(response) {
				const result = response.message || {};
				const blockers = result.blockers || [];
				frappe.msgprint({
					title: __("Zero-write governed resume preview"),
					indicator: blockers.length ? "orange" : "green",
					message: `<p>${__("Paused scope")}: ${esc(result.pause_scope)}</p>` +
						`<p>${__("Pause reason")}: ${esc(result.pause_reason)}</p>` +
						`<p>${__("Blockers")}: ${blockers.length ? blockers.map(esc).join("<br>") : __("None")}</p>` +
						`<p><strong>${__("No records were changed.")}</strong></p>`,
				});
				if (!blockers.length) {
					confirm_control(
						frm,
						__("Resume Tiered automation"),
						"db_connector.api_identity_qc.resume_tiered_automation",
						{},
						null,
					);
				}
			},
		});
	}, __("Automation"));
}

function preview_automatic(frm) {
	frappe.call({
		method: "db_connector.api_identity_automation.preview_automatic_tiered_run",
		freeze: true,
		callback(response) {
			const result = response.message || {};
			frappe.msgprint({
				title: __("Zero-write automatic Tiered preview"),
				indicator: (result.operational_blockers || []).length ? "orange" : "green",
				message: `<p>${__("Complete components")}: ${result.selected_component_count || 0}</p>` +
					`<p>${__("Recommendations")}: ${result.selected_recommendation_count || 0}</p>` +
					`<p>${__("Planned groups / memberships")}: ${result.planned_identity_group_count || 0} / ${result.planned_membership_count || 0}</p>` +
					`<p>${__("Unsafe components skipped")}: ${result.skipped_unsafe_component_count || 0}</p>` +
					`<p>${__("Operational blockers")}: ${(result.operational_blockers || []).length ? result.operational_blockers.map(esc).join("<br>") : __("None")}</p>` +
					`<p><strong>${__("No records were written.")}</strong></p>`,
			});
		},
	});
}

function show_result(result) {
	frappe.msgprint({
		title: __("Automatic Tiered cycle"),
		indicator: result.status === "Applied" ? "green" : "orange",
		message: `<p>${__("Status")}: ${esc(result.status)}</p>` +
			`<p>${__("Batch")}: ${esc(result.batch)}</p>` +
			`<p>${__("Groups / Memberships")}: ${result.created_groups || 0} / ${result.created_memberships || 0}</p>`,
	});
}

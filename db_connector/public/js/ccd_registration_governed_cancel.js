frappe.ui.form.on("CCD Registration", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || !frappe.user_roles.includes("System Manager")) {
			return;
		}
		frm.add_custom_button(__("Cancel with Identity Retirement"), () => {
			frappe.call({
				method: "db_connector.api_identity_retirement.preview_registration_cancellation",
				args: { registration_name: frm.doc.name },
				freeze: true,
				freeze_message: __("Calculating deletion and identity impact…"),
				callback(response) {
					const preview = response.message;
					if (!preview) return;
					const dialog = new frappe.ui.Dialog({
						title: __("Confirm Governed Registration Cancellation"),
						fields: [
							{
								fieldtype: "HTML",
								options: `<p>${__("This will retire identity state before deleting {0} CCD Master records.", [preview.target_ccd_master_count])}</p><p><strong>${__("Scope fingerprint")}</strong><br><code>${frappe.utils.escape_html(preview.scope_fingerprint)}</code></p>`,
							},
							{
								fieldname: "reason",
								fieldtype: "Small Text",
								label: __("Cancellation and retirement reason"),
								reqd: 1,
							},
							{
								fieldname: "fingerprint",
								fieldtype: "Data",
								label: __("Type the exact scope fingerprint"),
								reqd: 1,
							},
						],
						primary_action_label: __("Retire Source and Cancel"),
						primary_action(values) {
							if (values.fingerprint !== preview.scope_fingerprint) {
								frappe.throw(__("The typed fingerprint does not match the preview."));
							}
							dialog.hide();
							frappe.call({
								method: "db_connector.api_identity_retirement.cancel_registration_with_retirement",
								args: {
									registration_name: frm.doc.name,
									confirm_scope_fingerprint: values.fingerprint,
									reason: values.reason,
								},
								freeze: true,
								freeze_message: __("Retiring identity state and cancelling registration…"),
								callback() {
									frappe.show_alert({ message: __("Registration cancelled through governed retirement."), indicator: "green" });
									frm.reload_doc();
								},
							});
						},
					});
					dialog.show();
				},
			});
		}, __("Actions"));
	},
});

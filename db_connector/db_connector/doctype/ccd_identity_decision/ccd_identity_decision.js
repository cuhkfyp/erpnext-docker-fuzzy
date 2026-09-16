frappe.ui.form.on("CCD Identity Decision", {
	refresh(frm) {
		if (
			frm.is_new() ||
			frm.doc.status !== "Active" ||
			!(frappe.user_roles || []).includes("System Manager")
		) return;
		frm.add_custom_button(
			__("Correct Complete Identity Component"),
			() => window.db_connector_identity_correction.open(
				frm.doc.name,
				() => frm.reload_doc(),
			),
			__("Identity Resolution"),
		);
	},
});

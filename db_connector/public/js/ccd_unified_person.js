(() => {
	const allowed_roles = ["System Manager", "CCD Match Sensitive Reviewer"];

	frappe.ui.form.on("CCD Unified Person", {
		refresh(frm) {
			if (!allowed_roles.some((role) => frappe.user_roles.includes(role))) return;

			frm.add_custom_button(
				__("Unified Person Register"),
				() => {
					frappe.route_options = {
						unified_person: frm.doc.name,
						membership_status: "Active",
					};
					frappe.set_route(
						"query-report",
						"CCD Unified Person Register",
					);
				},
				__("Identity Resolution"),
			);
		},
	});
})();

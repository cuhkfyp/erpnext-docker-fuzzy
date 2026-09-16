(() => {
	const allowed_roles = ["System Manager", "CCD Match Sensitive Reviewer"];
	const settings = frappe.listview_settings["CCD Unified Person"] || {};
	const previous_onload = settings.onload;

	frappe.listview_settings["CCD Unified Person"] = {
		...settings,
		onload(listview) {
			if (previous_onload) previous_onload(listview);
			if (!allowed_roles.some((role) => frappe.user_roles.includes(role))) return;

			listview.page.add_inner_button(
				__("Unified Person Register"),
				() => {
					frappe.route_options = { membership_status: "Active" };
					frappe.set_route(
						"query-report",
						"CCD Unified Person Register",
					);
				},
			);
		},
	};
})();

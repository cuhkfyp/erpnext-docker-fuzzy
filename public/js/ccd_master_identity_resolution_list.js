(() => {
	const identity_roles = [
		"System Manager",
		"CCD Match Reviewer",
		"CCD Match Sensitive Reviewer",
	];
	const settings = frappe.listview_settings["CCD Master"] || {};
	const previous_onload = settings.onload;

	frappe.listview_settings["CCD Master"] = {
		...settings,
		onload(listview) {
			if (previous_onload) previous_onload(listview);
			if (!identity_roles.some((role) => frappe.user_roles.includes(role))) return;

			listview.page.add_inner_button(
				__("Identity Resolution Register"),
				() => frappe.set_route(
					"query-report",
					"CCD Identity Resolution Register",
				),
			);
		},
	};
})();

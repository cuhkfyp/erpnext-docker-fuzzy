frappe.query_reports["CCD Identity Resolution Register"] = {
	filters: [
		{
			fieldname: "identity_state",
			label: __("Identity State"),
			fieldtype: "Select",
			options: [
				"Any Resolved",
				"Linked",
				"Needs Revalidation",
				"Resolved Separately",
			].join("\n"),
			default: "Any Resolved",
			reqd: 1,
		},
		{
			fieldname: "ccd_master",
			label: __("CCD Master"),
			fieldtype: "Link",
			options: "CCD Master",
		},
		{
			fieldname: "ccd_reg_source",
			label: __("CCD Registration Source"),
			fieldtype: "Data",
		},
		{
			fieldname: "identity_group",
			label: __("Identity Group"),
			fieldtype: "Link",
			options: "CCD Identity Group",
		},
		{
			fieldname: "group_status",
			label: __("Group Status"),
			fieldtype: "Select",
			options: "\nActive\nNeeds Revalidation",
		},
		{
			fieldname: "min_group_members",
			label: __("Minimum Group Members"),
			fieldtype: "Int",
		},
		{
			fieldname: "max_group_members",
			label: __("Maximum Group Members"),
			fieldtype: "Int",
		},
		{
			fieldname: "has_active_different",
			label: __("Has Active Different Relationship"),
			fieldtype: "Select",
			options: "\nYes\nNo",
		},
		{
			fieldname: "limit",
			label: __("Maximum Rows"),
			fieldtype: "Int",
			default: 500,
			reqd: 1,
		},
	],
	formatter(value, row, column, data, default_formatter) {
		const rendered = default_formatter(value, row, column, data);
		if (column.fieldname !== "identity_state") return rendered;
		const colour = {
			Linked: "green",
			"Needs Revalidation": "orange",
			"Resolved Separately": "blue",
		}[data.identity_state] || "gray";
		return `<span class="indicator-pill ${colour}">${rendered}</span>`;
	},
};

frappe.query_reports["CCD Unified QC Workbench"] = {
	filters: [
		{
			fieldname: "work_state",
			label: __("Work State"),
			fieldtype: "Select",
			options: [
				"Pending",
				"All Assigned",
				"Completed",
				"Needs Adjudication",
				"Overdue",
				"Stale",
			].join("\n"),
			default: "Pending",
			reqd: 1,
		},
		{
			fieldname: "channel",
			label: __("QC Channel"),
			fieldtype: "Select",
			options: [
				"",
				"Tiered — Active Canary",
				"Tiered — Mandatory Backlog",
				"Ultra-high Splink",
				"Development Hybrid",
			].join("\n"),
		},
		{
			fieldname: "reviewer_scope",
			label: __("My Review"),
			fieldtype: "Select",
			options: ["All Cases", "Needs My Review", "Reviewed by Me"].join("\n"),
			default: "All Cases",
			reqd: 1,
		},
		{
			fieldname: "this_week_only",
			label: __("Assigned This Week"),
			fieldtype: "Check",
			default: 0,
		},
		{
			fieldname: "assigned_from",
			label: __("Assigned From"),
			fieldtype: "Date",
		},
		{
			fieldname: "assigned_to",
			label: __("Assigned To"),
			fieldtype: "Date",
		},
		{
			fieldname: "maximum_rows",
			label: __("Maximum Rows"),
			fieldtype: "Int",
			default: 500,
			reqd: 1,
		},
	],
	formatter(value, row, column, data, default_formatter) {
		const rendered = default_formatter(value, row, column, data);
		if (!data) return rendered;
		if (column.fieldname === "channel") {
			const colour = {
				"Tiered — Active Canary": "green",
				"Tiered — Mandatory Backlog": "orange",
				"Ultra-high Splink": "blue",
				"Development Hybrid": "purple",
			}[data.channel] || "gray";
			return `<span class="indicator-pill ${colour}">${rendered}</span>`;
		}
		if (column.fieldname === "review_status") {
			const colour = {
				Unreviewed: "gray",
				"Partially Reviewed": "blue",
				"Positive Confirmation Required": "orange",
				"Needs Adjudication": "orange",
				Agreed: "green",
				Adjudicated: "green",
				Stale: "red",
			}[data.review_status] || "gray";
			return `<span class="indicator-pill ${colour}">${rendered}</span>`;
		}
		if (column.fieldname === "my_action" && data.my_action === "Review now") {
			return `<span class="indicator-pill blue">${rendered}</span>`;
		}
		if (column.fieldname === "my_action" && data.my_action === "Adjudicate") {
			return `<span class="indicator-pill orange">${rendered}</span>`;
		}
		return rendered;
	},
};

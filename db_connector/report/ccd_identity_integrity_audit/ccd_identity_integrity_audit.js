frappe.query_reports["CCD Identity Integrity Audit"] = {
	filters: [],
	formatter(value, row, column, data, default_formatter) {
		const rendered = default_formatter(value, row, column, data);
		if (column.fieldname !== "active_issue_count" || !data.active_issue_count) return rendered;
		return `<span class="indicator-pill red">${rendered}</span>`;
	},
};

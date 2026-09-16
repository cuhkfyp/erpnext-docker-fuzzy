frappe.query_reports["CCD Unified Person Register"] = {
	filters: [
		{ fieldname: "unified_person", label: __("Unified Person Number"), fieldtype: "Link", options: "CCD Unified Person" },
		{ fieldname: "ccd_master", label: __("CCD Master"), fieldtype: "Link", options: "CCD Master" },
		{ fieldname: "governed_source", label: __("Stable CCD Source"), fieldtype: "Data" },
		{ fieldname: "person_status", label: __("Person Status"), fieldtype: "Select", options: "\nActive\nAlias\nRetired" },
		{ fieldname: "membership_status", label: __("Membership Status"), fieldtype: "Select", options: "Active\nEnded", default: "Active" },
		{ fieldname: "limit", label: __("Maximum Rows"), fieldtype: "Int", default: 500, reqd: 1 },
	],
	formatter(value, row, column, data, default_formatter) {
		const rendered = default_formatter(value, row, column, data);
		if (column.fieldname !== "person_status") return rendered;
		const colour = { Active: "green", Alias: "blue", Retired: "gray" }[data.person_status] || "gray";
		return `<span class="indicator-pill ${colour}">${rendered}</span>`;
	},
};

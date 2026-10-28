(() => {
	"use strict";

	const PREFIX = "CCD-REG-";
	const VALID_COMPONENT = /^[A-Za-z0-9_-]+$/;

	function source_component(value) {
		const component = String(value || "").trim();
		return VALID_COMPONENT.test(component) ? component : "";
	}

	function selected_table_component(value) {
		const table_name = String(value || "").trim();
		if (!table_name || table_name === "*** Customize ***") {
			return "";
		}
		return source_component(table_name.split(".").pop());
	}

	function generated_registration_name(frm) {
		const physical_hostname = source_component(frm.doc.physical_hostname);
		const database_name = source_component(frm.doc.db_database);
		if (!physical_hostname || !database_name) {
			return "";
		}
		const table_name = selected_table_component(frm.doc.ccd_table);
		const suffix = [physical_hostname, database_name, table_name]
			.filter(Boolean)
			.join("_");
		return `${PREFIX}${suffix}`;
	}

	function autofill_registration_name(frm, overwrite) {
		if (frm.doc.docstatus !== 0) {
			return;
		}
		const generated_name = generated_registration_name(frm);
		const current_name = String(frm.doc.ccd_reg_doctype || "").trim();
		if (!generated_name || current_name === generated_name || (!overwrite && current_name)) {
			return;
		}
		return frm.set_value("ccd_reg_doctype", generated_name);
	}

	frappe.ui.form.on("CCD Registration", {
		refresh(frm) {
			return autofill_registration_name(frm, false);
		},
		physical_hostname(frm) {
			return autofill_registration_name(frm, true);
		},
		db_database(frm) {
			return autofill_registration_name(frm, true);
		},
		ccd_table(frm) {
			return autofill_registration_name(frm, true);
		},
	});
})();

(() => {
	"use strict";

	const PREFIX = "CCD-REG-";
	const VALID_COMPONENT = /^[A-Za-z0-9_-]+$/;

	function source_component(value) {
		const component = String(value || "").trim();
		return VALID_COMPONENT.test(component) ? component : "";
	}

	function generated_registration_name(frm) {
		const physical_hostname = source_component(frm.doc.physical_hostname);
		const database_name = source_component(frm.doc.db_database);
		if (!physical_hostname || !database_name) {
			return "";
		}
		return `${PREFIX}${physical_hostname}_${database_name}`;
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
	});
})();

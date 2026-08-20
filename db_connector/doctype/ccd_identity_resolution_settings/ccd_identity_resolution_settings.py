import frappe
from frappe.model.document import Document


class CCDIdentityResolutionSettings(Document):
    def validate(self):
        for fieldname in (
            "initial_pilot_wave_components",
            "demo_holdout_components",
            "qc_cases_per_week",
            "rolling_qc_window",
            "qc_sla_days",
        ):
            if int(self.get(fieldname) or 0) < 0:
                frappe.throw(f"{self.meta.get_label(fieldname)} cannot be negative")

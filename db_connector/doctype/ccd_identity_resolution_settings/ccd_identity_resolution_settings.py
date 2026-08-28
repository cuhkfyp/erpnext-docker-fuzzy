import frappe
from frappe.model.document import Document


class CCDIdentityResolutionSettings(Document):
    def before_validate(self):
        if not self.automatic_tiered_components_per_run:
            self.automatic_tiered_components_per_run = 10
        if not self.qc_assignment_interval_days:
            self.qc_assignment_interval_days = 7
        if not self.automatic_tiered_schedule:
            self.automatic_tiered_schedule = "Daily"

    def validate(self):
        before = self.get_doc_before_save()
        governed_fields = (
            "automatic_tiered_canary",
            "automatic_tiered_policy",
            "automatic_tiered_components_per_run",
            "qc_cases_per_week",
            "qc_assignment_interval_days",
            "rolling_qc_window",
            "qc_sla_days",
        )
        if before and (
            before.automatic_tiered_enabled
            or before.automatic_qc_assignment_enabled
        ):
            changed = [
                self.meta.get_label(fieldname)
                for fieldname in governed_fields
                if self.get(fieldname) != before.get(fieldname)
            ]
            if changed:
                frappe.throw(
                    "Stop both automatic controls before changing governed "
                    "automation settings: " + ", ".join(changed)
                )
        if (
            before
            and not before.materialization_enabled
            and self.materialization_enabled
            and before.automatic_tiered_enabled
        ):
            frappe.throw(
                "Stop Automatic Tiered before re-enabling Live Identity Materialization"
            )
        for fieldname in (
            "initial_pilot_wave_components",
            "demo_holdout_components",
            "qc_cases_per_week",
            "qc_assignment_interval_days",
            "rolling_qc_window",
            "qc_sla_days",
            "default_review_batch_size",
            "automatic_tiered_components_per_run",
        ):
            if int(self.get(fieldname) or 0) < 0:
                frappe.throw(f"{self.meta.get_label(fieldname)} cannot be negative")

        for fieldname in (
            "qc_cases_per_week",
            "qc_assignment_interval_days",
            "rolling_qc_window",
            "qc_sla_days",
            "automatic_tiered_components_per_run",
        ):
            value = int(self.get(fieldname) or 0)
            if value < 1 or value > 100:
                frappe.throw(
                    f"{self.meta.get_label(fieldname)} must be between 1 and 100"
                )
        if int(self.rolling_qc_window or 0) < 73:
            frappe.throw(
                "Rolling QC Window must be at least 73; a smaller window cannot "
                "reach a 95% Wilson lower bound even when every result is Same"
            )

        if self.automatic_tiered_canary and self.automatic_tiered_policy:
            canary_policy = frappe.db.get_value(
                "CCD Match Canary Run", self.automatic_tiered_canary, "matching_policy"
            )
            if str(canary_policy or "") != str(self.automatic_tiered_policy):
                frappe.throw(
                    "The authorized Canary does not use the selected Matching Policy"
                )

        if self.automatic_tiered_enabled:
            if not self.automatic_qc_assignment_enabled:
                frappe.throw(
                    "Automatic QC Assignment must be enabled before Automatic Tiered Materialization"
                )
            if not self.automatic_tiered_canary or not self.automatic_tiered_policy:
                frappe.throw(
                    "Select the authorized Tiered Canary and Matching Policy before enabling automation"
                )

import frappe
from frappe.model.document import Document

from db_connector.notification_utils import (
    invalid_notification_recipients,
    parse_notification_recipients,
)


class CCDIdentityResolutionSettings(Document):
    def before_validate(self):
        if not self.automatic_tiered_components_per_run:
            self.automatic_tiered_components_per_run = 10
        if not self.qc_assignment_interval_days:
            self.qc_assignment_interval_days = 7
        if not self.automatic_tiered_schedule:
            self.automatic_tiered_schedule = "Daily"
        if not self.automatic_splink_components_per_run:
            self.automatic_splink_components_per_run = 10
        if not self.maximum_splink_component_size:
            self.maximum_splink_component_size = 2
        self.qc_cases_per_week = 20

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
            "authorized_splink_validation_run",
            "authorized_splink_queue",
            "authorized_splink_policy",
            "authorized_splink_cutoff",
            "maximum_splink_component_size",
            "automatic_splink_components_per_run",
            "operational_provenance_allowlist",
        )
        if before and (
            before.automatic_tiered_enabled
            or before.automatic_qc_assignment_enabled
            or before.automatic_splink_enabled
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
            and (before.automatic_tiered_enabled or before.automatic_splink_enabled)
        ):
            frappe.throw(
                "Stop Automatic Tiered and Automatic Splink before re-enabling Live Identity Materialization"
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
            "automatic_splink_components_per_run",
            "maximum_splink_component_size",
        ):
            if int(self.get(fieldname) or 0) < 0:
                frappe.throw(f"{self.meta.get_label(fieldname)} cannot be negative")

        for fieldname in (
            "qc_cases_per_week",
            "qc_assignment_interval_days",
            "rolling_qc_window",
            "qc_sla_days",
            "automatic_tiered_components_per_run",
            "automatic_splink_components_per_run",
        ):
            value = int(self.get(fieldname) or 0)
            if value < 1 or value > 100:
                frappe.throw(
                    f"{self.meta.get_label(fieldname)} must be between 1 and 100"
                )
        if int(self.maximum_splink_component_size or 0) < 2 or int(
            self.maximum_splink_component_size or 0
        ) > 100:
            frappe.throw("Maximum Splink Component Size must be between 2 and 100")
        if int(self.qc_cases_per_week or 0) != 20:
            frappe.throw("Shared Pair Cases per Week is governed at exactly 20")
        if int(self.rolling_qc_window or 0) < 73:
            frappe.throw(
                "Rolling QC Window must be at least 73; a smaller window cannot "
                "reach a 95% Wilson lower bound even when every result is Same"
            )

        recipients = parse_notification_recipients(
            self.automation_notification_recipients
        )
        invalid = invalid_notification_recipients(
            self.automation_notification_recipients
        )
        if invalid:
            frappe.throw(
                "Automation Notification Recipients contains invalid plain email "
                "addresses: " + ", ".join(invalid)
            )
        if self.automation_notifications_enabled and not recipients:
            frappe.throw(
                "Add at least one Automation Notification Recipient before "
                "enabling email notifications"
            )
        self.automation_notification_recipients = "\n".join(recipients)

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
        if self.automatic_splink_enabled:
            if not self.materialization_enabled or not self.automatic_qc_assignment_enabled:
                frappe.throw(
                    "Live Materialization and Automatic QC Assignment are required for Automatic Splink"
                )
            if (
                not self.authorized_splink_validation_run
                or not self.authorized_splink_queue
                or not self.authorized_splink_policy
                or not self.authorized_splink_cutoff
            ):
                frappe.throw("Complete governed Splink authorization is required")

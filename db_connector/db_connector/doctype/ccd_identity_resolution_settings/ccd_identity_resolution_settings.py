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
        if not self.maximum_tiered_one_to_many_component_size:
            self.maximum_tiered_one_to_many_component_size = 3
        if not self.tiered_one_to_many_components_per_run:
            self.tiered_one_to_many_components_per_run = 10
        if self.tiered_one_to_many_qc_percent in (None, ""):
            self.tiered_one_to_many_qc_percent = 10
        if not self.automatic_splink_components_per_run:
            self.automatic_splink_components_per_run = 10
        if not self.maximum_splink_component_size:
            self.maximum_splink_component_size = 2
        if not self.automatic_hybrid_components_per_run:
            self.automatic_hybrid_components_per_run = 1
        self.qc_cases_per_week = 20

    def validate(self):
        before = self.get_doc_before_save()
        if before:
            channel_guards = (
                (
                    "Automatic Tiered",
                    before.automatic_tiered_enabled,
                    (
                        "automatic_tiered_canary",
                        "automatic_tiered_policy",
                        "automatic_tiered_components_per_run",
                    ),
                ),
                (
                    "Tiered One-to-Many",
                    before.automatic_tiered_one_to_many_enabled,
                    (
                        "tiered_one_to_many_validation",
                        "tiered_one_to_many_qc_percent",
                    ),
                ),
                (
                    "Automatic QC Assignment",
                    before.automatic_qc_assignment_enabled,
                    (
                        "qc_cases_per_week",
                        "qc_assignment_interval_days",
                        "rolling_qc_window",
                        "qc_sla_days",
                    ),
                ),
                (
                    "Automatic Splink",
                    before.automatic_splink_enabled,
                    (
                        "authorized_splink_validation_run",
                        "authorized_splink_queue",
                        "authorized_splink_policy",
                        "authorized_splink_cutoff",
                        "maximum_splink_component_size",
                        "automatic_splink_components_per_run",
                    ),
                ),
                (
                    "Development Hybrid",
                    before.automatic_hybrid_enabled,
                    (
                        "authorized_hybrid_validation_run",
                        "authorized_hybrid_queue",
                        "authorized_hybrid_policy",
                        "authorized_hybrid_minimum_score",
                        "authorized_hybrid_upper_cutoff",
                        "authorized_hybrid_rule_version",
                        "authorized_hybrid_source_scope_json",
                        "authorized_hybrid_route_scope_json",
                        "authorized_hybrid_fingerprint",
                        "automatic_hybrid_components_per_run",
                    ),
                ),
            )
            blocked_changes = []
            for channel, enabled, fieldnames in channel_guards:
                if not enabled:
                    continue
                changed = [
                    self.meta.get_label(fieldname)
                    for fieldname in fieldnames
                    if self.get(fieldname) != before.get(fieldname)
                ]
                if changed:
                    blocked_changes.append(f"{channel}: {', '.join(changed)}")

            provenance_changed = (
                self.operational_provenance_allowlist
                != before.operational_provenance_allowlist
            )
            materialization_channel_active = any(
                (
                    before.automatic_tiered_enabled,
                    before.automatic_splink_enabled,
                    before.automatic_hybrid_enabled,
                    before.automatic_tiered_one_to_many_enabled,
                )
            )
            if provenance_changed and materialization_channel_active:
                blocked_changes.append(
                    "All materialization channels: "
                    + self.meta.get_label("operational_provenance_allowlist")
                )
            if blocked_changes:
                frappe.throw(
                    "Stop only the affected automatic channel before changing "
                    "its governed settings: " + "; ".join(blocked_changes)
                )
        if (
            before
            and not before.materialization_enabled
            and self.materialization_enabled
            and (
                before.automatic_tiered_enabled
                or before.automatic_splink_enabled
                or before.automatic_hybrid_enabled
                or before.automatic_tiered_one_to_many_enabled
            )
        ):
            frappe.throw(
                "Stop Automatic Tiered, Automatic Splink, Development Hybrid, and One-to-Many automation before re-enabling Live Identity Materialization"
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
            "automatic_hybrid_components_per_run",
            "tiered_one_to_many_components_per_run",
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
        if int(self.automatic_hybrid_components_per_run or 0) < 1:
            frappe.throw("Maximum Hybrid Components per Run must be at least 1")
        if int(self.tiered_one_to_many_components_per_run or 0) < 1:
            frappe.throw("Maximum One-to-Many Components per Run must be at least 1")
        qc_percent = float(self.tiered_one_to_many_qc_percent or 0)
        if qc_percent <= 0 or qc_percent > 100:
            frappe.throw("Ongoing One-to-Many QC Sample must be greater than 0 and at most 100 percent")
        if int(self.maximum_tiered_one_to_many_component_size or 0) < 3 or int(
            self.maximum_tiered_one_to_many_component_size or 0
        ) > 100:
            frappe.throw(
                "Maximum Tiered One-to-Many Research Component Size must be between 3 and 100"
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
        if self.automatic_splink_incomplete_enabled:
            if not self.automatic_splink_enabled:
                frappe.throw(
                    "Automatic Splink must be enabled before incomplete ultra-high automation"
                )
            if (
                str(self.splink_incomplete_rule_version or "")
                != "ultra-high-incomplete-v1"
                or not self.splink_incomplete_authorization_event
            ):
                frappe.throw(
                    "Complete governed incomplete-component authorization is required"
                )
        if self.automatic_hybrid_enabled:
            if not self.materialization_enabled or not self.automatic_qc_assignment_enabled:
                frappe.throw(
                    "Live Materialization and Automatic QC Assignment are required for Development Hybrid Automation"
                )
            if (
                not self.authorized_hybrid_validation_run
                or not self.authorized_hybrid_queue
                or not self.authorized_hybrid_policy
                or not self.authorized_hybrid_minimum_score
                or not self.authorized_hybrid_upper_cutoff
                or not self.authorized_hybrid_rule_version
                or not self.authorized_hybrid_fingerprint
            ):
                frappe.throw("Complete governed Development Hybrid authorization is required")
        if self.automatic_hybrid_scheduling_enabled and not self.automatic_hybrid_enabled:
            frappe.throw(
                "Development Hybrid Materialization must be enabled before scheduled execution"
            )
        if self.automatic_tiered_one_to_many_enabled:
            if not self.materialization_enabled:
                frappe.throw(
                    "Live Identity Materialization is required for One-to-Many automation"
                )
            if not self.tiered_one_to_many_validation:
                frappe.throw(
                    "An authorized One-to-Many validation is required before enabling automation"
                )
            validation = frappe.db.get_value(
                "CCD Tiered One-to-Many Validation",
                self.tiered_one_to_many_validation,
                ["status", "second_approval_status"],
                as_dict=True,
            )
            if (
                not validation
                or validation.status != "Authorized"
                or validation.second_approval_status != "Approved"
            ):
                frappe.throw("The selected One-to-Many authorization is not current and approved")

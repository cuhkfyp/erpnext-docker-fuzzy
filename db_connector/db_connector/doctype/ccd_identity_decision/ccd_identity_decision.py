import frappe
from frappe.model.document import Document


class CCDIdentityDecision(Document):
    def before_insert(self):
        if not self.decision_key:
            frappe.throw("Identity Decision key is required")

    def on_trash(self):
        frappe.throw("Identity Decisions are append-only and cannot be deleted")

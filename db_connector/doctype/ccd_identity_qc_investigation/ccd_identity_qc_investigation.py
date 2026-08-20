import frappe
from frappe.model.document import Document


class CCDIdentityQCInvestigation(Document):
    def validate(self):
        if self.status == "Resolved" and not str(self.resolution_notes or "").strip():
            frappe.throw("Resolution notes are required")

    def on_trash(self):
        frappe.throw("QC Investigations are governed audit records and cannot be deleted")

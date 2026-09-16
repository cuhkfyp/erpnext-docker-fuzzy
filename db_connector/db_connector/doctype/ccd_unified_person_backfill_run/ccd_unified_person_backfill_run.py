import frappe
from frappe.model.document import Document


class CCDUnifiedPersonBackfillRun(Document):
    def on_trash(self):
        frappe.throw("Unified Person Backfill Run history cannot be deleted")

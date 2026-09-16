import frappe
from frappe.model.document import Document


class CCDUnifiedPersonEvent(Document):
    def on_trash(self):
        frappe.throw("Unified Person lifecycle events cannot be deleted")

import frappe
from frappe.model.document import Document


class CCDIdentityEvent(Document):
    def on_trash(self):
        frappe.throw("Identity Events are append-only and cannot be deleted")

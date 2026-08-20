import frappe
from frappe.model.document import Document


class CCDIdentityExclusion(Document):
    def on_trash(self):
        frappe.throw("Identity Exclusions are governed history and cannot be deleted")

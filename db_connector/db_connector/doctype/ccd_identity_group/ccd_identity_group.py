import frappe
from frappe.model.document import Document


class CCDIdentityGroup(Document):
    def on_trash(self):
        frappe.throw("Identity Groups retain lifecycle history and cannot be deleted")

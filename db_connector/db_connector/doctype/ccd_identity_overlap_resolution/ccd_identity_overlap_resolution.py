import frappe
from frappe.model.document import Document


class CCDIdentityOverlapResolution(Document):
    def on_trash(self):
        frappe.throw("Identity overlap resolutions retain audit history and cannot be deleted")

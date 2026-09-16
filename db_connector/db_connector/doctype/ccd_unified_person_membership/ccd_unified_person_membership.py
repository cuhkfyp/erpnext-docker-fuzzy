import frappe
from frappe.model.document import Document


class CCDUnifiedPersonMembership(Document):
    def before_insert(self):
        if self.status == "Active" and frappe.db.exists(
            "CCD Unified Person Membership",
            {"ccd_master": self.ccd_master, "status": "Active"},
        ):
            frappe.throw("This CCD Master already has an active Unified Person Membership")

    def on_trash(self):
        frappe.throw("Unified Person Membership history cannot be deleted")

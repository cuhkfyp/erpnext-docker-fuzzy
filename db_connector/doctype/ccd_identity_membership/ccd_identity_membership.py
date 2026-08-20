import frappe
from frappe.model.document import Document


class CCDIdentityMembership(Document):
    def before_insert(self):
        if self.status == "Active" and frappe.db.exists(
            "CCD Identity Membership",
            {"ccd_master": self.ccd_master, "status": ["in", ["Active", "Needs Revalidation"]]},
        ):
            frappe.throw("This CCD Master already has a current Identity Membership")

    def on_trash(self):
        frappe.throw("Identity Membership history cannot be deleted")

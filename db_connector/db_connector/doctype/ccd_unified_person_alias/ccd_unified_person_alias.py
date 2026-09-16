import frappe
from frappe.model.document import Document


class CCDUnifiedPersonAlias(Document):
    def before_insert(self):
        if self.alias_person == self.canonical_person:
            frappe.throw("A Unified Person number cannot alias itself")
        if self.status == "Active" and frappe.db.exists(
            "CCD Unified Person Alias",
            {"alias_person": self.alias_person, "status": "Active"},
        ):
            frappe.throw("This Unified Person number already has an active alias target")

    def on_trash(self):
        frappe.throw("Unified Person Alias history cannot be deleted")

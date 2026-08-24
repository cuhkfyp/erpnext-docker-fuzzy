import frappe
from frappe.model.document import Document


class CCDIdentityCorrection(Document):
    def on_trash(self):
        if self.status in {"Applied", "Superseded"}:
            frappe.throw("Applied identity corrections are immutable audit records")

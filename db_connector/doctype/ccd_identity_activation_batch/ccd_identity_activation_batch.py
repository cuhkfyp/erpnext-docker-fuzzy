import frappe
from frappe.model.document import Document


class CCDIdentityActivationBatch(Document):
    def validate(self):
        if self.status not in {"Draft", "Reviewed"} and not self.selection_fingerprint:
            frappe.throw("A frozen selection fingerprint is required")

    def on_trash(self):
        if self.status not in {"Draft", "Failed"}:
            frappe.throw("Applied or approved Activation Batches cannot be deleted")

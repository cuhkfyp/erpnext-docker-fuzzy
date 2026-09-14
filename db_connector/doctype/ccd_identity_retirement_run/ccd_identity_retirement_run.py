import frappe
from frappe.model.document import Document


class CCDIdentityRetirementRun(Document):
    def validate(self):
        if self.status == "Applied" and not self.completed_at:
            frappe.throw("An Applied retirement run requires a completion time")

    def on_trash(self):
        frappe.throw("Identity Retirement Runs are permanent audit records")

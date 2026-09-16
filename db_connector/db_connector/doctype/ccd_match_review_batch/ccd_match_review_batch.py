import frappe
from frappe.model.document import Document


class CCDMatchReviewBatch(Document):
    def validate(self):
        if int(self.batch_size or 0) <= 0:
            frappe.throw("A Review Batch must contain at least one candidate; create no batch for zero assigned work")

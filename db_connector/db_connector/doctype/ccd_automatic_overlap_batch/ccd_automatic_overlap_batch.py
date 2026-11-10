import frappe
from frappe.model.document import Document


class CCDAutomaticOverlapBatch(Document):
    def on_trash(self):
        frappe.throw("Automatic overlap batches retain immutable audit history")

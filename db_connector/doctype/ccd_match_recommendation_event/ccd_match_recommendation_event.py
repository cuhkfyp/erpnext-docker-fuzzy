import frappe
from frappe.model.document import Document


class CCDMatchRecommendationEvent(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw("Recommendation events are immutable")

    def on_trash(self):
        frappe.throw("Recommendation events cannot be deleted")

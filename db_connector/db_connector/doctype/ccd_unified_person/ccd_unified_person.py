import frappe
from frappe.model.document import Document

from db_connector.fuzzy_matching.unified_person import parse_unified_person_number


class CCDUnifiedPerson(Document):
    def before_insert(self):
        sequence = parse_unified_person_number(self.unified_person_number)
        if int(self.sequence_number or 0) != sequence:
            frappe.throw("Unified Person sequence does not match its number")
        if self.canonical_person and self.canonical_person == self.unified_person_number:
            frappe.throw("A canonical Unified Person cannot alias itself")

    def on_trash(self):
        frappe.throw("Issued Unified Person numbers are permanent and cannot be deleted")

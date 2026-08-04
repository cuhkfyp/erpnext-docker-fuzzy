import frappe
from frappe.model.document import Document


class CCDMatchingPolicy(Document):
    def validate(self):
        if not 0 < float(self.high_precision_target or 0) <= 1:
            frappe.throw("High precision target must be between 0 and 1")
        if int(self.minimum_high_samples or 0) < 1:
            frappe.throw("Minimum high samples must be at least 1")
        if int(self.max_block_size or 0) < 2:
            frappe.throw("Maximum block size must be at least 2")
        if int(self.max_candidate_pairs or 0) < 1:
            frappe.throw("Maximum candidate pairs must be at least 1")

        allowed_attributes = {
            "chi_surname",
            "chi_firstname",
            "eng_surname",
            "eng_firstname",
            "phone",
            "email",
            "birthday",
            "hksr_num",
            "hkid",
        }
        expected_comparators = {
            "chi_surname": "Chinese Name",
            "chi_firstname": "Chinese Name",
            "eng_surname": "English Name",
            "eng_firstname": "English Name",
            "phone": "Phone Exact",
            "email": "Email Exact",
            "birthday": "Birthday Exact",
            "hksr_num": "Identifier Exact",
            "hkid": "Identifier Exact",
        }
        trusted = {
            value.strip()
            for value in str(self.trusted_global_identifiers or "").split(",")
            if value.strip()
        }
        unsupported_trusted = trusted - {"hkid", "hksr_num"}
        if unsupported_trusted:
            frappe.throw(
                "Unsupported trusted global identifier(s): "
                + ", ".join(sorted(unsupported_trusted))
            )
        seen = set()
        ccd_master = frappe.get_meta("CCD Master")
        for row in self.source_profiles or []:
            attribute = str(row.canonical_attribute or "").strip()
            if attribute not in allowed_attributes:
                frappe.throw(f"Unsupported canonical attribute: {attribute}")
            key = (row.ccd_registration, attribute)
            if key in seen:
                frappe.throw(
                    f"Duplicate source profile for {row.ccd_registration} and {attribute}"
                )
            seen.add(key)
            expected = expected_comparators[attribute]
            if not row.comparator:
                row.comparator = expected
            elif row.comparator != expected:
                frappe.throw(
                    f"{row.ccd_registration} {attribute} must use comparator {expected}"
                )
            if row.enabled and not ccd_master.has_field(row.fieldname):
                frappe.throw(f"CCD Master has no field named {row.fieldname}")

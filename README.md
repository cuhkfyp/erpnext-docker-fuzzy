# erpnext-docker-fuzzy

Server-side fuzzy record matching for cross-centre `CCD Master` records in
Frappe/ERPNext.

The baseline implementation is documented in [`api_ccd_fuzzy.md`](api_ccd_fuzzy.md).
It compares Chinese names, English names, telephone numbers, and identifiers
using a formula stored on each `CCD Registration` record.

Real client records and credentials must never be committed to this repository.

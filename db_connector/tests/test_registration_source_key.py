import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from db_connector import api_identity_retirement as retirement


class Registration(dict):
    def __init__(self, *, is_new=False, **values):
        super().__init__(values)
        self.name = values.get("name", "registration")
        self._is_new = is_new

    def is_new(self):
        return self._is_new

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as error:
            raise AttributeError(key) from error

    def __setattr__(self, key, value):
        if key.startswith("_") or key == "name":
            object.__setattr__(self, key, value)
        else:
            self[key] = value


class RegistrationSourceKeyTests(unittest.TestCase):
    def setUp(self):
        self.meta = SimpleNamespace(has_field=lambda fieldname: True)

    def test_existing_draft_refreshes_key_after_routing_correction(self):
        registration = Registration(
            name="draft-1",
            docstatus=0,
            ccd_reg_doctype="CCD-REG-HQ-vDB01_HKSRGRH_PROD",
            ccd_stable_source_key="HQ-vDB01_HMSGRH_PROD",
        )
        database = MagicMock()
        database.get_value.return_value = {
            "docstatus": 0,
            "ccd_stable_source_key": "HQ-vDB01_HMSGRH_PROD",
        }
        with patch.object(retirement.frappe, "get_meta", return_value=self.meta), patch.object(
            retirement.frappe, "db", database
        ):
            retirement.validate_registration_source_key(registration)

        self.assertEqual(
            registration.ccd_stable_source_key,
            "HQ-vDB01_HKSRGRH_PROD",
        )

    def test_submit_transition_can_refresh_the_last_draft_correction(self):
        registration = Registration(
            name="draft-1",
            docstatus=1,
            ccd_reg_doctype="CCD-REG-HQ-vDB01_HKSRGRH_PROD",
            ccd_stable_source_key="HQ-vDB01_HMSGRH_PROD",
        )
        database = MagicMock()
        database.get_value.return_value = {
            "docstatus": 0,
            "ccd_stable_source_key": "HQ-vDB01_HMSGRH_PROD",
        }
        with patch.object(retirement.frappe, "get_meta", return_value=self.meta), patch.object(
            retirement.frappe, "db", database
        ):
            retirement.validate_registration_source_key(registration)

        self.assertEqual(
            registration.ccd_stable_source_key,
            "HQ-vDB01_HKSRGRH_PROD",
        )

    def test_submitted_registration_rejects_a_source_change(self):
        registration = Registration(
            name="submitted-1",
            docstatus=1,
            ccd_reg_doctype="CCD-REG-HQ-vDB01_HKSRGRH_PROD",
            ccd_stable_source_key="HQ-vDB01_HMSGRH_PROD",
        )
        database = MagicMock()
        database.get_value.return_value = {
            "docstatus": 1,
            "ccd_stable_source_key": "HQ-vDB01_HMSGRH_PROD",
        }
        with patch.object(retirement.frappe, "get_meta", return_value=self.meta), patch.object(
            retirement.frappe, "db", database
        ), patch.object(retirement.frappe, "throw", side_effect=RuntimeError("blocked")):
            with self.assertRaisesRegex(RuntimeError, "blocked"):
                retirement.validate_registration_source_key(registration)


if __name__ == "__main__":
    unittest.main()

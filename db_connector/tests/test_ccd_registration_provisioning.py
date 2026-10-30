import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe import _dict

from db_connector import ccd_registration_provisioning as provisioning


class CCDRegistrationProvisioningTests(unittest.TestCase):
    def registration(self, **overrides):
        values = {
            "name": "replacement-2",
            "ccd_reg_doctype": "CCD-REG-HOST_DATABASE",
            "ccd_table": "ServiceUser",
            "custom_table": "",
            "time_interval": 5,
            "agent_config": "",
            "agent_os": "",
            "agent_installation": "",
            "fieldmatch": [
                _dict(ccd_fieldname="id", fieldtype="Data"),
                _dict(ccd_fieldname="birthday", fieldtype="Date"),
            ],
            "amended_from": "cancelled-revision",
        }
        values.update(overrides)
        return _dict(values)

    def mail(self):
        return {
            "SMTP_SERVER": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_USER": "agent@example.test",
            "SMTP_PASS": "test-app-password",
            "NOTIFY_EMAIL": "ops@example.test",
        }

    def test_normal_table_keeps_all_original_sync_actions_and_mail(self):
        config = provisioning.build_agent_config(self.registration(), self.mail())
        self.assertIn("ACTION_1 = SELECT * FROM ServiceUser", config)
        self.assertIn("ACTION_2 = SYNC_TO_CCD_REG_BULK", config)
        self.assertIn("ACTION_3 = SYNC_TO_CCD_MASTER_BULK", config)
        self.assertIn("[MAIL]", config)
        self.assertIn("SMTP_USER = agent@example.test", config)

    def test_custom_sql_is_flattened_for_the_agent_config_parser(self):
        registration = self.registration(
            ccd_table=provisioning.CUSTOM_TABLE_SENTINEL,
            custom_table="SELECT a.ID, b.Phone\nFROM ServiceUser a\nJOIN Contact b ON b.ID=a.ID;",
        )
        config = provisioning.build_agent_config(registration, self.mail())
        self.assertIn(
            "ACTION_1 = SELECT a.ID, b.Phone FROM ServiceUser a JOIN Contact b ON b.ID=a.ID",
            config,
        )
        self.assertNotIn("FROM ServiceUser a\n", config)

    def test_semicolon_inside_sql_string_is_not_a_second_statement(self):
        registration = self.registration(
            ccd_table=provisioning.CUSTOM_TABLE_SENTINEL,
            custom_table=(
                "SELECT STRING_AGG(CONCAT(Name, ' (', Phone, ')'), '; ') AS Contacts "
                "FROM Contact;"
            ),
        )
        config = provisioning.build_agent_config(registration, self.mail())
        self.assertIn("STRING_AGG", config)
        self.assertIn("'; '", config)

    def test_manual_non_job_sections_survive_generation(self):
        registration = self.registration(
            agent_config="[SYSTEM]\nCUSTOM = retained\n\n[JOB02]\nACTION = KEEP_ME\n"
        )
        config = provisioning.build_agent_config(registration, self.mail())
        self.assertIn("[SYSTEM]\nCUSTOM = retained", config)
        self.assertIn("[JOB02]\nACTION = KEEP_ME", config)

    def test_existing_mail_override_is_preserved(self):
        registration = self.registration(
            agent_config=(
                "[MAIL]\nSMTP_SERVER = own.example.test\nSMTP_PORT = 465\n"
                "SMTP_USER = own@example.test\nSMTP_PASS = @Password(\"own-secret\")\n"
                "NOTIFY_EMAIL = owner@example.test"
            )
        )
        config = provisioning.build_agent_config(registration, self.mail())
        self.assertIn("SMTP_SERVER = own.example.test", config)
        self.assertIn("SMTP_PASS = @Password(\"own-secret\")", config)

    def test_multiple_custom_sql_statements_are_rejected(self):
        registration = self.registration(
            ccd_table=provisioning.CUSTOM_TABLE_SENTINEL,
            custom_table="SELECT * FROM ServiceUser; DROP TABLE ServiceUser",
        )
        with self.assertRaises(Exception):
            provisioning.build_agent_config(registration, self.mail())

    def test_amended_replacement_uses_the_same_pre_submit_path(self):
        registration = self.registration()
        with patch.object(
            provisioning, "configured_mail_defaults", return_value=self.mail()
        ):
            provisioning.prepare_registration_before_submit(registration)
        self.assertEqual(registration.ccd_reg_doctype, "CCD-REG-HOST_DATABASE")
        self.assertIn("SYNC_TO_CCD_MASTER_BULK", registration.agent_config)
        self.assertEqual(registration.amended_from, "cancelled-revision")

    def test_existing_empty_generated_doctype_can_gain_new_fields(self):
        existing = SimpleNamespace(
            custom=1,
            module=provisioning.GENERATED_MODULE,
            fields=[SimpleNamespace(fieldname="id", fieldtype="Data")],
            append=lambda field, value: existing.fields.append(SimpleNamespace(**value)),
            save=lambda **kwargs: None,
            get=lambda field: getattr(existing, field, None),
        )
        database = SimpleNamespace(
            exists=lambda doctype, name: True,
            count=lambda doctype: 0,
        )
        with patch.object(provisioning.frappe, "db", database), patch.object(
            provisioning.frappe, "get_doc", return_value=existing
        ):
            name, created = provisioning._ensure_generated_doctype(self.registration())
        self.assertEqual(name, "CCD-REG-HOST_DATABASE")
        self.assertFalse(created)
        self.assertIn("birthday", {field.fieldname for field in existing.fields})
        self.assertIn("ccd_source_key", {field.fieldname for field in existing.fields})


if __name__ == "__main__":
    unittest.main()

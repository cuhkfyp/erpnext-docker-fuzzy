"""Governed provisioning for submitted CCD Registration revisions.

This module replaces the mixed legacy/supervisor After Submit Server Script.
It deliberately keeps the established workflow: create the generated staging
DocType, add its Desk links, and prepare the client agent configuration.  The
only new branch is the opt-in ``*** Customize ***`` SQL source.
"""

from __future__ import annotations

import re
from typing import Any

import frappe


CUSTOM_TABLE_SENTINEL = "*** Customize ***"
SETTINGS_DOCTYPE = "CCD Agent Sync Settings"
GENERATED_MODULE = "hksr-ccd"
WORKSPACE_NAME = "Common Client Database"
REQUIRED_MAIL_KEYS = (
    "SMTP_SERVER",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASS",
    "NOTIFY_EMAIL",
)
_SECTION_HEADER_RE = re.compile(r"(?im)^[ \t]*\[([A-Za-z0-9_]+)\][ \t]*$")
_PASSWORD_MACRO_RE = re.compile(r"^@Password\([\"'](.*)[\"']\)$", re.IGNORECASE)
_SAFE_DOCTYPE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_SYSTEM_FIELDNAMES = frozenset(
    {
        "name",
        "owner",
        "creation",
        "modified",
        "modified_by",
        "docstatus",
        "idx",
    }
)


def _section_span(config: str, section_name: str) -> tuple[int, int] | None:
    matches = list(_SECTION_HEADER_RE.finditer(str(config or "")))
    wanted = section_name.upper()
    for index, match in enumerate(matches):
        if match.group(1).upper() != wanted:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(config)
        return match.start(), end
    return None


def extract_config_section(config: str, section_name: str) -> str:
    span = _section_span(str(config or ""), section_name)
    if not span:
        return ""
    return str(config or "")[span[0] : span[1]].strip()


def _replace_config_section(config: str, section_name: str, section: str) -> str:
    current = str(config or "").strip()
    replacement = str(section or "").strip()
    span = _section_span(current, section_name)
    if not span:
        return "\n\n".join(value for value in (current, replacement) if value).strip() + "\n"
    before = current[: span[0]].rstrip()
    after = current[span[1] :].lstrip()
    return "\n\n".join(value for value in (before, replacement, after) if value).strip() + "\n"


def _parse_section_values(section: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in str(section or "").splitlines()[1:]:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().upper()] = value.strip()
    return values


def _password_value(value: Any) -> str:
    password = str(value or "").strip()
    match = _PASSWORD_MACRO_RE.fullmatch(password)
    return match.group(1) if match else password


def mail_defaults_from_existing_registration() -> dict[str, str]:
    """Find the established Ben mail block without exposing it in source code."""
    rows = frappe.get_all(
        "CCD Registration",
        fields=["name", "agent_config"],
        filters={"agent_config": ["is", "set"]},
        order_by="modified desc",
        limit_page_length=0,
    )
    candidates: list[tuple[int, dict[str, str]]] = []
    for row in rows:
        values = _parse_section_values(
            extract_config_section(str(row.agent_config or ""), "MAIL")
        )
        if not all(values.get(key) for key in REQUIRED_MAIL_KEYS):
            continue
        values["SMTP_PASS"] = _password_value(values["SMTP_PASS"])
        priority = 0 if values.get("SMTP_USER", "").lower().startswith("ben.kong@") else 1
        candidates.append((priority, values))
    return min(candidates, key=lambda item: item[0])[1] if candidates else {}


def configured_mail_defaults() -> dict[str, str]:
    defaults: dict[str, str] = {}
    if frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        settings = frappe.get_single(SETTINGS_DOCTYPE)
        defaults = {
            "SMTP_SERVER": str(settings.get("agent_mail_smtp_server") or "").strip(),
            "SMTP_PORT": str(settings.get("agent_mail_smtp_port") or "").strip(),
            "SMTP_USER": str(settings.get("agent_mail_smtp_user") or "").strip(),
            "SMTP_PASS": str(
                settings.get_password(
                    "agent_mail_smtp_password", raise_exception=False
                )
                or ""
            ).strip(),
            "NOTIFY_EMAIL": str(
                settings.get("agent_mail_notify_email") or ""
            ).strip(),
        }
    if not all(defaults.get(key) for key in REQUIRED_MAIL_KEYS):
        legacy = mail_defaults_from_existing_registration()
        for key in REQUIRED_MAIL_KEYS:
            defaults[key] = defaults.get(key) or legacy.get(key, "")
    return defaults


def _mail_section(existing_config: str, defaults: dict[str, str]) -> str:
    values = dict(defaults or {})
    existing = _parse_section_values(extract_config_section(existing_config, "MAIL"))
    values.update({key: value for key, value in existing.items() if value})
    values["SMTP_PASS"] = _password_value(values.get("SMTP_PASS"))
    missing = [key for key in REQUIRED_MAIL_KEYS if not values.get(key)]
    if missing:
        frappe.throw(
            "Agent failure-email defaults are incomplete in CCD Agent Sync Settings: "
            + ", ".join(missing)
        )
    password = values["SMTP_PASS"]
    if any(character in password for character in ('"', "'", "\r", "\n")):
        frappe.throw("The agent SMTP password contains a character unsupported by @Password")
    try:
        port = int(values["SMTP_PORT"])
    except (TypeError, ValueError):
        frappe.throw("Agent failure-email SMTP Port must be a number")
    if not 1 <= port <= 65535:
        frappe.throw("Agent failure-email SMTP Port must be between 1 and 65535")
    return (
        "[MAIL]\n"
        f"SMTP_SERVER = {values['SMTP_SERVER']}\n"
        f"SMTP_PORT = {port}\n"
        f"SMTP_USER = {values['SMTP_USER']}\n"
        f"SMTP_PASS = @Password(\"{password}\")\n"
        f"NOTIFY_EMAIL = {values['NOTIFY_EMAIL']}"
    )


def _unquoted_semicolons(query: str) -> list[int]:
    """Return statement separators while ignoring SQL literals/comments."""
    positions: list[int] = []
    state = "plain"
    index = 0
    while index < len(query):
        character = query[index]
        following = query[index + 1] if index + 1 < len(query) else ""
        if state == "plain":
            if character == "'":
                state = "single"
            elif character == '"':
                state = "double"
            elif character == "[":
                state = "bracket"
            elif character == "-" and following == "-":
                state = "line_comment"
                index += 1
            elif character == "/" and following == "*":
                state = "block_comment"
                index += 1
            elif character == ";":
                positions.append(index)
        elif state == "single":
            if character == "'" and following == "'":
                index += 1
            elif character == "'":
                state = "plain"
        elif state == "double":
            if character == '"' and following == '"':
                index += 1
            elif character == '"':
                state = "plain"
        elif state == "bracket" and character == "]":
            state = "plain"
        elif state == "line_comment" and character in "\r\n":
            state = "plain"
        elif state == "block_comment" and character == "*" and following == "/":
            state = "plain"
            index += 1
        index += 1
    return positions


def _single_line_query(registration: Any) -> str:
    table = str(registration.get("ccd_table") or "").strip()
    if table == CUSTOM_TABLE_SENTINEL:
        query = str(registration.get("custom_table") or "").strip()
        if not query:
            frappe.throw("Customize Client Table (SQL) is required")
        query_without_leading_comments = re.sub(
            r"\A(?:\s|--[^\r\n]*(?:\r?\n|\Z)|/\*.*?\*/)*",
            "",
            query,
            flags=re.DOTALL,
        )
        if not re.match(r"(?is)^(SELECT|WITH)\b", query_without_leading_comments):
            frappe.throw("Customize Client Table (SQL) must be a read-only SELECT query")
    else:
        if not table:
            frappe.throw("Common Client Table is required")
        query = f"SELECT * FROM {table}"

    query = query.strip()
    semicolons = _unquoted_semicolons(query)
    if semicolons and semicolons[-1] == len(query) - 1:
        query = query[:-1].rstrip()
        semicolons.pop()
    if semicolons:
        frappe.throw("The agent source query must contain only one SQL statement")
    return re.sub(r"\s+", " ", query).strip()


def build_agent_config(
    registration: Any, mail_defaults: dict[str, str] | None = None
) -> str:
    """Generate JOB01 while preserving unrelated manual config sections."""
    existing = str(registration.get("agent_config") or "")
    interval = str(registration.get("time_interval") or "5").strip()
    try:
        minutes = int(interval)
    except (TypeError, ValueError):
        frappe.throw("Time Interval must be a whole number of minutes")
    if minutes < 1:
        frappe.throw("Time Interval must be at least one minute")
    job = (
        "[JOB01]\n"
        "TASKNAME = Full sync\n"
        f"MINS = {minutes}\n"
        f"ACTION_1 = {_single_line_query(registration)}\n"
        "ACTION_2 = SYNC_TO_CCD_REG_BULK\n"
        "ACTION_3 = SYNC_TO_CCD_MASTER_BULK"
    )
    generated = _replace_config_section(existing, "JOB01", job)
    mail = _mail_section(existing, mail_defaults or configured_mail_defaults())
    return _replace_config_section(generated, "MAIL", mail)


def _generated_doctype_name(registration: Any) -> str:
    name = str(registration.get("ccd_reg_doctype") or "").strip()
    if not name:
        name = f"CCD-REG-{registration.name}"
        registration.ccd_reg_doctype = name
    if not name.startswith("CCD-REG-"):
        frappe.throw("CCD Register Name must start with CCD-REG-")
    invalid = sorted({character for character in name if character not in _SAFE_DOCTYPE_CHARS})
    if invalid:
        frappe.throw(
            "Generated CCD REG DocType name contains invalid character(s): "
            + ", ".join(invalid)
        )
    return name


def _generated_field_definitions(registration: Any) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in registration.get("fieldmatch", []):
        fieldname = str(row.get("ccd_fieldname") or "").strip()
        if not fieldname:
            continue
        if fieldname in _SYSTEM_FIELDNAMES or fieldname == "ccd_source_key":
            frappe.throw(f"Field Matching contains reserved field name {fieldname}")
        if fieldname in seen:
            frappe.throw(f"Field Matching contains duplicate field name {fieldname}")
        seen.add(fieldname)
        fields.append(
            {
                "fieldname": fieldname,
                "label": fieldname,
                "fieldtype": str(row.get("fieldtype") or "Data"),
            }
        )
    if not fields:
        frappe.throw("At least one Field Matching row is required")
    fields.append(
        {
            "fieldname": "ccd_source_key",
            "label": "CCD Source Key",
            "fieldtype": "Data",
            "unique": 1,
        }
    )
    return fields


def prepare_registration_before_submit(
    registration: Any, method: str | None = None
) -> None:
    """Prepare generated content for new and amended replacement revisions."""
    _generated_doctype_name(registration)
    _generated_field_definitions(registration)
    registration.agent_config = build_agent_config(registration)

    if not registration.get("agent_installation") and registration.get("agent_os"):
        template = frappe.get_doc(
            "CCD Agent OS Batch Template", registration.get("agent_os")
        )
        content = str(template.get("os_template") or "")
        if not content:
            frappe.throw("The selected CCD Agent OS template is empty")
        registration.agent_installation = content


def _ensure_generated_doctype(registration: Any) -> tuple[str, bool]:
    name = _generated_doctype_name(registration)
    desired_fields = _generated_field_definitions(registration)
    if frappe.db.exists("DocType", name):
        existing = frappe.get_doc("DocType", name)
        if not existing.get("custom") or existing.get("module") != GENERATED_MODULE:
            frappe.throw(f"Existing DocType {name} is not a generated CCD staging DocType")
        existing_fields = {row.fieldname: row for row in existing.fields}
        missing = []
        for definition in desired_fields:
            row = existing_fields.get(definition["fieldname"])
            if not row:
                missing.append(definition)
                continue
            if str(row.fieldtype) != str(definition["fieldtype"]):
                frappe.throw(
                    f"Generated DocType {name} field {row.fieldname} has type "
                    f"{row.fieldtype}, expected {definition['fieldtype']}"
                )
        if missing:
            if frappe.db.count(name):
                frappe.throw(
                    f"Generated DocType {name} still contains data and cannot be "
                    "updated; complete governed identity retirement first"
                )
            for definition in missing:
                existing.append("fields", definition)
            existing.save(ignore_permissions=True)
        return name, False

    frappe.get_doc(
        {
            "doctype": "DocType",
            "name": name,
            "module": GENERATED_MODULE,
            "custom": 1,
            "istable": 0,
            "fields": desired_fields,
            "permissions": [
                {
                    "role": "System Manager",
                    "read": 1,
                    "write": 1,
                    "create": 1,
                    "delete": 1,
                },
                {
                    "role": "ccd-user",
                    "read": 1,
                    "write": 1,
                    "create": 1,
                    "delete": 0,
                },
            ],
        }
    ).insert(ignore_permissions=True)
    return name, True


def _ensure_workspace_links(registration: Any, generated_doctype: str) -> None:
    if not frappe.db.exists("Workspace", WORKSPACE_NAME):
        frappe.log_error(
            title="CCD Registration workspace link skipped",
            message=f"Workspace {WORKSPACE_NAME} does not exist",
        )
        return
    workspace = frappe.get_doc("Workspace", WORKSPACE_NAME)
    changed = False

    if not any(
        row.type == "Link" and row.link_to == generated_doctype
        for row in workspace.links
    ):
        target_index = len(workspace.links)
        for index, row in enumerate(workspace.links):
            if row.type == "Card Break" and row.label == "Applications":
                target_index = index + 1
                while (
                    target_index < len(workspace.links)
                    and workspace.links[target_index].type != "Card Break"
                ):
                    target_index += 1
                break
        new_row = workspace.append(
            "links",
            {
                "type": "Link",
                "link_type": "DocType",
                "link_to": generated_doctype,
                "label": f"{registration.get('sc_label') or registration.name} ({registration.name})",
            },
        )
        workspace.links.remove(new_row)
        workspace.links.insert(target_index, new_row)
        changed = True

    url = str(registration.get("url") or "").strip()
    if url:
        workspace_label = str(registration.get("sc_label") or registration.name)
        shortcut_label = f"{workspace_label} ({registration.name}) web page"
        if not any(
            row.label == shortcut_label and row.link_to == url
            for row in workspace.shortcuts
        ):
            workspace.append(
                "shortcuts",
                {
                    "label": shortcut_label,
                    "type": "URL",
                    "link_to": url,
                    "icon": "icon-project-2",
                    "color": "Grey",
                    "is_query_report": 0,
                },
            )
            changed = True

    if changed:
        for index, row in enumerate(workspace.links):
            row.idx = index + 1
        workspace.save(ignore_permissions=True)


def provision_registration_on_submit(
    registration: Any, method: str | None = None
) -> None:
    """Idempotently create staging metadata for any submitted revision."""
    generated_doctype, created = _ensure_generated_doctype(registration)
    try:
        _ensure_workspace_links(registration, generated_doctype)
    except Exception:
        # Workspace navigation is helpful but must not invalidate a correctly
        # provisioned source. Preserve the established fail-open behavior.
        frappe.log_error(
            frappe.get_traceback(),
            "CCD Registration workspace provisioning failed",
        )
    action = "created" if created else "verified"
    frappe.msgprint(
        f"CCD staging DocType {generated_doctype} was {action}; agent configuration is ready."
    )

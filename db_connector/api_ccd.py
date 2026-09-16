from db_connector import default_value
import frappe
import json


def _require_ccd_writer():
    """Limit source-data mutation APIs to the integration role or managers."""
    roles = set(frappe.get_roles())
    if "System Manager" not in roles and "ccd-user" not in roles:
        frappe.throw(
            "ccd-user or System Manager role is required",
            frappe.PermissionError,
        )


def _validated_update_fields(doctype, row):
    """Return metadata-backed column updates and reject SQL identifiers."""
    meta = frappe.get_meta(doctype)
    protected = {
        "name",
        "owner",
        "creation",
        "modified",
        "modified_by",
        "docstatus",
        "idx",
        "parent",
        "parentfield",
        "parenttype",
        "ccd_source_key",
        "ccd_reg_source",
    }
    fields = {}
    for fieldname, value in row.items():
        fieldname = str(fieldname)
        if fieldname in protected:
            continue
        if not fieldname.replace("_", "").isalnum() or not meta.has_field(fieldname):
            frappe.throw(f"Invalid update field for {doctype}: {fieldname}")
        fields[fieldname] = value
    return fields

def get_connect(dt):  ## enter doctype to connect database, if ok return conncetionn else null and return error
    db_type = dt.get("db_type")
    port = dt.get("db_port")
    server = dt.get("db_server")
    database = dt.get("db_database")
    user = dt.get("db_username")
    password = dt.get_password("password_db")
    db_type = dt.get("db_type")

    if not db_type:
        return {"status": default_value.ERR_NOTYPE, "connection":None}

    db_type = str(db_type).strip().lower()
    conn = None

    try:
        msg = ""
        if (db_type == "mssql"):
           # Import pyodbc to match the rest of your system
           import pyodbc
           # Build the exact same connection string used in api_newtwo.py
           # We use Python f-strings (the 'f' before the string) to insert your variables
           conn_str = (
                   "DRIVER={ODBC Driver 18 for SQL Server}; "
                  f"SERVER={server},{port}; "
                  f"DATABASE={database}; "
                  f"UID={user};PWD={password}; "
                   "TrustServerCertificate=yes; "
            )
           conn = pyodbc.connect(conn_str, timeout=5)
        elif (db_type == "mysql"):
             import pymysql
             conn = pymysql.connect(host=server, user=user, password=password, database=database, cursor=pymysql.cursors.DictCursor)
        elif (db_type =="oracle"):
             import oracledb
             conn = oracledb.connect(user=user, password=password, dsn = f"{server}:{port}/{database}")
        return {"status":0, "connection": conn}
    except ImportError:
           return {"status":-5, "connection": conn}
    except Exception as e:
           # This will catch wrong passwords, bad IP addresses, etc.
           return {"status":e, "connection":conn}

def get_sqlversion(dt, conn):
    db_type = dt.get("db_type")
    db_type = str(db_type).strip().lower()
    if not db_type:
       return f"Type is not defined. {db_type}"
    try:
       msg = ""
       if (db_type=="mssql"):
           cursor = conn.cursor()
           cursor.execute("SELECT @@VERSION")
           msg = cursor.fetchone()[0]
       elif (db_type=="mysql"):
           cursor = conn.cursor()
       elif (db_type=="oracle"):
           cursor = conn.cursor()
    except Exception as e:
           msg = f"{db_type}:Error in {e}"
           return msg
    finally:
           return f"{db_type} version is {msg}"

@frappe.whitelist()
def connect_test(cdt):
    if isinstance(cdt, str):
        doc = json.loads(cdt)
    else:
        doc = cdt
    dt = frappe.get_doc("CCD Registration",doc.get('name'))
    if not dt:
        return "Error: no record found!"

    result = {}
    result = get_connect(dt)
    if result["status"] == 0:
        return get_sqlversion(dt, result["connection"])
    else:
        return result["status"]

@frappe.whitelist()
def get_tables(cdt):
    if isinstance(cdt, str):
        doc = json.loads(cdt)
    else:
        doc = cdt

    dt = frappe.get_doc("CCD Registration", doc.get('name'))
    if not dt:
        return {"status": "error", "message": "Error: no record found!"}

    # 1. Get connection using your existing function
    result = get_connect(dt)

    # Check if connection failed
    if result["status"] != 0:
        return {"status": "error", "message": str(result["status"])}

    conn = result["connection"]
    db_type = str(dt.get("db_type")).strip().lower()
    tables = []

    try:
        cursor = conn.cursor()

        # 2. Execute the correct query based on database type
        if db_type == "mssql":
            query = """
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME;
            """
            cursor.execute(query)
            for row in cursor.fetchall():
                # row[1] grabs the TABLE_NAME column from the query results
                tables.append(row[1])

        elif db_type == "mysql":
            query = "SHOW TABLES;"
            cursor.execute(query)
            for row in cursor.fetchall():
                # Handle MySQL dictionary cursor
                tables.append(list(row.values())[0])

        # 3. Get the version message you already created
        version_msg = get_sqlversion(dt, conn)

        # 4. Return both the success message and the list of tables
        return {
            "status": "success",
            "message": version_msg,
            "tables": tables
        }

    except Exception as e:
        return {"status": "error", "message": f"Error fetching tables: {str(e)}"}
    finally:
        if conn:
            conn.close()

@frappe.whitelist()
def get_fields(cdt):
    if isinstance(cdt, str):
        doc = json.loads(cdt)
    else:
        doc = cdt

    dt = frappe.get_doc("CCD Registration", doc.get('name'))
    if not dt:
        return {"status": "error", "message": "Error: no record found!"}

    # 1. Get connection using your existing function
    result = get_connect(dt)

    # Check if connection failed
    if result["status"] != 0:
        return {"status": "error", "message": str(result["status"])}

    conn = result["connection"]
    db_type = str(dt.get("db_type")).strip().lower()
    tablename = dt.get("ccd_table")
    fields = []
    try:
        cursor = conn.cursor()
        if db_type =="mssql":
            query="""
SELECT c.name AS Column_Name,t.name AS Data_Type,c.max_length,c.is_nullable,c.is_identity FROM sys.columns c
       JOIN sys.types t ON c.user_type_id = t.user_type_id
       WHERE c.object_id = OBJECT_ID(?);
"""
            cursor.execute(query, tablename)
            columns = [column[0] for column in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        elif db_type =="mysql":
            query = f"describe '{tablename}'"
            cursor.execute(query)
            results = cursor.fetchall()
        elif db_type =="oracle":
             ## define later...
            columns = {}
            results = {}
    finally:
        return results

@frappe.whitelist()
def add_shortcut_to_workspace(workspace_name, shortcut_label, link_to, shortcut_type="URL"):
    """
    Adds a shortcut link to a specific ERPNext Workspace.

    :param workspace_name: Name of the Workspace (e.g., 'CRM', 'Accounting')
    :param shortcut_label: The display text for the shortcut
    :param link_to: The target name (e.g., 'Customer' if it's a DocType)
    :param shortcut_type: Type of shortcut ('DocType', 'Report', 'Page', or 'URL')
    """
    # Check if the workspace exists
    if not frappe.db.exists("Workspace", workspace_name):
        frappe.msgprint(f"Workspace '{workspace_name}' not found.")
        return

    # Load the workspace document
    workspace_doc = frappe.get_doc("Workspace", workspace_name)

    # Check if the shortcut already exists to avoid duplicates
    for shortcut in workspace_doc.shortcuts:
        if shortcut.label == shortcut_label and shortcut.link_to == link_to:
            print(f"Shortcut '{shortcut_label}' already exists in '{workspace_name}'.")
            return

    # Append a new shortcut to the 'shortcuts' child table
    workspace_doc.append("shortcuts", {
        "label": shortcut_label,
        "type": shortcut_type,
        "link_to": link_to,
        "icon": "icon-project-2",                # Optional: Change to any standard Frappe icon
        "color": "Grey",               # Optional: Color of the shortcut card
        "is_query_report": 0           # Set to 1 if it's a query report
    })

    # Save the document to the database
    workspace_doc.save(ignore_permissions=True)

    # Clear cache so the UI updates immediately for users
    frappe.clear_cache(doctype="Workspace")

    print(f"Successfully added shortcut '{shortcut_label}' to '{workspace_name}' Workspace.")

# Example Usage:
# add_shortcut_to_workspace("CRM", "New Customer Entry", "Customer", "DocType")


@frappe.whitelist()
def auto_update_workspace(doctype_name, doctype_label):
    """Helper function to insert links directly into the Workspace child table"""
    workspace_name = "Common Client Database"
    workspace = frappe.get_doc("Workspace", workspace_name)

    target_index = -1

    # 1. Loop through the child table to find the "Applications" Card
    for i, row in enumerate(workspace.links):
        # In Frappe, cards are defined by a 'Card Break' row
        if row.type == "Card Break" and row.label == "Applications":

            # Found it! Now find the end of the links inside this specific card
            target_index = i + 1
            while target_index < len(workspace.links) and workspace.links[target_index].type != "Card Break":
                target_index += 1
            break

    if target_index != -1:
        # 2. Create the new link row
        new_row = workspace.append("links", {})
        new_row.type = "Link"
        new_row.link_type = "DocType"
        new_row.link_to = doctype_name
        new_row.label = doctype_label

        # 3. Move the new link to the correct position under "Applications"
        workspace.links.remove(new_row)
        workspace.links.insert(target_index, new_row)

        # 4. Reset the index numbers so Frappe saves the order correctly
        for i, row in enumerate(workspace.links):
            row.idx = i + 1

        # 5. Save the document and refresh caches so the new DocType route is visible
        workspace.save(ignore_permissions=True)
        frappe.db.commit()

        # 6. Aggressively clear the cache
        frappe.clear_cache(doctype=doctype_name)
        frappe.clear_cache(doctype="Workspace")
        frappe.clear_cache(user=frappe.session.user)
        frappe.cache().delete_value(f"workspace:{workspace_name}")

    else:
        # If it couldn't find the 'Applications' card break
        frappe.log_error(f"Could not find 'Applications' card in {workspace_name}")

@frappe.whitelist()
def remove_shortcut_from_workspace(workspace_name, shortcut_label, link_to=None):
    """
    Removes a specific shortcut from an ERPNext Workspace.

    :param workspace_name: Name of the Workspace (e.g., 'CRM')
    :param shortcut_label: The display text of the shortcut you want to remove
    :param link_to: (Optional) The target link name to guarantee matching the right shortcut
    """
    from db_connector.api_identity_retirement import _require_manager

    _require_manager()
    if not frappe.db.exists("Workspace", workspace_name):
        print(f"Workspace '{workspace_name}' does not exist.")
        return

    workspace_doc = frappe.get_doc("Workspace", workspace_name)
    initial_count = len(workspace_doc.shortcuts)

    # Filter the child table to keep rows that DO NOT match our target shortcut
    workspace_doc.shortcuts = [
        row for row in workspace_doc.shortcuts
        if not (row.label == shortcut_label and (link_to is None or row.link_to == link_to))
    ]

    # Check if a row was actually removed
    if len(workspace_doc.shortcuts) < initial_count:
        # Save changes to database
        workspace_doc.save(ignore_permissions=True)

        # Clear UI cache so changes appear immediately
        frappe.clear_cache(doctype="Workspace")
        print(f"Successfully removed shortcut '{shortcut_label}' from '{workspace_name}' Workspace.")
    else:
        print(f"Shortcut '{shortcut_label}' not found in '{workspace_name}' Workspace.")

@frappe.whitelist()
def remove_from_workspace(doctype_name):
    """Helper function to remove links from the Workspace child table"""
    from db_connector.api_identity_retirement import _require_manager

    _require_manager()
    workspace_name = "Common Client Database"

    if not frappe.db.exists("Workspace", workspace_name):
        frappe.log_error(f"Could not find Workspace: {workspace_name}")
        return

    workspace = frappe.get_doc("Workspace", workspace_name)
    original_length = len(workspace.links)

    # 1. Filter out the row that links to our generated doctype
    workspace.links = [row for row in workspace.links if row.link_to != doctype_name]

    # 2. If the length changed, it means we successfully found and removed the link
    if len(workspace.links) < original_length:

        # 3. Reset the index numbers so Frappe saves the order correctly
        for i, row in enumerate(workspace.links):
            row.idx = i + 1

        # 4. Save the document and force the database to commit
        workspace.save(ignore_permissions=True)

        # 5. Aggressively clear the cache to update the UI
        frappe.clear_cache(user=frappe.session.user)
        frappe.cache().delete_value(f"workspace:{workspace_name}")

    else:
        # Log if it was already missing, though not strictly an error
        frappe.log_error(f"Link for {doctype_name} was not found in {workspace_name} workspace.")

@frappe.whitelist()
def bulk_delete_ccd_masters(
    source_name,
    confirm_scope_fingerprint=None,
    reason=None,
    preview=0,
):
    """Compatibility entry point routed through governed source retirement."""
    from db_connector.api_identity_retirement import (
        apply_bulk_deletion,
        preview_bulk_deletion,
        resolve_source_key,
    )

    source_name = resolve_source_key(source_name)

    if str(preview).lower() in {"1", "true", "yes"}:
        return preview_bulk_deletion("CCD Master", source_name=source_name)
    return apply_bulk_deletion(
        "CCD Master",
        confirm_scope_fingerprint=confirm_scope_fingerprint,
        reason=reason,
        source_name=source_name,
    )

@frappe.whitelist()
def drop_generated_table(doctype_name):
    """Forcefully drop a generated CCD DocType's physical table.

    Current generated DocTypes use the ``CCD-REG-`` prefix. Older
    registrations can reference a custom DocType with a different name, so
    those are accepted only when both their metadata and a CCD Registration
    reference prove that they belong to the CCD integration.
    """
    from db_connector.api_identity_retirement import _require_manager

    _require_manager()
    if not _is_generated_ccd_doctype(doctype_name):
        frappe.throw("Invalid DocType name for deletion.")

    try:
        # Use sql_ddl for structural database commands
        frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{doctype_name}`")
    except Exception as e:
        frappe.log_error(f"Failed to drop table {doctype_name}: {str(e)}")
        frappe.throw(f"Failed to drop database table: {str(e)}")


def _is_generated_ccd_doctype(doctype_name):
    """Return whether a DocType is safe for the CCD cleanup path."""
    if not isinstance(doctype_name, str):
        return False

    doctype_name = doctype_name.strip()
    allowed_name_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not doctype_name or any(ch not in allowed_name_chars for ch in doctype_name):
        return False

    if doctype_name.startswith("CCD-REG-"):
        return True

    doctype_meta = frappe.db.get_value(
        "DocType",
        doctype_name,
        ["custom", "module"],
        as_dict=True,
    )
    if not doctype_meta:
        return False

    return bool(
        doctype_meta.custom
        and doctype_meta.module == "hksr-ccd"
        and frappe.db.exists(
            "CCD Registration",
            {"ccd_reg_doctype": doctype_name},
        )
    )

@frappe.whitelist()
def bulk_clear_ccd(
    doctype,
    hostname=None,
    source_id=None,
    confirm_scope_fingerprint=None,
    reason=None,
    preview=0,
):
    """
    Bulk-delete records from CCD doctypes.
    Security guard: only allows CCD-REG-* and CCD Master.
    """
    from db_connector.api_identity_retirement import (
        apply_bulk_deletion,
        preview_bulk_deletion,
    )

    source_name = source_id or hostname
    if doctype == "CCD Master":
        from db_connector.api_identity_retirement import resolve_source_key

        source_name = resolve_source_key(source_name)
    kwargs = {"doctype": doctype, "source_name": source_name}
    if str(preview).lower() in {"1", "true", "yes"}:
        return preview_bulk_deletion(**kwargs)
    return apply_bulk_deletion(
        **kwargs,
        confirm_scope_fingerprint=confirm_scope_fingerprint,
        reason=reason,
    )


@frappe.whitelist()
def send_action_to_agent(device_name, action_command):
    """
    Call this function from a button click in your ERPNext custom interface.
    It pushes data over websocket that your python agent script catches instantl
    """
    frappe.publish_realtime(
        event="trigger_windows_app_command",
        message={
            "target_device" : device_name,
            "action": action_command,
            "triggered_by" : frappe.session.user
        }
    )
    return {"status":"Message sent to channel"}

@frappe.whitelist()
def delete_by_source_keys_ccd(
    doctype,
    keys,
    hostname=None,
    source_id=None,
    confirm_scope_fingerprint=None,
    reason=None,
    preview=0,
):
    import json as _json
    if isinstance(keys, str):
        keys = _json.loads(keys)
    if not isinstance(keys, (list, tuple)):
        frappe.throw("keys must be a JSON array")
    source_name = source_id or hostname
    if doctype == "CCD Master":
        from db_connector.api_identity_retirement import resolve_source_key

        source_name = resolve_source_key(source_name)
    from db_connector.api_identity_retirement import (
        apply_bulk_deletion,
        preview_bulk_deletion,
    )

    kwargs = {
        "doctype": doctype,
        "source_name": source_name,
        "source_keys": keys,
    }
    if str(preview).lower() in {"1", "true", "yes"}:
        return preview_bulk_deletion(**kwargs)
    return apply_bulk_deletion(
        **kwargs,
        confirm_scope_fingerprint=confirm_scope_fingerprint,
        reason=reason,
    )

@frappe.whitelist()
def CCD_Match(rows):
    _require_ccd_writer()
    import json as __json
    from frappe.utils import now
    if isinstance(rows, str):
        rows = __json.load(rows)
    if not rows:
        return {"status":"err","updated":-1}
    source = row.get("ccd_source_key")
    if not source:
        errors.append("row missing or deleted")

@frappe.whitelist()
def update_by_source_key_ccd(doctype, rows, hostname=None, source_id=None):
    _require_ccd_writer()
    import json as _json
    from frappe.utils import now
    if isinstance(rows, str):
        rows = _json.loads(rows)
    if not rows:
        return {"status": "ok", "updated": 0}
    updated = 0
    errors = []
    source_name = source_id or hostname
    if doctype == "CCD Master" and source_name:
        from db_connector.api_identity_retirement import resolve_source_key

        source_name = resolve_source_key(source_name)
    for row in rows:
        source_key = row.get("ccd_source_key")
        if not source_key:
            errors.append("row missing ccd_source_key")
            continue
        fields = _validated_update_fields(doctype, row)
        if not fields:
            continue
        fields["modified"] = now()
        fields["modified_by"] = frappe.session.user
        set_clause = ", ".join([f"`{k}` = %s" for k in fields])
        values = list(fields.values())
        if doctype == "CCD Master":
            if not source_name:
                errors.append("missing source_id for CCD Master update")
                continue
            frappe.db.sql(
                f"UPDATE `tabCCD Master` SET {set_clause} WHERE `ccd_source_key` = %s AND `ccd_reg_source` = %s",
                values + [source_key, source_name]
            )
        elif doctype.startswith("CCD-REG-"):
            frappe.db.sql(
                f"UPDATE `tab{doctype}` SET {set_clause} WHERE `ccd_source_key` = %s",
                values + [source_key]
            )
        else:
            errors.append(f"unauthorized doctype: {doctype}")
            continue
        updated = updated + 1
    frappe.db.commit()
    return {"status": "ok", "updated": updated, "errors": errors}

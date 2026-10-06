"""Event-refreshed reporting snapshots for the optimized CCD dashboard.

The tables in this module are deliberately not Frappe DocTypes.  They are an
internal reporting interface: Superset receives only hashed logical-person
keys, aggregate-safe categories, and generation metadata.  A generation is
built off-screen and becomes visible with one state-row update.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path
from typing import Any

import frappe
from pymysql.cursors import SSCursor
from pymysql.err import OperationalError


SCHEMA_VERSION = 1
STATE_KEY = "active"
LOCK_NAME = "ccd-dashboard-snapshot-refresh-v1"
WARM_LOCK_NAME = "ccd-dashboard-snapshot-warm-v1"
MAINTENANCE_LOCK_NAME = "ccd-dashboard-snapshot-maintenance-v1"
FAST_IDENTITY_LOCK_NAME = "ccd-dashboard-fast-identity-v1"
JOB_PREFIX = "ccd-dashboard-snapshot-refresh-v1"
MAINTENANCE_JOB_PREFIX = "ccd-dashboard-snapshot-maintenance-v1"
DEFAULT_PRIVATE_ADDRESS_OVERRIDES = Path(
    "/home/frappe-user/superset/private/ccd_address_overrides.json"
)

CURRENT_MEMBERSHIP_STAGE = "ccd_snapshot_current_membership"
CURRENT_UNIFIED_STAGE = "ccd_snapshot_current_unified_membership"
SNAPSHOT_COPY_CHUNK_SIZE = 2_000
SNAPSHOT_DELETE_CHUNK_SIZE = 5_000

STATE_TABLE = "tabCCD Dashboard Snapshot State V1"
GENERATION_TABLE = "tabCCD Dashboard Snapshot Generation V1"
AUDIT_TABLE = "tabCCD Dashboard Snapshot Schema Audit V1"
PRESENCE_TABLE = "tabCCD Dashboard Person Service V1"
OVERLAP_TABLE = "tabCCD Dashboard Service Overlap V1"
IDENTITY_TABLE = "tabCCD Dashboard Identity Operation V1"
QUALITY_TABLE = "tabCCD Dashboard Data Quality V1"
DISTRICT_TABLE = "tabCCD Dashboard District Map V1"
FAST_IDENTITY_TABLE = "tabCCD Dashboard Identity Metric V1"
MEMBERSHIP_INDEX = "ccd_unified_membership_dashboard_current"

SNAPSHOT_INDEXES: dict[str, dict[str, tuple[str, ...]]] = {
    PRESENCE_TABLE: {
        "ccd_snapshot_presence_source_person": (
            "generation_id", "environment", "source", "logical_person_key",
        ),
        "ccd_snapshot_presence_source_rows": (
            "generation_id", "environment", "source_row_count",
        ),
        "ccd_snapshot_presence_overall_growth": (
            "generation_id", "environment", "overall_growth_month",
            "logical_person_key",
        ),
        "ccd_snapshot_presence_service_growth": (
            "generation_id", "environment", "service_growth_month", "service",
            "logical_person_key",
        ),
        "ccd_snapshot_presence_demographics": (
            "generation_id", "environment", "age_band", "dob_confidence",
            "sex_category", "logical_person_key",
        ),
    },
}

PACKAGE_ROOT = Path(__file__).resolve().parent / "dashboard_snapshot"
SQL_ROOT = PACKAGE_ROOT / "sql"
GEOMETRY_PATH = PACKAGE_ROOT / "assets" / "hk_districts_simplified.geojson"

DATA_TABLES = (
    PRESENCE_TABLE,
    OVERLAP_TABLE,
    IDENTITY_TABLE,
    QUALITY_TABLE,
    DISTRICT_TABLE,
)

SOURCE_WATERMARK_DOCTYPES = (
    "CCD Master",
    "CCD Registration",
    "CCD Field Match",
    "CCD Identity Group",
    "CCD Identity Membership",
    "CCD Unified Person",
    "CCD Unified Person Membership",
    "CCD Unified Person Alias",
    "CCD Match Recommendation",
    "CCD Identity Exclusion",
    "CCD Identity Overlap Resolution",
    "CCD Identity Retirement Run",
)

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    PRESENCE_TABLE: (
        "logical_person_key", "environment", "service", "source",
        "identity_state", "age_band", "dob_confidence", "sex_category",
        "district_code", "district_basis", "district", "area",
        "overall_growth_month", "service_growth_month", "services_per_person",
        "source_row_count", "service_available_from", "latest_ccd_modified",
    ),
    OVERLAP_TABLE: (
        "environment_a", "environment_b", "environment", "service_a",
        "service_b", "source_a", "source_b", "endpoint_a", "endpoint_b",
        "logical_person_count",
    ),
    IDENTITY_TABLE: (
        "operation_area", "status", "detail", "metric_count", "group_size",
        "snapshot_at",
    ),
    QUALITY_TABLE: (
        "source", "classification", "environment", "include_in_dashboard",
        "source_row_count", "populated_source", "service_metadata_coverage_pct",
        "service_start_coverage_pct", "growth_readiness", "sex_mapping_ready",
        "placeholder_sex_rows", "stored_sex_rows", "dob_mapping_ready",
        "dob_key_mapping_ready", "dob_populated_rows", "usable_dob_rows",
        "verified_dob_rows", "plausible_unverified_dob_rows", "invalid_dob_rows",
        "birthday_mapping_defect_rows", "residential_district_mapping_ready",
        "postal_district_mapping_ready", "address_inference_ready",
        "valid_district_rows", "inferred_district_rows", "ambiguous_address_rows",
        "unmapped_location_rows", "missing_district_rows",
        "service_available_from", "latest_ccd_modified",
    ),
    DISTRICT_TABLE: (
        "environment", "service", "source", "identity_state", "district_code",
        "district_basis", "district", "area", "district_polygon",
        "logical_person_count",
    ),
}

FAST_IDENTITY_COLUMNS = (
    "metric_key", "operation_area", "status", "detail", "metric_count",
    "group_size", "snapshot_at",
)


DDL = (
    f"""CREATE TABLE IF NOT EXISTS `{STATE_TABLE}` (
        state_key VARCHAR(32) NOT NULL,
        schema_version INT UNSIGNED NOT NULL,
        active_generation CHAR(32) NULL,
        previous_generation CHAR(32) NULL,
        dirty_version BIGINT UNSIGNED NOT NULL DEFAULT 1,
        built_dirty_version BIGINT UNSIGNED NOT NULL DEFAULT 0,
        last_dirty_at DATETIME(6) NULL,
        refresh_started_at DATETIME(6) NULL,
        active_generation_at DATETIME(6) NULL,
        refresh_status VARCHAR(32) NOT NULL DEFAULT 'Dirty',
        last_refresh_error VARCHAR(1000) NULL,
        last_job_id VARCHAR(140) NULL,
        modified DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
            ON UPDATE CURRENT_TIMESTAMP(6),
        PRIMARY KEY (state_key)
    ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC""",
    f"""CREATE TABLE IF NOT EXISTS `{GENERATION_TABLE}` (
        generation_id CHAR(32) NOT NULL,
        schema_version INT UNSIGNED NOT NULL,
        requested_dirty_version BIGINT UNSIGNED NOT NULL,
        status VARCHAR(32) NOT NULL,
        started_at DATETIME(6) NOT NULL,
        completed_at DATETIME(6) NULL,
        row_counts_json LONGTEXT NULL,
        checksums_json LONGTEXT NULL,
        error_summary VARCHAR(1000) NULL,
        PRIMARY KEY (generation_id),
        KEY ccd_snapshot_generation_status (status, completed_at)
    ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC""",
    f"""CREATE TABLE IF NOT EXISTS `{AUDIT_TABLE}` (
        audit_id CHAR(32) NOT NULL,
        migration_name VARCHAR(140) NOT NULL,
        recorded_at DATETIME(6) NOT NULL,
        before_sha256 CHAR(64) NOT NULL,
        after_sha256 CHAR(64) NOT NULL,
        audit_json LONGTEXT NOT NULL,
        PRIMARY KEY (audit_id),
        KEY ccd_snapshot_schema_audit_time (recorded_at)
    ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC""",
    f"""CREATE TABLE IF NOT EXISTS `{PRESENCE_TABLE}` (
        generation_id CHAR(32) NOT NULL,
        logical_person_key CHAR(64) NOT NULL,
        environment VARCHAR(32) NOT NULL,
        service VARCHAR(255) NOT NULL,
        source VARCHAR(255) NOT NULL,
        identity_state VARCHAR(32) NOT NULL,
        age_band VARCHAR(32) NOT NULL,
        dob_confidence VARCHAR(32) NOT NULL,
        sex_category VARCHAR(32) NOT NULL,
        district_code VARCHAR(16) NOT NULL,
        district_basis VARCHAR(64) NOT NULL,
        district VARCHAR(255) NOT NULL,
        area VARCHAR(255) NOT NULL,
        overall_growth_month CHAR(7) NULL,
        service_growth_month CHAR(7) NULL,
        services_per_person BIGINT UNSIGNED NOT NULL,
        source_row_count BIGINT UNSIGNED NOT NULL,
        service_available_from DATE NULL,
        latest_ccd_modified DATETIME(6) NULL,
        KEY ccd_snapshot_presence_filter (generation_id, environment, service, source),
        KEY ccd_snapshot_presence_person (generation_id, logical_person_key),
        KEY ccd_snapshot_presence_env_person (generation_id, environment, logical_person_key),
        KEY ccd_snapshot_presence_service_person
            (generation_id, environment, service, logical_person_key),
        KEY ccd_snapshot_presence_growth (generation_id, overall_growth_month, service_growth_month)
    ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC""",
    f"""CREATE TABLE IF NOT EXISTS `{OVERLAP_TABLE}` (
        generation_id CHAR(32) NOT NULL,
        environment_a VARCHAR(32) NOT NULL,
        environment_b VARCHAR(32) NOT NULL,
        environment VARCHAR(32) NOT NULL,
        service_a VARCHAR(255) NOT NULL,
        service_b VARCHAR(255) NOT NULL,
        source_a VARCHAR(255) NOT NULL,
        source_b VARCHAR(255) NOT NULL,
        endpoint_a VARCHAR(512) NOT NULL,
        endpoint_b VARCHAR(512) NOT NULL,
        logical_person_count BIGINT UNSIGNED NOT NULL,
        KEY ccd_snapshot_overlap_filter (generation_id, environment),
        KEY ccd_snapshot_overlap_endpoints (generation_id, endpoint_a(191), endpoint_b(191))
    ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC""",
    f"""CREATE TABLE IF NOT EXISTS `{IDENTITY_TABLE}` (
        generation_id CHAR(32) NOT NULL,
        operation_area VARCHAR(64) NOT NULL,
        status VARCHAR(255) NOT NULL,
        detail VARCHAR(255) NOT NULL,
        metric_count BIGINT UNSIGNED NOT NULL,
        group_size BIGINT NULL,
        snapshot_at DATETIME(6) NULL,
        KEY ccd_snapshot_identity_area (generation_id, operation_area, status)
    ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC""",
    f"""CREATE TABLE IF NOT EXISTS `{QUALITY_TABLE}` (
        generation_id CHAR(32) NOT NULL,
        source VARCHAR(255) NOT NULL,
        classification VARCHAR(32) NOT NULL,
        environment VARCHAR(32) NOT NULL,
        include_in_dashboard TINYINT(1) NOT NULL,
        source_row_count BIGINT UNSIGNED NOT NULL,
        populated_source TINYINT(1) NOT NULL,
        service_metadata_coverage_pct DECIMAL(8,2) NOT NULL,
        service_start_coverage_pct DECIMAL(8,2) NOT NULL,
        growth_readiness VARCHAR(255) NOT NULL,
        sex_mapping_ready TINYINT(1) NOT NULL,
        placeholder_sex_rows BIGINT UNSIGNED NOT NULL,
        stored_sex_rows BIGINT UNSIGNED NOT NULL,
        dob_mapping_ready TINYINT(1) NOT NULL,
        dob_key_mapping_ready TINYINT(1) NOT NULL,
        dob_populated_rows BIGINT UNSIGNED NOT NULL,
        usable_dob_rows BIGINT UNSIGNED NOT NULL,
        verified_dob_rows BIGINT UNSIGNED NOT NULL,
        plausible_unverified_dob_rows BIGINT UNSIGNED NOT NULL,
        invalid_dob_rows BIGINT UNSIGNED NOT NULL,
        birthday_mapping_defect_rows BIGINT UNSIGNED NOT NULL,
        residential_district_mapping_ready TINYINT(1) NOT NULL,
        postal_district_mapping_ready TINYINT(1) NOT NULL,
        address_inference_ready TINYINT(1) NOT NULL,
        valid_district_rows BIGINT UNSIGNED NOT NULL,
        inferred_district_rows BIGINT UNSIGNED NOT NULL,
        ambiguous_address_rows BIGINT UNSIGNED NOT NULL,
        unmapped_location_rows BIGINT UNSIGNED NOT NULL,
        missing_district_rows BIGINT UNSIGNED NOT NULL,
        service_available_from DATE NULL,
        latest_ccd_modified DATETIME(6) NULL,
        KEY ccd_snapshot_quality_filter (generation_id, environment, source),
        KEY ccd_snapshot_quality_class (generation_id, classification)
    ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC""",
    f"""CREATE TABLE IF NOT EXISTS `{DISTRICT_TABLE}` (
        generation_id CHAR(32) NOT NULL,
        environment VARCHAR(32) NOT NULL,
        service VARCHAR(255) NOT NULL,
        source VARCHAR(255) NOT NULL,
        identity_state VARCHAR(32) NOT NULL,
        district_code VARCHAR(16) NOT NULL,
        district_basis VARCHAR(64) NOT NULL,
        district VARCHAR(255) NOT NULL,
        area VARCHAR(255) NOT NULL,
        district_polygon LONGTEXT NOT NULL,
        logical_person_count BIGINT UNSIGNED NOT NULL,
        KEY ccd_snapshot_district_filter (generation_id, environment, service, source),
        KEY ccd_snapshot_district_code (generation_id, district_code)
    ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC""",
    f"""CREATE TABLE IF NOT EXISTS `{FAST_IDENTITY_TABLE}` (
        metric_key VARCHAR(255) NOT NULL,
        operation_area VARCHAR(64) NOT NULL,
        status VARCHAR(255) NOT NULL,
        detail VARCHAR(255) NOT NULL,
        metric_count BIGINT UNSIGNED NOT NULL,
        group_size BIGINT NULL,
        snapshot_at DATETIME(6) NULL,
        modified DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
            ON UPDATE CURRENT_TIMESTAMP(6),
        PRIMARY KEY (metric_key),
        KEY ccd_fast_identity_area (operation_area, status)
    ) ENGINE=InnoDB ROW_FORMAT=DYNAMIC""",
)


def _now() -> Any:
    return frappe.utils.now_datetime()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _require_manager() -> None:
    user = str(getattr(getattr(frappe, "session", None), "user", "") or "")
    if user == "Administrator":
        return
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def schema_ready() -> bool:
    rows = frappe.db.sql(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND table_name=%s",
        (STATE_TABLE,),
    )
    return bool(rows and int(rows[0][0]))


def _index_exists() -> bool:
    rows = frappe.db.sql(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s",
        ("tabCCD Unified Person Membership", MEMBERSHIP_INDEX),
    )
    return bool(rows and int(rows[0][0]))


def _ensure_snapshot_datetime_precision() -> list[str]:
    """Keep source ``modified`` microseconds during idempotent schema upgrades."""
    altered: list[str] = []
    for table, column in (
        (PRESENCE_TABLE, "latest_ccd_modified"),
        (IDENTITY_TABLE, "snapshot_at"),
        (QUALITY_TABLE, "latest_ccd_modified"),
    ):
        rows = frappe.db.sql(
            "SELECT DATETIME_PRECISION FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
            (table, column),
        )
        if rows and int(rows[0][0] or 0) != 6:
            alter_prefix = (
                f"ALTER TABLE `{table}` "
                f"MODIFY COLUMN `{column}` DATETIME(6) NULL, "
            )
            mode = "inplace"
            try:
                frappe.db.sql_ddl(alter_prefix + "ALGORITHM=INPLACE, LOCK=NONE")
            except OperationalError as exc:
                if not exc.args or int(exc.args[0]) != 1846:
                    raise
                # MariaDB cannot widen DATETIME precision in place.  These are
                # derived reporting tables, so a read-compatible table copy is
                # safe; source CCD tables and their online index are untouched.
                frappe.db.sql_ddl(alter_prefix + "ALGORITHM=COPY, LOCK=SHARED")
                mode = "copy-shared"
            altered.append(f"{table}.{column}:{mode}")
    return altered


def _ensure_snapshot_indexes() -> list[str]:
    """Install covering indexes for the preview's concurrent chart shapes."""
    created: list[str] = []
    for table, definitions in SNAPSHOT_INDEXES.items():
        created_for_table = False
        for index_name, columns in definitions.items():
            rows = frappe.db.sql(
                "SELECT COUNT(*) FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
                (table, index_name),
            )
            if rows and int(rows[0][0] or 0):
                continue
            quoted_columns = ",".join(f"`{column}`" for column in columns)
            frappe.db.sql_ddl(
                f"ALTER TABLE `{table}` ADD INDEX `{index_name}` ({quoted_columns}), "
                "ALGORITHM=INPLACE, LOCK=NONE"
            )
            created.append(f"{table}.{index_name}")
            created_for_table = True
        if created_for_table:
            frappe.db.sql(f"ANALYZE TABLE `{table}`")
    return created


def _registration_mapping_digest() -> dict[str, Any]:
    if not frappe.db.table_exists("CCD Registration"):
        return {"count": 0, "sha256": _sha256([]), "revisions": []}
    from db_connector.api_fuzzy_evaluation import _registration_mapping_fingerprint

    rows = frappe.get_all(
        "CCD Registration",
        fields=["name", "amended_from", "docstatus", "modified"],
        order_by="name asc",
        limit_page_length=100_000,
    )
    values = []
    for row in rows:
        registration = frappe.get_doc("CCD Registration", row.name)
        values.append(
            {
                "name": str(row.name),
                "amended_from": str(row.amended_from or ""),
                "docstatus": int(row.docstatus or 0),
                "modified": str(row.modified or ""),
                "mapping_fingerprint": _registration_mapping_fingerprint(registration),
            }
        )
    return {
        "count": len(values),
        "sha256": _sha256(values),
        "revisions": values,
    }


def provenance_snapshot() -> dict[str, Any]:
    """Return non-client governance fingerprints used around online DDL."""
    policy_audits = []
    if frappe.db.table_exists("CCD Matching Policy"):
        from db_connector.api_fuzzy_evaluation import _policy_provenance_audit

        for policy in frappe.get_all(
            "CCD Matching Policy", pluck="name", order_by="name asc",
            limit_page_length=10_000,
        ):
            audit = _policy_provenance_audit(policy)
            policy_audits.append(
                {
                    "policy": str(policy),
                    "valid": bool(audit.get("valid")),
                    "issue_count": len(audit.get("issues") or []),
                    "audit_sha256": _sha256(audit),
                    "audit": audit,
                }
            )
    evaluation_counts = []
    if frappe.db.table_exists("CCD Match Evaluation Run"):
        evaluation_counts = [
            {"status": str(status or ""), "count": int(count)}
            for status, count in frappe.db.sql(
                "SELECT status, COUNT(*) FROM `tabCCD Match Evaluation Run` "
                "GROUP BY status ORDER BY status"
            )
        ]
    result = {
        "policy_provenance": policy_audits,
        "registrations": _registration_mapping_digest(),
        "evaluation_run_status_counts": evaluation_counts,
    }
    return {**result, "sha256": _sha256(result)}


def install_covering_index_online() -> dict[str, Any]:
    """Install the membership covering index, requiring online InnoDB DDL."""
    _require_manager()
    if not schema_ready():
        raise RuntimeError("Install the reporting schema before the covering index")
    before = provenance_snapshot()
    created = not _index_exists()
    if created:
        # LOCK=NONE is intentional: MariaDB must fail rather than silently use
        # a blocking table-copy algorithm.
        frappe.db.sql_ddl(
            "ALTER TABLE `tabCCD Unified Person Membership` "
            f"ADD INDEX `{MEMBERSHIP_INDEX}` "
            "(`status`, `ccd_master`, `valid_to`, `unified_person`), "
            "ALGORITHM=INPLACE, LOCK=NONE"
        )
    after = provenance_snapshot()
    if before != after:
        raise RuntimeError("Governance provenance changed during index installation")
    audit_id = uuid.uuid4().hex
    payload = {"before": before, "after": after, "index_created": created}
    frappe.db.sql(
        f"INSERT INTO `{AUDIT_TABLE}` "
        "(audit_id,migration_name,recorded_at,before_sha256,after_sha256,audit_json) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (
            audit_id, "membership-dashboard-current-v1", _now(),
            before["sha256"], after["sha256"], _canonical_json(payload),
        ),
    )
    frappe.db.commit()
    return {
        "index": MEMBERSHIP_INDEX,
        "created": created,
        "provenance_sha256": before["sha256"],
        "evaluation_run_required": False,
    }


def install_reporting_schema(install_index: int | str = 0) -> dict[str, Any]:
    """Create the versioned tables; the online index remains opt-in."""
    _require_manager()
    for statement in DDL:
        frappe.db.sql_ddl(statement)
    altered_datetime_columns = _ensure_snapshot_datetime_precision()
    created_snapshot_indexes = _ensure_snapshot_indexes()
    frappe.db.sql(
        f"INSERT IGNORE INTO `{STATE_TABLE}` "
        "(state_key,schema_version,dirty_version,built_dirty_version,last_dirty_at,refresh_status) "
        "VALUES (%s,%s,1,0,%s,'Dirty')",
        (STATE_KEY, SCHEMA_VERSION, _now()),
    )
    frappe.db.commit()
    fast_identity = refresh_fast_identity_metrics()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tables": list(DATA_TABLES),
        "state_table": STATE_TABLE,
        "index_present": _index_exists(),
        "altered_datetime_columns": altered_datetime_columns,
        "created_snapshot_indexes": created_snapshot_indexes,
        "fast_identity": fast_identity,
    }
    if str(install_index).lower() in {"1", "true", "yes"}:
        result["index"] = install_covering_index_online()
    return result


def _district_geometry_rows() -> str:
    payload = json.loads(GEOMETRY_PATH.read_text(encoding="utf-8"))
    rows = []
    for feature in payload["features"]:
        code = str(feature["properties"]["district_code"]).replace("'", "''")
        coordinates = json.dumps(
            feature["geometry"]["coordinates"], separators=(",", ":")
        ).replace("'", "''")
        rows.append(
            f"SELECT '{code}' AS district_code, '{coordinates}' AS district_polygon"
        )
    if len(rows) != 18:
        raise RuntimeError("Hong Kong district geometry must contain 18 districts")
    return "\nUNION ALL\n".join(rows)


def _private_address_override_rows(path: Path = DEFAULT_PRIVATE_ADDRESS_OVERRIDES) -> str:
    if not path.exists():
        return "SELECT NULL AS address_hash, NULL AS district_code WHERE 0"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("overrides"), list):
        raise RuntimeError("Private address override file has an unsupported schema")
    rows = []
    seen = set()
    valid_districts = {
        "CW", "EST", "SOU", "WC", "KC", "KWT", "SSP", "WTS", "YTM",
        "IS", "KUI", "NOR", "SK", "ST", "TP", "TW", "TM", "YL",
    }
    for item in payload["overrides"]:
        address_hash = str(item.get("address_hash") or "").lower()
        district_code = str(item.get("district_code") or "").upper()
        if (
            len(address_hash) != 64
            or any(character not in "0123456789abcdef" for character in address_hash)
            or district_code not in valid_districts
            or address_hash in seen
        ):
            raise RuntimeError("Private address override contains invalid or duplicate data")
        seen.add(address_hash)
        rows.append(
            f"SELECT '{address_hash}' AS address_hash, "
            f"'{district_code}' AS district_code"
        )
    return "\nUNION ALL\n".join(rows) if rows else (
        "SELECT NULL AS address_hash, NULL AS district_code WHERE 0"
    )


def _source_sql(name: str) -> str:
    sql = (SQL_ROOT / f"{name}_v1.sql").read_text(encoding="utf-8").strip().rstrip(";")
    sql = sql.replace("/*__HK_DISTRICT_GEOMETRY_ROWS__*/", _district_geometry_rows())
    return sql.replace(
        "/*__PRIVATE_ADDRESS_OVERRIDE_ROWS__*/", _private_address_override_rows()
    )


def refresh_fast_identity_metrics() -> dict[str, Any]:
    """Refresh small aggregate identity controls without waiting for a full build.

    These rows contain no client or group identifiers. Plain MVCC SELECTs are
    deliberately separated from the reporting-table write so the fast path
    cannot take source-row locks during interactive materialization.
    """
    table_rows = frappe.db.sql(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND table_name=%s",
        (FAST_IDENTITY_TABLE,),
    )
    if not table_rows or not int(table_rows[0][0] or 0):
        return {"status": "Schema Missing", "row_count": 0}
    lock_rows = frappe.db.sql("SELECT GET_LOCK(%s,0)", (FAST_IDENTITY_LOCK_NAME,))
    if not lock_rows or int(lock_rows[0][0] or 0) != 1:
        return {"status": "Deferred", "row_count": 0}
    try:
        source_rows = frappe.db.sql(
            """
        SELECT
            'Governed Groups' AS operation_area,
            expected.status,
            'Group count' AS detail,
            COUNT(g.name) AS metric_count,
            NULL AS group_size,
            MAX(g.modified) AS snapshot_at
        FROM (
            SELECT 'Active' AS status
            UNION ALL SELECT 'Needs Revalidation'
        ) expected
        LEFT JOIN `tabCCD Identity Group` g ON g.status=expected.status
        GROUP BY expected.status

        UNION ALL

        SELECT
            'Group Size Distribution',
            g.status,
            CONCAT(g.active_member_count, ' members'),
            COUNT(*),
            g.active_member_count,
            MAX(g.modified)
        FROM `tabCCD Identity Group` g
        WHERE g.status IN ('Active','Needs Revalidation')
        GROUP BY g.status,g.active_member_count

        UNION ALL

        SELECT
            'Memberships',
            membership.status,
            'Membership rows',
            COUNT(*),
            NULL,
            MAX(membership.modified)
        FROM `tabCCD Identity Membership` membership
        GROUP BY membership.status
            """
        )
        values = []
        for operation_area, status, detail, metric_count, group_size, snapshot_at in source_rows:
            metric_key = _sha256(
                [str(operation_area or ""), str(status or ""), str(detail or ""), group_size]
            )
            values.append(
                (
                    metric_key,
                    operation_area,
                    status,
                    detail,
                    int(metric_count or 0),
                    group_size,
                    snapshot_at,
                )
            )
        frappe.db.sql(f"DELETE FROM `{FAST_IDENTITY_TABLE}`")
        if values:
            frappe.db.bulk_insert(
                FAST_IDENTITY_TABLE.removeprefix("tab"),
                list(FAST_IDENTITY_COLUMNS),
                values,
                chunk_size=500,
            )
        frappe.db.commit()
        return {"status": "Ready", "row_count": len(values), "refreshed_at": _now()}
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (FAST_IDENTITY_LOCK_NAME,))
        except Exception:
            pass


def _prepare_build_stage_tables(reader: Any) -> None:
    """Create indexed staging tables on the dedicated source reader."""
    cursor = reader.cursor()
    for table in (CURRENT_MEMBERSHIP_STAGE, CURRENT_UNIFIED_STAGE):
        cursor.execute(f"DROP TEMPORARY TABLE IF EXISTS `{table}`")
    cursor.execute(
        f"CREATE TEMPORARY TABLE `{CURRENT_MEMBERSHIP_STAGE}` ("
        "ccd_master VARCHAR(140) NOT NULL,identity_group VARCHAR(140) NULL,"
        "identity_state VARCHAR(32) NULL,PRIMARY KEY (ccd_master)"
        ") ENGINE=InnoDB"
    )
    cursor.execute(
        f"CREATE TEMPORARY TABLE `{CURRENT_UNIFIED_STAGE}` ("
        "ccd_master VARCHAR(140) NOT NULL,unified_person VARCHAR(140) NULL,"
        "PRIMARY KEY (ccd_master)"
        ") ENGINE=InnoDB"
    )
    cursor.close()
    reader.commit()


def _copy_rows_to_reader_stage(
    reader: Any,
    select_sql: str,
    insert_sql: str,
) -> None:
    """Copy a consistent read into a temporary table without source row locks.

    MariaDB takes shared locks for ``INSERT ... SELECT`` even when the target
    is temporary and the transaction is read-only. A plain SELECT is an MVCC
    consistent read, so buffer its result and insert values separately.
    """
    cursor = reader.cursor()
    cursor.execute(select_sql)
    rows = cursor.fetchall()
    for start in range(0, len(rows), SNAPSHOT_COPY_CHUNK_SIZE):
        cursor.executemany(insert_sql, rows[start : start + SNAPSHOT_COPY_CHUNK_SIZE])
    cursor.close()


def _populate_build_stages(reader: Any) -> None:
    """Materialize repeated projections inside one non-locking source snapshot."""
    _copy_rows_to_reader_stage(
        reader,
        "SELECT im.ccd_master,MAX(im.identity_group),MAX(ig.status) "
        "FROM `tabCCD Identity Membership` im "
        "JOIN `tabCCD Identity Group` ig ON ig.name=im.identity_group "
        "AND ig.status IN ('Active','Needs Revalidation') "
        "WHERE im.status='Active' "
        "AND (im.valid_to IS NULL OR im.valid_to>CURRENT_TIMESTAMP) "
        "GROUP BY im.ccd_master",
        f"INSERT INTO `{CURRENT_MEMBERSHIP_STAGE}` "
        "(ccd_master,identity_group,identity_state) VALUES (%s,%s,%s)",
    )
    _copy_rows_to_reader_stage(
        reader,
        "SELECT membership.ccd_master,MAX(membership.unified_person) "
        "FROM `tabCCD Unified Person Membership` membership "
        "JOIN `tabCCD Unified Person` person "
        "ON person.name=membership.unified_person AND person.status='Active' "
        "WHERE membership.status='Active' "
        "AND (membership.valid_to IS NULL OR membership.valid_to>CURRENT_TIMESTAMP) "
        "GROUP BY membership.ccd_master",
        f"INSERT INTO `{CURRENT_UNIFIED_STAGE}` "
        "(ccd_master,unified_person) VALUES (%s,%s)",
    )


def _open_snapshot_reader() -> Any:
    """Open one exact, MVCC source snapshot that never writes source tables."""
    reader = frappe.db.create_connection()
    try:
        _prepare_build_stage_tables(reader)
        cursor = reader.cursor()
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        cursor.close()
        _populate_build_stages(reader)
        return reader
    except Exception:
        reader.rollback()
        reader.close()
        raise


def _optimized_source_sql(name: str) -> str:
    """Use indexed staging and equivalent bounded district pattern matching."""
    sql = _source_sql(name)
    if name == "person_service_presence":
        sql, identity_replacements = re.subn(
            r"current_membership AS \(.*?\),\ncurrent_unified_membership AS \(",
            f"current_membership AS (SELECT * FROM `{CURRENT_MEMBERSHIP_STAGE}`),\n"
            "current_unified_membership AS (",
            sql,
            count=1,
            flags=re.S,
        )
        sql, unified_replacements = re.subn(
            r"current_unified_membership AS \(.*?\),\nraw_record_values AS \(",
            f"current_unified_membership AS (SELECT * FROM `{CURRENT_UNIFIED_STAGE}`),\n"
            "raw_record_values AS (",
            sql,
            count=1,
            flags=re.S,
        )
        if identity_replacements != 1 or unified_replacements != 1:
            raise RuntimeError("Presence staging rewrite did not match its SQL contract")
    elif name == "service_overlap":
        sql, replacements = re.subn(
            r"current_membership AS \(.*?\),\npresence AS \(",
            f"current_membership AS (SELECT ccd_master,identity_group "
            f"FROM `{CURRENT_MEMBERSHIP_STAGE}`),\npresence AS (",
            sql,
            count=1,
            flags=re.S,
        )
        if replacements != 1:
            raise RuntimeError("Overlap staging rewrite did not match its SQL contract")
    elif name == "district_map":
        sql, replacements = re.subn(
            r"current_membership AS \(.*?\),\ngroup_location_rollup AS \(",
            f"current_membership AS (SELECT ccd_master,identity_group,identity_state "
            f"FROM `{CURRENT_MEMBERSHIP_STAGE}`),\ngroup_location_rollup AS (",
            sql,
            count=1,
            flags=re.S,
        )
        if replacements != 1:
            raise RuntimeError("District staging rewrite did not match its SQL contract")
    if name in {"data_quality", "district_map"}:
        sql, replacements = re.subn(
            r"address_district_patterns AS \(.*?\),\nprivate_address_overrides AS \(",
            "address_district_patterns AS (\n"
            "    SELECT district_code, address_tokens AS address_pattern\n"
            "    FROM address_district_pattern_sets\n"
            "),\nprivate_address_overrides AS (",
            sql,
            count=1,
            flags=re.S,
        )
        if replacements != 1:
            raise RuntimeError("Location-pattern rewrite did not match its SQL contract")
        sql = sql.replace(
            "LOCATE(adp.address_token, ac.normalized_address) > 0",
            "ac.normalized_address REGEXP adp.address_pattern",
        )
    return sql


def _generation_selects() -> tuple[tuple[str, tuple[str, ...], str], ...]:
    """Return aggregate-safe source SELECTs executed by the snapshot reader."""
    presence_sql = _optimized_source_sql("person_service_presence")
    overlap_sql = _optimized_source_sql("service_overlap")
    overlap_dimensions = TABLE_COLUMNS[OVERLAP_TABLE][:-1]
    select_dimensions = ",".join(f"source_rows.`{column}`" for column in overlap_dimensions)
    group_dimensions = ",".join(f"source_rows.`{column}`" for column in overlap_dimensions)
    identity_sql = _source_sql("identity_operations")
    quality_sql = _optimized_source_sql("data_quality")
    district_sql = _optimized_source_sql("district_map")
    district_dimensions = TABLE_COLUMNS[DISTRICT_TABLE][:-1]
    district_select_dimensions = ",".join(
        f"source_rows.`{column}`" for column in district_dimensions
    )
    district_group_dimensions = ",".join(
        f"source_rows.`{column}`" for column in district_dimensions
    )
    return (
        (
            PRESENCE_TABLE,
            TABLE_COLUMNS[PRESENCE_TABLE],
            f"SELECT source_rows.* FROM ({presence_sql}) source_rows",
        ),
        (
            OVERLAP_TABLE,
            TABLE_COLUMNS[OVERLAP_TABLE],
            f"SELECT {select_dimensions},"
            "COUNT(DISTINCT source_rows.logical_person_key) "
            f"FROM ({overlap_sql}) source_rows GROUP BY {group_dimensions}",
        ),
        (
            IDENTITY_TABLE,
            TABLE_COLUMNS[IDENTITY_TABLE],
            f"SELECT source_rows.* FROM ({identity_sql}) source_rows",
        ),
        (
            QUALITY_TABLE,
            TABLE_COLUMNS[QUALITY_TABLE],
            f"SELECT source_rows.* FROM ({quality_sql}) source_rows",
        ),
        (
            DISTRICT_TABLE,
            TABLE_COLUMNS[DISTRICT_TABLE],
            f"SELECT {district_select_dimensions},"
            "COUNT(DISTINCT source_rows.logical_person_key) "
            f"FROM ({district_sql}) source_rows "
            f"GROUP BY {district_group_dimensions}",
        ),
    )


def _insert_generation(generation_id: str, reader: Any) -> None:
    """Stream one exact source snapshot into off-screen reporting rows."""
    for table, columns, select_sql in _generation_selects():
        cursor = reader.cursor(SSCursor)
        try:
            cursor.execute(select_sql)
            while rows := cursor.fetchmany(SNAPSHOT_COPY_CHUNK_SIZE):
                frappe.db.bulk_insert(
                    table.removeprefix("tab"),
                    ["generation_id", *columns],
                    tuple((generation_id, *row) for row in rows),
                    chunk_size=SNAPSHOT_COPY_CHUNK_SIZE,
                )
        finally:
            cursor.close()


def _generation_metrics(generation_id: str) -> tuple[dict[str, int], dict[str, str]]:
    counts: dict[str, int] = {}
    checksums: dict[str, str] = {}
    for table in DATA_TABLES:
        columns = TABLE_COLUMNS[table]
        row_expression = "CONCAT_WS(CHAR(31)," + ",".join(
            f"COALESCE(CAST(`{column}` AS CHAR),'∅')" for column in columns
        ) + ")"
        rows = frappe.db.sql(
            f"SELECT COUNT(*),COALESCE(BIT_XOR(CRC32({row_expression})),0),"
            f"COALESCE(SUM(CRC32({row_expression})),0) FROM `{table}` "
            "WHERE generation_id=%s",
            (generation_id,),
        )
        count, xor_checksum, sum_checksum = rows[0]
        counts[table] = int(count)
        checksums[table] = hashlib.sha256(
            f"{int(count)}:{int(xor_checksum)}:{int(sum_checksum)}".encode()
        ).hexdigest()
    invalid_keys = frappe.db.sql(
        f"SELECT COUNT(*) FROM `{PRESENCE_TABLE}` WHERE generation_id=%s "
        "AND (logical_person_key IS NULL OR logical_person_key NOT REGEXP '^[0-9a-f]{64}$')",
        (generation_id,),
    )[0][0]
    if int(invalid_keys):
        raise RuntimeError("Snapshot contains an invalid logical-person hash")
    return counts, checksums


def _state_for_update() -> dict[str, Any]:
    rows = frappe.db.sql(
        f"SELECT active_generation,previous_generation,dirty_version,built_dirty_version "
        f"FROM `{STATE_TABLE}` WHERE state_key=%s FOR UPDATE",
        (STATE_KEY,),
        as_dict=True,
    )
    if not rows:
        raise RuntimeError("CCD snapshot state is missing")
    return dict(rows[0])


def _set_generation_lifecycle(active: str, previous: str | None) -> None:
    frappe.db.sql(
        f"UPDATE `{GENERATION_TABLE}` SET status='Superseded' "
        "WHERE status='Previous' AND generation_id<>%s",
        (previous or "",),
    )
    if previous:
        frappe.db.sql(
            f"UPDATE `{GENERATION_TABLE}` SET status='Previous' "
            "WHERE generation_id=%s AND status='Active'",
            (previous,),
        )
    frappe.db.sql(
        f"UPDATE `{GENERATION_TABLE}` SET status='Active' WHERE generation_id=%s",
        (active,),
    )


def _maintenance_should_yield() -> bool:
    rows = frappe.db.sql(
        f"SELECT dirty_version,built_dirty_version,refresh_status "
        f"FROM `{STATE_TABLE}` WHERE state_key=%s",
        (STATE_KEY,),
    )
    if not rows:
        return True
    dirty_version, built_version, status = rows[0]
    return bool(
        int(dirty_version) > int(built_version)
        or str(status or "") in {"Dirty", "Building"}
    )


def _cleanup_generations() -> dict[str, Any]:
    """Delete only obsolete generation keys in interruptible indexed chunks."""
    generation_rows = frappe.db.sql(
        f"SELECT generation_id,status FROM `{GENERATION_TABLE}` "
        "WHERE status IN ('Superseded','Failed') ORDER BY started_at",
    )
    deleted_rows = 0
    completed_generations: list[str] = []
    for generation_id, status in generation_rows:
        generation_id = str(generation_id)
        for table in DATA_TABLES:
            while True:
                if _maintenance_should_yield():
                    return {
                        "status": "Deferred",
                        "deleted_rows": deleted_rows,
                        "completed_generations": completed_generations,
                    }
                frappe.db.sql(
                    f"DELETE FROM `{table}` WHERE generation_id=%s "
                    f"LIMIT {SNAPSHOT_DELETE_CHUNK_SIZE}",
                    (generation_id,),
                )
                affected = int(frappe.db.sql("SELECT ROW_COUNT()")[0][0] or 0)
                frappe.db.commit()
                deleted_rows += affected
                if affected < SNAPSHOT_DELETE_CHUNK_SIZE:
                    break
        completed_generations.append(generation_id)
        if str(status) == "Superseded":
            frappe.db.sql(
                f"DELETE FROM `{GENERATION_TABLE}` WHERE generation_id=%s "
                "AND status='Superseded'",
                (generation_id,),
            )
            frappe.db.commit()
    # Retain a bounded failure audit after every failed generation's data rows
    # have been removed. Active, Previous, and Building metadata are untouched.
    frappe.db.sql(
        f"DELETE FROM `{GENERATION_TABLE}` WHERE status='Failed' AND generation_id NOT IN ("
        f"SELECT generation_id FROM (SELECT generation_id FROM `{GENERATION_TABLE}` "
        "WHERE status='Failed' ORDER BY started_at DESC LIMIT 20) retained_failures)"
    )
    frappe.db.commit()
    return {
        "status": "Complete",
        "deleted_rows": deleted_rows,
        "completed_generations": completed_generations,
    }


def _record_failure(generation_id: str, exc: Exception) -> None:
    error = f"{type(exc).__name__}: {str(exc)}"[:1000]
    try:
        frappe.db.rollback()
    except Exception:
        pass
    frappe.db.sql(
        f"UPDATE `{GENERATION_TABLE}` SET status='Failed',completed_at=%s,error_summary=%s "
        "WHERE generation_id=%s",
        (_now(), error, generation_id),
    )
    frappe.db.sql(
        f"UPDATE `{STATE_TABLE}` SET refresh_status='Failed',last_refresh_error=%s "
        "WHERE state_key=%s",
        (error, STATE_KEY),
    )
    frappe.db.commit()


def _lock_acquired() -> bool:
    rows = frappe.db.sql("SELECT GET_LOCK(%s,0)", (LOCK_NAME,))
    return bool(rows and int(rows[0][0] or 0) == 1)


def _release_lock() -> None:
    try:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
    except Exception:
        pass


def _warm_query(
    label: str,
    sql: str,
    generation_id: str,
    site: str,
    sites_path: str,
) -> tuple[str, float]:
    """Run one aggregate warmup on an isolated thread-local connection."""
    frappe.init(site=site, sites_path=sites_path)
    frappe.connect()
    try:
        started = time.monotonic()
        frappe.db.sql(sql, (generation_id,))
        return label, round(time.monotonic() - started, 4)
    finally:
        frappe.destroy()


def warm_snapshot_preview() -> dict[str, Any]:
    """Warm the active generation's bounded chart-index working set."""
    if not schema_ready():
        return {"status": "Schema Missing"}
    state_rows = frappe.db.sql(
        f"SELECT active_generation FROM `{STATE_TABLE}` WHERE state_key=%s",
        (STATE_KEY,),
    )
    generation_id = str(state_rows[0][0] or "") if state_rows else ""
    if not generation_id:
        return {"status": "No Active Generation"}
    lock_rows = frappe.db.sql("SELECT GET_LOCK(%s,0)", (WARM_LOCK_NAME,))
    if not lock_rows or int(lock_rows[0][0] or 0) != 1:
        return {"status": "Already Running"}
    try:
        table = f"`{PRESENCE_TABLE}`"
        predicate = "generation_id=%s AND environment='Production'"
        queries = (
            ("logical_clients", f"SELECT COUNT(DISTINCT logical_person_key) FROM {table} WHERE {predicate}"),
            ("source_rows", f"SELECT SUM(source_row_count) FROM {table} WHERE {predicate}"),
            ("populated_sources", f"SELECT COUNT(DISTINCT source) FROM {table} WHERE {predicate}"),
            ("service", f"SELECT service,COUNT(DISTINCT logical_person_key) FROM {table} WHERE {predicate} GROUP BY service"),
            ("growth", f"SELECT overall_growth_month,COUNT(DISTINCT logical_person_key) FROM {table} WHERE {predicate} AND overall_growth_month IS NOT NULL GROUP BY overall_growth_month"),
            ("service_growth", f"SELECT service_growth_month,service,COUNT(DISTINCT logical_person_key) FROM {table} WHERE {predicate} AND service_growth_month IS NOT NULL GROUP BY service_growth_month,service"),
            ("demographics", f"SELECT age_band,sex_category,COUNT(DISTINCT logical_person_key) FROM {table} WHERE {predicate} GROUP BY age_band,sex_category"),
            (
                "identity_summary",
                f"SELECT operation_area,status,SUM(metric_count) FROM `{IDENTITY_TABLE}` "
                "WHERE generation_id=%s GROUP BY operation_area,status",
            ),
            (
                "district_summary",
                f"SELECT district_polygon,district,district_basis,SUM(logical_person_count) "
                f"FROM `{DISTRICT_TABLE}` WHERE generation_id=%s "
                "AND environment='Production' GROUP BY district_polygon,district,district_basis",
            ),
        )
        site = str(frappe.local.site)
        sites_path = str(frappe.local.sites_path)
        timings: dict[str, float] = {}
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(queries)) as executor:
            futures = {
                executor.submit(
                    _warm_query, label, sql, generation_id, site, sites_path
                ): label
                for label, sql in queries
            }
            for future in as_completed(futures):
                label, seconds = future.result()
                timings[label] = seconds
        return {
            "status": "Warmed",
            "generation_id": generation_id,
            "seconds": round(time.monotonic() - started, 4),
            "queries": timings,
        }
    finally:
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (WARM_LOCK_NAME,))
        except Exception:
            pass


def enqueue_refresh(dirty_version: int | None = None) -> str | None:
    if not schema_ready():
        return None
    if dirty_version is None:
        rows = frappe.db.sql(
            f"SELECT dirty_version FROM `{STATE_TABLE}` WHERE state_key=%s",
            (STATE_KEY,),
        )
        if not rows:
            return None
        dirty_version = int(rows[0][0])
    job_id = f"{JOB_PREFIX}-{int(dirty_version)}"
    from frappe.utils.background_jobs import is_job_enqueued

    if is_job_enqueued(job_id):
        return job_id
    frappe.enqueue(
        "db_connector.ccd_dashboard_snapshot.refresh_snapshot",
        queue="long",
        timeout=3600,
        enqueue_after_commit=False,
        job_id=job_id,
        at_front=True,
    )
    frappe.db.sql(
        f"UPDATE `{STATE_TABLE}` SET last_job_id=%s WHERE state_key=%s",
        (job_id, STATE_KEY),
    )
    frappe.db.commit()
    return job_id


def enqueue_snapshot_maintenance(generation_id: str) -> str | None:
    if not schema_ready():
        return None
    job_id = f"{MAINTENANCE_JOB_PREFIX}-{generation_id}"
    from frappe.utils.background_jobs import is_job_enqueued

    if is_job_enqueued(job_id):
        return job_id
    frappe.enqueue(
        "db_connector.ccd_dashboard_snapshot.run_snapshot_maintenance",
        queue="long",
        timeout=3600,
        enqueue_after_commit=False,
        job_id=job_id,
        generation_id=generation_id,
    )
    return job_id


def _mark_dirty_after_commit() -> None:
    if not schema_ready():
        return
    reasons = sorted(
        set(getattr(frappe.flags, "ccd_dashboard_snapshot_dirty_reasons", set()) or set())
    )
    frappe.db.sql(
        f"UPDATE `{STATE_TABLE}` SET dirty_version=dirty_version+1,last_dirty_at=%s,"
        "refresh_status='Dirty',last_refresh_error=NULL WHERE state_key=%s",
        (_now(), STATE_KEY),
    )
    rows = frappe.db.sql(
        f"SELECT dirty_version FROM `{STATE_TABLE}` WHERE state_key=%s",
        (STATE_KEY,),
    )
    frappe.db.commit()
    try:
        refresh_fast_identity_metrics()
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "CCD dashboard fast identity refresh failed"
        )
    if rows:
        try:
            enqueue_refresh(int(rows[0][0]))
        except Exception:
            # The committed dirty state is authoritative.  The one-minute
            # safety check will retry a missed or duplicate queue request.
            frappe.log_error(
                frappe.get_traceback(), "CCD dashboard snapshot enqueue failed"
            )
    if reasons:
        frappe.logger("ccd_dashboard_snapshot").info(
            "CCD dashboard snapshot marked dirty: %s", ", ".join(reasons)
        )


def mark_dirty_after_commit(doc: Any = None, method: str | None = None, reason: str | None = None) -> None:
    """Coalesce all mutations in one transaction into one dirty-version bump."""
    marker_reason = str(reason or method or getattr(doc, "doctype", "ccd-change"))
    if getattr(frappe.flags, "ccd_dashboard_snapshot_marker_registered", False):
        reasons = getattr(
            frappe.flags, "ccd_dashboard_snapshot_dirty_reasons", set()
        )
        reasons.add(marker_reason)
        frappe.flags.ccd_dashboard_snapshot_dirty_reasons = reasons
        return
    if not schema_ready():
        return
    reasons = getattr(frappe.flags, "ccd_dashboard_snapshot_dirty_reasons", None)
    if reasons is None:
        reasons = set()
        frappe.flags.ccd_dashboard_snapshot_dirty_reasons = reasons
    reasons.add(marker_reason)
    frappe.flags.ccd_dashboard_snapshot_marker_registered = True
    frappe.db.after_commit.add(_mark_dirty_after_commit)


def finish_agent_sync_with_snapshot(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """HTTP override that marks the completed agent ingestion transaction."""
    from db_connector.api_agent_sync import finish_sync

    result = finish_sync(*args, **kwargs)
    mark_dirty_after_commit(reason="agent-bulk-sync-completed")
    return result


def run_snapshot_maintenance(generation_id: str | None = None) -> dict[str, Any]:
    """Clean and warm outside the activation lock, yielding to fresh data."""
    if not schema_ready():
        return {"status": "Schema Missing"}
    lock_rows = frappe.db.sql("SELECT GET_LOCK(%s,0)", (MAINTENANCE_LOCK_NAME,))
    if not lock_rows or int(lock_rows[0][0] or 0) != 1:
        return {"status": "Already Running"}
    try:
        if _maintenance_should_yield():
            return {"status": "Deferred", "reason": "Refresh pending"}
        cleanup = _cleanup_generations()
        if cleanup["status"] != "Complete" or _maintenance_should_yield():
            return {"status": "Deferred", "cleanup": cleanup}
        rows = frappe.db.sql(
            f"SELECT active_generation FROM `{STATE_TABLE}` WHERE state_key=%s",
            (STATE_KEY,),
        )
        active_generation = str(rows[0][0] or "") if rows else ""
        warmup = None
        if active_generation and (
            not generation_id or active_generation == str(generation_id)
        ):
            warmup = warm_snapshot_preview()
        return {"status": "Complete", "cleanup": cleanup, "warmup": warmup}
    finally:
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (MAINTENANCE_LOCK_NAME,))
        except Exception:
            pass


def refresh_snapshot(force: int | str = 0) -> dict[str, Any]:
    """Build, validate, and atomically activate one reporting generation."""
    if not schema_ready():
        return {"status": "Schema Missing"}
    if not _lock_acquired():
        return {"status": "Already Running"}
    lock_held = True
    generation_id = uuid.uuid4().hex
    reader = None
    try:
        rows = frappe.db.sql(
            f"SELECT dirty_version,built_dirty_version FROM `{STATE_TABLE}` WHERE state_key=%s",
            (STATE_KEY,),
        )
        requested_version, built_version = (int(value) for value in rows[0])
        if requested_version <= built_version and str(force).lower() not in {"1", "true", "yes"}:
            return {"status": "Current", "dirty_version": requested_version}
        started_at = _now()
        frappe.db.sql(
            f"INSERT INTO `{GENERATION_TABLE}` "
            "(generation_id,schema_version,requested_dirty_version,status,started_at) "
            "VALUES (%s,%s,%s,'Building',%s)",
            (generation_id, SCHEMA_VERSION, requested_version, started_at),
        )
        frappe.db.sql(
            f"UPDATE `{STATE_TABLE}` SET refresh_status='Building',refresh_started_at=%s,"
            "last_refresh_error=NULL WHERE state_key=%s",
            (started_at, STATE_KEY),
        )
        frappe.db.commit()

        # Read operational data on a dedicated read-only MVCC connection and
        # stream it to the normal writer connection. MariaDB's INSERT...SELECT
        # otherwise takes shared source-row locks for the entire build, which
        # can block interactive identity materialization until request timeout.
        reader = _open_snapshot_reader()
        _insert_generation(generation_id, reader)
        reader.rollback()
        reader.close()
        reader = None
        counts, checksums = _generation_metrics(generation_id)
        state = _state_for_update()
        previous = state.get("active_generation")
        current_dirty_version = int(state["dirty_version"])
        completed_at = _now()
        refresh_status = (
            "Dirty" if current_dirty_version > requested_version else "Ready"
        )
        frappe.db.sql(
            f"UPDATE `{GENERATION_TABLE}` SET status='Active',completed_at=%s,"
            "row_counts_json=%s,checksums_json=%s,error_summary=NULL "
            "WHERE generation_id=%s",
            (
                completed_at, _canonical_json(counts), _canonical_json(checksums),
                generation_id,
            ),
        )
        frappe.db.sql(
            f"UPDATE `{STATE_TABLE}` SET previous_generation=%s,active_generation=%s,"
            "built_dirty_version=%s,active_generation_at=%s,refresh_status=%s,"
            "last_refresh_error=NULL WHERE state_key=%s",
            (
                previous, generation_id, requested_version, completed_at,
                refresh_status, STATE_KEY,
            ),
        )
        _set_generation_lifecycle(
            generation_id, str(previous) if previous else None
        )
        frappe.db.commit()

        # Visibility is complete at this commit. Release the activation lock
        # before queueing follow-up work; retention and warming must never
        # serialize a newer dirty version behind an already-visible snapshot.
        _release_lock()
        lock_held = False
        follow_up_job = None
        if current_dirty_version > requested_version:
            try:
                follow_up_job = enqueue_refresh(current_dirty_version)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "CCD dashboard snapshot follow-up enqueue failed",
                )
        maintenance_job = None
        try:
            maintenance_job = enqueue_snapshot_maintenance(generation_id)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), "CCD dashboard snapshot maintenance enqueue failed"
            )
        return {
            "status": refresh_status,
            "generation_id": generation_id,
            "previous_generation": previous,
            "dirty_version": current_dirty_version,
            "built_dirty_version": requested_version,
            "row_counts": counts,
            "checksums": checksums,
            "cleanup_error": None,
            "follow_up_job": follow_up_job,
            "maintenance_job": maintenance_job,
            "warmup": {"status": "Deferred to maintenance"},
        }
    except Exception as exc:
        _record_failure(generation_id, exc)
        frappe.log_error(frappe.get_traceback(), "CCD dashboard snapshot refresh failed")
        raise
    finally:
        if reader is not None:
            try:
                reader.rollback()
            finally:
                reader.close()
        if lock_held:
            _release_lock()


def rollback_to_previous_generation() -> dict[str, Any]:
    """Atomically swap the active and previous successful generations."""
    _require_manager()
    if not schema_ready():
        return {"status": "Schema Missing"}
    if not _lock_acquired():
        return {"status": "Already Running"}
    try:
        state = _state_for_update()
        active = str(state.get("active_generation") or "")
        previous = str(state.get("previous_generation") or "")
        if not active or not previous:
            frappe.db.rollback()
            raise RuntimeError("No previous successful snapshot generation is available")
        rows = frappe.db.sql(
            f"SELECT requested_dirty_version,completed_at FROM `{GENERATION_TABLE}` "
            "WHERE generation_id=%s AND status='Previous' FOR UPDATE",
            (previous,),
        )
        if not rows:
            frappe.db.rollback()
            raise RuntimeError("Previous snapshot generation is not valid")
        previous_version = int(rows[0][0])
        previous_completed_at = rows[0][1]
        dirty_version = int(state["dirty_version"])
        status = "Dirty" if dirty_version > previous_version else "Ready"
        frappe.db.sql(
            f"UPDATE `{GENERATION_TABLE}` SET status='Previous' WHERE generation_id=%s",
            (active,),
        )
        frappe.db.sql(
            f"UPDATE `{GENERATION_TABLE}` SET status='Active' WHERE generation_id=%s",
            (previous,),
        )
        frappe.db.sql(
            f"UPDATE `{STATE_TABLE}` SET active_generation=%s,previous_generation=%s,"
            "built_dirty_version=%s,active_generation_at=%s,refresh_status=%s,"
            "last_refresh_error=NULL WHERE state_key=%s",
            (
                previous, active, previous_version, previous_completed_at,
                status, STATE_KEY,
            ),
        )
        frappe.db.commit()
        job_id = None
        if status == "Dirty":
            try:
                job_id = enqueue_refresh(dirty_version)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "CCD dashboard rollback follow-up enqueue failed",
                )
        return {
            "status": status,
            "active_generation": previous,
            "previous_generation": active,
            "follow_up_job": job_id,
        }
    finally:
        _release_lock()


def run_snapshot_safety_check() -> dict[str, Any]:
    """One-minute fallback for missed dirty events and stalled refresh jobs."""
    if not schema_ready():
        return {"status": "Schema Missing"}
    rows = frappe.db.sql(
        f"SELECT state.active_generation,state.dirty_version,state.built_dirty_version,state.refresh_status,"
        "state.refresh_started_at,generation.started_at AS generation_started_at "
        f"FROM `{STATE_TABLE}` state LEFT JOIN `{GENERATION_TABLE}` generation "
        "ON generation.generation_id=state.active_generation WHERE state.state_key=%s",
        (STATE_KEY,),
        as_dict=True,
    )
    if not rows:
        return {"status": "State Missing"}
    state = rows[0]
    warmup = None
    uptime_rows = frappe.db.sql("SHOW GLOBAL STATUS LIKE 'Uptime'")
    database_uptime = int(uptime_rows[0][1]) if uptime_rows else 0
    if state.active_generation and 0 < database_uptime <= 180:
        try:
            warmup = warm_snapshot_preview()
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), "CCD dashboard startup warmup failed"
            )
    dirty = int(state.dirty_version) > int(state.built_dirty_version)
    started = state.refresh_started_at
    watermark_parts = []
    for doctype in SOURCE_WATERMARK_DOCTYPES:
        if frappe.db.table_exists(doctype):
            table = "tab" + doctype.replace("`", "")
            watermark_parts.append(f"SELECT MAX(modified) AS latest FROM `{table}`")
    source_modified = None
    if watermark_parts:
        watermark_rows = frappe.db.sql(
            "SELECT MAX(latest) FROM (" + " UNION ALL ".join(watermark_parts) + ") watermarks"
        )
        source_modified = watermark_rows[0][0] if watermark_rows else None
    missed_change = bool(
        not dirty
        and source_modified
        and state.generation_started_at
        and source_modified > state.generation_started_at
    )
    if missed_change:
        frappe.db.sql(
            f"UPDATE `{STATE_TABLE}` SET dirty_version=dirty_version+1,last_dirty_at=%s,"
            "refresh_status='Dirty',last_refresh_error=NULL WHERE state_key=%s",
            (_now(), STATE_KEY),
        )
        frappe.db.commit()
        try:
            refresh_fast_identity_metrics()
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "CCD dashboard watchdog fast identity refresh failed",
            )
        state.dirty_version = int(state.dirty_version) + 1
        dirty = True
    elif dirty:
        try:
            refresh_fast_identity_metrics()
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "CCD dashboard watchdog fast identity retry failed",
            )
    stalled = bool(
        state.refresh_status == "Building"
        and started
        and started < _now() - timedelta(minutes=20)
    )
    if stalled:
        frappe.db.sql(
            f"UPDATE `{STATE_TABLE}` SET refresh_status='Dirty',"
            "last_refresh_error='Previous refresh exceeded the 20-minute stall threshold' "
            "WHERE state_key=%s",
            (STATE_KEY,),
        )
        frappe.db.commit()
    job_id = enqueue_refresh(int(state.dirty_version)) if dirty or stalled else None
    return {
        "status": "Queued" if job_id else "Current",
        "dirty": dirty,
        "stalled": stalled,
        "missed_change": missed_change,
        "job_id": job_id,
        "database_uptime": database_uptime,
        "warmup": warmup,
    }


def snapshot_status() -> dict[str, Any]:
    """Aggregate-only operational status for administrators."""
    _require_manager()
    if not schema_ready():
        return {"schema_ready": False}
    rows = frappe.db.sql(
        f"SELECT schema_version,active_generation,previous_generation,dirty_version,"
        "built_dirty_version,last_dirty_at,refresh_started_at,active_generation_at,"
        f"refresh_status,last_refresh_error,last_job_id FROM `{STATE_TABLE}` "
        "WHERE state_key=%s",
        (STATE_KEY,),
        as_dict=True,
    )
    return {"schema_ready": True, **dict(rows[0])}

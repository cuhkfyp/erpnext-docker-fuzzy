/* One row per logical person and unordered governed service/source pair. */
WITH RECURSIVE registration_lineage AS (
    SELECT r.name AS current_registration, r.name AS source_name, r.amended_from, 0 AS depth
    FROM `tabCCD Registration` r
    WHERE r.docstatus = 1
    UNION ALL
    SELECT rl.current_registration, parent.name, parent.amended_from, rl.depth + 1
    FROM registration_lineage rl
    JOIN `tabCCD Registration` parent ON parent.name = rl.amended_from
    WHERE rl.depth < 100
),
resolved_registration AS (
    SELECT source_name, MAX(current_registration) AS current_registration
    FROM registration_lineage
    GROUP BY source_name
),
source_settings AS (
    SELECT
        rr.source_name,
        COALESCE(r.custom_include_in_dashboard, 0) AS include_in_dashboard,
        NULLIF(TRIM(r.custom_dashboard_environment), '') AS environment,
        NULLIF(TRIM(r.service_name), '') AS registration_service
    FROM resolved_registration rr
    JOIN `tabCCD Registration` r ON r.name = rr.current_registration
),
current_membership AS (
    SELECT im.ccd_master, MAX(im.identity_group) AS identity_group
    FROM `tabCCD Identity Membership` im
    JOIN `tabCCD Identity Group` ig
      ON ig.name = im.identity_group
     AND ig.status IN ('Active', 'Needs Revalidation')
    WHERE im.status = 'Active'
      AND (im.valid_to IS NULL OR im.valid_to > CURRENT_TIMESTAMP)
    GROUP BY im.ccd_master
),
presence AS (
    SELECT DISTINCT
        CASE
            WHEN cm.identity_group IS NULL THEN CONCAT('M:', m.name)
            ELSE CONCAT('G:', cm.identity_group)
        END AS person_anchor,
        ss.environment,
        COALESCE(NULLIF(TRIM(m.service_name), ''), ss.registration_service, m.ccd_reg_source) AS service,
        m.ccd_reg_source AS source,
        CONCAT(
            ss.environment, '|',
            COALESCE(NULLIF(TRIM(m.service_name), ''), ss.registration_service, m.ccd_reg_source),
            '|', m.ccd_reg_source
        ) AS presence_key
    FROM current_membership cm
    STRAIGHT_JOIN `tabCCD Master` m ON m.name = cm.ccd_master
    JOIN source_settings ss
      ON ss.source_name = m.ccd_reg_source
     AND ss.include_in_dashboard = 1
     AND ss.environment IN ('Production', 'UAT', 'SIT', 'Development', 'Test')
)
SELECT
    SHA2(CONCAT('ccd-dashboard-v1|', a.person_anchor), 256) AS logical_person_key,
    a.environment AS environment_a,
    b.environment AS environment_b,
    CASE WHEN a.environment = b.environment THEN a.environment ELSE 'Cross-environment' END AS environment,
    a.service AS service_a,
    b.service AS service_b,
    a.source AS source_a,
    b.source AS source_b,
    CONCAT(a.service, ' / ', a.source) AS endpoint_a,
    CONCAT(b.service, ' / ', b.source) AS endpoint_b
FROM presence a
JOIN presence b
  ON b.person_anchor = a.person_anchor
 AND b.presence_key > a.presence_key

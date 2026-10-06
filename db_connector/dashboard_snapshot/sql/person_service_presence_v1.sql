/*
Aggregate-safe presence grain: one row per logical person, environment,
service, and governed CCD source.  No direct CCD Master or group identifier is
projected.  The durable Unified Person number is used only as the internal
anchor and is SHA-256 hashed before projection; the output remains pseudonymous.
*/
WITH RECURSIVE registration_lineage AS (
    SELECT
        r.name AS current_registration,
        r.name AS source_name,
        r.amended_from,
        0 AS depth
    FROM `tabCCD Registration` r
    WHERE r.docstatus = 1

    UNION ALL

    SELECT
        rl.current_registration,
        parent.name AS source_name,
        parent.amended_from,
        rl.depth + 1
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
        rr.current_registration,
        COALESCE(r.custom_include_in_dashboard, 0) AS include_in_dashboard,
        NULLIF(TRIM(r.custom_dashboard_environment), '') AS environment,
        NULLIF(TRIM(r.service_name), '') AS registration_service,
        r.custom_service_available_from AS service_available_from
    FROM resolved_registration rr
    JOIN `tabCCD Registration` r ON r.name = rr.current_registration
),
sex_mapping AS (
    SELECT
        rr.source_name,
        MAX(
            CASE
                WHEN LOWER(TRIM(SUBSTRING_INDEX(f.sys_fieldname, ':', 1))) = 'sex'
                THEN 1 ELSE 0
            END
        ) AS has_explicit_sex_mapping
    FROM resolved_registration rr
    LEFT JOIN `tabCCD Field Match` f
        ON f.parent = rr.current_registration
       AND f.parenttype = 'CCD Registration'
    GROUP BY rr.source_name
),
current_membership AS (
    SELECT
        im.ccd_master,
        MAX(im.identity_group) AS identity_group,
        MAX(ig.status) AS identity_state
    FROM `tabCCD Identity Membership` im
    JOIN `tabCCD Identity Group` ig
      ON ig.name = im.identity_group
     AND ig.status IN ('Active', 'Needs Revalidation')
    WHERE im.status = 'Active'
      AND (im.valid_to IS NULL OR im.valid_to > CURRENT_TIMESTAMP)
    GROUP BY im.ccd_master
),
current_unified_membership AS (
    SELECT
        membership.ccd_master,
        MAX(membership.unified_person) AS unified_person
    FROM `tabCCD Unified Person Membership` membership
    JOIN `tabCCD Unified Person` person
      ON person.name = membership.unified_person
     AND person.status = 'Active'
    WHERE membership.status = 'Active'
      AND (membership.valid_to IS NULL OR membership.valid_to > CURRENT_TIMESTAMP)
    GROUP BY membership.ccd_master
),
raw_record_values AS (
    SELECT
        m.name AS master_name,
        COALESCE(ss.include_in_dashboard, 0) AS include_in_dashboard,
        ss.environment,
        COALESCE(NULLIF(TRIM(m.service_name), ''), ss.registration_service, m.ccd_reg_source) AS service,
        m.ccd_reg_source AS source,
        ss.service_available_from,
        m.custom_service_start_date AS service_start_date,
        m.modified,
        CASE
            WHEN m.birthday IS NULL THEN NULL
            WHEN m.birthday > CURRENT_DATE THEN NULL
            WHEN m.birthday < DATE_SUB(CURRENT_DATE, INTERVAL 120 YEAR) THEN NULL
            WHEN LOWER(TRIM(COALESCE(m.birthday_key, ''))) IN
                 ('no', 'n', '0', 'false', 'estimated', 'estimate', 'approximate',
                  'non-actual', 'not actual', 'unknown') THEN NULL
            ELSE m.birthday
        END AS usable_dob,
        CASE
            WHEN m.birthday IS NOT NULL
             AND (m.birthday > CURRENT_DATE
                  OR m.birthday < DATE_SUB(CURRENT_DATE, INTERVAL 120 YEAR))
            THEN 1 ELSE 0
        END AS invalid_dob,
        CASE
            WHEN m.birthday IS NOT NULL
             AND LOWER(TRIM(COALESCE(m.birthday_key, ''))) IN
                 ('no', 'n', '0', 'false', 'estimated', 'estimate', 'approximate',
                  'non-actual', 'not actual', 'unknown')
            THEN 1 ELSE 0
        END AS explicit_non_actual_dob,
        CASE
            WHEN m.birthday IS NOT NULL
             AND LOWER(TRIM(COALESCE(m.birthday_key, ''))) IN
                 ('yes', 'y', '1', 'true', 'actual', 'verified')
            THEN 1 ELSE 0
        END AS verified_dob,
        CASE
            WHEN COALESCE(sm.has_explicit_sex_mapping, 0) = 0 THEN NULL
            WHEN UPPER(TRIM(COALESCE(m.sex, ''))) IN ('M', 'MALE', '男') THEN 'Male / 男'
            WHEN UPPER(TRIM(COALESCE(m.sex, ''))) IN ('F', 'FEMALE', '女') THEN 'Female / 女'
            WHEN UPPER(TRIM(COALESCE(m.sex, ''))) IN
                 ('X', 'OTHER', 'NON-BINARY', 'NONBINARY') THEN 'Other / 其他'
            ELSE NULL
        END AS trusted_sex,
        CASE
            WHEN NULLIF(TRIM(m.res_district), '') IS NULL THEN NULL
            ELSE CASE
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN
                 ('CENTRAL AND WESTERN', 'CENTRAL & WESTERN', 'CW', '中西區') THEN 'CW'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('EASTERN', 'EST', '東區') THEN 'EST'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('SOUTHERN', 'SOU', '南區') THEN 'SOU'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('WAN CHAI', 'WC', '灣仔區') THEN 'WC'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('KOWLOON CITY', 'KC', '九龍城區') THEN 'KC'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('KWUN TONG', 'KWT', '觀塘區') THEN 'KWT'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('SHAM SHUI PO', 'SSP', '深水埗區') THEN 'SSP'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('WONG TAI SIN', 'WTS', '黃大仙區') THEN 'WTS'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('YAU TSIM MONG', 'YTM', '油尖旺區') THEN 'YTM'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('ISLANDS', 'IS', '離島區') THEN 'IS'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('KWAI TSING', 'KUI', '葵青區') THEN 'KUI'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('NORTH', 'NOR', '北區') THEN 'NOR'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('SAI KUNG', 'SK', '西貢區') THEN 'SK'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('SHA TIN', 'ST', '沙田區') THEN 'ST'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('TAI PO', 'TP', '大埔區') THEN 'TP'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('TSUEN WAN', 'TW', '荃灣區') THEN 'TW'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('TUEN MUN', 'TM', '屯門區') THEN 'TM'
            WHEN UPPER(TRIM(COALESCE(m.res_district, ''))) IN ('YUEN LONG', 'YL', '元朗區') THEN 'YL'
            ELSE NULL
            END
        END AS residential_district_code,
        CASE
            WHEN NULLIF(TRIM(m.post_district), '') IS NULL THEN NULL
            ELSE CASE
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN
                 ('CENTRAL AND WESTERN', 'CENTRAL & WESTERN', 'CW', '中西區') THEN 'CW'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('EASTERN', 'EST', '東區') THEN 'EST'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('SOUTHERN', 'SOU', '南區') THEN 'SOU'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('WAN CHAI', 'WC', '灣仔區') THEN 'WC'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('KOWLOON CITY', 'KC', '九龍城區') THEN 'KC'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('KWUN TONG', 'KWT', '觀塘區') THEN 'KWT'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('SHAM SHUI PO', 'SSP', '深水埗區') THEN 'SSP'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('WONG TAI SIN', 'WTS', '黃大仙區') THEN 'WTS'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('YAU TSIM MONG', 'YTM', '油尖旺區') THEN 'YTM'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('ISLANDS', 'IS', '離島區') THEN 'IS'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('KWAI TSING', 'KUI', '葵青區') THEN 'KUI'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('NORTH', 'NOR', '北區') THEN 'NOR'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('SAI KUNG', 'SK', '西貢區') THEN 'SK'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('SHA TIN', 'ST', '沙田區') THEN 'ST'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('TAI PO', 'TP', '大埔區') THEN 'TP'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('TSUEN WAN', 'TW', '荃灣區') THEN 'TW'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('TUEN MUN', 'TM', '屯門區') THEN 'TM'
            WHEN UPPER(TRIM(COALESCE(m.post_district, ''))) IN ('YUEN LONG', 'YL', '元朗區') THEN 'YL'
            ELSE NULL
            END
        END AS postal_district_code
    FROM `tabCCD Master` m
    LEFT JOIN sex_mapping sm ON sm.source_name = m.ccd_reg_source
    LEFT JOIN source_settings ss ON ss.source_name = m.ccd_reg_source
),
record_demographics AS (
    SELECT
        cm.identity_group,
        CONCAT('U:', COALESCE(cum.unified_person, CONCAT('legacy-group:', cm.identity_group))) AS person_anchor,
        rv.include_in_dashboard,
        rv.environment,
        rv.service,
        rv.source,
        cm.identity_state,
        rv.service_available_from,
        rv.service_start_date,
        rv.modified,
        rv.usable_dob,
        rv.invalid_dob,
        rv.explicit_non_actual_dob,
        rv.verified_dob,
        rv.trusted_sex,
        rv.residential_district_code,
        rv.postal_district_code
    FROM current_membership cm
    STRAIGHT_JOIN raw_record_values rv ON rv.master_name = cm.ccd_master
    LEFT JOIN current_unified_membership cum ON cum.ccd_master = cm.ccd_master

    UNION ALL

    SELECT
        NULL,
        CONCAT('U:', COALESCE(cum.unified_person, CONCAT('legacy-master:', rv.master_name))),
        rv.include_in_dashboard,
        rv.environment,
        rv.service,
        rv.source,
        'Standalone',
        rv.service_available_from,
        rv.service_start_date,
        rv.modified,
        rv.usable_dob,
        rv.invalid_dob,
        rv.explicit_non_actual_dob,
        rv.verified_dob,
        rv.trusted_sex,
        rv.residential_district_code,
        rv.postal_district_code
    FROM raw_record_values rv
    LEFT JOIN current_membership cm ON cm.ccd_master = rv.master_name
    LEFT JOIN current_unified_membership cum ON cum.ccd_master = rv.master_name
    WHERE cm.ccd_master IS NULL
),
demographic_rollup AS (
    SELECT
        person_anchor,
        COUNT(DISTINCT usable_dob) AS usable_dob_count,
        MAX(usable_dob) AS canonical_dob,
        MAX(verified_dob) AS has_verified_dob,
        SUM(invalid_dob) AS invalid_dob_count,
        SUM(explicit_non_actual_dob) AS non_actual_dob_count,
        COUNT(DISTINCT trusted_sex) AS trusted_sex_count,
        MAX(trusted_sex) AS canonical_sex,
        COUNT(DISTINCT residential_district_code) AS residential_district_count,
        MAX(residential_district_code) AS canonical_residential_district_code,
        COUNT(DISTINCT postal_district_code) AS postal_district_count,
        MAX(postal_district_code) AS canonical_postal_district_code,
        0 AS inferred_district_count,
        NULL AS canonical_inferred_district_code
    FROM record_demographics
    WHERE identity_group IS NOT NULL
    GROUP BY person_anchor
),
group_person_demographics AS (
    SELECT
        person_anchor,
        CASE
            WHEN usable_dob_count > 1 THEN 'Conflicting'
            WHEN usable_dob_count = 0 THEN 'Unknown/Invalid'
            WHEN TIMESTAMPDIFF(YEAR, canonical_dob, CURRENT_DATE) BETWEEN 0 AND 17 THEN '0-17'
            WHEN TIMESTAMPDIFF(YEAR, canonical_dob, CURRENT_DATE) BETWEEN 18 AND 24 THEN '18-24'
            WHEN TIMESTAMPDIFF(YEAR, canonical_dob, CURRENT_DATE) BETWEEN 25 AND 34 THEN '25-34'
            WHEN TIMESTAMPDIFF(YEAR, canonical_dob, CURRENT_DATE) BETWEEN 35 AND 44 THEN '35-44'
            WHEN TIMESTAMPDIFF(YEAR, canonical_dob, CURRENT_DATE) BETWEEN 45 AND 54 THEN '45-54'
            WHEN TIMESTAMPDIFF(YEAR, canonical_dob, CURRENT_DATE) BETWEEN 55 AND 64 THEN '55-64'
            WHEN TIMESTAMPDIFF(YEAR, canonical_dob, CURRENT_DATE) >= 65 THEN '65+'
            ELSE 'Unknown/Invalid'
        END AS age_band,
        CASE
            WHEN usable_dob_count > 1 THEN 'Conflicting DOB'
            WHEN usable_dob_count = 1 AND has_verified_dob = 1 THEN 'Verified'
            WHEN usable_dob_count = 1 THEN 'Plausible-unverified'
            WHEN invalid_dob_count > 0 THEN 'Invalid'
            WHEN non_actual_dob_count > 0 THEN 'Estimated/non-actual'
            ELSE 'Missing'
        END AS dob_confidence,
        CASE
            WHEN trusted_sex_count > 1 THEN 'Conflicting'
            WHEN trusted_sex_count = 1 THEN canonical_sex
            ELSE 'Unknown'
        END AS sex_category,
        CASE
            WHEN residential_district_count > 1 THEN 'CONFLICT'
            WHEN residential_district_count = 1 THEN canonical_residential_district_code
            WHEN postal_district_count > 1 THEN 'CONFLICT'
            WHEN postal_district_count = 1 THEN canonical_postal_district_code
            WHEN inferred_district_count > 1 THEN 'CONFLICT'
            WHEN inferred_district_count = 1 THEN canonical_inferred_district_code
            ELSE 'UNKNOWN'
        END AS district_code,
        CASE
            WHEN residential_district_count > 1 THEN 'Conflicting / 衝突'
            WHEN residential_district_count = 1 THEN 'Residential district / 住宅地區'
            WHEN postal_district_count > 1 THEN 'Conflicting / 衝突'
            WHEN postal_district_count = 1 THEN 'Postal district / 郵寄地區'
            WHEN inferred_district_count > 1 THEN 'Conflicting / 衝突'
            WHEN inferred_district_count = 1 THEN 'Address inferred / 地址推斷'
            ELSE 'Unknown / 未知'
        END AS district_basis
    FROM demographic_rollup
),
included_records AS (
    SELECT
        rd.*
    FROM record_demographics rd
    WHERE rd.include_in_dashboard = 1
      AND rd.environment IN ('Production', 'UAT', 'SIT', 'Development', 'Test')
),
person_growth AS (
    SELECT
        person_anchor,
        environment,
        MIN(service_start_date) AS overall_service_start_date
    FROM included_records
    WHERE identity_group IS NOT NULL
      AND service_start_date IS NOT NULL
    GROUP BY person_anchor, environment
),
person_service_growth AS (
    SELECT
        person_anchor,
        environment,
        service,
        MIN(service_start_date) AS service_start_date
    FROM included_records
    WHERE identity_group IS NOT NULL
      AND service_start_date IS NOT NULL
    GROUP BY person_anchor, environment, service
),
person_service_counts AS (
    SELECT person_anchor, environment, COUNT(DISTINCT service) AS services_per_person
    FROM included_records
    WHERE identity_group IS NOT NULL
    GROUP BY person_anchor, environment
),
grouped_presence AS (
    SELECT
        person_anchor,
        environment,
        service,
        source,
        identity_state,
        service_available_from,
        COUNT(*) AS source_row_count,
        MAX(modified) AS latest_ccd_modified
    FROM included_records
    WHERE identity_group IS NOT NULL
    GROUP BY person_anchor, environment, service, source, identity_state, service_available_from
),
categorized_presence AS (
    SELECT
        gp.person_anchor,
        gp.environment,
        gp.service,
        gp.source,
        gp.identity_state,
        pd.age_band,
        pd.dob_confidence,
        pd.sex_category,
        pd.district_code,
        pd.district_basis,
        DATE_FORMAT(pg.overall_service_start_date, '%Y-%m') AS overall_growth_month,
        DATE_FORMAT(psg.service_start_date, '%Y-%m') AS service_growth_month,
        psc.services_per_person,
        gp.source_row_count,
        gp.service_available_from,
        gp.latest_ccd_modified
    FROM grouped_presence gp
    JOIN group_person_demographics pd ON pd.person_anchor = gp.person_anchor
    JOIN person_service_counts psc
      ON psc.person_anchor = gp.person_anchor
     AND psc.environment = gp.environment
    LEFT JOIN person_growth pg
      ON pg.person_anchor = gp.person_anchor
     AND pg.environment = gp.environment
    LEFT JOIN person_service_growth psg
      ON psg.person_anchor = gp.person_anchor
     AND psg.environment = gp.environment
     AND psg.service = gp.service

    UNION ALL

    SELECT
        ir.person_anchor,
        ir.environment,
        ir.service,
        ir.source,
        ir.identity_state,
        CASE
            WHEN ir.usable_dob IS NULL THEN 'Unknown/Invalid'
            WHEN TIMESTAMPDIFF(YEAR, ir.usable_dob, CURRENT_DATE) BETWEEN 0 AND 17 THEN '0-17'
            WHEN TIMESTAMPDIFF(YEAR, ir.usable_dob, CURRENT_DATE) BETWEEN 18 AND 24 THEN '18-24'
            WHEN TIMESTAMPDIFF(YEAR, ir.usable_dob, CURRENT_DATE) BETWEEN 25 AND 34 THEN '25-34'
            WHEN TIMESTAMPDIFF(YEAR, ir.usable_dob, CURRENT_DATE) BETWEEN 35 AND 44 THEN '35-44'
            WHEN TIMESTAMPDIFF(YEAR, ir.usable_dob, CURRENT_DATE) BETWEEN 45 AND 54 THEN '45-54'
            WHEN TIMESTAMPDIFF(YEAR, ir.usable_dob, CURRENT_DATE) BETWEEN 55 AND 64 THEN '55-64'
            WHEN TIMESTAMPDIFF(YEAR, ir.usable_dob, CURRENT_DATE) >= 65 THEN '65+'
            ELSE 'Unknown/Invalid'
        END,
        CASE
            WHEN ir.usable_dob IS NOT NULL AND ir.verified_dob = 1 THEN 'Verified'
            WHEN ir.usable_dob IS NOT NULL THEN 'Plausible-unverified'
            WHEN ir.invalid_dob = 1 THEN 'Invalid'
            WHEN ir.explicit_non_actual_dob = 1 THEN 'Estimated/non-actual'
            ELSE 'Missing'
        END,
        COALESCE(ir.trusted_sex, 'Unknown'),
        COALESCE(ir.residential_district_code, ir.postal_district_code, 'UNKNOWN'),
        CASE
            WHEN ir.residential_district_code IS NOT NULL THEN 'Residential district / 住宅地區'
            WHEN ir.postal_district_code IS NOT NULL THEN 'Postal district / 郵寄地區'
            ELSE 'Unknown / 未知'
        END,
        DATE_FORMAT(ir.service_start_date, '%Y-%m'),
        DATE_FORMAT(ir.service_start_date, '%Y-%m'),
        1,
        1,
        ir.service_available_from,
        ir.modified
    FROM included_records ir
    WHERE ir.identity_group IS NULL
)
SELECT
    SHA2(CONCAT('ccd-dashboard-v1|', cp.person_anchor), 256) AS logical_person_key,
    cp.environment,
    cp.service,
    cp.source,
    cp.identity_state,
    cp.age_band,
    cp.dob_confidence,
    cp.sex_category,
    cp.district_code,
    cp.district_basis,
    CASE cp.district_code
        WHEN 'CW' THEN 'Central and Western / 中西區'
        WHEN 'EST' THEN 'Eastern / 東區'
        WHEN 'SOU' THEN 'Southern / 南區'
        WHEN 'WC' THEN 'Wan Chai / 灣仔區'
        WHEN 'KC' THEN 'Kowloon City / 九龍城區'
        WHEN 'KWT' THEN 'Kwun Tong / 觀塘區'
        WHEN 'SSP' THEN 'Sham Shui Po / 深水埗區'
        WHEN 'WTS' THEN 'Wong Tai Sin / 黃大仙區'
        WHEN 'YTM' THEN 'Yau Tsim Mong / 油尖旺區'
        WHEN 'IS' THEN 'Islands / 離島區'
        WHEN 'KUI' THEN 'Kwai Tsing / 葵青區'
        WHEN 'NOR' THEN 'North / 北區'
        WHEN 'SK' THEN 'Sai Kung / 西貢區'
        WHEN 'ST' THEN 'Sha Tin / 沙田區'
        WHEN 'TP' THEN 'Tai Po / 大埔區'
        WHEN 'TW' THEN 'Tsuen Wan / 荃灣區'
        WHEN 'TM' THEN 'Tuen Mun / 屯門區'
        WHEN 'YL' THEN 'Yuen Long / 元朗區'
        WHEN 'CONFLICT' THEN 'Conflicting / 衝突'
        ELSE 'Unknown / 未知'
    END AS district,
    CASE
        WHEN cp.district_code IN ('CW', 'EST', 'SOU', 'WC') THEN 'Hong Kong Island / 香港島'
        WHEN cp.district_code IN ('KC', 'KWT', 'SSP', 'WTS', 'YTM') THEN 'Kowloon / 九龍'
        WHEN cp.district_code IN ('IS', 'KUI', 'NOR', 'SK', 'ST', 'TP', 'TW', 'TM', 'YL')
            THEN 'New Territories / 新界'
        WHEN cp.district_code = 'CONFLICT' THEN 'Conflicting / 衝突'
        ELSE 'Unknown / 未知'
    END AS area,
    cp.overall_growth_month,
    cp.service_growth_month,
    cp.services_per_person,
    cp.source_row_count,
    cp.service_available_from,
    cp.latest_ccd_modified
FROM categorized_presence cp

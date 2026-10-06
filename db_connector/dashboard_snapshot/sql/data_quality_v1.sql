/* Source-level readiness and warning aggregates; never projects client data. */
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
registration_roots AS (
    SELECT rl.current_registration, rl.source_name AS root_source
    FROM registration_lineage rl
    JOIN (
        SELECT current_registration, MAX(depth) AS max_depth
        FROM registration_lineage
        GROUP BY current_registration
    ) depth_by_current
      ON depth_by_current.current_registration = rl.current_registration
     AND depth_by_current.max_depth = rl.depth
),
source_universe AS (
    SELECT DISTINCT ccd_reg_source AS source
    FROM `tabCCD Master`
    WHERE COALESCE(ccd_reg_source, '') <> ''
    UNION
    SELECT root_source FROM registration_roots
),
mapping_readiness AS (
    SELECT
        rr.source_name,
        MAX(LOWER(TRIM(SUBSTRING_INDEX(f.sys_fieldname, ':', 1))) = 'sex') AS sex_mapping_ready,
        MAX(LOWER(TRIM(SUBSTRING_INDEX(f.sys_fieldname, ':', 1))) = 'birthday') AS dob_mapping_ready,
        MAX(LOWER(TRIM(SUBSTRING_INDEX(f.sys_fieldname, ':', 1))) = 'birthday_key') AS dob_key_mapping_ready,
        MAX(LOWER(TRIM(SUBSTRING_INDEX(f.sys_fieldname, ':', 1))) = 'res_district') AS residential_district_mapping_ready,
        MAX(LOWER(TRIM(SUBSTRING_INDEX(f.sys_fieldname, ':', 1))) IN
            ('post_district', 'pos_district')) AS postal_district_mapping_ready,
        MAX(LOWER(TRIM(SUBSTRING_INDEX(f.sys_fieldname, ':', 1))) = 'res_addr1') AS address_inference_ready
    FROM resolved_registration rr
    LEFT JOIN `tabCCD Field Match` f
      ON f.parent = rr.current_registration
     AND f.parenttype = 'CCD Registration'
    GROUP BY rr.source_name
),
source_metadata AS (
    SELECT
        rr.source_name,
        COALESCE(r.custom_include_in_dashboard, 0) AS include_in_dashboard,
        NULLIF(TRIM(r.custom_dashboard_environment), '') AS environment,
        NULLIF(TRIM(r.service_name), '') AS registration_service,
        r.custom_service_available_from AS service_available_from,
        r.modified AS registration_modified
    FROM resolved_registration rr
    JOIN `tabCCD Registration` r ON r.name = rr.current_registration
),
address_district_pattern_sets AS (
    SELECT 'CW' AS district_code,
           '中西區|中環|上環|西營盤|石塘咀|堅尼地城|摩星嶺|山頂|金鐘|半山|西環|柏道|般咸道|薄扶林道|皇后大道西|德輔道西|第三街|高街|正街|CENTRAL|SHEUNGWAN|SAIYINGPUN|SHEKTONGTSUI|KENNEDYTOWN|ADMIRALTY|MIDLEVELS|THEPEAK|PARKROAD|BONHAMROAD' AS address_tokens
    UNION ALL SELECT 'EST', '東區|炮台山|北角|鰂魚涌|西灣河|筲箕灣|柴灣|小西灣|太古城|杏花邨|FORTRESSHILL|NORTHPOINT|QUARRYBAY|SAIWANHO|SHAUKEIWAN|CHAIWAN|SIUSAIWAN|TAIKOO|HENGFACHUEN'
    UNION ALL SELECT 'SOU', '南區|香港仔|鴨脷洲|黃竹坑|赤柱|石澳|淺水灣|深水灣|壽臣山|ABERDEEN|APLEICHAU|WONGCHUKHANG|STANLEY|SHEKO|REPULSEBAY|DEEPWATERBAY|SHOUSONHILL'
    UNION ALL SELECT 'WC', '灣仔區|灣仔|銅鑼灣|跑馬地|大坑|渣甸山|WANCHAI|CAUSEWAYBAY|HAPPYVALLEY|TAIHANG|JARDINESLOOKOUT'
    UNION ALL SELECT 'KC', '九龍城區|九龍城|土瓜灣|馬頭角|馬頭圍|紅磡|何文田|啟德|九龍塘|KOWLOONCITY|TOKWAWAN|MATAUKOK|MATAUWAI|HUNGHOM|HOMANTIN|KAITAK|KOWLOONTONG'
    UNION ALL SELECT 'KWT', '觀塘區|觀塘|牛頭角|九龍灣|秀茂坪|藍田|油塘|茶果嶺|順利邨|KWUNTONG|NGAUTAUKOK|KOWLOONBAY|SAUMAUPING|LAMTIN|YAUTONG|CHAKWOLING'
    UNION ALL SELECT 'SSP', '深水埗區|深水埗|長沙灣|荔枝角|美孚|石硤尾|又一村|大窩坪|SHAMSHUIPO|CHEUNGSHaWAN|LAICHIKOK|MEIFOO|SHEKKIPMEI|YAUYATCHUEN'
    UNION ALL SELECT 'WTS', '黃大仙區|黃大仙|新蒲崗|鑽石山|慈雲山|樂富|橫頭磡|竹園|牛池灣|WONGTAISIN|SANPOKONG|DIAMONDHILL|TSZWANSHAN|LOKFU|WANGTAUHOM|CHUKYUEN|NGAUCHIWAN'
    UNION ALL SELECT 'YTM', '油尖旺區|油麻地|尖沙咀|旺角|大角咀|佐敦|京士柏|MONGKOK|YAUMATEI|TSIMSHATSUI|TAIKOKTSUI|JORDAN|KINGSPARK'
    UNION ALL SELECT 'IS', '離島區|大嶼山|東涌|愉景灣|長洲|南丫島|坪洲|梅窩|大澳|赤鱲角|LANTAU|TUNGCHUNG|DISCOVERYBAY|CHEUNGCHAU|LAMMA|PENGCHAU|MUIWO|TAIO|CHEKLAPKOK'
    UNION ALL SELECT 'KUI', '葵青區|葵涌|葵芳|葵興|青衣|KWAICHUNG|KWAIFONG|KWAIHING|TSINGYI'
    UNION ALL SELECT 'NOR', '北區|上水|粉嶺|沙頭角|打鼓嶺|古洞|SHEUNGSHUI|FANLING|SHATAUKOK|TAKWULING|KWUTUNG'
    UNION ALL SELECT 'SK', '西貢區|西貢|將軍澳|調景嶺|坑口|寶琳|康城|SAIKUNG|TSEUNGKWANO|TIUKENGLENG|HANGHAU|POLAM|LOHAS'
    UNION ALL SELECT 'ST', '沙田區|沙田|大圍|馬鞍山|火炭|石門|小瀝源|SHATIN|TAIWAI|MAONSHAN|FOTAN|SHEKMUN|SIULEKYUEN'
    UNION ALL SELECT 'TP', '大埔區|大埔|太和|林村|TAIPO|TAIWO|LAMTSUEN'
    UNION ALL SELECT 'TW', '荃灣區|荃灣|深井|馬灣|汀九|TSUENWAN|SHAMTSENG|MAWAN|TINGKAU'
    UNION ALL SELECT 'TM', '屯門區|屯門|藍地|掃管笏|小欖|TUENMUN|LAMTEI|SOKWUNWAT|SIULAM'
    UNION ALL SELECT 'YL', '元朗區|元朗|天水圍|洪水橋|錦田|八鄉|流浮山|YUENLONG|TINSHUIWAI|HUNGSHUIKIU|KAMTIN|PATHEUNG|LAUFAUSHAN'
),
address_district_patterns AS (
    SELECT
        district_code,
        SUBSTRING_INDEX(address_tokens, '|', 1) AS address_token,
        CASE WHEN INSTR(address_tokens, '|') = 0 THEN ''
             ELSE SUBSTRING(address_tokens, INSTR(address_tokens, '|') + 1) END
            AS remaining_tokens
    FROM address_district_pattern_sets

    UNION ALL

    SELECT
        district_code,
        SUBSTRING_INDEX(remaining_tokens, '|', 1),
        CASE WHEN INSTR(remaining_tokens, '|') = 0 THEN ''
             ELSE SUBSTRING(remaining_tokens, INSTR(remaining_tokens, '|') + 1) END
    FROM address_district_patterns
    WHERE remaining_tokens <> ''
),
private_address_overrides AS (
    /*__PRIVATE_ADDRESS_OVERRIDE_ROWS__*/
),
location_flags AS (
    SELECT
        m.name AS master_name,
        m.ccd_reg_source AS source,
        UPPER(TRIM(COALESCE(m.res_district, ''))) IN
            ('CENTRAL AND WESTERN', 'CENTRAL & WESTERN', 'CW', '中西區',
             'EASTERN', 'EST', '東區', 'SOUTHERN', 'SOU', '南區',
             'WAN CHAI', 'WC', '灣仔區', 'KOWLOON CITY', 'KC', '九龍城區',
             'KWUN TONG', 'KWT', '觀塘區', 'SHAM SHUI PO', 'SSP', '深水埗區',
             'WONG TAI SIN', 'WTS', '黃大仙區', 'YAU TSIM MONG', 'YTM', '油尖旺區',
             'ISLANDS', 'IS', '離島區', 'KWAI TSING', 'KUI', '葵青區',
             'NORTH', 'NOR', '北區', 'SAI KUNG', 'SK', '西貢區',
             'SHA TIN', 'ST', '沙田區', 'TAI PO', 'TP', '大埔區',
             'TSUEN WAN', 'TW', '荃灣區', 'TUEN MUN', 'TM', '屯門區',
             'YUEN LONG', 'YL', '元朗區') AS residential_valid,
        UPPER(TRIM(COALESCE(m.post_district, ''))) IN
            ('CENTRAL AND WESTERN', 'CENTRAL & WESTERN', 'CW', '中西區',
             'EASTERN', 'EST', '東區', 'SOUTHERN', 'SOU', '南區',
             'WAN CHAI', 'WC', '灣仔區', 'KOWLOON CITY', 'KC', '九龍城區',
             'KWUN TONG', 'KWT', '觀塘區', 'SHAM SHUI PO', 'SSP', '深水埗區',
             'WONG TAI SIN', 'WTS', '黃大仙區', 'YAU TSIM MONG', 'YTM', '油尖旺區',
             'ISLANDS', 'IS', '離島區', 'KWAI TSING', 'KUI', '葵青區',
             'NORTH', 'NOR', '北區', 'SAI KUNG', 'SK', '西貢區',
             'SHA TIN', 'ST', '沙田區', 'TAI PO', 'TP', '大埔區',
             'TSUEN WAN', 'TW', '荃灣區', 'TUEN MUN', 'TM', '屯門區',
             'YUEN LONG', 'YL', '元朗區') AS postal_valid,
        NULLIF(TRIM(m.res_district), '') IS NOT NULL AS residential_present,
        NULLIF(TRIM(m.post_district), '') IS NOT NULL AS postal_present,
        NULLIF(TRIM(m.res_addr1), '') IS NOT NULL AS address_present
    FROM `tabCCD Master` m
),
address_candidates AS (
    SELECT
        m.name AS master_name,
        m.ccd_reg_source AS source,
        REGEXP_REPLACE(UPPER(TRIM(m.res_addr1)), '[[:space:][:punct:]]', '')
            AS normalized_address,
        SHA2(
            REGEXP_REPLACE(UPPER(TRIM(m.res_addr1)), '[[:space:][:punct:]]', ''),
            256
        ) AS address_hash
    FROM `tabCCD Master` m
    JOIN location_flags lf ON lf.master_name = m.name
    WHERE lf.residential_valid = 0
      AND lf.postal_valid = 0
      AND lf.address_present = 1
),
address_match_rollup AS (
    SELECT
        ac.master_name,
        ac.source,
        CASE
            WHEN MAX(pao.district_code) IS NOT NULL THEN 1
            ELSE COUNT(DISTINCT adp.district_code)
        END AS address_district_match_count
    FROM address_candidates ac
    LEFT JOIN address_district_patterns adp
      ON LOCATE(adp.address_token, ac.normalized_address) > 0
    LEFT JOIN private_address_overrides pao ON pao.address_hash = ac.address_hash
    GROUP BY ac.master_name, ac.source
),
address_quality AS (
    SELECT
        source,
        SUM(address_district_match_count = 1) AS inferred_district_rows,
        SUM(address_district_match_count > 1) AS ambiguous_address_rows,
        SUM(address_district_match_count = 0) AS unmatched_address_rows
    FROM address_match_rollup
    GROUP BY source
),
row_quality AS (
    SELECT
        m.ccd_reg_source AS source,
        COUNT(*) AS source_row_count,
        SUM(NULLIF(TRIM(m.service_name), '') IS NOT NULL) AS row_service_populated,
        SUM(m.custom_service_start_date IS NOT NULL) AS service_start_populated_rows,
        SUM(m.birthday IS NOT NULL) AS dob_populated_rows,
        SUM(
            m.birthday IS NOT NULL
            AND m.birthday BETWEEN DATE_SUB(CURRENT_DATE, INTERVAL 120 YEAR) AND CURRENT_DATE
            AND LOWER(TRIM(COALESCE(m.birthday_key, ''))) NOT IN
                ('no', 'n', '0', 'false', 'estimated', 'estimate', 'approximate',
                 'non-actual', 'not actual', 'unknown')
        ) AS usable_dob_rows,
        SUM(
            m.birthday IS NOT NULL
            AND LOWER(TRIM(COALESCE(m.birthday_key, ''))) IN
                ('yes', 'y', '1', 'true', 'actual', 'verified')
        ) AS verified_dob_rows,
        SUM(
            m.birthday IS NOT NULL
            AND NULLIF(TRIM(COALESCE(m.birthday_key, '')), '') IS NULL
            AND m.birthday BETWEEN DATE_SUB(CURRENT_DATE, INTERVAL 120 YEAR) AND CURRENT_DATE
        ) AS plausible_unverified_dob_rows,
        SUM(
            m.birthday IS NOT NULL
            AND (m.birthday > CURRENT_DATE
                 OR m.birthday < DATE_SUB(CURRENT_DATE, INTERVAL 120 YEAR))
        ) AS invalid_dob_rows,
        SUM(
            LOWER(TRIM(COALESCE(m.birthday_key, ''))) IN
                ('yes', 'y', '1', 'true', 'actual', 'verified')
            AND m.birthday IS NULL
        ) AS birthday_mapping_defect_rows,
        SUM(UPPER(TRIM(COALESCE(m.sex, ''))) = 'M') AS stored_m_rows,
        SUM(NULLIF(TRIM(m.sex), '') IS NOT NULL) AS sex_populated_rows,
        SUM(
            lf.residential_valid = 1
            OR lf.postal_valid = 1
        ) AS direct_valid_district_rows,
        SUM(
            lf.residential_valid = 0
            AND lf.postal_valid = 0
            AND (lf.residential_present = 1 OR lf.postal_present = 1)
            AND lf.address_present = 0
        ) AS direct_unmapped_without_address_rows,
        SUM(
            lf.residential_present = 0
            AND lf.postal_present = 0
            AND lf.address_present = 0
        ) AS missing_location_rows,
        MAX(m.modified) AS latest_ccd_modified
    FROM `tabCCD Master` m
    JOIN location_flags lf ON lf.master_name = m.name
    GROUP BY m.ccd_reg_source
)
SELECT
    su.source,
    CASE
        WHEN su.source IN ('DT-NB181', 'BBBB-BBB', 'From-PJCHW') THEN 'Excluded'
        WHEN sm.include_in_dashboard = 1
         AND sm.environment IN ('Production', 'UAT', 'SIT', 'Development', 'Test') THEN 'Included'
        WHEN sm.environment IS NOT NULL THEN 'Excluded'
        ELSE 'Unclassified'
    END AS classification,
    COALESCE(sm.environment, 'Unclassified') AS environment,
    COALESCE(sm.include_in_dashboard, 0) AS include_in_dashboard,
    COALESCE(rq.source_row_count, 0) AS source_row_count,
    CASE WHEN COALESCE(rq.source_row_count, 0) > 0 THEN 1 ELSE 0 END AS populated_source,
    CASE
        WHEN sm.registration_service IS NOT NULL THEN 100.0
        WHEN COALESCE(rq.source_row_count, 0) = 0 THEN 0.0
        ELSE ROUND(100.0 * COALESCE(rq.row_service_populated, 0) / rq.source_row_count, 2)
    END AS service_metadata_coverage_pct,
    CASE
        WHEN COALESCE(rq.source_row_count, 0) = 0 THEN 0.0
        ELSE ROUND(
            100.0 * COALESCE(rq.service_start_populated_rows, 0) / rq.source_row_count,
            2
        )
    END AS service_start_coverage_pct,
    CASE
        WHEN COALESCE(rq.source_row_count, 0) = 0
            THEN 'Unavailable - no source rows'
        WHEN COALESCE(rq.service_start_populated_rows, 0) = 0
            THEN 'Unavailable - service start dates not populated'
        ELSE CONCAT(
            'Ready - client service-start coverage ',
            ROUND(100.0 * rq.service_start_populated_rows / rq.source_row_count, 2),
            '%; missing dates excluded'
        )
    END AS growth_readiness,
    COALESCE(mr.sex_mapping_ready, 0) AS sex_mapping_ready,
    CASE
        WHEN COALESCE(mr.sex_mapping_ready, 0) = 0 THEN COALESCE(rq.stored_m_rows, 0)
        ELSE 0
    END AS placeholder_sex_rows,
    COALESCE(rq.sex_populated_rows, 0) AS stored_sex_rows,
    COALESCE(mr.dob_mapping_ready, 0) AS dob_mapping_ready,
    COALESCE(mr.dob_key_mapping_ready, 0) AS dob_key_mapping_ready,
    COALESCE(rq.dob_populated_rows, 0) AS dob_populated_rows,
    COALESCE(rq.usable_dob_rows, 0) AS usable_dob_rows,
    COALESCE(rq.verified_dob_rows, 0) AS verified_dob_rows,
    COALESCE(rq.plausible_unverified_dob_rows, 0) AS plausible_unverified_dob_rows,
    COALESCE(rq.invalid_dob_rows, 0) AS invalid_dob_rows,
    COALESCE(rq.birthday_mapping_defect_rows, 0) AS birthday_mapping_defect_rows,
    COALESCE(mr.residential_district_mapping_ready, 0) AS residential_district_mapping_ready,
    COALESCE(mr.postal_district_mapping_ready, 0) AS postal_district_mapping_ready,
    COALESCE(mr.address_inference_ready, 0) AS address_inference_ready,
    COALESCE(rq.direct_valid_district_rows, 0)
        + COALESCE(aq.inferred_district_rows, 0) AS valid_district_rows,
    COALESCE(aq.inferred_district_rows, 0) AS inferred_district_rows,
    COALESCE(aq.ambiguous_address_rows, 0) AS ambiguous_address_rows,
    COALESCE(rq.direct_unmapped_without_address_rows, 0)
        + COALESCE(aq.ambiguous_address_rows, 0)
        + COALESCE(aq.unmatched_address_rows, 0) AS unmapped_location_rows,
    COALESCE(rq.missing_location_rows, 0) AS missing_district_rows,
    sm.service_available_from,
    rq.latest_ccd_modified
FROM source_universe su
LEFT JOIN source_metadata sm ON sm.source_name = su.source
LEFT JOIN mapping_readiness mr ON mr.source_name = su.source
LEFT JOIN row_quality rq ON rq.source = su.source
LEFT JOIN address_quality aq ON aq.source = su.source

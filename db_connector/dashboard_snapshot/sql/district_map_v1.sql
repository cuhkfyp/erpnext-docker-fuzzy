/* Aggregate-safe Hong Kong district map with on-server address inference. */
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
address_candidates AS (
    SELECT
        m.name AS master_name,
        REGEXP_REPLACE(UPPER(TRIM(m.res_addr1)), '[[:space:][:punct:]]', '') AS normalized_address,
        SHA2(
            REGEXP_REPLACE(UPPER(TRIM(m.res_addr1)), '[[:space:][:punct:]]', ''),
            256
        ) AS address_hash
    FROM `tabCCD Master` m
    WHERE NULLIF(TRIM(m.res_addr1), '') IS NOT NULL
),
address_match_rollup AS (
    SELECT
        ac.master_name,
        COUNT(DISTINCT adp.district_code) AS district_match_count,
        MAX(adp.district_code) AS matched_district_code
    FROM address_candidates ac
    JOIN address_district_patterns adp
      ON LOCATE(adp.address_token, ac.normalized_address) > 0
    GROUP BY ac.master_name
),
district_geometry AS (
    /*__HK_DISTRICT_GEOMETRY_ROWS__*/
),
location_records AS (
    SELECT
        m.name AS master_name,
        CASE
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
        END AS residential_district_code,
        CASE
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
        END AS postal_district_code,
        CASE
            WHEN pao.district_code IS NOT NULL THEN pao.district_code
            WHEN COALESCE(amr.district_match_count, 0) = 1 THEN amr.matched_district_code
        END
            AS inferred_district_code,
        CASE
            WHEN pao.district_code IS NOT NULL THEN 1
            ELSE COALESCE(amr.district_match_count, 0)
        END AS address_district_match_count
    FROM `tabCCD Master` m
    LEFT JOIN address_candidates ac ON ac.master_name = m.name
    LEFT JOIN address_match_rollup amr ON amr.master_name = m.name
    LEFT JOIN private_address_overrides pao ON pao.address_hash = ac.address_hash
    WHERE NULLIF(TRIM(m.res_district), '') IS NOT NULL
       OR NULLIF(TRIM(m.post_district), '') IS NOT NULL
       OR NULLIF(TRIM(m.res_addr1), '') IS NOT NULL
),
current_membership AS (
    SELECT im.ccd_master, MAX(im.identity_group) AS identity_group, MAX(ig.status) AS identity_state
    FROM `tabCCD Identity Membership` im
    JOIN `tabCCD Identity Group` ig
      ON ig.name = im.identity_group
     AND ig.status IN ('Active', 'Needs Revalidation')
    WHERE im.status = 'Active'
      AND (im.valid_to IS NULL OR im.valid_to > CURRENT_TIMESTAMP)
    GROUP BY im.ccd_master
),
group_location_rollup AS (
    SELECT
        cm.identity_group,
        COUNT(DISTINCT lr.residential_district_code) AS residential_count,
        MAX(lr.residential_district_code) AS residential_code,
        COUNT(DISTINCT lr.postal_district_code) AS postal_count,
        MAX(lr.postal_district_code) AS postal_code,
        COUNT(DISTINCT lr.inferred_district_code) AS inferred_count,
        MAX(lr.inferred_district_code) AS inferred_code
    FROM location_records lr
    JOIN current_membership cm ON cm.ccd_master = lr.master_name
    GROUP BY cm.identity_group
),
group_location AS (
    SELECT
        identity_group,
        CASE
            WHEN residential_count = 1 THEN residential_code
            WHEN residential_count > 1 THEN 'CONFLICT'
            WHEN postal_count = 1 THEN postal_code
            WHEN postal_count > 1 THEN 'CONFLICT'
            WHEN inferred_count = 1 THEN inferred_code
            WHEN inferred_count > 1 THEN 'CONFLICT'
            ELSE 'UNKNOWN'
        END AS district_code,
        CASE
            WHEN residential_count = 1 THEN 'Residential district / 住宅地區'
            WHEN residential_count > 1 THEN 'Conflicting / 衝突'
            WHEN postal_count = 1 THEN 'Postal district / 郵寄地區'
            WHEN postal_count > 1 THEN 'Conflicting / 衝突'
            WHEN inferred_count = 1 THEN 'Address inferred / 地址推斷'
            WHEN inferred_count > 1 THEN 'Conflicting / 衝突'
            ELSE 'Unknown / 未知'
        END AS district_basis
    FROM group_location_rollup
),
categorized_presence AS (
    SELECT
        CONCAT('G:', cm.identity_group) AS person_anchor,
        ss.environment,
        COALESCE(NULLIF(TRIM(m.service_name), ''), ss.registration_service, m.ccd_reg_source)
            AS service,
        m.ccd_reg_source AS source,
        cm.identity_state,
        gl.district_code, gl.district_basis
    FROM current_membership cm
    STRAIGHT_JOIN `tabCCD Master` m ON m.name = cm.ccd_master
    JOIN source_settings ss
      ON ss.source_name = m.ccd_reg_source
     AND ss.include_in_dashboard = 1
     AND ss.environment IN ('Production', 'UAT', 'SIT', 'Development', 'Test')
    JOIN group_location gl ON gl.identity_group = cm.identity_group

    UNION ALL

    SELECT
        CONCAT('M:', m.name) AS person_anchor,
        ss.environment,
        COALESCE(NULLIF(TRIM(m.service_name), ''), ss.registration_service, m.ccd_reg_source)
            AS service,
        m.ccd_reg_source AS source,
        'Standalone' AS identity_state,
        COALESCE(lr.residential_district_code, lr.postal_district_code, lr.inferred_district_code, 'UNKNOWN'),
        CASE
            WHEN lr.residential_district_code IS NOT NULL THEN 'Residential district / 住宅地區'
            WHEN lr.postal_district_code IS NOT NULL THEN 'Postal district / 郵寄地區'
            WHEN lr.inferred_district_code IS NOT NULL THEN 'Address inferred / 地址推斷'
            WHEN lr.address_district_match_count > 1 THEN 'Conflicting / 衝突'
            ELSE 'Unknown / 未知'
        END
    FROM location_records lr
    STRAIGHT_JOIN `tabCCD Master` m ON m.name = lr.master_name
    LEFT JOIN current_membership cm ON cm.ccd_master = m.name
    JOIN source_settings ss
      ON ss.source_name = m.ccd_reg_source
     AND ss.include_in_dashboard = 1
     AND ss.environment IN ('Production', 'UAT', 'SIT', 'Development', 'Test')
    WHERE cm.ccd_master IS NULL
),
mapped_presence AS (
    SELECT DISTINCT *
    FROM categorized_presence
    WHERE district_code NOT IN ('UNKNOWN', 'CONFLICT')
)
SELECT
    SHA2(CONCAT('ccd-dashboard-v1|', mp.person_anchor), 256) AS logical_person_key,
    mp.environment,
    mp.service,
    mp.source,
    mp.identity_state,
    mp.district_code,
    mp.district_basis,
    CASE mp.district_code
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
    END AS district,
    CASE
        WHEN mp.district_code IN ('CW', 'EST', 'SOU', 'WC') THEN 'Hong Kong Island / 香港島'
        WHEN mp.district_code IN ('KC', 'KWT', 'SSP', 'WTS', 'YTM') THEN 'Kowloon / 九龍'
        ELSE 'New Territories / 新界'
    END AS area,
    dg.district_polygon
FROM mapped_presence mp
JOIN district_geometry dg ON dg.district_code = mp.district_code

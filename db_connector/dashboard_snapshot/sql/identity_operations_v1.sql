/*
Global aggregate-only identity state.  This dataset intentionally has no
environment/service/source filter columns, preventing dashboard filters from
silently changing governed totals.
*/
SELECT
    'Governed Groups' AS operation_area,
    expected.status AS status,
    'Group count' AS detail,
    COUNT(g.name) AS metric_count,
    NULL AS group_size,
    MAX(g.modified) AS snapshot_at
FROM (
    SELECT 'Active' AS status
    UNION ALL SELECT 'Needs Revalidation'
) expected
LEFT JOIN `tabCCD Identity Group` g ON g.status = expected.status
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
WHERE g.status IN ('Active', 'Needs Revalidation')
GROUP BY g.status, g.active_member_count

UNION ALL

SELECT
    'Memberships',
    im.status,
    'Membership rows',
    COUNT(*),
    NULL,
    MAX(im.modified)
FROM `tabCCD Identity Membership` im
GROUP BY im.status

UNION ALL

SELECT
    'Unified Person Registry',
    person.status,
    'Permanent numbers',
    COUNT(*),
    NULL,
    MAX(person.modified)
FROM `tabCCD Unified Person` person
GROUP BY person.status

UNION ALL

SELECT
    'Unified Person Memberships',
    membership.status,
    'Assignment history',
    COUNT(*),
    NULL,
    MAX(membership.modified)
FROM `tabCCD Unified Person Membership` membership
GROUP BY membership.status

UNION ALL

SELECT
    'Unified Person Aliases',
    alias_row.status,
    'Merge alias history',
    COUNT(*),
    NULL,
    MAX(alias_row.modified)
FROM `tabCCD Unified Person Alias` alias_row
GROUP BY alias_row.status

UNION ALL

SELECT
    'Tiered Recommendations',
    r.status,
    COALESCE(NULLIF(r.model_tier, ''), 'Unclassified tier'),
    COUNT(*),
    NULL,
    MAX(r.modified)
FROM `tabCCD Match Recommendation` r
GROUP BY r.status, COALESCE(NULLIF(r.model_tier, ''), 'Unclassified tier')

UNION ALL

SELECT
    'Splink Review',
    c.review_status,
    CONCAT(COALESCE(NULLIF(c.model_tier, ''), 'Review'),
           CASE WHEN c.stale = 1 THEN ' (stale)' ELSE '' END),
    COUNT(*),
    NULL,
    MAX(c.modified)
FROM `tabCCD Match Review Candidate` c
GROUP BY c.review_status, c.model_tier, c.stale

UNION ALL

SELECT
    'Component Review',
    c.review_status,
    CONCAT(COALESCE(NULLIF(c.materialization_status, ''), 'Not Final'),
           CASE WHEN c.stale = 1 THEN ' (stale)' ELSE '' END),
    COUNT(*),
    NULL,
    MAX(c.modified)
FROM `tabCCD Match Component Review` c
GROUP BY c.review_status, c.materialization_status, c.stale

UNION ALL

SELECT
    'Exceptions',
    e.status,
    'Governed exclusions',
    COUNT(*),
    NULL,
    MAX(e.modified)
FROM `tabCCD Identity Exclusion` e
GROUP BY e.status

UNION ALL

SELECT
    'Overlap Resolution',
    o.status,
    'Resolution records',
    COUNT(*),
    NULL,
    MAX(o.modified)
FROM `tabCCD Identity Overlap Resolution` o
GROUP BY o.status

UNION ALL

SELECT
    'Group Source Composition',
    LEAST(a.ccd_reg_source, b.ccd_reg_source),
    GREATEST(a.ccd_reg_source, b.ccd_reg_source),
    COUNT(DISTINCT im_a.identity_group),
    NULL,
    MAX(GREATEST(im_a.modified, im_b.modified))
FROM `tabCCD Identity Membership` im_a
JOIN `tabCCD Identity Membership` im_b
  ON im_b.identity_group = im_a.identity_group
 AND im_b.ccd_master > im_a.ccd_master
 AND im_b.status = 'Active'
JOIN `tabCCD Identity Group` ig
  ON ig.name = im_a.identity_group
 AND ig.status IN ('Active', 'Needs Revalidation')
JOIN `tabCCD Master` a ON a.name = im_a.ccd_master
JOIN `tabCCD Master` b ON b.name = im_b.ccd_master
WHERE im_a.status = 'Active'
GROUP BY LEAST(a.ccd_reg_source, b.ccd_reg_source),
         GREATEST(a.ccd_reg_source, b.ccd_reg_source)

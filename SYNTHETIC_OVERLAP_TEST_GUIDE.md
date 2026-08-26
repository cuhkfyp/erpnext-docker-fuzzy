# Synthetic Identity-Overlap Test Guide

This guide is for the `frontend` **development site only**. The fixture is
labelled `synthetic-overlap-v1-20260826` and must not be copied to production.

The setup inserts nine clearly identifiable synthetic people into the existing
source-staging DocTypes (`CCD-REG-*` or their legacy equivalent) and into `CCD
Master`. It then creates six pair-scoped canary runs using the installed
`pilot-1.6` snapshot and the normal deterministic-High and safety-gate code.
It does not run a new full-population canary, alter real recommendations, or
create Identity Decisions, Groups, Memberships, or Different exclusions.

All synthetic source keys start with `SYNTH-OVL-20260826-`. The source
configuration remains the existing `CCD Registration`; the fixture does not
create fake source configurations.

## Important test rule

Complete stage 1 for a scenario before touching stage 2. Each scenario uses
three records:

```text
A --phone exact--> B --email exact--> C
```

All three names are equal, but A and C do not share an independent exact field.
That makes A-B and B-C deterministic High without accidentally creating an
A-C High edge.

Keep Materialization off while inspecting and reviewing. Before each step that
will create live identity objects, take a fresh backup, enable Materialization,
perform the one intended Apply/Retry operation, verify the result, and disable
Materialization again.

## Tiered Evidence to Exception

1. Open the `tiered_exception_baseline` canary run.
2. Create a one-component Pilot Wave, review, approve, and apply it. This
   creates the baseline Identity Group for A=B.
3. Open `tiered_exception_later` and its Component Review.
4. Have the required reviewers decide **All Same** for B=C.
5. Preview the combined component. It must expand to A=B=C and show the
   existing group plus the finalized exception decision.
6. Approve and apply the atomic overlap resolution, then verify one active
   three-member group and the superseded earlier decision/group history.

## Tiered Evidence to Tiered Evidence

1. Open the `tiered_tiered_baseline` canary run.
2. Create a one-component Pilot Wave, review, approve, and apply A=B.
3. Open the `tiered_tiered_later` recommendation.
4. Use **Prepare Overlap Resolution Batch**. The ordinary activation preview
   should report `partial_existing_identity_group` for B=C.
5. Preview A=B=C, approve the overlap batch, and apply the atomic result.
6. Verify one active three-member group and complete audit lineage.

## Exception to Exception

1. Open `exception_exception_baseline` and its Component Review.
2. Have the required reviewers decide **All Same**, then materialize A=B.
3. Open `exception_exception_later` and have the required reviewers decide
   **All Same** for B=C.
4. Preview the combined component. It must expand to A=B=C and show both the
   existing group and the later finalized exception decision.
5. Approve and apply the atomic overlap resolution.
6. Verify one active three-member group and both component-review decisions in
   the audit trail.

## Current fixture manifest

Created on the `frontend` development site on 2026-08-26. Routes below are
relative to the ERPNext host.

| Scenario | Stage | Canary run | Recommendation | Component review | CCD Master pair |
|---|---:|---|---|---|---|
| Tiered ↔ Exception | 1 | [c9ar8irjoh](/app/ccd-match-canary-run/c9ar8irjoh) | [c9b77lgpao](/app/ccd-match-recommendation/c9b77lgpao) | — | HKSR0762544 ↔ HKSR0762545 |
| Tiered ↔ Exception | 2 | [c9oeeb89c1](/app/ccd-match-canary-run/c9oeeb89c1) | [c9oaf4d8ms](/app/ccd-match-recommendation/c9oaf4d8ms) | [c9pgvrnb5i](/app/ccd-match-component-review/c9pgvrnb5i) | HKSR0762545 ↔ HKSR0762546 |
| Tiered ↔ Tiered | 1 | [c9rmhiqd5t](/app/ccd-match-canary-run/c9rmhiqd5t) | [c9r753i0af](/app/ccd-match-recommendation/c9r753i0af) | — | HKSR0762547 ↔ HKSR0762548 |
| Tiered ↔ Tiered | 2 | [c9rmcrqsli](/app/ccd-match-canary-run/c9rmcrqsli) | [c9r0h2l7eg](/app/ccd-match-recommendation/c9r0h2l7eg) | — | HKSR0762548 ↔ HKSR0762549 |
| Exception ↔ Exception | 1 | [c9snichuql](/app/ccd-match-canary-run/c9snichuql) | [c9s7b80glp](/app/ccd-match-recommendation/c9s7b80glp) | [c9sa51sbfi](/app/ccd-match-component-review/c9sa51sbfi) | HKSR0762550 ↔ HKSR0762551 |
| Exception ↔ Exception | 2 | [c9s2qgraea](/app/ccd-match-canary-run/c9s2qgraea) | [c9s0sqm6f8](/app/ccd-match-recommendation/c9s0sqm6f8) | [c9s1cprvlp](/app/ccd-match-component-review/c9s1cprvlp) | HKSR0762551 ↔ HKSR0762552 |

Synthetic CCD Master records:

| Label | CCD Master | Existing CCD Registration source | Source-staging row |
|---|---|---|---|
| TE_A | HKSR0762544 | HQ-vDB01_HMSSHP_Prod | c8npa82mgc |
| TE_B | HKSR0762545 | HQ-vDB01_DHCE_Prod | c91e5cg1au |
| TE_C | HKSR0762546 | PHI-vDBUAT_HMSPhi_SIT | c920f39u0h |
| TT_A | HKSR0762547 | HQ-vDB01_DHCE_Prod | c937d83ocv |
| TT_B | HKSR0762548 | HQ-vDB01_HMSSHP_Prod | c93d73d9vo |
| TT_C | HKSR0762549 | HQ-vDB01_HKSReCCMS_PROD | c93fkl1fs8 |
| EE_A | HKSR0762550 | PHI-vDBUAT_HMSPhi_SIT | c94t1b8ms6 |
| EE_B | HKSR0762551 | HQ-vDB01_DHCE_Prod | c94473srg6 |
| EE_C | HKSR0762552 | PHI-vDBUAT_HMSPhi_UAT | c952hn6b29 |

Pre-fixture recovery point:

- `20260826_131824-frontend-database.sql.gz`
- `20260826_131824-frontend-files.tgz`
- `20260826_131824-frontend-private-files.tgz`
- `20260826_131824-frontend-site_config_backup.json`

## Re-running and cleanup

The setup is idempotent: it reuses rows with the same synthetic source keys and
reuses its labelled canary runs. It refuses to continue if an existing fixture
record has different evidence.

Once a test has created identity audit objects, do not delete individual rows
to "clean up" the history. Keep the development-only audit records, or restore
the fresh pre-fixture backup if the whole fixture must be removed.

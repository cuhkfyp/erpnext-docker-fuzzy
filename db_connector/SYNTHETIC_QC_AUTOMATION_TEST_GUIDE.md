# Synthetic QC and Tiered Automation Acceptance Guide

## Purpose and boundary

This development-only fixture provides six isolated deterministic-High
components in one dedicated canary. It is for accepting continuous QC,
circuit-breaker, masking, and bounded Automatic Tiered behavior without adding
test decisions to canary `p1mucmhogd`.

Fixture creation writes only synthetic staging records, synthetic `CCD Master`
records, one Canary, six Proposed Recommendations, and their audit metadata. It
creates no Identity Decision, Group, Membership, or Exclusion. Later acceptance
steps do create reversible identity objects for these clearly labelled
synthetic records.

The six pairs all represent the same person within each pair. Therefore, when a
reviewer deliberately labels one `Different` to exercise the breaker, resolve
the investigation as **QC Review Error**. Choosing **Relationship Corrected**
records a genuine policy failure and correctly prevents the same Pilot policy
from being reauthorized.

## 1. Safe starting state and backup

Confirm in **CCD Identity Resolution Settings**:

```text
Live Identity Materialization Enabled = off
Automatic Tiered Materialization Enabled = off
Automatic QC Assignment Enabled = off
Tiered Automation Paused = off
```

Take and verify a fresh development backup before creating the fixture. Then
create it from the backend container:

```bash
bench --site frontend execute \
  db_connector.synthetic_qc_automation_fixture.prepare_synthetic_qc_automation_fixture \
  --kwargs "{'confirm':'CREATE DEVELOPMENT SYNTHETIC QC AUTOMATION FIXTURE'}"
```

The output gives the Canary ID, six Recommendation IDs, and twelve synthetic
CCD Master IDs. Re-running the command is idempotent: it inspects the existing
fixture rather than duplicating it.

Verify the untouched structure:

```bash
bench --site frontend execute \
  db_connector.synthetic_qc_automation_fixture.verify_synthetic_qc_automation_fixture \
  --kwargs "{'expect_pristine':1}"
```

The expected initial state is six Proposed/Available complete components,
three selected-but-unassigned QC cases, three eligible unselected cases, and no
Membership involving a fixture record.

### Current development fixture checkpoint (2026-08-28)

The full backup completed at 17:30 UTC under
`sites/frontend/private/backups/20260828_172630-frontend-*`. The idempotent
fixture command then created Canary `o2c67pgdv9` and passed pristine
verification. Identity object totals remained `60 Decisions / 56 Groups / 136
Memberships / 33 Exclusions / 381 Events`; all three write-control switches
remained off.

| Pair | Recommendation | CCD Master records | Initially selected for QC |
| --- | --- | --- | --- |
| P01 | `o2d9m3ndmj` | `HKSR0762573`, `HKSR0762574` | Yes |
| P02 | `o2ed28s5v3` | `HKSR0762575`, `HKSR0762576` | Yes |
| P03 | `o2e0c2g3r4` | `HKSR0762577`, `HKSR0762578` | Yes |
| P04 | `o2foiqsla2` | `HKSR0762579`, `HKSR0762580` | No |
| P05 | `o2ftcsdsre` | `HKSR0762581`, `HKSR0762582` | No |
| P06 | `o2f11mfeca` | `HKSR0762583`, `HKSR0762584` | No |

This table is an operator checkpoint, not authorization to enable an automatic
control. Continue at section 2.

## 2. Configure while both automatic controls are off

In **CCD Identity Resolution Settings**, select:

- **Authorized Tiered Canary**: the fixture Canary;
- **Authorized Matching Policy**: `pilot-1.6`;
- **Maximum Components per Automatic Run**: `2`;
- **QC Cases per Week**: `2`;
- **QC Assignment Interval Days**: `7`;
- **Rolling QC Window**: `100`;
- **QC SLA Days**: `14`.

Save before enabling either automatic control. Governed configuration fields
cannot be changed while Automatic QC or Automatic Tiered is enabled. A rolling
window below 73 is rejected because it cannot achieve a 95% Wilson lower bound
even with all-correct results.

## 3. Assignment boundary, two-reviewer logic, and masking

Open the fixture Canary and click **Assign Next QC Cases**, count `2`.

Expected results:

- exactly two cases receive `QC Assigned At` and `QC Due At`;
- `QC Due At` is 14 days after release;
- the assignment-cycle count increments and an immutable `QC Assign` Identity
  Event is created;
- **Review Assigned QC** lists released cases only; and
- direct submission on a selected but unassigned case is rejected.

Use two different `CCD Match Reviewer` accounts on one assigned case and label
it `Same`. The first decision produces `Partially Reviewed`; the matching second
decision produces `Agreed / Same`. Verify that an ordinary reviewer sees masked
identity evidence and no CCD Master links, while a Sensitive Reviewer or System
Manager sees only the full values their role permits.

Completed QC evidence is immutable. A later CCD Master edit does not erase a
completed result from the rolling history; only unfinished cases can become
stale and be replenished.

## 4. Zero-write preview and bounded Automatic Tiered

Before enabling writes, click **Preview Automatic Tiered Run**. With
Materialization off, the preview must say `No records were written`, select no
more than two safe components, and report `master_materialization_disabled`.

For the controlled write test:

1. take a fresh backup checkpoint;
2. enable **Live Identity Materialization**;
3. use **Enable Automatic QC Assignment** with a reason and exact Settings ID;
4. use **Enable Automatic Tiered** with a reason and exact Settings ID;
5. click **Run One Automatic Cycle Now**; and
6. immediately inspect the resulting `Automatic Tiered` Activation Batch,
   Identity Decisions, Groups, Memberships, authorization Event, and control
   revision.

One cycle may apply at most two safe complete components. Held, stale, unsafe,
and overlapping components are skipped; no partial component is allowed. The
worker privately creates, approves, rechecks, and applies its batch. The normal
Activation Batch **Apply** and **Revalidate for Retry** buttons are unavailable
for automatic batches, and their public APIs reject that bypass.

Run one more cycle to prove that already applied components are not duplicated.
Each successful cycle advances to other Proposed components; after all eligible
components are applied the result is `No Eligible Components`, not duplicate
Groups or Memberships.

Use **Stop Automatic Tiered** whenever the unattended write test is not actively
being performed. Turning Materialization off is also an immediate write gate,
but re-enabling it while Automatic Tiered remains armed is rejected.

## 5. QC Different and current-group revalidation

Choose an assigned fixture Recommendation that has been applied, and have two
independent reviewers label it `Different`.

Expected immediate effects:

- the global Tiered circuit breaker changes to Paused;
- one open `CCD Identity QC Investigation` is created;
- only the current active Identity Group shared by the two endpoints changes to
  `Needs Revalidation`;
- every current Membership in that one Group changes to `Needs Revalidation`;
- historical/ended Groups are not changed; and
- an automatic cycle reports blockers and creates no identity objects.

Because the fixture pair is intentionally the same person, stop Automatic
Tiered, open the Investigation, and choose **Resolve Governed Investigation →
QC Review Error** with detailed notes and the exact Investigation ID. This
reactivates the affected Group/Memberships, retains immutable QC and failure
history, and excludes only that adjudged review mistake from rolling precision.

Then use **Preview Governed Resume**. Resume is allowed only after every open
investigation, overdue case, precision failure, and genuine unresolved policy
failure is cleared. The resume itself creates an immutable Event; it does not
automatically re-enable Automatic Tiered.

## 6. Replenishment and scheduled cadence

After the initially assigned cases are complete, assign two more. One remaining
preselected case is released and one previously unselected eligible case is
deterministically added to the continuous QC pool. The Canary's **Continuous QC
Cases Added** count increases.

To exercise the daily cadence without waiting seven days, this bench-only
fixture helper moves only the synthetic Canary's next assignment time into the
past:

```bash
bench --site frontend execute \
  db_connector.synthetic_qc_automation_fixture.make_synthetic_qc_cadence_due \
  --kwargs "{'confirm':'MAKE DEVELOPMENT SYNTHETIC QC CADENCE DUE'}"
bench --site frontend execute db_connector.api_identity_qc.run_qc_monitor
```

With Automatic QC enabled, the monitor releases the configured bounded count
and advances the next cadence. With Automatic QC disabled, it releases zero new
cases. The daily safety monitor still checks already assigned work; stopping
assignment does not erase existing SLAs or QC history.

## 7. Overdue safety test

Select one open assigned fixture Recommendation and run:

```bash
bench --site frontend execute \
  db_connector.synthetic_qc_automation_fixture.make_synthetic_qc_case_overdue \
  --kwargs "{'recommendation_name':'<fixture-recommendation>',\
'confirm':'MAKE DEVELOPMENT SYNTHETIC QC CASE OVERDUE'}"
bench --site frontend execute db_connector.api_identity_qc.run_qc_monitor
```

Expected: the Canary reports one overdue QC case, the circuit breaker pauses,
and Automatic Tiered writes remain blocked. Complete/adjudicate that QC case,
resolve any investigation if one is opened by its final result, stop Automatic
Tiered, and use the governed resume preview/action. Once every assigned case is
finalized, it cannot become overdue later; a missing future assignment creates
no SLA by itself.

## 8. Staleness and idempotency

For an unfinished assigned synthetic case only, edit one governed identity
field on one participating synthetic CCD Master. The next submission or monitor
must mark the case `Stale`; it must not accept the old snapshot. A later
assignment replenishes from another eligible Recommendation.

Finally record before/after counts for Decisions, Groups, Memberships,
Exclusions, Events, Activation Batches, QC Investigations, assignment cycles,
and replenished cases. Repeated previews must remain zero-write, and an exact
retry must never duplicate identity objects or audit events.

## 9. Production boundary

Passing this fixture is development acceptance only. It does not authorize
production Materialization or Automatic Tiered. Production requires a fresh
backup, target-server migration rehearsal, named responsible reviewers,
management authorization for the policy/canary/limits, and a separately
recorded decision to enable both automatic controls.

# CCD Identity Resolution Implementation Status

## Safe takeover checkpoint

| Item | Verified state |
| --- | --- |
| Date | 2026-08-25 UTC |
| Site | `frontend` |
| Takeover basis | Recovered local predecessor session and its committed specification |
| Specification | `IDENTITY_RESOLUTION_WORKFLOW_PLAN.md` |
| Deployment | Schema, services, controllers, managed CCD Master Client Script, and frontend assets deployed |
| Materialization | Disabled by default (`materialization_enabled = 0`) |
| Automation circuit breaker | Not tripped (`automation_paused = 0`) |
| Live identity writes | Development testing: 33 Decisions (27 active / 6 superseded), 30 Groups (25 active / 5 ended), 68 Memberships (58 active / 10 ended), and 9 active Exclusions |

The predecessor session completed the workflow specification and pushed it at
commit `cfef788`. This fresh session recovered that durable artifact, audited
the application and live data, implemented the specification, deployed it in a
default-off state, and verified the result. It did not depend on reconstructing
the predecessor's compacted conversational reasoning.

On 2026-08-24, explicitly bounded Tiered, human-component, and Splink
development decisions were applied after verified backup checkpoints.
Materialization was turned off again immediately afterward. The waves created
reversible identity objects only; they did not merge or modify the participating
CCD Master source documents.

On 2026-08-25, the unified overlap resolver was deployed and tested with
Materialization still off. Its live tests used transaction rollback; they did
not add an Overlap Resolution, Decision, Group, Membership, Exclusion, or Event.

## Implemented controls

- Immutable `CCD Identity Decision` records with policy, model, human-review,
  fingerprint, safety, and provenance fields.
- Reversible `CCD Identity Group` and `CCD Identity Membership` records; CCD
  Master source documents remain unchanged.
- Fingerprint-scoped `CCD Identity Exclusion` records for final Different
  outcomes, plus append-only identity events.
- One idempotent, lock-protected materialization service shared by Tiered
  Evidence, Splink human review, and exception-component decisions.
- Fresh fingerprint, complete-HKID conflict, stale-input, exclusion, and
  component-partition gates before writes.
- Complete frozen-snapshot enforcement: every governed materialization now
  requires both the frozen identity fingerprint and frozen `modified` value for
  every participant, and rechecks both after acquiring record locks.
- A timestamp-guarded legacy repair backfilled all 15,138 August-13 snapshot
  rows (3,961 Tiered recommendations and 11,177 Splink candidates) using their
  verified frozen `pilot-1.6` policy. No stale, corrupt, or inconsistent row was
  accepted, and the repair is idempotent.
- Component-atomic activation batches with zero-write preview, explicit
  review/approval/application, deliberate hold, and release.
- A protected **Review Pair(s)** action on every frozen Activation Batch item,
  showing source pair(s), permitted CCD Master/Recommendation links, current
  evidence, and stale status without duplicating raw identity values into the
  batch document.
- Optional non-zero Splink Review Batches. Creating no batch correctly leaves
  assigned work at zero.
- Asynchronous QC sampling, rolling Wilson precision monitoring, overdue-case
  checks, investigations, revalidation state, and an automation circuit
  breaker.
- Permission-masked identity fields and an Identity Resolution tab on CCD
  Master. Legacy fuzzy fields are retained but are not written by this system.
- Dynamic ungrouped-state wording: a fingerprint-current Different decision is
  shown as **Resolved Separately**, a record with neither membership nor a
  current Different decision is **Not Grouped**, and an active Membership takes
  display precedence if a later approved link is created. No state implies a
  physical CCD Master merge.
- A System Manager bulk action on the Component Review list: select the exact
  Pending/Exception rows, run a zero-write safety preview, and atomically
  materialize 1–25 complete components in one operation.
- The equivalent bounded action on the Splink Review Candidate list: select
  exactly 1–25 finalized Pending/Exception decisions, preview every planned
  object, reject overlapping participants, and apply the complete set
  atomically.
- A deliberately narrow false-Same correction on an applied two-record Splink
  candidate. It is System-Manager-only in both the form and server APIs,
  requires Materialization off, a zero-write preview, mandatory reason, and
  exact candidate-ID confirmation, then atomically ends the two Memberships and
  Group, creates a fingerprint-scoped Different decision/exclusion, supersedes
  the old Same Decision, and preserves the original reviews and audit history.
- A general System-Manager-only **Complete Identity Component Correction** for
  applied Tiered Evidence, Component Review, Splink, and earlier Governance
  Override decisions. It expands through every affected live group, freezes a
  2–25-record scope, accepts an exact replacement partition, produces a
  zero-write preview, requires Materialization off/reason/exact-ID confirmation,
  and atomically ends or supersedes all affected relationship objects before
  creating the versioned replacement. Each application creates an immutable
  `CCD Identity Correction`; no CCD Master record is edited or merged.
- A unified System-Manager-only **Combined Identity Component** workflow for a
  finalized pending Splink decision, Exception Component Review, or approved
  Tiered Activation Item that overlaps existing identity state. Its zero-write
  preview recursively includes complete active Groups, applicable active
  Different exclusions, and all connected finalized pending sources across the
  three routes. Unreviewed work remains adjacent evidence only. The operator
  chooses All Same, All Different, or one complete Partial partition; Apply is
  one lock-protected transaction with a frozen scope fingerprint and immutable
  `CCD Identity Overlap Resolution` audit. A true already-represented result
  records No Change without creating identity objects. Changed results require
  Materialization enabled, while a tripped QC circuit breaker fails closed.
- Tiered structural overlaps are reachable without weakening ordinary batch
  safety: Preview Approve All links each unsafe component to a Recommendation;
  a manager may freeze exactly one structurally overlapping component into an
  **Overlap Resolution** batch, review/approve it, and use the Exception item's
  Resolve Overlap action. Stale or non-structural unsafe components remain
  rejected, and ordinary batch Apply refuses unresolved Exception items.
- Corrected Component Review evidence now separates the immutable reviewer
  outcome from the live identity state. The original decision is labelled
  **Original reviewed grouping (historical)**, while the latest decision in its
  supersession chain is shown as **Current effective identity result**, with
  distinct links to the original Decision, correction audit, and active
  replacement Decision. Out-of-component identities are counted without
  exposing their record IDs to reviewers who lack sensitive access.
- An idempotently managed **CCD Master Identity Resolution** Form Client Script
  for the current custom CCD Master DocType. Frappe skips `doctype_js` hooks for
  custom DocTypes, so checking the form metadata is a mandatory deployment
  acceptance test.
- Recommendation lifecycle vocabulary migrated to
  Proposed/Approved/Exception/Withdrawn/Superseded without changing the live
  recommendation population.

## Verification evidence

### Automated checks

- All new and modified DocType JSON files parse successfully.
- Python compilation succeeds.
- The complete test suite passes: 74 tests, 0 failures, including transitive
  overlap partition, Different-constraint conflict, and active-group split
  detection tests.
- Frappe migration, role/policy setup, workflow installation, asset build,
  cache clear, and service restart completed.
- The frontend-local public renderer is served with HTTP 200, and live CCD
  Master FormMeta contains `load_identity_resolution` through the managed
  Client Script.
- All backend, scheduler, and queue containers are running; two workers are
  online.
- Re-running setup is designed to be idempotent.
- The deployed combined preview was exercised against finalized Splink
  candidate `d41a94b39e` inside an isolated rollback-only transaction. It
  expanded the complete active two-record Group, included the one authoritative
  pending source, displayed seven adjacent unreviewed sources without absorbing
  them, recognized the final state as Already Represented, and left all live
  object counts unchanged. The candidate returned to `Applied` afterward.
- A real non-manager Reviewer received `System Manager role is required` from
  the combined-preview API. The changed-partition Apply path was rejected while
  Materialization was off. With commit suppressed, the Already Represented path
  transiently created exactly one `No Change` overlap audit and one Event, no
  identity objects, then a full rollback restored every count and source status.
- Tiered recommendation `roocvcovtr` exercised the dedicated entry route with
  commit suppressed. It transiently produced one Reviewed **Overlap
  Resolution** batch and one Exception item carrying
  `partial_existing_identity_group`; normal approval exposed a safe zero-write
  three-record combined preview, ordinary batch Apply was refused, and rollback
  restored both batch/item counts. No batch or identity object from the probe
  remains.
- Live API checks prove all three identity-view branches: a partial-match
  singleton returns `Resolved Separately` with three current exclusions, its
  linked pair member returns `Linked` despite also participating in exclusions,
  and an untouched record returns `Not Grouped`.
- The Component Review and Splink Review Candidate list hooks and frontend
  assets are deployed. Their bulk APIs reject ineligible rows without writing
  and report the global switch state.
- The two-record Splink false-Same correction schema and form are deployed.
  Candidate `8ac22119c8` passed the zero-write eligibility preview with exactly
  one active Group and two active Memberships while Materialization was off.
  A real non-manager Reviewer received `System Manager role is required` from
  the preview API; that same reviewer's evidence payload reported
  `can_reverse_materialization = false` and omitted the manager-only correction
  Decision field. A later authorized development exercise successfully reversed
  one candidate; its old Group/two Memberships and Same Decision remain as
  ended/superseded history and its replacement Different exclusion is active.
- The complete-component correction schema, manager UI, and three server APIs
  are deployed. Zero-write live previews succeeded for Tiered decision
  `0t7kj3mkhn` (`Same → Different`, two records) and Component Review decision
  `vknlnno1mu` (`All Same → Partial`, three records). A same-source replacement
  raised the explicit governance-warning confirmation, and a real non-manager
  reviewer received `System Manager role is required`. These previews created
  zero `CCD Identity Correction` records and changed no identity relationships.
- The full Apply lifecycle was then exercised for both those Tiered and
  Component Review replacements with commit temporarily disabled inside an
  isolated development transaction. Tiered produced the expected Different
  result (1 Group / 2 Memberships ended; 1 exclusion planned); Component Review
  produced the expected Partial result (1 Group / 3 Memberships ended; 1 new
  Group / 2 Memberships / 2 exclusions planned). Each transaction was explicitly
  rolled back; temporary correction/decision names do not exist, both original
  Decisions and Groups remain Active, and the Event count remains 96.
- Corrected Component Review `l30evokvod` was used to verify the historical
  versus current renderer. Its immutable reviewed result remains Partial
  (`R1 = R3; R2 separate`), while its active Governance Override correctly
  displays All Same (`R1 = R2 = R3`) and links to both the correction audit and
  current Decision.
- Splink candidate `ed9e2a25c4` is now `Applied`. This bulk-workflow deployment
  performed no identity writes. The remaining finalized candidate `8ac22119c8`
  passes the new zero-write bulk preview with two frozen fingerprints, two
  matching timestamps, zero conflicts, one planned Group, and two planned
  Memberships while Materialization is off.
- Current **Preview Approve All** covers 3,511 remaining complete Tiered
  components / 3,519 recommendations. It reports 3,510 safe, one unsafe, zero
  stale, and 7,024 planned Memberships; the one conflict is
  `partial_existing_identity_group` from the already identified Splink/Tiered
  overlap and is not eligible for a batch.

### Live read-only state

| Measure | Result |
| --- | ---: |
| Frozen Tiered recommendations | 3,961 |
| Proposed | 3,517 |
| Exception | 433 |
| Approved | 10 |
| Superseded recommendations | 1 |
| Splink candidates available | 11,177 |
| Splink candidates assigned | 0 |
| Component reviews | 191 |
| Identity decisions | 33 total (27 active / 6 superseded) |
| Identity groups | 30 total (25 active / 5 ended) |
| Identity memberships | 68 total (58 active / 10 ended) |
| Identity exclusions/events | 9 active exclusions / 163 events |
| Complete identity corrections | 4 total (3 applied / 1 superseded) |
| Combined overlap resolutions | 0 |
| Activation batches | 4 (`Applied`; 10 Applied and 1 Corrected items) |
| Finalized Component Reviews | 9 (7 Applied / 2 Corrected) |
| Applied Splink candidates | 5 |
| Reversed Splink candidates | 2 |
| Review batches | 0 |
| QC investigations | 0 |

### Zero-write activation preview

For canary `p1mucmhogd`, **Preview Approve All** returned:

| Measure | Result |
| --- | ---: |
| Selected complete components | 3,509 |
| Selected recommendations | 3,517 |
| Safe components | 3,507 |
| Unsafe components | 2 (`partial_existing_identity_group`) |
| Stale components | 0 |
| Planned identity groups | 3,507 |
| Planned memberships | 7,018 |
| Conflict counts | 2 |

The preview made zero writes. Three separately approved development batches
have applied nine Tiered components in total; they created the live counts
recorded above but did not change the zero-write meaning of Preview.

The 3,509 selected components are 3,505 two-record components containing one
recommendation each and four three-record components containing three
recommendations each. Thus the eight-count difference between recommendations
and components is an edge-count difference. The two unsafe components are
excluded from planned writes, leaving 7,018 planned member records.

## Next controlled decision

The development pilot does not authorize a production rollout or wider wave.
Before any further activation:

1. complete browser acceptance for Linked, Resolved Separately, and Not Grouped
   CCD Master records;
2. verify the twenty-five active Groups, fifty-eight active Memberships, nine
   active Exclusions, 163 Events,
   masking, QC assignments, idempotent re-Apply behavior, and correction path;
3. demonstrate the bounded pilot and obtain explicit management authorization;
4. create and inspect a new component-atomic batch for the authorized scope;
5. take a fresh full backup immediately before that next write window; and
6. enable Materialization only long enough to Apply the one approved batch,
   then disable it and repeat post-write verification.

Materialization is currently off. Existing pilot links remain visible and
reversible, but no new identity links can be created until a separately
authorized route temporarily enables the global switch.

Server transfer, clean-target installation, backup/restore, cutover, rollback,
and new-centre onboarding are covered in
`ERPNext_SERVER_MIGRATION_RUNBOOK.md`. Identity activation remains a separate
operation after a successful transfer.

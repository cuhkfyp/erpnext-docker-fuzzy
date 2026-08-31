# CCD Identity Resolution Implementation Status

## Safe takeover checkpoint

| Item | Verified state |
| --- | --- |
| Date | 2026-08-31 UTC |
| Status updated | 2026-08-31 UTC |
| Site | `frontend` |
| Takeover basis | Recovered local predecessor session and its committed specification |
| Specification | `IDENTITY_RESOLUTION_WORKFLOW_PLAN.md` |
| Deployment | Schema, services, controllers, managed CCD Master Form/List Client Scripts, and frontend assets deployed |
| Materialization | Disabled by default (`materialization_enabled = 0`) |
| Automation circuit breaker | Not tripped (`automation_paused = 0`) |
| Automatic QC / Tiered | Both separately controlled and disabled (`automatic_qc_assignment_enabled = 0`, `automatic_tiered_enabled = 0`) |
| 2026-08-25 identity-write snapshot | Development testing: 33 Decisions (27 active / 6 superseded), 30 Groups (25 active / 5 ended), 68 Memberships (58 active / 10 ended), and 9 active Exclusions |
| 2026-08-31 current totals | 66 Decisions (44 Active / 22 Superseded), 62 Groups (40 Active / 22 Ended), 148 Memberships (96 Active / 52 Ended), 33 Exclusions (22 Active / 11 Superseded), 429 Events, 15 Activation Batches (14 Applied / 1 Reviewed), and 1 resolved QC Investigation |
| Overlap acceptance | Completed on the development site; all six route combinations, all result modes, stale safety, active-Different override, two-group bridging, and two applied-overlap corrections passed |
| QC / automation acceptance | Completed on the development site; masking, independent review, bounded automatic writes, QC Different recovery, replenishment/cadence, overdue safety, staleness/revalidation, scheduler execution, and idempotency passed |

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

On 2026-08-28, continuous QC governance and bounded Automatic Tiered were
deployed with Materialization, Automatic QC, and Automatic Tiered all off. The
migration added schema and controls only; the identity-object totals remained
exactly unchanged and `surfshark-wireguard` remained healthy throughout. After
a full backup, the development-only QC/automation fixture was created as Canary
`o2c67pgdv9`: six isolated Proposed/Available Tiered components, three initially
selected but unassigned for QC and three eligible for cadence replenishment.
Fixture creation left the identity-object totals unchanged.

On 2026-08-31, the fixture completed its controlled browser and scheduler
acceptance. Two bounded Automatic Tiered cycles applied four isolated complete
components; the QC workflow then exercised masked independent review, a
deliberate Different result and governed investigation recovery, deterministic
pool replenishment, the seven-day cadence gate, an overdue-SLA pause, source
staleness, Membership/Group revalidation, governed Resume, and exact-repeat
idempotency. The acceptance checkpoint ended with 64 Decisions, 60 Groups, 144
Memberships, 33 Exclusions, 416 Events, 14 Activation Batches (13 Applied / 1
Reviewed), and one resolved QC Investigation. Later manual development
demonstrations at 16:18 and 16:26 created two additional Same Decisions,
Groups, and four Memberships;
the larger current totals in the table above therefore are not attributed to
the QC scheduler.

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
- Continuous QC release, SLA/cadence, deterministic replenishment, finalization-
  ordered rolling Wilson precision, overdue checks, immutable investigations,
  current-shared-Group revalidation, and a global Tiered circuit breaker.
- Separate default-off, manager-only and audited controls for Automatic QC and
  bounded Automatic Tiered. Each automatic batch freezes its authorization
  revision/Event, skips unsafe complete components, and rechecks every control
  under lock; ordinary batch Apply cannot apply an automatic batch.
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
  finalized pending Splink decision, Exception Component Review, or reviewed/
  approved Tiered Activation Item that overlaps existing identity state. Its
  zero-write preview recursively includes complete active Groups, applicable active
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
  **Overlap Resolution** batch and use the Exception item's **Preview / Resolve
  Overlap** action while the batch is still Reviewed. The preview explicitly
  identifies the pending records, existing Identity Group, shared bridge
  record, current members/Decision, active Different exclusions, and displays
  every complete-scope record side by side near the top. Compact governed
  values and the original Recommendation ID are also embedded in the overlap
  rows, so linked documents are optional audit navigation. Reviewed batches are preview-only;
  Apply remains server-blocked until explicit approval. Stale or non-structural
  unsafe components remain rejected, and ordinary batch Apply refuses
  unresolved Exception items.
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
- A read-only **CCD Identity Resolution Register** Script Report and an
  idempotently managed **CCD Master Identity Resolution List** Client Script.
  The CCD Master List button opens server-backed filters for current identity
  state, CCD/source/group, Group status and member count, and current Different
  relationships. The register recomputes fingerprint-scoped current state from
  the governed identity objects; it deliberately stores no derived fields on
  CCD Master, so using it cannot modify source records or stale frozen matching
  snapshots.
- Recommendation lifecycle vocabulary migrated to
  Proposed/Approved/Exception/Withdrawn/Superseded without changing the live
  recommendation population.

## Verification evidence

### Automated checks

- All new and modified DocType JSON files parse successfully.
- Python compilation succeeds.
- The changed automation/identity/overlap/correction suite passes 24/24 in both
  the workspace and deployed Frappe container. In broad container discovery,
  62 tests passed; two older import-only test classes could not initialize
  because the unrelated private-app `__init__.py` now imports `api_ai` while
  those legacy tests replace Frappe with a minimal stub. Product imports,
  migration, and runtime startup succeeded; the legacy test harness remains a
  separate cleanup item rather than being hidden as a passing full suite.
- Frappe migration, role/policy setup, workflow installation, asset build,
  cache clear, and service restart completed.
- The frontend-local public renderer is served with HTTP 200, and live CCD
  Master FormMeta contains `load_identity_resolution` through the managed
  Client Script.
- All backend, scheduler, and queue containers are running; two workers are
  online.
- Re-running setup is designed to be idempotent.
- The 2026-08-28 zero-write Automatic Tiered preview returned both automatic
  controls off, Materialization off, no authorized Canary/Policy, zero selected
  components, zero planned identity objects, and `would_write_now = false`.
  Decision/Group/Membership/Exclusion/Event counts remained
  `60/56/136/33/381` afterward.
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
- On 2026-08-27 the deployed **CCD Identity Resolution Register** returned 99
  current resolved CCD Masters: 84 `Linked`, zero `Needs Revalidation`, and 15
  fingerprint-current `Resolved Separately`. Live checks also exercised the
  Linked/minimum-group-size and Separate/active-Different filters, confirmed
  the standard Report and enabled custom-DocType List Client Script, and found
  both Materialization and the automation circuit breaker at `0`. The report
  returned the same governed result for an existing CCD Match Reviewer while
  rejecting Guest with `CCD Match Reviewer role is required`. The deployment
  did not update any CCD Master document.
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

### Historical pre-overlap read-only state (2026-08-25 snapshot)

This table is the immutable pre-overlap acceptance baseline, not a current
inventory. Later controlled development tests intentionally added overlap and
correction audits; their exact evidence is recorded in the following matrix.

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
| Activation batches | 5 (4 Applied / 1 Reviewed; 10 Applied, 1 Corrected, and 1 Exception items) |
| Finalized Component Reviews | 9 (7 Applied / 2 Corrected) |
| Applied Splink candidates | 5 |
| Reversed Splink candidates | 2 |
| Review batches | 0 |
| QC investigations | 0 |

### Completed overlap acceptance matrix — development

The following browser tests were completed on 2026-08-26 and 2026-08-27 using
the deployed workflows. Each successful write has an immutable **CCD Identity
Overlap Resolution** audit. IDs in the tables are retained so another operator
can reproduce the audit trail without relying on this conversation. The three
synthetic route pairs use the isolated records documented in
`SYNTHETIC_OVERLAP_TEST_GUIDE.md`; they are development evidence, not production
identity assertions.

#### Route-combination coverage

| Route combination | Applied source sequence | Overlap Resolution | Observed result | Acceptance |
| --- | --- | --- | --- | --- |
| Tiered Evidence ↔ Splink | Splink candidate `9209ece5f3`, then Tiered recommendation `rsuqpl6o3l` / Activation Item `sg3e2q8s90` | `66qehmis6i` | The complete three-record scope replaced the touched two-member Splink Group atomically | Pass |
| Tiered Evidence ↔ Exception Component | Canary `c9ar8irjoh`, then Component Review `c9pgvrnb5i` | `cd9u47trjp` | The synthetic three-record combined scope was applied as one Group | Pass |
| Tiered Evidence ↔ Tiered Evidence | Canary `c9rmhiqd5t`, then recommendation `c9r0h2l7eg` | `6jf2eq47pg` | The later synthetic Tiered edge expanded and replaced the earlier two-record Group atomically | Pass |
| Splink ↔ Splink | Candidate `0c9c266414`, then candidate `f8a4546e22` | `618ri00drh` | The shared-record bridge produced the expected complete three-member Group | Pass |
| Splink ↔ Exception Component | Candidate `4f4117525a`, then Component Review `l2s0d4o677` | `h3d9b677fb` | The complete four-record scope replaced the touched Splink Group atomically | Pass |
| Exception Component ↔ Exception Component | Component Review `c9sa51sbfi`, then Component Review `c9s1cprvlp` | `gq809ba7gn` | The synthetic shared-record bridge produced the expected complete three-member Group | Pass |

This covers the complete unordered route cross-product: each route against
itself and each of the three pairwise cross-route combinations.

#### Result-mode and safety coverage

| Behaviour | Test source / immutable audit | Observed result | Acceptance |
| --- | --- | --- | --- |
| Already Represented / No Change | Candidate `8acbcca1ee`; Overlap Resolution `fpeumc18r9` | Status `No Change`; no Decision, Group, Membership, or Exclusion was created or ended | Pass |
| Partial Match | Existing candidate `007bb1c300`, then candidate `609cd5fe5e`; Overlap Resolution `4gk9d6et1i` | One two-record Group remained active, the third record remained separate, and two cross-partition Different exclusions were created | Pass |
| All Different | Existing candidate `00115bf214`, then candidate `a548d9e68c`; Overlap Resolution `opumkjd37b` | The old Group and two Memberships ended; all three records became singletons with three pairwise Different exclusions | Pass |
| Bridge two active Groups | Groups `0t7rirlenu` and `vknjkjc60g` through false bridge candidate `cc8e335528`; Overlap Resolution `m2tq57q3pn` | Both complete Groups were absorbed into one five-member result; no partial Group was left behind | Pass |
| Override active Different | Candidate `ca5fc0b764`; Overlap Resolution `ohgcu05pg2` | Six active cross-group exclusions were explicitly superseded before one five-member Same Group was created | Pass |
| Stale after preview | Candidate `fe4cfb9294` | Apply failed closed with both `source_modified_after_snapshot` and `identity_fingerprint_changed`; no Overlap Resolution or identity write was created | Pass |
| Correct an applied overlap — split bridge back | Correction `1o8s384r8t` against the decision from `m2tq57q3pn` | The temporary five-member bridge Group ended and the original two complete Groups were recreated with cross-group exclusions | Pass |
| Correct an applied overlap — restore Different partition | Correction `pitqlh34sk` against the decision from `ohgcu05pg2` | The temporary five-member Group ended and the prior two-group partition plus six exclusions was recreated | Pass |

The `Superseded` status now shown on overlap audits `m2tq57q3pn` and
`ohgcu05pg2` is the expected result of the two successful correction tests, not
a failed overlap application. Together these tests accept All Same, Partial
Match, All Different, and Already Represented/No Change; complete-group
bridging; explicit Different override; stale-snapshot/fingerprint rejection;
and correction of an applied overlap. This completes functional overlap
acceptance on the development site. It does not authorize production
materialization, establish production identity truth for synthetic records, or
replace backup, migration-rehearsal, QC/automation, and management-approval
gates.

### Completed QC and bounded-automation acceptance matrix — development

The browser and scheduler acceptance described in
`SYNTHETIC_QC_AUTOMATION_TEST_GUIDE.md` was completed on 2026-08-31 against the
isolated six-component fixture Canary `o2c67pgdv9` and policy `pilot-1.6`. A
fresh pre-write backup was taken at
`sites/frontend/private/backups/20260831_102011-frontend-*`. All fixture
identities are synthetic development evidence; none is a production identity
assertion or production authorization.

The tested governed configuration was:

| Control | Accepted value |
| --- | --- |
| Maximum complete components per Automatic Tiered execution | 2 |
| Automatic Tiered scheduler hook | Daily |
| QC cases per cadence | 2 |
| QC assignment interval | 7 days |
| QC SLA | 14 days |
| Rolling QC window | 100 comparable finalized cases |
| Materialization during bounded write exercises | Explicitly enabled, then disabled |
| Automatic controls after acceptance | Automatic QC off; Automatic Tiered off |

| Behaviour | Test source / immutable audit | Observed result | Acceptance |
| --- | --- | --- | --- |
| Independent QC and masking | First two assigned fixture Recommendations; one System Manager and one ordinary `CCD Match Reviewer` | Two different reviewers produced the required final result. The ordinary reviewer saw masked evidence and no CCD Master links; the manager saw only role-permitted full evidence | Pass |
| Zero-write automatic preview | Settings preview for Canary `o2c67pgdv9` | Selected two complete components / two Recommendations and planned two Groups / four Memberships. With controls off it reported the exact operational blockers and wrote no record | Pass |
| Bounded Automatic Tiered | Automatic batches `2qds16kh0f` and `74rnvcp089` | Each independently applied exactly two complete components and created two Groups / four Memberships. The second cycle advanced to different Proposed components rather than duplicating the first | Pass |
| QC Different circuit breaker | Recommendation `o2d9m3ndmj`; affected Group `2qh35g8jdb`; Investigation `ftkk4gd01f` | A deliberate finalized Different paused the global Tiered circuit breaker, opened one Investigation, and changed only the current shared Group and its Memberships to Needs Revalidation. A blocked cycle created no identity object | Pass |
| Governed QC-review-error recovery | Investigation `ftkk4gd01f`; Resolve event `ksdeed5aiq`; Group revalidation event `ksdogi5vda`; Resume event `km0gunvljd` | `QC Review Error` preserved the immutable Different history, reactivated the affected Group/Memberships, and excluded that adjudged reviewer mistake from rolling precision. Governed Resume cleared the breaker without re-enabling Automatic Tiered | Pass |
| Source staleness and identity revalidation | Recommendation `o2ftcsdsre`; CCD Master `HKSR0762581`; Membership `74s5tu1hvk`; Group `74s9mntmlj`; Events `al9tahdk0s` and `pfcc5e4792` | Editing one governed source value made the unfinished QC snapshot Stale and changed the live Membership/Group to Needs Revalidation. Restoring the source and using governed revalidation returned both to Active; the QC Recommendation correctly remained Stale | Pass |
| Scheduled cadence and deterministic replenishment | Automatic QC Assign event `3qnhng8pse`; Recommendations `o2foiqsla2` and `o2f11mfeca` | A synthetically due cadence assigned exactly two cases, advanced the next cadence by seven days, increased sample count 4→6, assignment cycles 2→3, and replenished count 1→3 | Pass |
| Cadence idempotency | Immediate repeat of `run_qc_monitor` | Assigned zero additional cases, created no second cadence Event, and left every identity-object and batch count unchanged | Pass |
| Overdue SLA breaker | Recommendation `o2foiqsla2`; Pause event `vc32rlr1ob` | Moving the open case due date into the past produced one overdue case, changed the Canary to Paused, and set `pause_reason = qc_sla_overdue:1`. A repeat monitor created no duplicate Pause event or identity write | Pass |
| Overdue recovery | Recommendation `o2foiqsla2`; Resume event `6isgd4k54a` | Independent Same finalization cleared the overdue count. Governed Resume returned the Canary to Monitoring while leaving Automatic Tiered off | Pass |
| Remaining-QC closure | Recommendation `o2f11mfeca` | Independent Same finalization left zero open assigned non-stale QC cases, so no fixture SLA remains capable of becoming overdue | Pass |
| Live scheduler registration | Scheduled Job Type `api_identity_qc.run_qc_monitor` | Frappe scheduler enabled; job frequency Daily; `stopped = 0`; two workers online; recorded scheduled execution at `2026-08-31 00:00:51` UTC. The persistent process is `bench schedule`; generic workers execute the short-lived Python method | Pass |

At the final fixture checkpoint, Canary `o2c67pgdv9` is Active and Monitoring
with six selected QC cases: five finalized, four comparable Same, zero
comparable Different, one adjudged review error excluded from precision, and
one Stale unfinished snapshot. Rolling precision is `1.0`; its Wilson 95% lower
bound is `0.510099980`, as expected for only four comparable successes. The
100-case rolling window is not complete, so the precision circuit breaker is
not yet eligible to decide policy reliability. Overdue count is zero,
assignment cycles are three, and three cases were added through continuous
replenishment.

The acceptance ended fail-closed: Live Materialization, Automatic QC, and
Automatic Tiered are all off; the circuit breaker is clear; and the authorized
synthetic Canary, policy, and bounded test values remain recorded but inert.
The daily monitor remains registered so already-existing QC safety state would
still be checked, but with no open assigned non-stale fixture case and both
automatic controls off it cannot create a QC assignment or identity object.

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

Functional overlap and QC/automation acceptance are complete on the development
site. The next controlled phase is production migration/readiness assessment,
not another automatic development wave. It must include a clean-target
migration rehearsal, a fresh post-acceptance backup checkpoint, verification of
scheduled backups and restore, named reviewer/manager ownership, production
capacity and monitoring checks, and an explicit management decision covering
the exact production Canary, Policy, QC cadence, SLA, rolling window, automatic
component limit, and rollback authority.

Materialization, Automatic QC, and Automatic Tiered are currently off, and the
circuit breaker is clear. The retained synthetic Canary and policy settings are
inert acceptance evidence and do not authorize production use. No production
rollout, wider development wave, or unattended write is implicit in this
status. Manual demonstrations must keep Automatic Tiered off; enable Live
Materialization only for the deliberate creation step, and disable it again for
any correction or rollback exercise.

Server transfer, clean-target installation, backup/restore, cutover, rollback,
and new-centre onboarding are covered in
`ERPNext_SERVER_MIGRATION_RUNBOOK.md`. Identity activation remains a separate
operation after a successful transfer.

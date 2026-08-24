# CCD Identity Resolution Implementation Status

## Safe takeover checkpoint

| Item | Verified state |
| --- | --- |
| Date | 2026-08-24 UTC |
| Site | `frontend` |
| Takeover basis | Recovered local predecessor session and its committed specification |
| Specification | `IDENTITY_RESOLUTION_WORKFLOW_PLAN.md` |
| Deployment | Schema, services, controllers, managed CCD Master Client Script, and frontend assets deployed |
| Materialization | Disabled by default (`materialization_enabled = 0`) |
| Automation circuit breaker | Not tripped (`automation_paused = 0`) |
| Live identity writes | Development pilot applied: 5 Decisions, 5 active Groups, and 10 active Memberships |

The predecessor session completed the workflow specification and pushed it at
commit `cfef788`. This fresh session recovered that durable artifact, audited
the application and live data, implemented the specification, deployed it in a
default-off state, and verified the result. It did not depend on reconstructing
the predecessor's compacted conversational reasoning.

On 2026-08-24, an explicitly approved five-component development pilot was
applied after a verified full backup. Materialization was turned off again
immediately afterward. The pilot created reversible identity objects only; it
did not merge or modify the participating CCD Master source documents.

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
- The complete in-container test suite passes: 63 tests, 0 failures.
- Frappe migration, role/policy setup, workflow installation, asset build,
  cache clear, and service restart completed.
- The frontend-local public renderer is served with HTTP 200, and live CCD
  Master FormMeta contains `load_identity_resolution` through the managed
  Client Script.
- All backend, scheduler, and queue containers are running; two workers are
  online.
- Re-running setup is designed to be idempotent.

### Live read-only state

| Measure | Result |
| --- | ---: |
| Frozen Tiered recommendations | 3,961 |
| Proposed | 3,523 |
| Exception | 433 |
| Approved | 5 |
| Splink candidates available | 11,177 |
| Splink candidates assigned | 0 |
| Component reviews | 191 |
| Identity decisions | 5 |
| Identity groups | 5 active |
| Identity memberships | 10 active |
| Identity exclusions/events | 0 exclusions / 20 create-activate events |
| Activation batches | 1 (`Applied`; 5 complete components) |
| Review batches | 0 |
| QC investigations | 0 |

### Zero-write activation preview

For canary `p1mucmhogd`, **Preview Approve All** returned:

| Measure | Result |
| --- | ---: |
| Selected complete components | 3,520 |
| Selected recommendations | 3,528 |
| Safe components | 3,520 |
| Unsafe components | 0 |
| Stale components | 0 |
| Planned identity groups | 3,520 |
| Planned memberships | 7,044 |
| Conflict counts | 0 |

The preview made zero writes. A subsequent, separately approved five-component
development pilot created the live counts recorded above; it did not change the
zero-write meaning of Preview.

The 3,520 components are 3,516 two-record components containing one
recommendation each and four three-record components containing three
recommendations each. Thus the eight-count difference between recommendations
and components is an edge-count difference; those four triplets account for 12
of the 7,044 planned member records.

## Next controlled decision

The development pilot does not authorize a production rollout or wider wave.
Before any further activation:

1. complete browser acceptance for linked and unlinked CCD Master records;
2. verify the five Groups, ten Memberships, twenty create/activate Events,
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

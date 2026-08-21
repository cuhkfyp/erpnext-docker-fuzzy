# CCD Identity Resolution Implementation Status

## Safe takeover checkpoint

| Item | Verified state |
| --- | --- |
| Date | 2026-08-20 UTC |
| Site | `frontend` |
| Takeover basis | Recovered local predecessor session and its committed specification |
| Specification | `IDENTITY_RESOLUTION_WORKFLOW_PLAN.md` |
| Deployment | Schema, services, controllers, and assets deployed |
| Materialization | Disabled by default (`materialization_enabled = 0`) |
| Automation circuit breaker | Not tripped (`automation_paused = 0`) |
| Live identity writes | None |

The predecessor session completed the workflow specification and pushed it at
commit `cfef788`. This fresh session recovered that durable artifact, audited
the application and live data, implemented the specification, deployed it in a
default-off state, and verified the result. It did not depend on reconstructing
the predecessor's compacted conversational reasoning.

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
- Optional non-zero Splink Review Batches. Creating no batch correctly leaves
  assigned work at zero.
- Asynchronous QC sampling, rolling Wilson precision monitoring, overdue-case
  checks, investigations, revalidation state, and an automation circuit
  breaker.
- Permission-masked identity fields and an Identity Resolution tab on CCD
  Master. Legacy fuzzy fields are retained but are not written by this system.
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
- All backend, scheduler, and queue containers are running; two workers are
  online.
- Re-running setup is designed to be idempotent.

### Live read-only state

| Measure | Result |
| --- | ---: |
| Frozen Tiered recommendations | 3,961 |
| Proposed | 3,528 |
| Exception | 433 |
| Splink candidates available | 11,177 |
| Splink candidates assigned | 0 |
| Component reviews | 191 |
| Identity decisions | 0 |
| Identity groups | 0 |
| Identity memberships | 0 |
| Identity exclusions/events | 0 |
| Activation batches | 0 |
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

The preview made zero writes. A subsequent read-only check confirmed every new
operational table was still empty.

The 3,520 components are 3,516 two-record components containing one
recommendation each and four three-record components containing three
recommendations each. Thus the eight-count difference between recommendations
and components is an edge-count difference; those four triplets account for 12
of the 7,044 planned member records.

## Next controlled decision

No production activation is implicit in this implementation. The next action
requires explicit management authorization and should be bounded:

1. select complete components for a small pilot wave and any deliberate
   holdout;
2. take the normal ERPNext backup;
3. enable materialization through Identity Resolution Settings;
4. create, review, approve, and apply the frozen pilot activation batch; and
5. verify memberships, audit events, masking, QC assignments, and rollback
   behavior before any wider wave.

Until that decision is made, the deployed system is observable and testable
but cannot create live identity links.

Server transfer, clean-target installation, backup/restore, cutover, rollback,
and new-centre onboarding are covered in
`ERPNext_SERVER_MIGRATION_RUNBOOK.md`. Identity activation remains a separate
operation after a successful transfer.

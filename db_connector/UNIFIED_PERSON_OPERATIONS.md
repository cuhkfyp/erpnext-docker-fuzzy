# CCD Unified Person Operations

## Purpose and identifier format

The Unified Person registry gives every current logical person a durable number
without writing that number onto `CCD Master`.

- Format: `HKSR-U#########C`.
- The nine-digit sequence is monotonic and never recycled.
- `C` is a Luhn check digit.
- The registry, Membership history, aliases, Events, and Backfill Runs are
  separate identity-governance records.

## Where to use it in ERPNext Desk

- Open a CCD Master and select **Identity Resolution** to see its current
  Unified Person Number and active aliases.
- Open **CCD Unified Person** for the permanent registry. Its list has a
  one-click **Unified Person Register** button, and each registry record has an
  **Identity Resolution → Unified Person Register** button that opens the
  report pre-filtered to that number.
- Open the **CCD Unified Person Register** report for current or historical
  assignments, source lineage, and validity dates.
- Open **CCD Identity Resolution Register** for the existing operational view;
  users without direct CCD Master access see masked record/group/person aliases.
- Open **CCD Identity Resolution Settings** and use the **Identity Integrity**
  button group for backfill status and the zero-write Unified Person audit.

Direct registry data is restricted to System Managers and Sensitive Reviewers.
Ordinary reviewers continue to use masked review/register surfaces and receive
no CCD Master permission from this feature.

## Delete and recreate behavior

Technical deletion must use the governed source-retirement or CCD Registration
cancellation service. It ends the current Unified Person Membership before the
CCD Master row is deleted and preserves the number and source lineage forever.

A replacement CCD Master recovers the same current canonical number only when:

1. `ccd_reg_source` is unchanged;
2. `ccd_source_key` is unchanged and non-empty;
3. all historical assignments for that exact source lineage resolve to one
   canonical Unified Person; and
4. no other current CCD Master still owns that source lineage.

If any condition fails, the system does not guess. It creates an independent
singleton and logs the lineage conflict for human review. A changed source key
therefore represents a new lineage unless it is later joined by a governed
identity decision.

## Merge, alias, and correction behavior

- A governed merge keeps the oldest issued sequence as canonical.
- Every other issued number remains permanently resolvable as an alias.
- A governed split reactivates an original lineage number when the lineage is
  unambiguous.
- If no safe original number exists, the deterministic survivor remains and a
  new non-recycled number is issued.
- No merge or split deletes registry, Membership, alias, or Event history.

## Initial backfill checkpoint

Backfill `8srqf32s7o` completed on 2026-09-16 in 52 bounded batches:

- 256,095 current CCD Masters;
- 256,095 active Unified Person Memberships;
- 256,046 issued people;
- zero integrity issues; and
- unchanged CCD Master count and `modified` snapshot SHA-256
  `a1169187c20e81edfa0e7ba018e2b4b5c72fe12fa2edf46d2b8b44312c056440`.

The backfill is idempotent after completion. New CCD Masters receive a
singleton in the insert transaction. The daily scheduler reconciles at most
500 records missed by a raw-import hook, then runs the same read-only integrity
audit.

## Integrity response

The audit must remain at zero for:

- active Memberships whose CCD Master no longer exists;
- current CCD Masters without an active Membership;
- duplicate active Memberships;
- active assignments to alias/retired numbers;
- one governed Identity Group spanning multiple Unified People;
- registry active-member count mismatch;
- alias state mismatch;
- duplicate active stable source lineage; and
- invalid number/check-digit or sequence pairs.

If a nonzero count appears, keep all identity materialization/automation
controls off, capture the report and Error Log, and repair through the governed
lifecycle/reconciliation service. Do not update registry tables or CCD Master
directly.

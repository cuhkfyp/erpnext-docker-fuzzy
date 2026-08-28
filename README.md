# erpnext-docker-fuzzy

Server-side cross-centre identity-matching tools for `CCD Master` in
Frappe/ERPNext.

This repository contains two deliberately separate paths:

- `api_ccd_fuzzy.py` is the current production baseline. It evaluates the
  formula stored in each `CCD Registration`, writes accepted candidates to the
  Matching Score child table, and stores an escaped HTML audit explanation on
  each row. See [`api_ccd_fuzzy.md`](api_ccd_fuzzy.md).
- `api_fuzzy_evaluation.py` and `fuzzy_matching/` are a recommendation-only
  shadow pilot. They compare the baseline with deterministic evidence tiers,
  two safe identifier-conflict policies, a local Splink model, and a hybrid.
  They never set `Is Matched?` and never modify the production match table. See
  [`MATCHING_PILOT.md`](MATCHING_PILOT.md).
- `api_fuzzy_canary.py` turns only the validated Tiered Evidence High rule into
  versioned, reversible recommendation records. It applies full-population
  cluster and source-coverage gates and still never merges CCD records or
  modifies production match fields.
- `api_fuzzy_review_queue.py` creates a separate optional human-review queue
  for eligible candidate pairs at or above the selected maximum-F1 Splink
  cutoff. Every row remains model tier `Review`; human decisions are stored
  separately and never turn the probability into automatic High.
- `api_fuzzy_splink_experiment.py` reproduces an approved frozen evaluation
  for read-only training-size research. It returns sanitized aggregates, makes
  no database writes, and cannot replace the approved model or queue.
- `api_identity_qc.py` and `api_identity_automation.py` provide continuous QC,
  governed circuit-breaker recovery, and separately authorized default-off
  bounded Tiered materialization. They do not make Splink probabilistic output
  automatic.

## Management POC

The completed, sanitized proof-of-concept package is available in:

- [`POC_REPORT.md`](POC_REPORT.md) — executive case, five-model evaluation,
  aggregate evidence, controls, limitations, and rollout proposal;
- [`POC_DEMO.md`](POC_DEMO.md) — a 12–15 minute presentation and live-demo
  guide for management;
- [`POC_SYNTHETIC_EXAMPLES.md`](POC_SYNTHETIC_EXAMPLES.md) — presentation-safe
  fictional pair cards showing model tiers versus human decisions; and
- [`POC_RESULTS.json`](POC_RESULTS.json) — machine-readable, non-identifying
  aggregate results.

The deployed follow-up implementation is specified in
[`IDENTITY_RESOLUTION_WORKFLOW_PLAN.md`](IDENTITY_RESOLUTION_WORKFLOW_PLAN.md).
It defines reversible identity groups and memberships, Tiered and human-review
materialization, continuous QC, optional review batches, deliberate rollout
holds, bulk-approval testing, and the next management demo. The code, schema,
and UI are deployed in guarded default-off mode. Controlled development
acceptance has created reversible identity/audit objects, but Materialization,
Automatic QC, and Automatic Tiered are currently off. See
[`IDENTITY_RESOLUTION_IMPLEMENTATION_STATUS.md`](IDENTITY_RESOLUTION_IMPLEMENTATION_STATUS.md)
for the verified boundary, [`ERPNext_SERVER_MIGRATION_RUNBOOK.md`](ERPNext_SERVER_MIGRATION_RUNBOOK.md)
for transfer, and
[`SYNTHETIC_QC_AUTOMATION_TEST_GUIDE.md`](SYNTHETIC_QC_AUTOMATION_TEST_GUIDE.md)
for the development QC/automation acceptance matrix.

Before 2026-08-19, the ERPNext evaluation approvals and Pilot promotion were
recorded by the project operator, not management. Management reviewed the POC
results and live demonstration on 2026-08-19 and approved the limited follow-up
workflow: Tiered Evidence for reversible safety-gated recommendations, and
Splink above the selected cutoff for optional human-review ordering. This does
not authorize automatic record merging, `Is Matched?` changes, or a general
Splink probability threshold.

The locked 251,520-record POC snapshot contains 161,112 Production records
(64.06%), 89,377 UAT records (35.53%), and 1,031 Fake/test records (0.41%). The
POC metrics therefore describe a mixed governed population, not a separately
measured Production-only result.

The project-operator-initiated recommendation-canary preview is now `Ready`.
On the same 251,520-record governed snapshot, 3,528 of 3,961 Tiered High
candidates passed all gates as `Proposed`; 433 were isolated as one-to-many
source conflicts. No recommendation is `Active`, and no production match field
or CCD record was changed. See the sanitized aggregate result in
[`POC_RESULTS.json`](POC_RESULTS.json).

The separate optional Splink queue is also `Ready`. It excluded all 3,961
Tiered High pairs and 1,097 previously human-used pairs, scored all remaining
816,534 governed candidates, and stored 11,177 at or above the selected
`0.938995074` maximum-F1 cutoff. All remain model tier `Review`; none is an
automatic match, and the queue made no CCD Master change.

A controlled 2026-08-14 shadow experiment reproduced the approved 500 labels
on one worker with Splink 4.0.16, DuckDB 1.4.5, and equal 250,000-pair sampling
budgets. The 5,000-record control completed with average precision 0.6242, ROC
AUC 0.8714, and 73.33% precision in its top 30, but still produced no valid
automatic High threshold. The equivalent 20,000-record run exceeded the
worker's memory limit, so it produced no comparable accuracy result and is not
a candidate model. The approved v1.1 cutoff and 11,177-row queue are unchanged.

## Install the pilot

On the managed Docker host, use the checked-in deployment helper. It captures
the complete private `db_connector` app outside the containers, overlays it
into every Python process container, installs optional packages into a
persistent site-local target, migrates, builds assets, and restarts the Python
services:

```bash
/root/erpnext_docker_volume/deploy_db_connector.sh
```

`/root/erpnext_docker_volume/erpnext_restart.sh` calls the same deployment
helper after the stack starts. This protects the fuzzy changes when existing
containers restart and restores them if those containers are recreated. It is
not a backup strategy for unrelated private apps or the database. Because the
site also installs the private `hksr` app, deployment mirrors that app from the
existing backend container and registers its path in the scheduler and workers
before restart; the backend copy remains its source of truth.

For a Python-only revision that changes no DocType, dependency, or asset, use
`deploy_db_connector.sh --code-only`. It still refreshes the persistent copy
and all Python containers, clears cache, restarts services, and remounts the
backend view.

For a conventional non-Docker installation, install manually:

Install the pinned packages in the same Python environment used by the
backend, scheduler, and long-queue workers:

```bash
cd /home/frappe/frappe-bench
./env/bin/pip install -r apps/db_connector/db_connector/requirements.txt
bench --site <site> migrate
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_matching_roles
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_default_pilot_policy
```

Restart the backend and workers after installing dependencies. Splink and
DuckDB run locally on CPU; this is not an LLM, needs no Ollama, needs no API key,
and sends no client data to an external service.

For a complete transfer to another ERPNext server, do not use this section or
the Docker helper alone. The repository contains the versioned matching and
identity component, while the target also needs the complete private
`db_connector` app, the app that owns `CCD Master`/`CCD Registration`, matching
Frappe/ERPNext versions, and—when moving the existing site—the database, files,
site configuration, encryption key, and every installed app. Follow
[`ERPNext_SERVER_MIGRATION_RUNBOOK.md`](ERPNext_SERVER_MIGRATION_RUNBOOK.md).

## Safe first run

1. Create a `CCD Matching Policy` with status `Draft` or `Pilot`.
2. Import source profiles from live `CCD Registration.fieldmatch` rows. Only
   approved identity targets are imported; arbitrary centre-specific fields
   remain available in CCD but do not silently become matching evidence.
3. Leave identifier scope as `Unknown` or `Local` unless governance has proven
   that the identifier uses one shared organization-wide namespace. In
   `pilot-1.6`, HKID is the approved exception, but it is global evidence only
   when both values are complete and pass the HKID check-digit validation.
   Partial, masked, and invalid values remain review-only evidence.
4. Start a 500-pair run with 100 double-reviewed pairs:

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_evaluation_run \
  --kwargs '{"policy_name":"pilot-1.6","sample_size":500,"double_review_count":100}'
```

5. Review the generated `CCD Match Evaluation Pair` documents as `Same`,
   `Different`, or `Unsure`. Resolve disagreements through adjudication. Every
   observed `Same` automatically requires a second independent confirmation.
6. Finalize only after all intended labels are complete:

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.finalize_evaluation \
  --kwargs '{"run_name":"<evaluation-run>"}'
```

Finalization reports held-out performance, Wilson confidence intervals, and
candidate thresholds. Thresholds remain disabled when either the calibration
or held-out partition has fewer than 10 confirmed matches. Finalization does
not approve or deploy a policy; production activation remains a separate
management decision.

When random candidate review yields too few confirmed matches, create a
separate 100-pair positive benchmark with
`install_positive_benchmark_run`. It uses unseen legacy high-score links only
to discover records for blinded relabeling and reports blocking recall. Because
that cohort is deliberately enriched, its precision is not production
precision and its thresholds are always disabled.

After a representative run has measured the deterministic High tier, validate
that tier on a fresh uniform sample of unseen High predictions:

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_high_tier_validation_run \
  --kwargs '{"policy_name":"pilot-1.6","sample_size":100}'
```

All 100 pairs are assigned for two independent reviews. The run reports High
precision and its Wilson 95% confidence interval, but does not recalibrate
score thresholds or alter production matching. Pilot 1.6 also discards
malformed and obvious sequential Hong Kong phone placeholders before blocking
or scoring.

## Recommendation canary and guarded materialization

After the unchanged policy has both an approved High Tier Validation and an
approved Threshold Evaluation, promote it from Draft to Pilot:

```bash
bench --site <site> execute db_connector.api_fuzzy_canary.install_promote_policy_to_pilot \
  --kwargs '{"policy_name":"pilot-1.6"}'
```

Create a preview run:

```bash
bench --site <site> execute db_connector.api_fuzzy_canary.install_canary_run \
  --kwargs '{"policy_name":"pilot-1.6"}'
```

The preview fails closed if candidate generation is truncated or skips any
oversized block. Only the validated exact-full-name-plus-independent-evidence
High rule may become `Proposed`; HKID-only High, unvalidated source pairs,
stale records, one-to-many components, and transitive contradictions become
`Exception`.

Every recommendation form now renders the pair in one protected side-by-side
table. `CCD Match Reviewer` sees masked identity values and no CCD record keys;
`CCD Match Sensitive Reviewer` and `System Manager` see the full permitted
values and may follow the record links. Raw model reason codes remain restricted
to System Managers.

Exception edges are grouped into one `CCD Match Component Review` per connected
component, so staff decide the complete 3–7-record case rather than switching
between pair tabs. Reviewers choose `All Same`, `Partial Match`, `All Different`,
or `Unsure`. Partial Match stores a canonical partition of the component.
Two independent matching submissions finalize an agreement; disagreements and
Unsure go to manager adjudication, and a positive adjudication still requires
an independent matching confirmation. These human decisions never rewrite the
model's `Exception` status or modify CCD Master. When live materialization is
disabled, a final decision remains `Pending`; when enabled, a finalized All
Same/Partial Match/All Different decision is passed through the shared safety
service to create reversible Groups/Memberships or fingerprint-scoped
Different exclusions.

A deterministic 100-pair sample of passing Proposed recommendations is marked
`Selected for QC`, but preselection alone does not permit review. A manager or
the separately enabled cadence releases a bounded shared-work-pool cohort,
starts its SLA, and uses the same blinded `Same` / `Different` / `Unsure`
workflow. Completed cases remain immutable; unfinished stale cases and exhausted
preselection are replenished deterministically. The canary form links directly
to assigned QC work.

Each recommendation stores the frozen policy version, source-record snapshot,
reason codes, safety status, and opaque pair/cluster fingerprints. Separate
immutable events retain Created, Approved, Withdrawn, and Superseded history.
The former status-only **Approve Recommendations** path is retired. A System
Manager now uses zero-write preview and a frozen, component-atomic
`CCD Identity Activation Batch`; only the separately confirmed Apply action can
create reversible identity links, and it remains blocked while live
materialization is disabled or automatic materialization is paused. An
unmaterialized Proposed recommendation may be withdrawn, while a materialized
relationship must be ended or superseded through Identity Membership history.
Each frozen component row includes source pair(s) and a protected **Review
Pair(s)** dialog with permitted CCD Master links and the evidence needed to
inspect the exact selected records before approval.

If an applied Tiered, Component Review, Splink, or prior Governance Override
decision is later found wrong, a System Manager can use **Correct Complete
Identity Component** on the source or active Identity Decision. The 2–25-record
workflow expands through complete live groups, accepts a replacement partition,
requires a zero-write frozen preview and Materialization off, and atomically
ends/supersedes the old relationship objects before creating an immutable
`CCD Identity Correction` and replacement Decision. It never edits or merges a
CCD Master document. The narrower two-record Splink false-Same shortcut remains
available for its exact case.

## Optional Splink Review queue

From a `Ready` or `Active` canary, a System Manager can press **Create Splink
Review Queue**, or run:

```bash
bench --site <site> execute db_connector.api_fuzzy_review_queue.install_review_queue \
  --kwargs '{"canary_name":"<canary-run>"}'
```

The queue reuses the canary's frozen data and policy and the approved threshold
evaluation's `pilot-splink-1.1` model/cutoff. Eligible pairs are complete
governed candidates after excluding every Tiered High recommendation and every
pair already used by human evaluation or an earlier queue. Each eligible pair
must receive exactly one score; a changed/unreproducible frozen canary,
truncated/skipped candidate generation, stale calibration records, or
incomplete scoring fails the run without publishing a partial queue.

Only pairs at or above the selected maximum-calibration-F1 cutoff are stored,
ordered from highest probability downward. A lower score means lower priority,
not `Different`. Candidate forms show protected side-by-side evidence:
ordinary reviewers see masked values and no CCD record keys; Sensitive
Reviewers and System Managers see full permitted values and links. Individual
probabilities and blocking diagnostics are restricted to System Managers.

Reviewers submit `Same`, `Different`, or `Unsure`. `Same` requires two
independent confirmations; `Unsure` and disagreements require adjudication.
The recorded model tier stays `Review`, and queue generation never links,
merges, or modifies CCD Master. A finalized human Same/Different decision may
create reversible identity objects only when live materialization is enabled;
otherwise its materialization state remains Pending. Splink probability itself
never becomes automatic High.

## Development validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q api_ccd_fuzzy.py api_fuzzy_evaluation.py api_fuzzy_canary.py api_fuzzy_review_queue.py api_identity_*.py identity_resolution_setup.py fuzzy_matching db_connector/doctype tests
```

Real client records, model databases, logs, exports, credentials, and secrets
must never be committed to this repository.

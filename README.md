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

The POC approves evidence for a narrow deterministic High recommendation rule.
It does not authorize automatic record merging, `Is Matched?` changes, or a
general Splink probability threshold. The package also includes the latest
`pilot-1.6` representative recalibration as of 2026-08-13: Splink has a
management-approved review-priority operating point, but still has no validated
automatic High threshold.

The separately authorized recommendation-canary preview is now `Ready`. On the
same 251,520-record governed snapshot, 3,528 of 3,961 Tiered High candidates
passed all gates as `Proposed`; 433 were isolated as one-to-many source
conflicts. No recommendation is `Active`, and no production match field or CCD
record was changed. See the sanitized aggregate result in
[`POC_RESULTS.json`](POC_RESULTS.json).

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

## Recommendation-only canary

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
model's `Exception` status or modify CCD Master.

A deterministic 100-pair sample of passing Proposed recommendations is marked
`Selected for QC` and uses the same blinded `Same` / `Different` / `Unsure`
workflow. The canary form links directly to both the component queue and the QC
queue.

Each recommendation stores the frozen policy version, source-record snapshot,
reason codes, safety status, and opaque pair/cluster fingerprints. Separate
immutable events retain Created, Approved, Reversed, and Superseded history.
Approval is a distinct System Manager action after aggregate inspection.
Both individual recommendations and the complete active canary can be
reversed with a required reason.

The Desk button is deliberately named **Approve Recommendations**. It changes
only dedicated recommendation statuses from `Proposed` to `Active` and appends
audit events. It does not link or merge CCD records, set `Is Matched?`, or
populate Matching Score.

The approved Splink cutoff is stored with the canary for future Review-queue
ordering. This first canary emits Tiered High recommendations only; it does not
turn Splink scores into automatic High decisions.

## Development validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q api_ccd_fuzzy.py api_fuzzy_evaluation.py fuzzy_matching db_connector/doctype tests
```

Real client records, model databases, logs, exports, credentials, and secrets
must never be committed to this repository.

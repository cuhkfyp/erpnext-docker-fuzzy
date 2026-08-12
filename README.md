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
general Splink probability threshold.

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

## Development validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q api_ccd_fuzzy.py api_fuzzy_evaluation.py fuzzy_matching db_connector/doctype tests
```

Real client records, model databases, logs, exports, credentials, and secrets
must never be committed to this repository.

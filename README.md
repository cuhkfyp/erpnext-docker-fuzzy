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
not a backup strategy for unrelated private apps or the database.

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
   that the identifier uses one shared organization-wide namespace.
4. Start a 500-pair run with 100 double-reviewed pairs:

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_evaluation_run \
  --kwargs '{"policy_name":"pilot-1.0","sample_size":500,"double_review_count":100}'
```

5. Review the generated `CCD Match Evaluation Pair` documents as `Same`,
   `Different`, or `Unsure`. Resolve disagreements through adjudication.
6. Finalize only after all intended labels are complete:

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.finalize_evaluation \
  --kwargs '{"run_name":"<evaluation-run>"}'
```

Finalization reports held-out performance and candidate thresholds; it does not
approve or deploy a policy. Production activation remains a separate management
decision.

## Development validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q api_ccd_fuzzy.py api_fuzzy_evaluation.py fuzzy_matching db_connector/doctype tests
```

Real client records, model databases, logs, exports, credentials, and secrets
must never be committed to this repository.

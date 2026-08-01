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

Install the pinned packages in the same Python environment used by the
backend, scheduler, and long-queue workers:

```bash
cd /home/frappe/frappe-bench
./env/bin/pip install -r apps/db_connector/db_connector/requirements.txt
bench --site <site> migrate
bench --site <site> execute db_connector.api_fuzzy_evaluation.ensure_matching_roles
```

Restart the backend and workers after installing dependencies. Splink and
DuckDB run locally on CPU; this is not an LLM, needs no Ollama, needs no API key,
and sends no client data to an external service.

## Safe first run

1. Create a `CCD Matching Policy` with status `Draft` or `Pilot`.
2. Map each source's canonical attributes in `Source Profiles`.
3. Leave identifier scope as `Unknown` or `Local` unless governance has proven
   that the identifier uses one shared organization-wide namespace.
4. Start a 500-pair run with 100 double-reviewed pairs:

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.enqueue_evaluation \
  --kwargs '{"policy_name":"1.0.0","sample_size":500,"double_review_count":100}'
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
python -m compileall -q api_ccd_fuzzy.py api_fuzzy_evaluation.py fuzzy_matching doctype tests
```

Real client records, model databases, logs, exports, credentials, and secrets
must never be committed to this repository.

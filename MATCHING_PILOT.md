# CCD Matching Shadow Pilot

## Purpose

The current weighted formula is useful for finding candidates, but a single
percentage is not a reliable identity decision. Missing phone data reduces the
score of an otherwise convincing pair, while two different people with common,
similar names can receive a high score. Wrapping the same expression in `MAX()`
does not add evidence and is not supported by the legacy formula evaluator.

The pilot therefore evaluates several approaches side by side without changing
live matching decisions. Its purpose is to measure performance on locally
reviewed CCD pairs before any deployment choice is made.

## Decision model

The deterministic policy treats evidence by meaning rather than assigning one
fixed weight to every row:

| Evidence | Default outcome |
| --- | --- |
| Exact, trusted global identifier in both approved source profiles | High, with a name-conflict warning if needed |
| Two non-empty trusted global identifiers disagree | Conflict Review |
| Exact full Chinese or English name plus exact birthday, phone, or email | High |
| Names only, including exact names | Human Review |
| Similar names without independent evidence | Human Review |
| No useful shared evidence | Low / Insufficient |
| A value missing on either side | No evidence; it is not treated as disagreement |

`hksr_num`, staff number, client primary ID, or a similar source key must not be
treated as a global identifier merely because its spelling looks authoritative.
It becomes decisive only when:

1. the policy lists its canonical attribute as trusted; and
2. both source profiles mark that attribute's scope as `Global`.

The default for separate or unknown namespaces is `Unknown` or `Local`, so an
accidental same number in two centres cannot force a match.

Two identifier-conflict variants are evaluated:

- **Gated:** any non-empty trusted-ID disagreement remains `Conflict Review`.
- **Recoverable:** independent name and exact secondary evidence can recover the
  pair to ordinary `Review`, never directly to `High`.

This implements both safe alternatives requested for the pilot; the results,
not preference, should decide which is retained.

## Models compared

Each sampled pair stores results for:

1. **Current baseline:** each side's actual `fuzzymachingscript` is evaluated in
   its own direction. The maximum directional score is reported, and a pair is
   considered flagged if either source formula passes.
2. **Tiered gated policy:** deterministic safety gates described above.
3. **Tiered recoverable-conflict policy:** the alternative conflict handling.
4. **Fellegi–Sunter model:** Splink estimates field agreement/disagreement
   likelihoods locally with DuckDB.
5. **Hybrid:** deterministic identifier safety gates around the calibrated
   probabilistic score.

Splink is an open-source statistical record-linkage library, not a language
model. It is free to run locally, requires no Ollama or GPU, makes no network
calls, and does not upload CCD data. Its package versions are pinned in
`requirements.txt` for reproducibility.

If Splink is unavailable or cannot train on a sparse run, the run continues
with the deterministic models and records a sanitized warning. The optional
model must never prevent generation of the review sample.

## Candidate generation

The pilot compares records from different CCD sources only. It takes the union
of several blocking routes:

- trusted global identifier, only for profiles that mark it global;
- normalized exact phone or email;
- normalized Chinese full name;
- Chinese surname pinyin initial;
- normalized English surname plus given-name prefix;
- birthday plus normalized surname.

Large blocks and the total candidate set have policy limits. Oversized block
metadata stores only its route, a one-way digest, and count—not the underlying
name, phone, email, or identifier. Candidate truncation is recorded on the run
and must be treated as a recall warning.

## Sampling and labels

The initial recommendation is 500 stratified pairs, including 100 pairs assigned
for two independent reviews. Sampling covers source pair, baseline score band,
model agreement/disagreement, identifier conflicts, and blocking route.

Reviewers label each pair:

- `Same`
- `Different`
- `Unsure`

Double-review labels are hidden from the other reviewer. Model scores and reason
codes are restricted to System Managers during labeling to reduce anchoring
bias. `Unsure` and reviewer disagreement require management adjudication. Cohen's
kappa is reported for double-reviewed pairs.

Standard reviewers see trusted strong identifiers in masked form. System
Managers and users with `CCD Match Sensitive Reviewer` can see the full value.
Record values rendered in HTML are escaped.

## Threshold calibration

Do not select `65%`, `70%`, or another threshold from intuition or from the three
screenshots. Scores from the legacy weighted equation are similarity scores, not
calibrated probabilities.

Finalization uses a stable 60/40 calibration/held-out split:

- candidate **High** threshold: lowest calibration threshold reaching the
  policy precision target (default 95%) with at least the configured number of
  predicted-high examples (default 30), then checked again on held-out data;
- candidate **Review** threshold: calibration threshold with the best balanced
  F1 score, with held-out metrics reported;
- if held-out precision or sample size fails, the High tier is disabled.

These thresholds are evaluation output only. They do not change production
records and do not approve a policy.

Because the review set is deliberately stratified toward score bands, model
disagreements, and conflicts, these figures describe the pilot challenge set;
they are not automatically a population-prevalence estimate. Before production
automation, repeat validation on a representative locked holdout and report
confidence intervals as well as point estimates.

## Frappe records and workflow

### `CCD Matching Policy`

A versioned policy stores the pilot state, precision target, sample safeguards,
candidate limits, trusted global identifier list, and source-profile mappings.
Policies remain centrally versioned instead of silently changing each centre's
logic during the comparison.

Each `CCD Matching Source Profile` row maps one canonical attribute to an actual
`CCD Master` field and records identifier scope/reliability. Start all strong-ID
scope values as `Unknown` until profiling and governance review are complete.

### `CCD Match Evaluation Run`

The run stores its policy version, record snapshot time, data-quality profile,
candidate counts, skipped/truncated blocks, model versions, sample size, metrics,
and approval state. Evaluation is queued on Frappe's `long` queue.

### `CCD Match Evaluation Pair`

The pair stores cross-source record references, shadow model outputs, review
state, stale-record state, and cluster-conflict state. Review labels are child
rows and are not shown to ordinary reviewers.

If either CCD Master record changes after the run snapshot, opening the pair
marks it stale. Stale pairs are excluded from final calibration. A new run is
preferred over trying to reinterpret changed records.

### Cluster safety

The pilot checks connected pair groups for contradictions. If A–B and B–C are
links but A–C has a trusted identifier conflict, all affected evaluated edges
are flagged for management attention. The pilot does not create or merge person
clusters.

## Operations

### 1. Install and migrate

Install `requirements.txt` in every process image/environment that can execute
the API or long queue, migrate the site, then restart services:

```bash
cd /home/frappe/frappe-bench
./env/bin/pip install -r apps/db_connector/db_connector/requirements.txt
bench --site <site> migrate
bench --site <site> execute db_connector.api_fuzzy_evaluation.ensure_matching_roles
```

Frappe creates the roles referenced by the DocTypes during migration; the last
command is idempotent and verifies that both pilot roles exist.

### 2. Profile and configure

Create a Draft policy and its source mappings. Inspect the run's source profile
for coverage, distinctness, duplicates, HKID validity, and cross-source overlap.
Profiling never promotes an identifier automatically—scope approval is a data
governance action.

### 3. Queue a recommendation-only run

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.enqueue_evaluation \
  --kwargs '{"policy_name":"1.0.0","sample_size":500,"double_review_count":100}'
```

### 4. Review and adjudicate

Assign `CCD Match Reviewer` to ordinary reviewers and `CCD Match Sensitive
Reviewer` only where full strong identifiers are necessary. System Managers can
adjudicate through:

```python
frappe.call(
    "db_connector.api_fuzzy_evaluation.adjudicate_review",
    pair_name="<pair>",
    label="Same",  # or Different
    notes="<reason>",
)
```

### 5. Finalize the evaluation

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.finalize_evaluation \
  --kwargs '{"run_name":"<evaluation-run>"}'
```

Management should compare false positives, false negatives, precision, recall,
F1, reviewer agreement, source-pair coverage, candidate truncation, and cluster
conflicts. A separate reviewed change is required before any policy is allowed
to affect live matching.

## Privacy and repository boundary

- GitHub contains code and synthetic tests only.
- Do not commit real names, phone numbers, email addresses, birthdays, HKIDs,
  client primary IDs, database files, exports, logs, credentials, or secrets.
- Splink works in memory through local DuckDB for this adapter; no raw records
  are persisted by the repository code.
- Error summaries stored on runs are sanitized. Detailed tracebacks remain in
  the protected Frappe Error Log.
- The shadow evaluator never writes `CCD Master.match_table` and never changes
  `is_matched`.

## Known pilot limits

- Blocking can miss a true pair that shares none of the configured routes.
- Pair-classification recall is measured only among generated candidates. To
  measure blocking recall, add a separately governed benchmark of known
  cross-source same-person pairs and verify that candidate generation recovers
  them.
- Common-name blocks may be skipped to protect runtime; the run records this.
- Unsupervised probabilities still require human labels and held-out validation.
- Sparse fields or small source pairs can make the statistical model unstable;
  deterministic results remain available.
- A score is pair evidence, not proof of identity. Human review and governance
  remain necessary for consequential decisions.

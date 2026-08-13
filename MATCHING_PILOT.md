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

The adapter explicitly starts with Splink's conservative default random-pair
match prior (`0.0001`). It does not estimate that prior from exact-name blocks:
common or incomplete CCD names are not a high-precision identity rule and can
otherwise produce an implausibly large prior. The run records this setting;
human labels still determine candidate High and Review thresholds.

To stay within the ERPNext long worker's memory budget, training uses at most
5,000 deterministic background records and always includes every record in the
retained human-review pairs. Full-population blocking and the deterministic
models remain governed by the policy candidate limits. The run records both
the statistical training limit and actual training count.

After bounded training, ordinary Splink predictions remain restricted to the
safeguarded population blocking rules. The adapter then uses the same trained
model to directly score only the already-selected review pairs that were not
emitted by those rules (hard limit: 5,000). Direct scoring does not create a new
candidate, production match, or cluster; it gives each governed label a
comparable statistical score.

If Splink is unavailable or cannot train on a sparse run, the run continues
with the deterministic models and records a sanitized warning. The optional
model must never prevent generation of the review sample.
Pairs outside the statistical model's safeguarded prediction blocks may have
no probability; this is missing model evidence, not a zero-probability match.
Canonical missing values are converted to SQL nulls before Splink training and
scoring. They therefore use Splink's null level and can never be learned as an
exact empty-string agreement.

## Candidate generation

The pilot compares records from different CCD sources only. It takes the union
of several blocking routes:

- trusted global identifier, only for profiles that mark it global;
- normalized exact phone or email;
- normalized Chinese full name;
- exact Chinese surname plus exact full given-name pinyin;
- exact Chinese surname plus order-insensitive given-name characters;
- normalized Chinese surname plus given-name prefix;
- normalized English surname plus given-name prefix;
- birthday plus normalized surname.

Large blocks and the total candidate set have policy limits. Oversized block
metadata stores only its route, a one-way digest, and count—not the underlying
name, phone, email, or identifier. Candidate truncation is recorded on the run
and must be treated as a recall warning. Exact identifiers, contacts, dates,
and bounded exact-name variants are retained first. For broad Chinese and
English prefix blocks, each record nominates its closest name per other source;
the remaining budget round-robins across routes with sparse endpoints first.
This prevents high-volume integrations and common-name blocks from starving
records that have only a few possible cross-source counterparts. Pilot 1.6 uses
a 1,000,000-pair safety ceiling. Phone evidence is limited to normalized
eight-digit Hong Kong subscriber numbers with an allocated initial digit;
obvious full ascending or descending sequences are treated as missing
placeholders rather than identity evidence.

## Sampling and labels

The initial recommendation is 500 stratified pairs, including 100 pairs assigned
for two independent reviews. Sampling covers source pair, baseline score band,
model agreement/disagreement, identifier conflicts, and blocking route. Pilot
1.1 balances both the review sample and the pre-assigned double-review subset
across source pairs so one high-volume integration cannot dominate either set.

Reviewers label each pair:

- `Same`
- `Different`
- `Unsure`

Double-review labels are hidden from the other reviewer. Model scores and reason
codes are restricted to System Managers during labeling to reduce anchoring
bias. `Unsure` and reviewer disagreement require management adjudication. Any
first-pass `Same` label automatically requires a second independent reviewer,
even when the pair was not in the original double-review subset. Cohen's kappa
is reported only when label variation makes it estimable; raw agreement and
label-pattern counts are always retained. An ordinary submission cannot be
replaced after it is recorded; reconciliation is stored as a separate manager
adjudication so the independent labels remain auditable.

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
- neither threshold is marked validation-ready until both partitions contain
  at least the policy's configured number of confirmed `Same` labels (default
  10 per split);
- if held-out precision or sample size fails, the High tier is disabled.

The probabilistic model may add a pair to the review queue, but it cannot
downgrade a deterministic Review signal. An uncalibrated deterministic High is
also retained as review-only rather than automatic matching.

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
`CCD Master` field and records identifier scope/reliability. Start strong-ID
scope values as `Unknown` until profiling and governance review are complete.
The governed `pilot-1.6` exception is HKID: a mapped HKID field is global only
for values that are structurally complete and pass the official check-digit
calculation. Partial values, masks such as `*` or `X`, and invalid check digits
remain unverified review-only evidence and never create a deterministic High
match or conflict.
The default installer derives these rows from each live
`CCD Registration.fieldmatch` table. It accepts only explicitly allow-listed
identity targets (names, phone, email, birthday, HKSR number, and HKID), so a
centre may retain any additional operational fields without those fields
unexpectedly influencing identity scores. Registrations missing from the live
system are reported and skipped rather than guessed.

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

An adjudicated `Same` still requires two distinct people to have submitted
`Same`. Until that independent confirmation exists, the pair is shown as
`Positive Confirmation Required` and has no final label. Adjudication authority
does not replace independent positive evidence.

### Cluster safety

The pilot checks connected groups within the retained review sample for
contradictions. If A–B and B–C are links but A–C has a trusted identifier
conflict, affected sampled edges are flagged for management attention. The
pilot does not create or merge person clusters. Full-population cluster
validation remains a separate prerequisite before production grouping.

## Reversible recommendation canary

The post-evaluation canary is implemented in `api_fuzzy_canary.py` and uses
three dedicated DocTypes:

- `CCD Match Canary Run` freezes the policy snapshot, approved evidence runs,
  approved Splink Review cutoff, data snapshot, and aggregate safety result.
- `CCD Match Recommendation` stores a versioned pair-level `Proposed`,
  `Active`, `Exception`, `Reversed`, or `Superseded` decision.
- `CCD Match Recommendation Event` is an immutable lifecycle audit record.

The full governed candidate population is regenerated for each preview. Any
candidate truncation or skipped block fails the run. The canary then evaluates
only deterministic Tiered High edges and applies these gates before a
recommendation can be proposed:

1. the evidence reason must be the validated exact-full-name plus independent
   phone, birthday, or email rule;
2. the source pair must be represented in the approved High validation;
3. neither endpoint may have changed after the run snapshot;
4. a connected component may contain at most one record from each source;
5. a component may not contain contradictory complete trusted identifiers; and
6. a component may not contain a transitive Tiered conflict.

Passing edges are only reversible recommendations. They do not create a person
cluster, merge records, set `Is Matched?`, or update `CCD Master.match_table`.
Activation is separate from preview generation. A subsequent active canary
supersedes the prior version while preserving both histories, and a manager can
reverse one recommendation or the whole canary with a mandatory reason.

## Operations

### 1. Install and migrate

Install `requirements.txt` in every process image/environment that can execute
the API or long queue, migrate the site, then restart services:

```bash
cd /home/frappe/frappe-bench
./env/bin/pip install -r apps/db_connector/db_connector/requirements.txt
bench --site <site> migrate
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_matching_roles
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_default_pilot_policy
```

Both helper commands are idempotent. The second creates `pilot-1.6` only when
missing and imports governed source mappings. HKID is the only default trusted
global identifier and is still gated per value by complete-format/check-digit
validation.

### 2. Profile and configure

Create a Draft policy and its source mappings. Inspect the run's source profile
for coverage, distinctness, duplicates, HKID validity, and cross-source overlap.
Profiling never promotes an identifier beyond the explicit policy. HKID's
complete-value rule reflects the recorded governance decision; all other strong
identifiers remain unverified until separately approved.

### 3. Queue a recommendation-only run

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_evaluation_run \
  --kwargs '{"policy_name":"pilot-1.6","sample_size":500,"double_review_count":100}'
```

`install_evaluation_run` is deliberately bench-only and avoids putting an
ERPNext login password in shell history. Desk integrations must call the
manager-protected whitelisted `enqueue_evaluation` method instead.

For a separate positive-enriched blocking benchmark, use:

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_positive_benchmark_run \
  --kwargs '{"policy_name":"pilot-1.6","sample_size":100,"double_review_count":20}'
```

This benchmark discovers unseen cross-source pairs from legacy score rows at or
above 0.90, resolves both records against the current CCD snapshot, and hides
the discovery score from reviewers. Legacy score is never treated as ground
truth or as a model feature. Finalization reports recovery of human-confirmed
matches by the current blocking rules, but marks the cohort non-representative
and disables all deployable thresholds. Previously labeled pairs are excluded.

After the representative evaluation, validate the narrow deterministic High
tier on fresh predictions with:

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_high_tier_validation_run \
  --kwargs '{"policy_name":"pilot-1.6","sample_size":100}'
```

This run selects a reproducible uniform bottom-k sample from all previously
unseen High predictions at the locked snapshot. It records the full eligible
High population and source/route distributions, and assigns every sampled pair
to two independent reviewers. Finalization reports conditional High precision
with a Wilson 95% interval. Because the cohort contains only model-predicted
High pairs, it cannot estimate recall or calibrate a general score threshold;
those outputs remain disabled and production matching remains unchanged.
The dedicated High-validation metric therefore reports precision and its
confidence interval only; recall/F1 values from a High-only cohort are not
presented as deployable evidence.

### Docker persistence on the managed host

The host's `backend` directory is an SSHFS view from the backend container; it
is not a host bind mount into the containers. The deployment scripts therefore
keep a separate full app copy at
`/root/erpnext_docker_volume/persistent_apps/db_connector` and restore/overlay
that copy into backend, scheduler, and both queue workers. The restart helper
uses this deployment path and then remounts the backend view idempotently.

This protects the `db_connector` fuzzy component through container recreation.
Continue to use normal site/database backups, and separately persist any other
private apps in the ERPNext stack.

For Python-only changes, the deployment helper accepts `--code-only`; do not
use that option for DocType, dependency, or asset changes.

If a completed review set was created while only the optional statistical
adapter was unavailable, an operator can repair that column without changing
the snapshot, sampled pairs, or review assignments:

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_probability_repair \
  --kwargs '{"run_name":"<evaluation-run>"}'
```

If a repaired adapter invalidates only the optional probability column of an
already finalized run, an operator may explicitly reopen that locked run:

```bash
bench --site <site> execute db_connector.api_fuzzy_evaluation.install_probability_repair \
  --kwargs '{"run_name":"<evaluation-run>","reopen_finalized":true}'
```

This records the previous status/decision and adapter version, clears the stale
metrics, resets management approval to pending, and recomputes only the
probability column. The snapshot, sampled pairs, human labels, and deterministic
scores remain unchanged before the run is finalized again.

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

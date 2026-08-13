# POC Presentation and Live-Demo Guide

This guide turns [POC_REPORT.md](POC_REPORT.md) into a 12–15 minute presentation
for management. Do not use screenshots containing client names, identifiers,
phones, emails, birthdays, source keys, or credentials.

## Presentation sequence

### 1. The problem — 1 minute

- CCD Master contains approximately 251,000 governed records from multiple
  sources.
- The existing fuzzy percentage looks authoritative but is not a probability.
- Reviewing every record or candidate pair is operationally impossible.
- POC objective: prove a narrow automatic recommendation tier and leave humans
  only exceptions.

Suggested statement:

> We are not trying to automate every fuzzy match. We are proving which narrow
> evidence combinations can be safely recommended automatically and measuring
> them against blinded human decisions.

### 2. Safety premise — 1 minute

- Shadow-only: no production match table or `Is Matched?` changes.
- Local processing: no client data sent to an external AI service.
- Versioned source mappings and policy snapshot.
- Complete valid HKID may be governed globally; masked/partial values cannot.
- Missing evidence is not disagreement and is never exact agreement.

### 3. Five methods — 2 minutes

| Method | Simple explanation | Outcome |
| --- | --- | --- |
| Baseline | Existing weighted fuzzy percentage | Control only; insufficient precision |
| Tiered Evidence | Rules based on evidence meaning | Narrow High rule approved |
| Recoverable Conflict | Tests safe handling of identifier conflicts | Human routing only |
| Splink | Local statistical record linkage | Review ranking only |
| Hybrid | Splink inside deterministic safety gates | Shadow only without a probability High threshold |

Emphasize that the methods have different jobs; the project did not simply
choose one score and discard everything else.

### 4. Human validation workflow — 2 minutes

Show the workflow diagram from the POC report. Explain:

- Representative 500-pair evaluation.
- Blinded Same/Different/Unsure labels.
- 100 randomized double reviews.
- Every Same required two independent confirmations.
- Disagreements required adjudication.
- Previously reviewed pairs were excluded from later tests.

### 5. Results — 2 minutes

Lead with the conclusion:

- Latest baseline current flag: 23.64% held-out precision.
- General Splink model: no validated automatic High threshold.
- Approved Splink first-priority Review cutoff: `0.938995074`, with 56.52%
  held-out precision and 61.90% held-out recall.
- Tiered High targeted validation: 100/100 Same.
- Precision: 100%; 95% confidence lower bound: 96.30%.
- Management approved the validation evidence.

State the statistical limitation:

> This establishes conditional precision for the High rule. It does not claim
> that the rule finds every duplicate, and it does not guarantee zero errors.

### 6. Quality-control discoveries — 2 minutes

Use these findings to demonstrate that the POC tested the system, not just a
formula:

- Invalid/partial HKIDs were prevented from becoming global identifiers.
- A sequential phone placeholder was removed from identity evidence.
- Splink missing values were corrected from empty strings to nulls.
- Random double-review metrics were separated from positive confirmations.
- The 500-pair conclusion was recalculated after the Splink correction and
  still did not support a probabilistic automatic High threshold.
- The latest `pilot-1.6` run generated 821,592 candidates without truncation or
  an oversized skipped block.

### 7. Feasible operating workload — 2 minutes

- Do not review approximately 251,000 records.
- Do not review approximately 821,000 candidate pairs.
- Emit safe deterministic High recommendations automatically after canary
  authorization.
- Human work consists of conflicts, one-to-many/cluster exceptions, sparse
  unvalidated source groups, and small random QC samples.
- Splink ranks optional Review work; it does not turn every Review candidate
  into a mandatory task.
- Its approved `0.938995074` cutoff is the first-priority band, not a
  Same/Different boundary. Lower-scored deterministic Review/Conflict pairs
  remain review candidates when capacity or operational need permits.

### 8. Requested next decision — 1 minute

Ask management to authorize only:

> Activation of the 3,528 safety-gated `Proposed` records inside the reversible
> recommendation register. This does not merge records, set `Is Matched?`, or
> populate the production matching table; the 433 exceptions remain inactive.

## Presentation and live-demo checklist

The policy document and its evaluation run have separate lifecycles. At the
time the evaluation evidence was prepared, `pilot-1.6` was **Draft**, while its
High Tier Validation run was **Completed / Approved**. It was subsequently and
separately promoted to **Pilot** to create the recommendation-only preview. It
has not been promoted to `Approved`, and the preview has not been activated.

1. In ERPNext, open `pilot-1.6` and identify it as the **Pilot policy whose
   unchanged frozen snapshot was evaluated before promotion**.
2. In ERPNext, open the approved High Tier Validation run and show its header:
   purpose `High Tier Validation`, status `Completed`, approval status
   `Approved`, sampled pairs `100`, and double-review count `100`. Keep its
   internal document ID out of public slides and recordings.
3. Use the sanitized [`POC_RESULTS.json`](POC_RESULTS.json) or the Results
   section of [`POC_REPORT.md`](POC_REPORT.md) for management metrics. Do not
   screen-share the complete raw `Metrics` JSON field: it contains internal
   source-pair labels as well as technical diagnostics.
4. Explain the decisive High-validation metrics: 100 predictions sampled, 100
   confirmed Same, 0 confirmed Different, precision 100%, Wilson 95% interval
   96.30%–100%, and recall not estimated.
5. Show [`POC_SYNTHETIC_EXAMPLES.md`](POC_SYNTHETIC_EXAMPLES.md), particularly
   Example B, to demonstrate that `Model Review` can become `Human Confirmed
   Same` without changing the recorded model tier.
6. If audit workflow must be demonstrated, use only the synthetic sequence in
   that file. Do not open real pair records in a recorded presentation.
7. State the run diagnostics rather than exposing internal labels: candidate
   generation was not truncated and no oversized block was skipped in the
   corrected High run.
8. Open the latest `Ready` canary and show aggregate fields only: 251,520
   records, 821,592 candidates, 3,961 Tiered High candidates, 3,528 Proposed,
   433 Exception, zero Active, no truncation, and no skipped block. Do not open
   real recommendation rows in a recorded presentation.
9. Explain that all 433 exceptions are one-to-many source conflicts and that
   the immutable event count equals the recommendation count, 3,961.
10. Show that the preview did not modify production match fields, then stop
    before pressing **Activate Recommendations**.

### What the ERPNext `Metrics` field means

`Metrics` is read-only JSON generated when an evaluation run is finalized. It
is the full technical result, not a single score and not a management approval.
Its main sections are:

| JSON section | Meaning |
| --- | --- |
| `run_purpose`, `labeled_pairs` | What question the run tested and how many finalized pairs were usable |
| `reviewer_agreement` | Double-review completion, label patterns, raw agreement, and kappa |
| `automatic_matching_readiness` | Whether this run alone can produce a general deployable threshold |
| `high_tier_validation` | The decisive conditional-precision result for a High-only validation run |
| `models` | Confusion-matrix and calibration diagnostics for the five shadow outputs |

For the approved High run, `automatic_matching_readiness.ready` is `false` with reason
`high_tier_validation_nonrepresentative`. This is expected: the run sampled
only predictions already classified High. It validates precision conditional
on that narrow rule, but cannot estimate population recall or calibrate a
general score threshold.

Two technical values should not be presented without that context:

- The generic model block can display recall `1.0` because all sampled rows
  were predicted High. The authoritative `high_tier_validation` block correctly
  says `recall_estimated: false`.
- Cohen's kappa is unstable in this all-positive cohort even though raw reviewer
  agreement was 98%. Use the representative random double-review cohort for
  inter-reviewer reliability, and use 98% only as the High run's raw agreement.

Do not open arbitrary real pair records during a recorded presentation. If a
real example is operationally required, restrict the audience, do not record
the screen, and follow the organization's privacy policy.

## Expected management questions

### “Does High mean definitely the same person?”

No. It means a versioned rule produced a high-confidence pair recommendation.
The measured precision lower bound is 96.30%, not 100% certainty. Recommendations
remain reversible and pass cluster/conflict gates.

### “Do staff need to review hundreds of thousands of records?”

No. Staff review safety exceptions, optional operational cases, and periodic QC
samples. Review-tier candidates are not automatically a mandatory backlog.

### “Why not use the highest fuzzy or Splink score?”

The baseline score is not calibrated, and corrected Splink scores did not yield
a High threshold that met the 95% precision and minimum-sample requirements.

### “Does a Splink score below 0.938995074 mean Different?”

No. The cutoff maximized calibration F1 for first-priority human work. In the
latest labeled cohort, 27/70 confirmed Same pairs scored below it. Lower scores
remain ranked candidates and deterministic Review/Conflict signals are not
discarded. Only human review can confirm Same or Different in this tier.

### “Can staff approve a Review pair?”

Yes. It becomes `Human Confirmed Same`; the original model tier remains
unchanged for audit. A human confirmation is not misreported as model High.

### “Are the other models abandoned?”

No. Baseline remains the control, Recoverable Conflict routes exceptions,
Splink ranks Review candidates, and Hybrid remains available for future
threshold validation.

### “What happens if a new centre is added?”

Its mappings must be explicitly registered and governed. New source groups are
monitored and can remain exception-only until adequate validation exists.

### “Can approval merge records now?”

No. Activating this canary changes only `Proposed` recommendation status to
`Active` and appends audit events. It does not merge records, set `Is Matched?`,
or write the Matching Score table. Any production action remains a later,
separate decision.

## Demo success criteria

- Management understands why the baseline fuzzy percentage was insufficient.
- Management can distinguish model High from Human Confirmed Same.
- Management sees quantitative evidence and confidence bounds.
- Management understands that human workload is exception-based.
- The requested decision is limited to activating reversible recommendation
  records; no production match action is included.

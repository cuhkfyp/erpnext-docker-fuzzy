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

- Baseline current flag: only 33.33% precision on the challenge set.
- General Splink model: no validated automatic High threshold.
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

### 7. Feasible operating workload — 2 minutes

- Do not review approximately 251,000 records.
- Do not review approximately 821,000 candidate pairs.
- Emit safe deterministic High recommendations automatically after canary
  authorization.
- Human work consists of conflicts, one-to-many/cluster exceptions, sparse
  unvalidated source groups, and small random QC samples.
- Splink ranks optional Review work; it does not turn every Review candidate
  into a mandatory task.

### 8. Requested next decision — 1 minute

Ask management to authorize only:

> A reversible recommendation-only canary for the approved Tiered High rule,
> with exception-only human review and no automatic merge or `Is Matched?`.

## Live ERPNext demo checklist

1. Open the approved matching policy and show its version/status.
2. Open the completed High validation run and show aggregate metrics only.
3. Show that all 100 pairs were double reviewed and finalized.
4. On a prepared non-sensitive/synthetic example, show masked evidence and the
   difference between model tier and human final label.
5. Show the audit trail for two ordinary reviews plus adjudication.
6. Show candidate truncation/skipped-block fields are clear on the corrected
   High run.
7. Show that production match fields were not modified by the shadow run.
8. End with the recommendation-only canary diagram.

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

No. The approved decision accepts POC evidence. A recommendation canary and a
separate production-action decision come next.

## Demo success criteria

- Management understands why the baseline fuzzy percentage was insufficient.
- Management can distinguish model High from Human Confirmed Same.
- Management sees quantitative evidence and confidence bounds.
- Management understands that human workload is exception-based.
- The requested decision is limited to a reversible recommendation-only canary.

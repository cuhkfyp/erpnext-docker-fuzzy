# Presentation-Safe Synthetic Matching Examples

Every value and record key on this page is fictional and exists only in this
document. These examples are not CCD Master records and must not be inserted
into production merely for a presentation. Values are masked even though they
are synthetic, so the screen resembles the reviewer experience without
revealing client or infrastructure data.

Model tier and human decision answer different questions:

- **Model tier** records what a specific versioned method concluded from the
  available evidence.
- **Human final decision** records what independent reviewers concluded after
  permitted review and, where necessary, adjudication.
- A human decision never rewrites the historical model tier.

## Example A — narrow deterministic High confirmed by QC

| Item | Synthetic left | Synthetic right | Comparison |
| --- | --- | --- | --- |
| Record key | `SYN-A-L` | `SYN-A-R` | Different source records |
| English full name | `CASE, ALEX` | `CASE, ALEX` | Exact |
| Phone | `****0101` | `****0101` | Exact independent evidence |
| Birthday | Not available | Not available | Missing, not a disagreement |
| Trusted identifier | Not available | Not available | No conflict |

**Tiered Evidence output:** `High`

**Reason:** exact full name plus exact independent phone evidence, with no
trusted-identifier conflict.

**QC human final decision:** `Same`.

This illustrates the narrow rule validated by the approved High validation
run. It does not imply that every similar-looking pair is automatically merged.

## Example B — Model Review becomes Human Confirmed Same

| Item | Synthetic left | Synthetic right | Comparison |
| --- | --- | --- | --- |
| Record key | `SYN-B-L` | `SYN-B-R` | Different source records |
| English full name | `SAMPLE, ALEX` | `SAMPLE, ALEXANDER` | Similar, not exact |
| Birthday | `19**-**-07` | `19**-**-07` | Exact independent evidence |
| Phone | `****0201` | Not available | Missing on one side |
| Trusted identifier | Not available | Not available | No conflict |

**Tiered Evidence output:** `Review`

**Reason:** there is name support and exact birthday evidence, but the full name
is not exact, so the automatic High rule is not satisfied.

**Reviewer 1:** `Same`

**Reviewer 2:** `Same`

**Human final decision:** `Human Confirmed Same`

**Stored historical model tier:** still `Review`.

This is the clearest presentation example of why a human can approve a
Review-ranked pair without falsely reporting that the model predicted High.

## Example C — identifier conflict remains an exception

| Item | Synthetic left | Synthetic right | Comparison |
| --- | --- | --- | --- |
| Record key | `SYN-C-L` | `SYN-C-R` | Different source records |
| Chinese full name | `示例○明` | `示例○明` | Exact |
| Phone | `****0301` | `****0301` | Exact independent evidence |
| Complete trusted ID | `*****A` | `*****B` | Conflict |

**Tiered gated output:** `Conflict Review`

**Recoverable-conflict output:** `Review`, never `High`

**Human workflow:** independent review and adjudication if reviewers disagree.

This demonstrates that supporting name and phone evidence cannot silently
override a complete trusted-identifier conflict.

## Synthetic audit sequence for a recorded demo

Use Example B and narrate this fictional sequence instead of opening a real
evaluation pair:

1. Model version `pilot-1.6` records tier `Review` and its reason codes.
2. Reviewer A submits `Same`.
3. The positive decision requires an independent confirmation.
4. Reviewer B submits `Same` without seeing Reviewer A's answer.
5. The final outcome becomes `Human Confirmed Same`.
6. The immutable model tier remains `Review`; reviewer identities and
   timestamps form the audit trail.

If the two reviewers disagree or either chooses `Unsure`, an authorized
adjudicator records the final human outcome. None of these actions changes the
original shadow-model prediction.

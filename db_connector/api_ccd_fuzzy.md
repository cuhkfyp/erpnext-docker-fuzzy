# api_ccd_fuzzy.py — CCD Fuzzy Customer Matching Engine

## Table of Contents

1. [Purpose](#1-purpose)
2. [System Context](#2-system-context)
3. [Library Stack](#3-library-stack)
4. [Architecture Overview](#4-architecture-overview)
5. [Formula Syntax Guide](#5-formula-syntax-guide)
6. [Function Reference](#6-function-reference)
7. [Performance Design](#7-performance-design)
8. [How to Test](#8-how-to-test)
9. [Known Limitations](#9-known-limitations)
10. [Phase Roadmap](#10-phase-roadmap)
11. [Shadow Pilot and Future Policy](#11-shadow-pilot-and-future-policy)

---

## 1. Purpose

Multiple HKSR service centres independently enter customer data into their own
**CCD Registration** system (e.g. `CENTRE-A-UAT`, `CENTRE-B-UAT`). The same
person can be registered in several centres under slightly different names or
formats:

| Centre | Chinese name         | English name |
| ------ | -------------------- | ------------ |
| A      | 陳大文 (Traditional) | CHAN Tai Man |
| HKI    | 陈大文 (Simplified)  | Tai Man Chan |
| KLN    | Chan, Tai Man        | (missing)    |

`api_ccd_fuzzy.py` is a **server-side Frappe Python module** that:

- Compares every record in one centre against every record in all other centres
- Scores the similarity using a configurable formula (Chinese name, English name,
  phone, HKID)
- Writes identified potential duplicates into the **Matching Score** child table
  (`match_table`) of each **CCD Master** record
- Stores an HTML audit breakdown on every accepted match row, showing the values,
  rule scores, and formula calculation used to produce the combined score
- Lets the supervisor review and confirm which cross-centre records are the same
  person without opening a separate record to understand why the match was made

---

## 2. System Context

### ERPNext / Frappe doctypes involved

| Doctype               | Role                                                                                                                                                                                |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CCD Registration`    | One record per centre. Stores the centre's `fuzzymachingscript` formula and field-mapping config.                                                                                   |
| `CCD Master`          | Unified master record. Has a `ccd_reg_source` field (which centre it came from) and a `ccd_source_key` (unique key from the source system).                                         |
| `CCD Master matching` | Child table of CCD Master. Each row is one potential duplicate, with `mas_client`, `client`, `client_id`, `score`, and an HTML audit breakdown stored in `match_equation`.          |

### Data flow

```
CCD Registration (centre config)
        │  fuzzymachingscript formula
        ▼
api_ccd_fuzzy.py  ←── CCD Master (all centres' records)
        │
        ▼
CCD Master → Matching Score tab → rows in tabCCD Master matching
                                      │
                                      └─ match_equation HTML audit:
                                         rule values + scores + formula trail
```

### Key field names (actual DB column names)

> **Note:** The `fuzzymachingscript` field on `CCD Registration` has a typo in
> its original definition ("maching" not "matching", no underscores). The code
> uses the actual DB column name — do not "fix" the spelling or the lookup will
> break.

---

## 3. Library Stack

| Library     | Version | Purpose                                                     | Why this library                                                                                                   |
| ----------- | ------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `rapidfuzz` | ≥3.x    | Fuzzy string similarity for English names and phone numbers | C-extension, 10–100× faster than pure-Python `fuzzywuzzy`; exposes `ratio`, `token_sort_ratio`, `token_set_ratio`  |
| `pypinyin`  | ≥0.5    | Convert Chinese characters → pinyin romanisation            | Needed to compare names that sound the same but use different characters (e.g. 陳 vs 陈, or different IME choices) |
| `hanziconv` | ≥0.3    | Traditional ↔ Simplified Chinese normalisation              | So 陳 (Traditional) and 陈 (Simplified) are treated as identical before any comparison                             |

All three are installed in the Frappe bench virtualenv
(`/home/frappe/frappe-bench/env/`). `rapidfuzz` and `pypinyin` were already
in the Docker image; `hanziconv` was added via
`/home/frappe/frappe-bench/env/bin/pip install hanziconv` and declared in
`db_connector/requirements.txt` for persistence across container rebuilds.

---

## 4. Architecture Overview

### Phase 1 (implemented — this file)

```
run_fuzzy_match_for_center(hostname, ..., specific_records=None)
│
├─ 1. Load formula from CCD Registration
│      _get_formula_and_fields(hostname)
│      → formula text + chinese_fields list + english_fields list
│
├─ 2. Pre-compile formula  [ONCE per job]
│      compile_formula(formula_text)
│      → precompute_fn  (normalise one record: HanziConv + pypinyin)
│      → pair_fn        (score one source×candidate pair and return rule scores)
│      → slot_details   (rule/function and target-field metadata)
│
├─ 3. Load data via direct SQL  [faster than frappe.db.get_all for 80K rows]
│      SELECT * FROM `tabCCD Master` WHERE ccd_reg_source = hostname
│      optionally restrict source rows by specific_records (document names)
│      SELECT * FROM `tabCCD Master` WHERE ccd_reg_source != hostname
│
├─ 4. Pre-normalise ALL records  [ONCE per record]
│      for each record: row['_fc'] = precompute(row)
│      → stores (simplified_string, pinyin_string) tuples in memory
│      → eliminates redundant HanziConv/pypinyin calls in the hot loop
│
├─ 5. Build blocking index  [reduces O(n×m) to ~O(n × m/26)]
│      compute_block_keys(row, chinese_fields, english_fields)
│      → Chinese key: first pinyin initial of each Chinese field value
│        e.g. 黃 → "huang" → H → key "cn_chi_surname_H"
│      → English key: first 3 lowercase chars of English field value
│        e.g. "Wong Tai" → key "en_won"
│      block_index: { block_key → [candidate_rows...] }
│
├─ 6. Batch-delete stale match rows  [one SQL DELETE for all source docs]
│
└─ 7. Main matching loop
       for each source record:
         a. find candidates sharing ≥1 block key
         b. pair_fn(src_cache, cand_cache) → (score, is_match, rule_scores)
         c. for each accepted match, build an HTML audit table
         d. collect the score and audit HTML
       _insert_match_rows(all_new_rows)
       frappe.db.commit()
```

### Key design decisions

| Decision                                  | Reason                                                                                                                                                                                                           |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Direct SQL instead of `frappe.db.get_all` | Frappe ORM adds per-row Python overhead. At 80K rows this is prohibitive. Direct SQL is 5–20× faster for bulk reads.                                                                                             |
| Pre-compile formula (`compile_formula`)   | Avoids re-parsing the formula string and re-`eval()`ing the arithmetic expression on every comparison. Arithmetic is compiled to a Python code object once.                                                      |
| Pre-normalise records (`precompute`)      | `hanziconv` + `pypinyin` are expensive per call. A candidate record may appear in thousands of source comparisons — pre-computing once saves thousands of redundant calls.                                       |
| Blocking index                            | Limits which candidates each source record is compared against. Two records must share a surname-initial or English-prefix block key to be compared. Reduces ~80K × 50K = 4B comparisons to a manageable subset. |
| Batch DELETE before INSERT                | Guarantees no stale rows from previous runs. One DELETE per job (not one per record) minimises SQL round-trips.                                                                                                  |
| Per-match HTML audit                      | Stores the scoring evidence with the match row so a supervisor can review the comparison directly inside CCD Master.                                                                                           |

### Match audit table

For every candidate that passes the configured threshold,
`building_html_audit_table()` creates an HTML explanation and stores it in the
child row's `match_equation` field. The audit is attached to the accepted match
row, not to candidates that were rejected by the threshold.

The rendered table contains:

| Column                         | Meaning                                                                    |
| ------------------------------ | -------------------------------------------------------------------------- |
| Matching Rule / Function       | The formula macro, such as `@ChineseMatch` or `@PhoneMatch`.               |
| Target Field                   | The configured field evaluated by that macro.                              |
| Current Record (Doc)           | The cached value from the CCD Master record being processed.               |
| Target Record (Master)         | The cached value from the candidate record in another centre.              |
| Function Score                 | The individual rule score as a percentage and a decimal from 0.00 to 1.00. |

Function scores use a green indicator for scores of at least 70%, orange for
scores from 40% to below 70%, and red for scores below 40%. Beneath the table,
the **Formula Evaluation Trail** substitutes the rule results into the weighted
formula and displays the **Total Combined Score**. This is the same combined
score written to the match row's `score` field when the formula uses the audit
trail's supported syntax described under [Known Limitations](#audit-formula-trail-syntax).

---

## 5. Formula Syntax Guide

Each centre's supervisor enters a formula in the `fuzzymachingscript` field of
the centre's **CCD Registration** record. The formula defines:

- Which fields to compare
- What weight to give each comparison
- What score threshold counts as a "match"

### Syntax

```
( @MacroName(field_expression) * weight  [+ ...] ) > threshold
```

### Available macros

| Macro                 | Compares             | Score                                                                                             |
| --------------------- | -------------------- | ------------------------------------------------------------------------------------------------- |
| `@ChineseMatch(expr)` | Chinese name strings | 0.0–1.0 via HanziConv normalisation + rapidfuzz character similarity + pinyin phonetic similarity |
| `@EnglishMatch(expr)` | English name strings | 0.0–1.0 via token_set_ratio (handles word-order differences)                                      |
| `@PhoneMatch(expr)`   | Phone numbers        | 0.0–1.0; normalises `(+852)`, `00852` formats first; exact match = 1.0                            |
| `@IDMatch(expr)`      | HKID / staff numbers | 1.0 or 0.0 only; normalises case and hyphens; exact match required                                |

### Field expressions inside a macro

| Form                   | What it does                                    | Example                                          |
| ---------------------- | ----------------------------------------------- | ------------------------------------------------ |
| `f"{field1} {field2}"` | Concatenates two fields with a space            | `f"{chi_firstname} {chi_surname}"` → `"和強 黃"` |
| `f"field_name"`        | Reads a single field (f-string syntax, no `{}`) | Same as plain string form below                  |
| `"field_name"`         | Reads a single field                            | `"eng_firstname"` → `"Wo Keung"`                 |

### Threshold

The trailing `> 0.4` (or `>= 0.5`, `> 0.6`, etc.) sets the minimum weighted
score to store as a match. If omitted, the default threshold is `0.5`.

### Example formula

```
(@ChineseMatch(f"{chi_firstname} {chi_surname}")*0.5
 + @EnglishMatch(f"eng_firstname") * 0.4) > 0.4
```

Breakdown:

- Chinese name similarity (given + surname combined) contributes up to 0.5
- English first-name similarity contributes up to 0.4
- Maximum possible score = 0.9
- Anything scoring > 0.4 is written to the Matching Score tab

### Score interpretation guide

> These ranges describe the legacy formula output only. They have not been
> calibrated as identity probabilities and must not be used as automatic
> same-person decisions. Missing fields currently contribute zero, which is one
> reason the shadow pilot uses evidence tiers and local calibration instead.

| Score range | Typical meaning                                              |
| ----------- | ------------------------------------------------------------ |
| 0.85 – 0.90 | Almost certainly the same person (same name, same phonetics) |
| 0.65 – 0.84 | Strong candidate — one name component differs slightly       |
| 0.40 – 0.64 | Weak candidate — share a surname or one name component       |
| < 0.40      | Not stored (below threshold)                                 |

### Tuning recommendations

> The values below are retained to document the old program. For new matching
> policy decisions, do not choose a threshold from intuition; use the reviewed,
> held-out process in [`MATCHING_PILOT.md`](MATCHING_PILOT.md).

| Situation                            | Suggested threshold |
| ------------------------------------ | ------------------- |
| Initial discovery run, cast wide net | `> 0.4`             |
| Normal production run                | `> 0.55`            |
| High-confidence only                 | `> 0.7`             |

---

## 6. Function Reference

### Low-level match functions

#### `chinese_match(str_a, str_b) → float`

Compares two Chinese name strings.

1. `hanziconv.HanziConv.toSimplified()` — normalise Traditional → Simplified
2. `rapidfuzz.fuzz.ratio` — character-level similarity
3. `pypinyin.lazy_pinyin` + `rapidfuzz.fuzz.token_sort_ratio` — phonetic similarity
4. Returns `max(char_sim, pinyin_sim)`

#### `english_match(str_a, str_b) → float`

Compares two English name strings using `rapidfuzz.fuzz.token_set_ratio`.
Handles word-order differences: `"CHAN TAI MAN"` vs `"TAI MAN CHAN"` → 1.0.

#### `phone_match(str_a, str_b) → float`

Normalises phone formats (`(+852) …`, `00852…`) then compares.
Exact match → 1.0; partial similarity otherwise.

#### `id_match(str_a, str_b) → float`

Strips spaces/hyphens, uppercases, then exact-match only.
Returns 1.0 or 0.0.

---

### Formula engine

#### `_eval_expr(expr, row) → str`

Evaluates a macro argument expression against a record dict.
Handles `f"{field1} {field2}"`, `f"field"`, `"field"`, and bare field names.

#### `evaluate_fuzzy_formula(formula_text, source_row, candidate_row) → (float, bool)`

Single-pair formula evaluator (used in tests and smoke-checks).
Parses and evaluates the formula fresh on every call — **not for bulk use**.
Returns `(raw_score, is_match)`.

#### `compile_formula(formula_text) → (precompute_fn, pair_fn, slot_details)`

Bulk-optimised formula compiler. Call once, reuse for all comparisons.

- **`precompute_fn(row) → cache_dict`** — normalise one record; call once per record
- **`pair_fn(src_cache, cand_cache) → (score, is_match, rule_scores)`** — score
  one pair using cached values and return the per-rule scores used by the audit
- **`slot_details`** — ordered `(slot, macro_type, field_expression)` metadata
  used to label each audit row

`rule_scores` is a dictionary keyed by the compiler's internal slot names. It is
an intermediate value used to build `match_equation`; callers normally use the
combined `score` and `is_match` values.

#### `building_html_audit_table(slot_details, rule_scores, src_cache, cand_cache, formula) → str`

Builds the HTML audit explanation for one accepted source/candidate match.
It combines the formula metadata, cached record values, and individual rule
scores into the comparison table and formula evaluation trail described in
[Match audit table](#match-audit-table). Returns an empty string when no formula
is supplied.

---

### Blocking index

#### `_extract_formula_fields(formula_text) → (chinese_fields, english_fields)`

Parses the formula and returns the field names used in `@ChineseMatch` and
`@EnglishMatch` macros. These field names drive the blocking key generation.

#### `compute_block_keys(row, chinese_fields, english_fields) → set[str]`

Computes the set of blocking keys for a record.

- Chinese key per field: `cn_<field>_<pinyin_initial>` e.g. `cn_chi_surname_H`
- English key: `en_<first_3_chars>` e.g. `en_won`
- Fallback: `__all__` (triggers full O(n×m) scan — only when no fields populated)

---

### Data helpers

#### `_get_formula_and_fields(hostname) → (formula, chinese_fields, english_fields)`

Reads `fuzzymachingscript` from the `CCD Registration` doc for `hostname`.

#### `_insert_match_rows(rows)`

Bulk-inserts rows into `tabCCD Master matching` via direct SQL.
Each row tuple contains:

```
(source_doc_name, mas_client, client, client_id,
 score, rule_scores, html_table)
```

The helper writes `score` to the numeric score field and `html_table` to
`match_equation`. `rule_scores` is carried with the internal row tuple but is
not written as a separate database column.

#### `_clear_match_table(source_doc_name)`

Deletes all match rows for a single CCD Master document (kept for internal use).

---

### Main entry points

#### `run_fuzzy_match_for_center(hostname, changed_keys=None, is_new_center=False, limit=None, specific_records=None) → dict`

Main Phase 1 entry point.

| Parameter          | Type                | Purpose                                                                                           |
| ------------------ | ------------------- | ------------------------------------------------------------------------------------------------- |
| `hostname`         | `str`               | The CCD Registration name (e.g. `'CENTRE-A-UAT'`).                                                |
| `changed_keys`     | `set \| None`       | Incremental mode: only re-process these `ccd_source_key` values. `None` means no source-key filter. |
| `is_new_center`    | `bool`              | Reserved for Phase 3 cross-trigger logic.                                                         |
| `limit`            | `int \| None`       | Process only the first N selected source records. Use for testing (e.g. `limit=50`).              |
| `specific_records` | `list[str] \| None` | Restrict the source query to these CCD Master document `name` values. `None` means all records for the centre. |

`specific_records` and `changed_keys` identify different fields:

- `specific_records` contains Frappe CCD Master document names, such as
  `CCD-TEST-0001`.
- `changed_keys` contains external `ccd_source_key` values.
- If both are supplied, the runner first selects `specific_records` and then
  applies the `changed_keys` filter to that selected set.

Returns `{'processed': N, 'matches_found': M, 'errors': E}`.

**Run behaviour:**

1. Deletes ALL existing match rows for the processed source records (batch DELETE)
2. Computes fresh matches
3. Builds and stores an HTML audit table for every accepted match
4. Inserts the new match rows
5. Commits

Running the same call twice with unchanged data produces identical results — no
doubling. Running with a higher threshold deletes all old rows and replaces
with only the new (higher-threshold) matches.

#### `run_fuzzy_match_all() → dict`

Runs `run_fuzzy_match_for_center` for every CCD Registration centre.
Intended for the nightly scheduler (Phase 2).

#### `run_fuzzy_match_enqueued(hostname, action, changed_keys_str)`

Frappe `enqueue()` target for background job execution (Phase 2).
`action` is `'full'`, `'incremental'`, or `'new_center'`.
`changed_keys_str` is a JSON array string of `ccd_source_key` values.

---

### Test helpers (bench console)

#### `test_match_functions() → dict`

Smoke-tests all four match functions with known pairs and expected score ranges.

#### `test_formula_eval(formula=None) → dict`

Tests the formula evaluator with synthetic source/candidate rows.
Pass a custom formula string to test a new formula before deploying it.

#### `test_run_for_center(hostname) → dict`

Runs a full match for the given centre with real CCD Master data.
Equivalent to calling `run_fuzzy_match_for_center(hostname)` directly.

---

## 7. Performance Design

### Problem scale

- A large centre may contain tens of thousands of CCD Master records
- Naïve O(n×m) with n=80K and m=50K candidates = **4 billion comparisons**
- Each comparison with HanziConv + pypinyin ≈ 0.5 ms → **23 days** for one centre

### Optimisations applied

| Optimisation                                                                            | Reduction                                                                                      |
| --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| **Blocking index** — only compare records sharing a surname initial or English prefix   | ~1/26 of comparisons survive (surname-based grouping)                                          |
| **Pre-normalise records** — HanziConv + pypinyin run once per record, not once per pair | If a candidate appears in 10K source comparisons, pypinyin runs 1× not 10K×                    |
| **Compile formula** — arithmetic expression compiled to Python code object once         | Eliminates per-pair string regex + `eval(string)` overhead; `eval(code_object)` is ~50× faster |
| **Direct SQL** — `frappe.db.sql` instead of `frappe.db.get_all`                         | Bypasses Frappe ORM per-row Python overhead; ~5–20× faster for 80K rows                        |
| **Batch DELETE** — one SQL DELETE for all source docs before INSERT                     | 80K individual DELETEs → 1 DELETE with IN clause                                               |

### Recommended execution mode

For production (80K records), always run as a **background job** via
`frappe.enqueue` (Phase 2), not synchronously in bench console. The bench
console `limit=50` test is sufficient to verify correctness before scheduling.

---

## 8. How to Test

### Step 1: Verify match functions (no DB needed)

```python
# In bench console
from db_connector.api_ccd_fuzzy import test_match_functions
import json
print(json.dumps(test_match_functions(), indent=2, ensure_ascii=False))
```

### Step 2: Test formula evaluation with synthetic data

```python
from db_connector.api_ccd_fuzzy import test_formula_eval
import json
# Uses default formula; or pass your centre's formula string
print(json.dumps(test_formula_eval(), indent=2, ensure_ascii=False))
```

### Step 3: Run against real data (small sample)

```python
import importlib, db_connector.api_ccd_fuzzy as m
importlib.reload(m)   # pick up any file changes without restarting bench
from db_connector.api_ccd_fuzzy import run_fuzzy_match_for_center

result = run_fuzzy_match_for_center('CENTRE-A-UAT', limit=50)
print(result)
# Expected: {'processed': 50, 'matches_found': N, 'errors': 0}
```

### Step 4: Re-match selected CCD Master records

Use Frappe CCD Master document names—not `ccd_source_key` values—in
`specific_records`:

```python
result = run_fuzzy_match_for_center(
    'CENTRE-A-UAT',
    specific_records=['CCD-TEST-0001'],
)
print(result)
# Expected: {'processed': 1, 'matches_found': N, 'errors': 0}
```

The runner deletes and rebuilds only the selected record's match rows. Candidate
records are still loaded from all other centres.

### Step 5: Check what formula was used

```python
import frappe
doc = frappe.get_doc('CCD Registration', 'CENTRE-A-UAT')
print(doc.get('fuzzymachingscript'))
```

### Step 6: Inspect match rows and audit HTML

```python
rows = frappe.db.sql("""
    SELECT parent, mas_client, client, client_id, score, match_equation
    FROM `tabCCD Master matching`
    ORDER BY score DESC
    LIMIT 50
""", as_dict=True)
import json; print(json.dumps(rows, indent=2, default=str))
```

For accepted matches, `match_equation` should contain an HTML `<table>` and the
text `Formula Evaluation Trail`. The displayed total should agree with `score`
apart from presentation rounding.

### Step 7: Check error log

```python
errs = frappe.db.sql("""
    SELECT title, error FROM `tabError Log`
    WHERE title LIKE 'CCD Fuzzy Match%'
    ORDER BY creation DESC LIMIT 5
""", as_dict=True)
for e in errs:
    print(e['title'], '---', (e['error'] or '')[:400])
```

### Step 8: Verify in UI

Open any **CCD Master** record → **Matching Score** tab.
Open a matching child row and use the **Edit** control under **Result
Description**. The embedded audit should show:

1. One row for each matching rule in the configured formula
2. Current and target record values
3. A percentage and decimal score for every rule
4. Green, orange, or red score indicators at the documented boundaries
5. A formula evaluation trail and total combined score matching the row score

The supervisor can inspect this evidence inside the CCD Master row without
navigating to a separate comparison screen.

---

## 9. Known Limitations

### `fuzzymachingscript` typo

The field name on `CCD Registration` was created with a typo ("maching" instead
of "matching", no underscores). The code deliberately uses the typo'd name.
**Do not rename this field** without also updating `_get_formula_and_fields`.

### Blocking recall risk

Blocking is a recall/speed trade-off. Two records that share no surname initial
AND no English prefix will NOT be compared, even if they are the same person
with completely different entries in both fields. This is an accepted
false-negative risk. Adding `@PhoneMatch` or `@IDMatch` to the formula
does NOT create additional block keys — those fields only add scoring weight
once a pair is already selected by the name-based blocking.

### English blocking — surname vs given name

The English block key uses the **first** non-empty `english_fields` value and
takes its first 3 characters. If the formula references `eng_firstname`
("Wo Keung"), the block key is `en_wo_`. Two people named "Wo Keung Chan"
and "Wo Keung Lee" will share a block key; two people both named "Wong" in
`eng_surname` will share `en_won`. Adjust the formula field order if needed.

### `hanziconv` not persisted across container rebuild

`hanziconv` is not in the base Docker image. It is declared in
`db_connector/requirements.txt` so `bench setup requirements` re-installs it.
After a container rebuild, run:

```bash
docker exec frappe_docker-backend-1 \
  /home/frappe/frappe-bench/env/bin/pip install hanziconv
```

(repeat for scheduler, queue-long, queue-short containers).

### `@IDMatch` is exact-only

HKID matching is strict by design — normalised uppercase comparison only.
A typo in one character = 0.0. This is intentional: ID numbers should not
be fuzzy-matched.

### Audit formula trail syntax

The matching engine and HTML trail accept the formula argument and comparison
forms described in the formula guide. The audit substitutes the exact macro
expressions captured by `compile_formula`, including supported f-string field
expressions, and removes any supported trailing comparison operator before
calculating the displayed total. Record values, labels, and the resulting
formula text are HTML-escaped before storage. The match row's numeric `score`
remains the authoritative combined score.

---

## 10. Phase Roadmap

### Phase 1 — Matching Engine (COMPLETE)

- ✅ Four match functions: Chinese, English, phone, ID
- ✅ Per-centre formula configuration via `fuzzymachingscript`
- ✅ Blocking index (performance)
- ✅ Pre-compilation and pre-normalisation (performance)
- ✅ Results written to `CCD Master matching` child table
- ✅ Per-match HTML audit table stored in `match_equation`
- ✅ `limit` parameter for safe testing
- ✅ `specific_records` parameter for re-matching selected CCD Master documents

### Phase 2 — Scheduler + Background Trigger

- Nightly scheduler: wire `run_fuzzy_match_all` into `hooks.py`
  `scheduler_events → daily`
- Frappe Server Script `trigger_fuzzy_match` (whitelisted API) — lets the
  agent daemon trigger a background enqueue after a sync
- `bench restart` to apply hooks changes

### Phase 3 — Agent Daemon Integration

- Add `TRIGGER_FUZZY_MATCH` macro to `execute_step()` inside
  `build_job_daemon_script()` in `agent_program_18_5_26.py`
- Agent passes `changed_keys` (the `ccd_source_key` values it just synced)
  so only modified records are re-matched (incremental mode)
- Avoids nightly full re-scan for centres that sync frequently

---

## 11. Shadow Pilot and Future Policy

The weighted formula remains the production baseline. It is not improved by
adding `MAX()` around the same expression: that supplies no new evidence and the
legacy evaluator does not implement a `MAX` macro.

The separately implemented shadow evaluator compares the current formula with
deterministic evidence tiers, two strong-identifier conflict policies, a local
Splink/DuckDB Fellegi–Sunter model, and a safety-gated hybrid. Missing data is
neutral, names alone require review, and an identifier is decisive only when
both source profiles explicitly approve it as organization-wide.

The pilot writes only dedicated evaluation DocTypes. It does not change this
module's match rows or set `Is Matched?`. See
[`MATCHING_PILOT.md`](MATCHING_PILOT.md) for policy rules, privacy controls,
review workflow, calibration criteria, deployment commands, and limitations.

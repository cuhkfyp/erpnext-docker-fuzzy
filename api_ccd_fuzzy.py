"""
api_ccd_fuzzy.py — CCD Fuzzy Customer Matching Engine (Phase 1)

Provides fuzzy matching for CCD Master records across multiple centres.

Library stack (all installed in venv):
  rapidfuzz  — fast fuzzy string matching (English names, phone)
  pypinyin   — Chinese character → pinyin phonetics
  hanziconv  — Traditional ↔ Simplified Chinese normalisation

Phase 1 delivers:
  • Four low-level match functions (Chinese, English, phone, ID)
  • Formula evaluator (interprets each centre's fuzzy_matching_script)
  • Blocking index (groups candidates by name-initial to reduce comparisons)
  • run_fuzzy_match_for_center() — main entry point
  • run_fuzzy_match_all()        — nightly scheduler entry (Phase 2 wires this)
  • run_fuzzy_match_enqueued()   — Frappe enqueue target (Phase 2 triggers this)
  • test_*() helpers             — callable from bench console for verification
"""

import ast
import json
import re
from collections import defaultdict
from html import escape

import frappe

# ─────────────────────────────────────────────────────────────────────────────
# Low-level Match Functions
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_phone(val):
    """Normalise a phone string to +CC NNN format.
    Handles (+852) XXXX XXXX, (852) XXXX XXXX, 00852 XXXX XXXX, bare digits.
    Mirrors the normalization used in the client-side agent daemon.
    """
    s = str(val).strip() if val else ""
    if not s:
        return ""
    if re.match(r'^\+[0-9]', s):
        return s
    m = re.match(r'^\(\+([0-9]+)\)\s*(.*)', s)
    if m:
        return '+' + m.group(1) + ' ' + m.group(2).strip()
    m = re.match(r'^\(([0-9]+)\)\s*(.*)', s)
    if m:
        return '+' + m.group(1) + ' ' + m.group(2).strip()
    m = re.match(r'^00([0-9]{1,4})\s*(.*)', s)
    if m:
        return '+' + m.group(1) + ' ' + m.group(2).strip()
    return s


def chinese_match(str_a, str_b):
    """Compare two Chinese name strings.  Returns score 0.0–1.0.

    Strategy:
      1. Normalise Traditional → Simplified (hanziconv) so 陳 and 陈 are equal.
      2. Character-level rapidfuzz.ratio  (catches typos, extra chars).
      3. Pinyin phonetic rapidfuzz.token_sort_ratio (catches different input-
         method choices, e.g. 陳 vs 程 same sound, different chars).
      Result = max(char_sim, pinyin_sim).
    """
    from hanziconv import HanziConv
    from pypinyin import lazy_pinyin
    from rapidfuzz import fuzz as _fuzz

    if not str_a or not str_b:
        return 0.0
    str_a = str(str_a).strip()
    str_b = str(str_b).strip()
    if not str_a or not str_b:
        return 0.0

    # Normalise Traditional → Simplified
    str_a = HanziConv.toSimplified(str_a)
    str_b = HanziConv.toSimplified(str_b)

    if str_a == str_b:
        return 1.0

    char_sim = _fuzz.ratio(str_a, str_b) / 100.0

    try:
        pin_a = ' '.join(lazy_pinyin(str_a))
        pin_b = ' '.join(lazy_pinyin(str_b))
        pin_sim = _fuzz.token_sort_ratio(pin_a, pin_b) / 100.0
    except Exception:
        pin_sim = 0.0

    return max(char_sim, pin_sim)


def english_match(str_a, str_b):
    """Compare two English name strings.  Returns score 0.0–1.0.

    Uses token_set_ratio which handles word-order differences:
      "CHAN TAI MAN" vs "TAI MAN CHAN" → 1.0
    """
    from rapidfuzz import fuzz as _fuzz

    if not str_a or not str_b:
        return 0.0
    str_a = str(str_a).strip().lower()
    str_b = str(str_b).strip().lower()
    if not str_a or not str_b:
        return 0.0
    if str_a == str_b:
        return 1.0
    return _fuzz.token_set_ratio(str_a, str_b) / 100.0


def _chinese_match_normalized(simp_a, pinyin_a, simp_b, pinyin_b):
    """Fast Chinese comparison using already-normalized (Simplified + pinyin) strings.

    Called by the compiled formula's pair_fn for bulk matching.
    Avoids redundant HanziConv + lazy_pinyin calls inside the hot comparison loop.
    """
    from rapidfuzz import fuzz as _fuzz
    if not simp_a or not simp_b:
        return 0.0
    if simp_a == simp_b:
        return 1.0
    char_sim = _fuzz.ratio(simp_a, simp_b) / 100.0
    pin_sim = (_fuzz.token_sort_ratio(pinyin_a, pinyin_b) / 100.0
               if (pinyin_a and pinyin_b) else 0.0)
    return max(char_sim, pin_sim)


def phone_match(str_a, str_b):
    """Compare two phone number strings after normalisation.  Returns score 0.0–1.0.

    Exact match on normalised form → 1.0.
    Partial similarity otherwise (handles small typos / formatting differences).
    """
    from rapidfuzz import fuzz as _fuzz

    if not str_a or not str_b:
        return 0.0
    norm_a = _normalize_phone(str_a)
    norm_b = _normalize_phone(str_b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    return _fuzz.ratio(norm_a, norm_b) / 100.0


def id_match(str_a, str_b):
    """Compare two ID strings (HKID, staff number, etc.).  Returns 1.0 or 0.0.

    Normalises: uppercase, strip spaces and hyphens, then exact-match only.
    """
    if not str_a or not str_b:
        return 0.0
    norm_a = re.sub(r'[\s\-]', '', str(str_a).strip().upper())
    norm_b = re.sub(r'[\s\-]', '', str(str_b).strip().upper())
    if not norm_a or not norm_b:
        return 0.0
    return 1.0 if norm_a == norm_b else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Formula Evaluator
# ─────────────────────────────────────────────────────────────────────────────

_ALLOWED_ARITHMETIC_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.UAdd,
    ast.USub,
    ast.Name,
    ast.Load,
    ast.Constant,
)


def _compile_safe_arithmetic(expression, allowed_names=()):
    """Compile numeric formula arithmetic while rejecting calls and attributes."""
    tree = ast.parse(expression, mode='eval')
    allowed_names = set(allowed_names)
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_ARITHMETIC_NODES):
            raise ValueError(f'unsupported formula syntax: {type(node).__name__}')
        if isinstance(node, ast.Name) and node.id not in allowed_names:
            raise ValueError(f'unknown formula value: {node.id}')
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise ValueError('formula constants must be numeric')
    return compile(tree, '<fuzzy_formula>', 'eval')

def _eval_expr(expr, row):
    """Evaluate a macro argument expression using a record row as variable scope.

    Supported forms (expr is the raw text between the outer parentheses of a macro):
      f"{chin_name} {chin_last}"  →  row['chin_name'] + ' ' + row['chin_last']
      f"eng_name"                 →  row['eng_name']   (no {}, treated as field name)
      "eng_name"                  →  row['eng_name']   (plain quoted field name)
      eng_name                    →  row['eng_name']   (unquoted field name)
    """
    expr = expr.strip()

    # f-string: f"..." or f'...'
    fstr_m = re.match(r'^f["\'](.+)["\']$', expr, re.DOTALL)
    if fstr_m:
        template = fstr_m.group(1)
        if re.search(r'\{\w+\}', template):
            # Has {field_name} interpolations → substitute each
            def _replace(m):
                return str(row.get(m.group(1).strip(), '') or '')
            return re.sub(r'\{(\w+)\}', _replace, template)
        else:
            # No interpolation → treat the whole template as a field name
            return str(row.get(template, '') or '')
    else:
        # Plain string (possibly quoted) → treat as field name
        unquoted = expr.strip('"\'')
        return str(row.get(unquoted, '') or '')


def evaluate_fuzzy_formula(formula_text, source_row, candidate_row):
    """Evaluate a fuzzy_matching_script formula for one source/candidate pair.

    The formula is written by the centre admin and may look like:
        (@ChineseMatch(f"{chin_name} {chin_last}")*0.5
         + @EnglishMatch(f"eng_name") * 0.4) > 0.5

    Steps:
      1. Replace each @Macro(expr) with the computed float score for the pair.
      2. Detect optional trailing threshold (e.g. "> 0.5").
      3. Evaluate the resulting arithmetic expression.
      4. Return (raw_score: float, is_match: bool).

    raw_score is the numeric value of the weighted sum (before the threshold
    comparison) — this is what gets stored in match_score.

    Security note: eval() is called with __builtins__={} to block access to
    Python built-ins.  Formulas are stored in admin-controlled ERPNext fields.
    """
    if not formula_text:
        return (0.0, False)

    _MACRO_MAP = {
        'ChineseMatch': chinese_match,
        'EnglishMatch': english_match,
        'PhoneMatch':   phone_match,
        'IDMatch':      id_match,
    }

    processed = formula_text

    for macro_name, match_func in _MACRO_MAP.items():
        pattern = r'@' + macro_name + r'\((.+?)\)'

        def _substitutor(m, _fn=match_func):
            src_val  = _eval_expr(m.group(1), source_row)
            cand_val = _eval_expr(m.group(1), candidate_row)
            return str(round(_fn(src_val, cand_val), 6))

        processed = re.sub(pattern, _substitutor, processed)

    processed = processed.strip()

    # Detect trailing threshold, e.g. "> 0.5" or ">= 0.4" at end of formula
    thresh_m = re.search(
        r'\s*(>=|<=|!=|>|<|==)\s*(-?[0-9]+\.?[0-9]*)\s*$',
        processed
    )
    if thresh_m:
        score_expr = processed[:thresh_m.start()].strip()
        op_str     = thresh_m.group(1)
        threshold  = float(thresh_m.group(2))
    else:
        score_expr = processed
        op_str     = '>'
        threshold  = 0.5

    # Evaluate the arithmetic score expression
    try:
        code = _compile_safe_arithmetic(score_expr)
        raw_score = float(eval(code, {"__builtins__": {}}, {}))
    except Exception as e:
        frappe.log_error(
            f'evaluate_fuzzy_formula: eval failed for expr [{score_expr}]: {e}',
            'CCD Fuzzy Match'
        )
        return (0.0, False)

    # Apply threshold comparison
    _OPS = {
        '>=': raw_score >= threshold,
        '<=': raw_score <= threshold,
        '>':  raw_score >  threshold,
        '<':  raw_score <  threshold,
        '==': raw_score == threshold,
        '!=': raw_score != threshold,
    }
    is_match = _OPS.get(op_str, raw_score > threshold)

    return (round(raw_score, 4), is_match)


def compile_formula(formula_text):
    """Pre-compile a formula into a fast (precompute_fn, pair_fn) pair for bulk matching.

    Unlike evaluate_fuzzy_formula() which re-parses and re-evaluates for every
    pair, this function:
      1. Parses the formula and threshold ONCE.
      2. Compiles the arithmetic expression into a Python code object ONCE.
      3. Returns a precompute_fn that normalises a record (HanziConv + pypinyin)
         ONCE PER RECORD — so 80 K records each call pypinyin only once, not once
         per candidate they are compared against.
      4. Returns a pair_fn that uses cached normalized values to score any
         (source, candidate) pair very cheaply.

    Usage:
        precompute, pair_fn = compile_formula(formula_text)
        for row in all_records:
            row['_fc'] = precompute(row)       # once per record
        # inner matching loop
        score, is_match = pair_fn(src['_fc'], cand['_fc'])

    Returns:
        (precompute_fn, pair_fn)
        precompute_fn : callable(row: dict) -> cache_dict
        pair_fn       : callable(src_cache, cand_cache) -> (score: float, is_match: bool)
    """
    if not formula_text:
        return (lambda row: {}), (lambda s, c: (0.0, False, {})), []

    # ── Strip trailing threshold ──────────────────────────────────────────────
    thresh_m = re.search(
        r'\s*(>=|<=|!=|>|<|==)\s*(-?[0-9]+\.?[0-9]*)\s*$',
        formula_text
    )
    if thresh_m:
        score_template = formula_text[:thresh_m.start()].strip()
        op_str    = thresh_m.group(1)
        threshold = float(thresh_m.group(2))
    else:
        score_template = formula_text.strip()
        op_str    = '>'
        threshold = 0.5

    _CMP = {
        '>=': lambda s, t: s >= t,
        '<=': lambda s, t: s <= t,
        '>':  lambda s, t: s >  t,
        '<':  lambda s, t: s <  t,
        '==': lambda s, t: s == t,
        '!=': lambda s, t: s != t,
    }
    cmp_fn = _CMP.get(op_str, lambda s, t: s > t)

    # ── Extract all @Macro(arg) calls in formula order ────────────────────────
    slot_defs = []   # list of (slot_name, macro_type, arg_str)
    expr      = score_template

    for m in re.finditer(
        r'@(ChineseMatch|EnglishMatch|PhoneMatch|IDMatch)\((.+?)\)',
        score_template
    ):
        slot = f'_m{len(slot_defs)}_'
        slot_defs.append((slot, m.group(1), m.group(2)))

    # Replace macro calls with slot names using simple string replacement
    slot_details_definitions = []
    for slot, macro_type, arg_str in slot_defs:
        #print(f"DEBUG | Slot: {slot} | Macro: {macro_type} | Arg: {arg_str}")
        full_call = '@' + macro_type + '(' + arg_str + ')'
        expr = expr.replace(full_call, slot, 1)
        slot_details_definitions.append((slot, macro_type, arg_str))

    # ── Compile arithmetic expression once ───────────────────────────────────
    try:
        code = _compile_safe_arithmetic(expr, {slot for slot, _, _ in slot_defs})
    except (SyntaxError, ValueError) as e:
        raise ValueError(f'compile_formula: syntax error in [{expr}]: {e}')

    # ── Pre-computation closure (called once per record) ──────────────────────
    def precompute(row):
        """Normalise a record's match fields into a cache dict."""
        cache = {}
        for slot, macro_type, arg_str in slot_defs:
            val = _eval_expr(arg_str, row)
            if macro_type == 'ChineseMatch':
                try:
                    from hanziconv import HanziConv
                    from pypinyin import lazy_pinyin
                    simp   = HanziConv.toSimplified(str(val).strip()) if val else ''
                    pinyin = ' '.join(lazy_pinyin(simp)) if simp else ''
                except Exception:
                    simp   = str(val).strip()
                    pinyin = ''
                cache[slot] = (simp, pinyin)
            else:
                cache[slot] = str(val).strip() if val else ''
        return cache

    # ── Pair scoring closure (called once per source×candidate pair) ──────────
    def pair_fn(src_cache, cand_cache):
        """Score a pair using pre-computed caches."""
        ns = {}
        for slot, macro_type, _ in slot_defs:
            sv = src_cache.get(slot, '')
            cv = cand_cache.get(slot, '')
            if macro_type == 'ChineseMatch':
                simp_a, pin_a = sv if isinstance(sv, tuple) else ('', '')
                simp_b, pin_b = cv if isinstance(cv, tuple) else ('', '')
                ns[slot] = _chinese_match_normalized(simp_a, pin_a, simp_b, pin_b)
            elif macro_type == 'EnglishMatch':
                ns[slot] = english_match(sv, cv)
            elif macro_type == 'PhoneMatch':
                ns[slot] = phone_match(sv, cv)
            elif macro_type == 'IDMatch':
                ns[slot] = id_match(sv, cv)
            else:
                ns[slot] = 0.0
        try:
            raw = float(eval(code, {'__builtins__': {}}, ns))
        except Exception:
            return 0.0, False, ns
        return round(raw, 4), cmp_fn(raw, threshold), ns

    return precompute, pair_fn, slot_details_definitions


# ─────────────────────────────────────────────────────────────────────────────
# Blocking Index  (performance: avoids O(n×m) full cross-comparison)
# ─────────────────────────────────────────────────────────────────────────────

def _get_pinyin_initial(text):
    """Return the first pinyin syllable's initial letter (uppercase) of a string."""
    try:
        from pypinyin import lazy_pinyin
        syllables = lazy_pinyin(str(text).strip())
        if syllables:
            first = syllables[0].strip()
            if first:
                return first[0].upper()
    except Exception:
        pass
    return '#'


def _extract_formula_fields(formula_text):
    """Parse formula and extract field names used in @ChineseMatch / @EnglishMatch.

    Returns (chinese_fields: list[str], english_fields: list[str]).
    Field names come from:
      @ChineseMatch(f"{chin_name} {chin_last}")  → ['chin_name', 'chin_last']
      @EnglishMatch(f"eng_name")                 → ['eng_name']
      @EnglishMatch("eng_name")                  → ['eng_name']
    """
    def _fields_from_expr(expr):
        expr = expr.strip()
        fstr_m = re.match(r'^f["\'](.+)["\']$', expr, re.DOTALL)
        if fstr_m:
            template = fstr_m.group(1)
            found = re.findall(r'\{(\w+)\}', template)
            return found if found else [template]  # no {} → whole template is field name
        return [expr.strip('"\'')]

    chinese_fields, english_fields = [], []

    for m in re.finditer(r'@ChineseMatch\((.+?)\)', formula_text):
        chinese_fields.extend(_fields_from_expr(m.group(1)))
    for m in re.finditer(r'@EnglishMatch\((.+?)\)', formula_text):
        english_fields.extend(_fields_from_expr(m.group(1)))

    return chinese_fields, english_fields


def compute_block_keys(row, chinese_fields, english_fields):
    """Compute a set of blocking keys for a record.

    Two records need to share at least one block key to be compared.
    Keys are cheap to compute and designed to keep recall high (few false negatives).

    Chinese keys: one key per Chinese field using first-pinyin-initial.
      e.g.  chin_last='陳' → 'cn_chin_last_C'
    English keys: first 3 lowercase chars of the first non-empty English field value.
      e.g.  eng_name='Chan Tai Man' → 'en_cha'
    Fallback: '__all__' when no fields are available (safe but slow — full O(n×m)).
    """
    keys = set()

    for field in chinese_fields:
        val = str(row.get(field, '') or '').strip()
        if val:
            initial = _get_pinyin_initial(val)
            keys.add(f'cn_{field}_{initial}')

    for field in english_fields:
        val = str(row.get(field, '') or '').strip().lower()
        if len(val) >= 2:
            keys.add('en_' + val[:3])
            break  # one English block key per record is enough

    if not keys:
        keys.add('__all__')

    return keys


# ─────────────────────────────────────────────────────────────────────────────
# Data Fetch Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_formula_and_fields(hostname):
    """Fetch fuzzy_matching_script from CCD Registration for hostname.

    Returns (formula_text: str, chinese_fields: list, english_fields: list).
    Returns empty strings/lists when no formula is configured.
    """
    try:
        doc = frappe.get_doc('CCD Registration', hostname)
        formula = (doc.get('fuzzymachingscript') or '').strip()
    except Exception as e:
        frappe.log_error(
            f'_get_formula_and_fields: cannot load CCD Registration [{hostname}]: {e}',
            'CCD Fuzzy Match'
        )
        formula = ''

    chinese_fields, english_fields = _extract_formula_fields(formula)
    return formula, chinese_fields, english_fields


# ─────────────────────────────────────────────────────────────────────────────
# Match Table Write Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clear_match_table(source_doc_name):
    """Delete ALL match_table rows for a given CCD Master document."""
    frappe.db.sql(
        "DELETE FROM `tabCCD Master matching` WHERE parent = %s",
        (source_doc_name,)
    )


#def _insert_match_rows(rows):
#    """Insert match_table rows via direct SQL for performance.

#    rows: list of tuples:
#      (source_doc_name, mas_client, client, client_id, score, ns)

#    idx (row number) is assigned per parent in sequence starting from 1.
#    """
#    if not rows:
#        return

#    idx_counter = defaultdict(int)
#    for (source_doc_name, mas_client, client, client_id, score, ns) in rows:
#        idx_counter[source_doc_name] += 1
#        frappe.db.sql(
#            """INSERT INTO `tabCCD Master matching`
#               (name, parent, parenttype, parentfield, idx,
#                mas_client, client, client_id, score,match_equations)
#               VALUES (%s, %s, 'CCD Master', 'match_table', %s,
#                       %s, %s, %s, %s, %s)""",
#            (
#                frappe.generate_hash(length=12),
#                source_doc_name,
#                idx_counter[source_doc_name],
#                mas_client,
#                client,
#                client_id,
#                float(score),
#                ns,
#            )
#        )

def _insert_match_rows(rows):
#    """Insert match_table rows via direct SQL for performance.

#    rows: list of tuples:
#      (source_doc_name, mas_client, client, client_id, score, ns)

#    idx (row number) is assigned per parent in sequence starting from 1.
#    """

    """Insert match_table rows via direct SQL for performance."""
    if not rows:
        return


    idx_counter = defaultdict(int)

    for (source_doc_name, mas_client, client, client_id, score, _ns, html_table) in rows:
        #print(f"DEBUG | Processing row for source_doc_name: {source_doc_name}")
        idx_counter[source_doc_name] += 1

        # Convert the python dictionary to a valid JSON string
        #ns_json_string = json.dumps(html_table) if html_table else "{}"

        frappe.db.sql(
            """INSERT INTO `tabCCD Master matching`
               (name, parent, parenttype, parentfield, idx,
                mas_client, client, client_id, score, match_equation)
               VALUES (%s, %s, 'CCD Master', 'match_table', %s,
                       %s, %s, %s, %s, %s)""",
            (
                frappe.generate_hash(length=12),
                source_doc_name,
                idx_counter[source_doc_name],
                mas_client,
                client,
                client_id,
                float(score),
                html_table,  # <--- Use the JSON string here instead of raw 'ns'
            )
        )

# HTML audit table
def building_html_audit_table(slot_details_definitions, ns, src_cache, cand_cache, formula):
    # Start building the visual HTML audit breakdown table
    if not formula:
        return ""

    processed_expression = formula

    html_table = """
    <div style="overflow-x: auto; margin-top: 5px;">
        <table class="table table-bordered table-condensed" style="font-size: 12px; margin-bottom: 5px; background-color: #fafbfc;">
            <thead>
                <tr style="background-color: #f1f3f5; font-weight: bold;">
                    <th style="padding: 4px 8px;">Matching Rule / Function</th>
                    <th style="padding: 4px 8px;">Target Field</th>
                    <th style="padding: 4px 8px;">Current Record (Doc)</th>
                    <th style="padding: 4px 8px;">Target Record (Master)</th>
                    <th style="padding: 4px 8px; text-align: center;">Function Score</th>
                </tr>
            </thead>
            <tbody>
    """

    for slot, function_name, field in slot_details_definitions:
        val1 = src_cache.get(slot) or ""

        val2 = cand_cache.get(slot) or ""

        # Calculate raw similarity (0-100)
        if function_name == 'ChineseMatch':

            field_score = ns.get(slot, 0.0)
            #print(f"DEBUG | ChineseMatch: {val1} vs {val2} → Score: {field_score}")

        elif function_name == 'EnglishMatch':
            #field_score = english_match(val1, val2) * 100
            field_score = ns.get(slot, 0.0)
            #print(f"DEBUG | EnglishMatch: {val1} vs {val2} → Score: {field_score}")

        elif function_name == 'PhoneMatch':
            #field_score = phone_match(val1, val2) * 100
            field_score = ns.get(slot, 0.0)
            #print(f"DEBUG | PhoneMatch: {val1} vs {val2} → Score: {field_score}")

        elif function_name == 'IDMatch':
            #field_score = id_match(val1, val2) * 100
            field_score = ns.get(slot, 0.0)
            #print(f"DEBUG | IDMatch: {val1} vs {val2} → Score: {field_score}")
        else:
            #print(f"DEBUG | Unknown function: {function_name} for field: {field}")
            field_score = 0.0


        score_full = field_score * 100
        #print(f"DEBUG | Score full for {function_name}({field}): {score_full:.4f}")

        # Generate clean presentation variables for our table body rows
        clean_field_label = field.replace('_', ' ').title()
        # Replace the exact macro call captured by compile_formula. This also
        # supports f-string/concatenated field expressions used by the engine.
        processed_expression = processed_expression.replace(
            f'@{function_name}({field})', str(field_score), 1
        )
        html_table += f"""
                <tr>
                    <td style="padding: 4px 8px; font-family: monospace; color: #b91c1c; font-weight: bold;">@{escape(str(function_name))}</td>
                    <td style="padding: 4px 8px; font-weight: 500; color: #1f2937;">{escape(clean_field_label.replace('"', '').replace("'", ""))}</td>
                    <td style="padding: 4px 8px; color: #4b5563;">{escape(str(val1))}</td>
                    <td style="padding: 4px 8px; color: #4b5563;">{escape(str(val2))}</td>
                    <td style="padding: 4px 8px; text-align: center;">
                        <span class="indicator { 'green' if score_full >= 70 else 'orange' if score_full >= 40 else 'red' }">
                            {score_full:.2f}% ({field_score:.2f})
                        </span>
                    </td>
                </tr>
        """

    threshold = re.search(
        r'\s*(>=|<=|!=|>|<|==)\s*(-?[0-9]+\.?[0-9]*)\s*$',
        processed_expression,
    )
    equation_part = (
        processed_expression[:threshold.start()].strip()
        if threshold
        else processed_expression.strip()
    )
    try:
        code = _compile_safe_arithmetic(equation_part)
        score = float(eval(code, {'__builtins__': {}}, {}))
    except Exception:
        score = 0
    html_table += f"""
                </tbody>
            </table>
            <div style="font-size: 11px; color: #6b7280; padding: 4px 8px; background: #f8fafc; border: 1px solid #e2e8f0; border-top: 0; border-radius: 0 0 4px 4px;">
                <strong>Formula Evaluation Trail:</strong> <code style="color: #db2777; font-size: 11px;">{escape(equation_part)}</code> &rarr; <strong>Total Combined Score: <span style="color: #059669; font-size: 12px;">{round(score, 3)}</span></strong>
            </div>
        </div>
        """
    return html_table
# ─────────────────────────────────────────────────────────────────────────────
# Core Matching Runner
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def run_fuzzy_match_for_center(hostname, changed_keys=None, is_new_center=False, limit=None,specific_records=None):
    """Compute fuzzy matches for CCD Master records from `hostname` against all
    other centres and update the match_table child table.

    Args:
        hostname (str):
            The CCD Registration hostname to process.

        changed_keys (set | None):
            If given, only re-process source records whose ccd_source_key is in
            this set (incremental mode).  None → process ALL records.

        is_new_center (bool):
            Reserved for Phase 3 cross-trigger logic.  Not used in Phase 1.

        specific_records (list[str] | None):
            If given, only process the CCD Master records whose names are in this list.

    Returns:
        dict: {'processed': N, 'matches_found': M, 'errors': E}

    Blocking strategy:
        Records are grouped by (first-pinyin-initial of each Chinese field) and
        (first-3-chars of English field).  A source record is only compared against
        candidates that share at least one block key.  This reduces comparisons
        from O(n×m) to roughly O(n × m/26) while keeping recall high.

    Clearing strategy:
        Before inserting new matches for a source record, ALL its existing
        match_table rows are deleted and replaced with the freshly computed set.
        This guarantees no stale rows remain.
    """
    formula, chinese_fields, english_fields = _get_formula_and_fields(hostname)
    if not formula:
        msg = f'run_fuzzy_match_for_center: no fuzzy_matching_script configured for [{hostname}]'
        frappe.log_error(msg, 'CCD Fuzzy Match')
        return {'processed': 0, 'matches_found': 0, 'errors': 1}

    # ── Pre-compile formula ONCE (avoids per-pair regex + eval overhead) ──────
    precompute, pair_fn, slot_details_definitions = compile_formula(formula)

    # ── Fetch source records via direct SQL (faster than get_all for bulk) ────
    if not specific_records:
        source_records = frappe.db.sql(
            'SELECT * FROM `tabCCD Master` WHERE ccd_reg_source = %s',
            (hostname,), as_dict=True
        )
    else:
        source_records = frappe.db.sql(
            'SELECT * FROM `tabCCD Master` WHERE ccd_reg_source = %s AND name IN ({})'.format(
                ', '.join(['%s'] * len(specific_records))
            ),
            (hostname, *specific_records), as_dict=True
        )
    if not source_records:
        return {'processed': 0, 'matches_found': 0, 'errors': 0}

    # Optional limit for sampling / testing
    if limit:
        source_records = source_records[:limit]

    # Build source map: ccd_source_key → row_dict
    source_map = {r.get('ccd_source_key', ''): r for r in source_records}

    # Filter to changed_keys for incremental mode
    if changed_keys is not None:
        sources_to_process = {k: v for k, v in source_map.items() if k in changed_keys}
    else:
        sources_to_process = source_map

    if not sources_to_process:
        return {'processed': 0, 'matches_found': 0, 'errors': 0}

    # ── Fetch ALL candidate records from other centres ────────────────────────
    all_other_records = frappe.db.sql(
        'SELECT * FROM `tabCCD Master` WHERE ccd_reg_source != %s',
        (hostname,), as_dict=True
    )
    if not all_other_records:
        return {'processed': len(sources_to_process), 'matches_found': 0, 'errors': 0}

    # ── Pre-normalise ALL records ONCE (HanziConv + pypinyin per record, not per pair) ──
    for row in all_other_records:
        row['_fc'] = precompute(row)
    for row in sources_to_process.values():
        row['_fc'] = precompute(row)

    # Build blocking index for candidates: block_key → [candidate_row, ...]
    block_index = defaultdict(list)
    for cand in all_other_records:
        for bk in compute_block_keys(cand, chinese_fields, english_fields):
            block_index[bk].append(cand)

    # ── Batch-delete stale match rows for the entire source set at once ───────
    # (For a full run this is one DELETE; for incremental only the changed docs)
    src_doc_names = [r.get('name', '') for r in sources_to_process.values() if r.get('name')]
    if src_doc_names:
        placeholders = ', '.join(['%s'] * len(src_doc_names))
        frappe.db.sql(
            f'DELETE FROM `tabCCD Master matching` WHERE parent IN ({placeholders})',
            tuple(src_doc_names)
        )

    # ── Main matching loop ────────────────────────────────────────────────────
    processed     = 0
    matches_found = 0
    errors        = 0
    new_rows      = []  # (source_doc_name, master_client, client_apps, cand_key, score)

    for src_key, src_row in sources_to_process.items():
        try:
            src_doc_name = src_row.get('name', '')
            src_center   = src_row.get('ccd_reg_source', hostname)
            src_cache    = src_row.get('_fc', {})

            # Deduplicate candidates via blocking
            src_block_keys  = compute_block_keys(src_row, chinese_fields, english_fields)
            candidates_seen = set()
            candidates      = []
            for bk in src_block_keys:
                for cand in block_index.get(bk, []):
                    cand_name = cand.get('name', '')
                    if cand_name not in candidates_seen:
                        candidates_seen.add(cand_name)
                        candidates.append(cand)

            # Score each candidate using pre-compiled, pre-normalised pair_fn
            record_matches = []
            for cand in candidates:
                cand_center = cand.get('ccd_reg_source', '')
                if not cand_center or cand_center == hostname:
                    continue

                score, is_match, ns = pair_fn(src_cache, cand.get('_fc', {}))
                #cand_name = cand.get('name', 'Unknown')
                #print(f"DEBUG | Cand: {cand_name} | Total: {score} | Match: {is_match} | Marks: {ns}")
                if is_match:
                    #print("src_cache:", src_cache)
                    #print("cand_cache:", cand.get('_fc', {}))
                    cand_key = cand.get('ccd_source_key', '')
                    #print(f"DEBUG | Match found: {src_key} ↔ {cand_key} | Score: {score} | Marks: {ns} | Formula: {formula}")
                    html_table = building_html_audit_table(slot_details_definitions, ns, src_cache, cand.get('_fc', {}), formula)

                    record_matches.append((cand_center, cand_key, score,ns,html_table))

            # Queue new rows
            for cand_center, cand_key, score, ns, html_table in record_matches:
                #print(f"New row: {(ns, html_table)}")

                new_rows.append((src_doc_name, src_center, cand_center, cand_key, score, ns, html_table))
                matches_found += 1

            processed += 1

        except Exception as e:
            errors += 1
            frappe.log_error(
                f'run_fuzzy_match_for_center: error on src_key [{src_key}]: {e}',
                'CCD Fuzzy Match'
            )

    # ── Write all new match rows ───────────────────────────────────────────────
    try:
        _insert_match_rows(new_rows)
    except Exception as e:
        errors += 1
        frappe.log_error(
            f'run_fuzzy_match_for_center: _insert_match_rows failed: {e}',
            'CCD Fuzzy Match'
        )

    frappe.db.commit()

    summary = {'processed': processed, 'matches_found': matches_found, 'errors': errors}
    frappe.log_error(
        f'run_fuzzy_match_for_center [{hostname}]: {summary}',
        'CCD Fuzzy Match INFO'
    )
    return summary


@frappe.whitelist()
def run_fuzzy_match_all():
    """Run fuzzy matching for ALL CCD Registration centres.

    Intended to be called by the Frappe nightly scheduler (wired in Phase 2).
    Also callable directly from bench console.
    """
    centers = frappe.get_all('CCD Registration', fields=['name'])
    results = {}
    for center in centers:
        hostname = center['name']
        try:
            results[hostname] = run_fuzzy_match_for_center(hostname)
        except Exception as e:
            results[hostname] = {'error': str(e)}
            frappe.log_error(
                f'run_fuzzy_match_all: {hostname}: {e}',
                'CCD Fuzzy Match'
            )
    return results


def run_fuzzy_match_enqueued(hostname, action='full', changed_keys_str=''):
    """Frappe enqueue() target.  Called by the trigger_fuzzy_match Server Script (Phase 2).

    Args:
        hostname (str):         Centre hostname (CCD Registration document name).
        action (str):           'full' | 'incremental' | 'new_center'
        changed_keys_str (str): JSON array string of changed ccd_source_key values
                                (only used when action='incremental').
    """
    changed_keys  = None
    is_new_center = (action == 'new_center')

    if action == 'incremental' and changed_keys_str:
        try:
            changed_keys = set(json.loads(changed_keys_str))
        except Exception:
            changed_keys = None  # fall back to full re-match

    return run_fuzzy_match_for_center(
        hostname,
        changed_keys=changed_keys,
        is_new_center=is_new_center,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test / Verification Helpers
# ─────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def test_match_functions():
    """Smoke-test all four match functions with known pairs.

    Call from bench console:
        bench --site <site> execute db_connector.api_ccd_fuzzy.test_match_functions

    Or from within the Frappe Python console (bench console):
        from db_connector.api_ccd_fuzzy import test_match_functions
        import json; print(json.dumps(test_match_functions(), indent=2, ensure_ascii=False))
    """
    return {
        # ── Chinese ────────────────────────────────────────────────────
        'cn_traditional_vs_simplified': chinese_match('陳大文', '陈大文'),   # expect ~1.0
        'cn_same_simplified':           chinese_match('陈大文', '陈大文'),   # expect  1.0
        'cn_similar_given_name':        chinese_match('陳大文', '陳大明'),   # expect  0.6–0.85
        'cn_different_surname':         chinese_match('陳大文', '黃大文'),   # expect  0.4–0.7
        'cn_pinyin_same_different_char':chinese_match('陳', '程'),           # expect  0.0–0.3
        'cn_empty_a':                   chinese_match('', '陳大文'),          # expect  0.0
        'cn_empty_both':                chinese_match('', ''),                # expect  0.0
        # ── English ───────────────────────────────────────────────────
        'en_exact':                     english_match('CHAN TAI MAN', 'CHAN TAI MAN'),   # 1.0
        'en_reordered_words':           english_match('CHAN TAI MAN', 'TAI MAN CHAN'),   # ~1.0
        'en_similar':                   english_match('CHAN TAI MAN', 'CHAN TAI MING'),  # 0.8+
        'en_different':                 english_match('CHAN TAI MAN', 'WONG SIU LAM'),   # low
        'en_empty':                     english_match('', 'CHAN TAI MAN'),               # 0.0
        # ── Phone ─────────────────────────────────────────────────────
        'ph_exact_normalised':          phone_match('+852 9123 4567', '+852 9123 4567'), # 1.0
        'ph_format_parens':             phone_match('(+852) 9123 4567', '+852 9123 4567'), # 1.0
        'ph_format_00prefix':           phone_match('00852 91234567', '+852 91234567'),  # 1.0
        'ph_different':                 phone_match('+852 9123 4567', '+852 9999 8888'), # low
        # ── ID ────────────────────────────────────────────────────────
        'id_exact':                     id_match('A123456(7)', 'A123456(7)'),   # 1.0
        'id_case_normalised':           id_match('A123456', 'a123456'),          # 1.0
        'id_hyphen_stripped':           id_match('A-123456', 'A123456'),         # 1.0
        'id_different':                 id_match('A123456', 'B654321'),          # 0.0
    }


@frappe.whitelist()
def test_formula_eval(formula=None):
    """Test formula evaluation with synthetic rows.

    Optional: pass a custom formula string.
    Default formula: (@ChineseMatch(f"{chin_name} {chin_last}")*0.5
                      + @EnglishMatch(f"eng_name") * 0.4) > 0.5

    Call from bench console:
        from db_connector.api_ccd_fuzzy import test_formula_eval
        import json; print(json.dumps(test_formula_eval(), indent=2, ensure_ascii=False))
    """
    if not formula:
        formula = (
            '(@ChineseMatch(f"{chin_name} {chin_last}")*0.5'
            ' + @EnglishMatch(f"eng_name") * 0.4)>0.5'
        )

    source = {
        'chin_name': '大文', 'chin_last': '陳',
        'eng_name': 'Chan Tai Man',
        'ccd_source_key': 'TEST-SRC-001', 'ccd_reg_source': 'CENTER-A',
    }
    similar = {
        'chin_name': '大文', 'chin_last': '陳',    # same Chinese name
        'eng_name': 'Chan Tai Man',                 # same English name
        'ccd_source_key': 'TEST-SIM-001', 'ccd_reg_source': 'CENTER-B',
    }
    trad_variant = {
        'chin_name': '大文', 'chin_last': '陈',    # Simplified variant of 陳
        'eng_name': 'CHAN TAI MAN',                 # uppercase variant
        'ccd_source_key': 'TEST-SIM-002', 'ccd_reg_source': 'CENTER-B',
    }
    reordered = {
        'chin_name': '大文', 'chin_last': '陳',
        'eng_name': 'Tai Man Chan',                 # word-order reordered
        'ccd_source_key': 'TEST-SIM-003', 'ccd_reg_source': 'CENTER-B',
    }
    different = {
        'chin_name': '志明', 'chin_last': '黃',
        'eng_name': 'Wong Chi Ming',
        'ccd_source_key': 'TEST-DIF-001', 'ccd_reg_source': 'CENTER-B',
    }

    def _pair(label, cand):
        score, is_match = evaluate_fuzzy_formula(formula, source, cand)
        return {'score': score, 'is_match': is_match}

    return {
        'formula': formula,
        'source_record': source,
        'pairs': {
            'similar_exact':        _pair('similar_exact',   similar),
            'trad_simplified_diff': _pair('trad_variant',    trad_variant),
            'english_reordered':    _pair('reordered',       reordered),
            'completely_different': _pair('different',        different),
        },
    }


@frappe.whitelist()
def test_run_for_center(hostname):
    """Run fuzzy matching for a specific centre using real CCD Master data.

    Usage (bench console):
        cd /root/erpnext_docker_volume/backend
        source ../venv/bin/activate
        bench --site <your-site-name> execute db_connector.api_ccd_fuzzy.test_run_for_center \\
              --args '{"hostname": "YOUR-HOSTNAME-HERE"}'

    Or from within bench console (interactive):
        from db_connector.api_ccd_fuzzy import run_fuzzy_match_for_center
        result = run_fuzzy_match_for_center('YOUR-HOSTNAME-HERE')
        print(result)

    Returns a summary dict: {'processed': N, 'matches_found': M, 'errors': E}
    The match_table of each processed CCD Master record will be updated in-place.
    """
    if not hostname:
        return {'error': 'hostname is required'}
    return run_fuzzy_match_for_center(hostname)

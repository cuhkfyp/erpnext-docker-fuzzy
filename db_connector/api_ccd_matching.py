import re
import frappe
import pandas as pd
from rapidfuzz import fuzz
import pypinyin
import pycantonese
from frappe import _
from difflib import SequenceMatcher
from opencc import OpenCC

def calculate_similarity(str1, str2):
    """Calculates similarity between two strings (0 ~ 100)"""
    if not str1 or not str2:
        return 0
    return int(SequenceMatcher(None, str(str1).strip().lower(), str(str2).strip().lower()).ratio() * 100)

def _normalize(phone):
    if not phone:
        return ""
    return re.sub(r"\D","", phone)

def evaluate_fuzzy_phone(str1, str2):
    """Return [0] value: 0 ==> no comparsion, 1 comparsion with value
              [1] sorce
    """
    if not str1 or not str2:
        return 0, 0
    return 1, calculate_similarity(_normalize(str1), _normalize(str2))

def evaluate_fuzzy_hkid(str1, str2):
    if not str1 or not str2:
        return 0, 0
    return 1, calculate_similarity(_normalize(str1), _normalize(str2));

def evaluate_fuzzy_chinesename(str1, str2):
    def match_chinese_TS(str1, str2):
        """Compare Str1 and Str2 in Traditional and Simple Chinese"""
        converter = OpenCC('t2s')
        str1 = converter.convert(str1)
        str2 = converter.convert(str2)
        return calculate_similarity(str1, str2)

    def match_chinese_py(str1, str2):
        """Compare a Chinese String to lowercase , tone-free PinYin"""
        str1_list = pypinyin.lazy_pinyin(str1)
        str2_list = pypinyin.lazy_pinyin(str2)
        return calculate_similarity(" ".join(str1_list).strip().lower(), " ".join(str2_list).strip().lower())

    def match_chinese_ct(str1, str2):
        """Compare a Chinese string by Cantonese"""
        str1_list = pycantonese.characters_to_jyutping(str1)
        str1_token = [item[1] for item in str1_list if item[1]]
        str2_list = pycantonese.characters_to_jyutping(str2)
        str2_token = [item[1] for item in str2_list if item[1]]
        return calculate_similarity(" ".join(str1_token), " ".join(str2_token))

    if not(str1) or not(str2):
        return 0,0
    score1 = match_chinese_TS(str1, str2)
    score2 = match_chinese_py(str1, str2)
    score3 = match_chinese_ct(str1, str2)
    return 1, max(score1, score2, score3)

def evaluate_fuzzy_englishname(str1, str2):
    if not(str1) or not(str2):
        return 0,0

    return 0, 0

def fuzzy_calculation(logic_name, field_name, master_doc, doc):
    """
    Executes specific text comparison rules based on the matching function type.
    Returns a score between 0 and 100.
    """
    # Standardize string values safely, defaulting to empty strings if None
    val1 = str(doc.get(field_name) or "").strip()
    val2 = str(master_doc.get(field_name) or "").strip()

    # If either value is empty, there is nothing to match
    if not val1 or not val2:
        return 0

    # 1. Chinese Character Matching
    if logic_name == "@ChineseMatch":
        # Direct comparison works best for Chinese text since capitalization doesn't apply
        return calculate_similarity(val1, val2)

    # 2. English Text Matching
    elif logic_name == "@EnglishMatch":
        # Standardize case and replace common punctuation quirks
        v1_clean = val1.lower().replace(".", "").replace(",", "")
        v2_clean = val2.lower().replace(".", "").replace(",", "")
        return calculate_similarity(v1_clean, v2_clean)

    # 3. Telephone / Mobile Number Matching
    elif logic_name == "@PhoneMatch":
        # Keep only digits to clear out formatting dashes, spaces, or brackets
        # e.g., "+852 9123-4567" -> "85291234567"
        v1_digits = "".join(filter(str.isdigit, val1))
        v2_digits = "".join(filter(str.isdigit, val2))

        # Strip common international prefixes if lengths mismatch (e.g., removing leading '852' or '86')
        if len(v1_digits) != len(v2_digits):
            if v1_digits.startswith("852") and len(v1_digits) > 8: v1_digits = v1_digits[3:]
            if v2_digits.startswith("852") and len(v2_digits) > 8: v2_digits = v2_digits[3:]
            if v1_digits.startswith("86") and len(v1_digits) > 11: v1_digits = v1_digits[2:]
            if v2_digits.startswith("86") and len(v2_digits) > 11: v2_digits = v2_digits[2:]

        return calculate_similarity(v1_digits, v2_digits)

    # 4. Pinyin Romanization Matching
    elif logic_name == "@PinyinMatch":
        # Lowercase everything and strip spaces to catch variations like "TaiMan" vs "Tai Man"
        v1_pinyin = val1.lower().replace(" ", "")
        v2_pinyin = val2.lower().replace(" ", "")
        return calculate_similarity(v1_pinyin, v2_pinyin)
    elif logic_name == "@IDMatch":
        # HKID checking
        return calculate_similarity(val1, val2)

    # Fallback default condition
    else:
        return calculate_similarity(val1, val2)

def parse_script_fields(script_text):
    """Extracts field names wrapped in double quotes from the script string"""
    if not script_text:
        return []
    return re.findall(r'"([^"]+)"', script_text)

def evaluate_fuzzy_script(doc, master, script_text):
    """
    Parses full `@Function("field")` patterns from the matching script,
    calculates their individual similarity scores, evaluates the total math result,
    and returns an HTML audit table displaying function names and specific scores.
    """
    if not script_text:
        return 0, ""

    processed_expression = script_text

    # Find all function blocks matching something like: @ChineseMatch("chi_surname")
    # Group 1 captures the Macro Name (e.g., @ChineseMatch)
    # Group 2 captures the target database field name (e.g., chi_surname)
    macro_pattern = r'(@[A-Za-z]+Match)\("([^"]+)"\)'
    matches = re.findall(macro_pattern, script_text)

    # Start building the visual HTML audit breakdown table
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

    for function_name, field in matches:
        val1 = doc.get(field) or ""
        val2 = master.get(field) or ""

        # Calculate raw similarity (0-100)
        #field_score = calculate_similarity(val1, val2)
        field_score = fuzzy_calculation(function_name, field, master, doc)

        # Convert score to a decimal fraction to scale cleanly with your math multipliers (e.g., * 0.42)
        score_fraction = field_score / 100.0

        # Safely rebuild the exact literal function string representation to replace inside our execution script
        literal_macro_string = f'{function_name}("{field}")'
        processed_expression = processed_expression.replace(literal_macro_string, str(score_fraction))

        # Generate clean presentation variables for our table body rows
        clean_field_label = field.replace('_', ' ').title()
        html_table += f"""
                <tr>
                    <td style="padding: 4px 8px; font-family: monospace; color: #b91c1c; font-weight: bold;">{function_name}</td>
                    <td style="padding: 4px 8px; font-weight: 500; color: #1f2937;">{clean_field_label}</td>
                    <td style="padding: 4px 8px; color: #4b5563;">{val1}</td>
                    <td style="padding: 4px 8px; color: #4b5563;">{val2}</td>
                    <td style="padding: 4px 8px; text-align: center;">
                        <span class="indicator { 'green' if field_score >= 70 else 'orange' if field_score >= 40 else 'red' }">
                            {field_score}% ({score_fraction})
                        </span>
                    </td>
                </tr>
        """

    # Isolate the executable math calculation string by splitting out conditional operators (removes the '> 0.65' suffix)
    equation_part = processed_expression.split('>')[0].strip()

    try:
        # Evaluates the literal math result pattern string safely via the native Frappe environment framework
        score = frappe.safe_eval(equation_part)
    except Exception:
        score = 0

    # Append calculation breakdown details to the HTML structural card footer wrapper template block
    html_table += f"""
            </tbody>
        </table>
        <div style="font-size: 11px; color: #6b7280; padding: 4px 8px; background: #f8fafc; border: 1px solid #e2e8f0; border-top: 0; border-radius: 0 0 4px 4px;">
            <strong>Formula Evaluation Trail:</strong> <code style="color: #db2777; font-size: 11px;">{equation_part}</code> &rarr; <strong>Total Combined Score: <span style="color: #059669; font-size: 12px;">{round(score, 3)}</span></strong>
        </div>
    </div>
    """

    return score, html_table

def get_matching_fields(matching_script):
    """ Getting fields from matching script """
    if not matching_script:
       return ["name"]
    fuzzy_matching = parse_script_fields(matching_script)
    match = re.search(r'>\s*([\d.]+)', matching_script)

    if match:
        # Extract the string representation ("0.65")
        threshold_string = match.group(1)

        # Convert it to a float
        threshold_float = float(threshold_string)
    else:
        threshold_float = 0.65      ## Default sorce rate1
    # Fix: Used append() instead of JavaScript's push()

    if "name" not in fuzzy_matching:
        fuzzy_matching.append("name")
    return fuzzy_matching, threshold_float

@frappe.whitelist()
def ccd_matching(parent_doc):
    doc = frappe.get_doc("CCD Master", parent_doc)
    # Get Registration Record from CCD Master Reg_Source
    from db_connector.api_fuzzy_evaluation import _latest_registration_for_source

    registration_doc = _latest_registration_for_source(str(doc.ccd_reg_source or ""))
    if not registration_doc:
        frappe.throw(
            f"No submitted CCD Registration revision owns source {doc.ccd_reg_source}"
        )

    # Fix: Pass the script text directly from the current doc instead of treating a list as an object
    script_text = registration_doc.fuzzymachingscript

    # Fix: Keep child table name consistent (using 'matching_item')
    doc.set('match_table', [])

    # Extract list of fields required by your formula script
    match_fields, match_score = get_matching_fields(script_text)
    print(f"Match fields are {match_fields}")

    # Ensure critical fields for your loop logic are also fetched
    for mandatory_field in ["ccd_reg_source", "ccd_source_key"]:
        if mandatory_field not in match_fields:
            match_fields.append(mandatory_field)
    print(f"Match fields includes mandatory:{mandatory_field}")

    # Fetch unmatched Master records based on filters
    unmatched_masters = frappe.get_all(
        "CCD Master",
        filters={
            "is_matched": 0,
            "name": ["!=", parent_doc],
            "ccd_reg_source": ["!=", doc.ccd_reg_source]
        },
        fields=match_fields
    )
    total_records = len(unmatched_masters)
    matched_count = 0

    for idx, master in enumerate(unmatched_masters, start=1):
        # Evaluate script using the current master record values
        score, match_equation = evaluate_fuzzy_script(doc, master, script_text)
        # Send live numebrs's to the frontend every 5 records to protect server performance
        if idx % 5 == 0 or idx == total_records:
            frappe.publish_realtime(
                event="ccd_matching_progress",
                message={
                    "current": idx,
                    "total": total_records,
                    "message": f"Matching master record {idx} of {total_records}..."
                },
                user=frappe.session.user
            )

        # If score exceeds threshold (your formula outputs decimals like 0.65, so we compare against decimal)
        # If your formula returns full ints (e.g. out of 100), adjust this value to 65 or 80 accordingly.
        if (score > match_score):
            print(f"{master.ccd_source_key} is {score}, Match equation is {match_equation}")
            doc.append("match_table", {
                "mas_client": doc.name,
                "client": doc.ccd_reg_source,
                "client_id": master.ccd_source_key,
                "score": score,
                "match_equation": match_equation,
                "is_matched": False,
            })
            matched_count += 1

    if matched_count > 0:
        doc.match_ct = matched_count
        doc.save()
        frappe.db.commit()

    return { "status": "success", "messages": f"Matched count is {matched_count}" }

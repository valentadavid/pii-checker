# column_filter.py
# Determines whether a dataset column is a candidate for PII checking.
#
# Logic:
#   - Missing columns are skipped
#   - Columns whose name/label matches a force-include pattern (age, birth, ethnicity,
#     religion, disability, etc. — see _FORCE_INCLUDE_PATTERNS) or looks like a platform
#     participant ID (Prolific/MTurk/workerid — see is_platform_id_candidate) are always
#     checked, regardless of dtype or value range
#   - Boolean columns are otherwise skipped (never PII)
#   - Datetime columns are always checked (could be birthdays etc.)
#   - Numeric columns are checked if:
#       - possible GPS coordinates (abs value <= 180, >= 3 decimal places) — but excluded if
#         >10% of values fall in -6.5 to 4.7 (no major landmass there), since that pattern
#         more likely indicates a standardized index/z-score than real coordinates
#       - large integers (abs value >= 1000 (i.e. have at least 4 digits), no real decimals)
#   - String columns are checked if max length >= 4
#        - exclude clearly categorical data  - each unique value has at least 10 observations AND data have maximum of 20 unique values AND n_unique/n_observations <0.1
#   - All other types are normalized to object and treated as strings

import re
import pandas as pd
from pii_patterns import is_platform_id_candidate

# Column names/labels matching these patterns are always checked regardless of data type or value range.
# Covers quasi-identifiers and sensitive categories that numeric/categorical filters would otherwise skip.
# Includes English and Spanish variants. Stubs are intentional to allow for variants like disab > disability,disabled...
# 'age' uses negative lookbehinds to exclude words where 'age' is a suffix unrelated to the demographic
# (average, percentage, storage, message, image, stage, coverage, language, wage). More can be added as needed.
_FORCE_INCLUDE_PATTERNS = re.compile(
    r'(?<!aver)(?<!percent)(?<!stor)(?<!mess)(?<!im)(?<!st)(?<!cover)(?<!langu)(?<!w)age'
    r'|birth|bday|born|dob|yob|mob'
    r'|edad|nacimiento|nacido|fdn'
    r'|ethn|race|relig|faith|disab'
    r'|etnia|etni|raza|discap',
    re.IGNORECASE
)


def is_candidate_column(series: pd.Series, label: str = None) -> tuple[bool, str]:
    """
    Determines if a dataset column should be sent to LLM for PII checking.
    Returns (is_candidate, reason)
    """
    try:
        n_nonmissing = series.notna().sum()
    except TypeError:
        n_nonmissing = series.apply(lambda x: x is not None).sum()

    # --- Skip if all values are missing ---
    if n_nonmissing == 0:
        return False, "all values missing"

    # --- Force include by name/label --- age, birth, DOB etc. are always checked - patterns defined above in _FORCE_INCLUDE_PATTERNS
    # even if numeric filters (or the boolean skip below) would normally skip them (e.g. small values like age 0-100)
    col_text = f"{series.name or ''} {label or ''}"
    if _FORCE_INCLUDE_PATTERNS.search(col_text):
        return True, f"name/label matches must include pattern"

    # --- Force include platform participant IDs --- workerid / prolific / mturk columns
    if is_platform_id_candidate(str(series.name or ''), str(label) if label is not None else ''):
        return True, "name/label suggests platform participant ID (Prolific/MTurk)"

    # --- Bool --- considered as never PII (unless name/label matched a force-include pattern above)
    if series.dtype == bool:
        return False, "boolean dtype"

    # --- Datetime --- always check
    if pd.api.types.is_datetime64_any_dtype(series):
        return True, "datetime dtype"

    # normalize non-numeric, non-datetime to object
    if not pd.api.types.is_numeric_dtype(series):
        series = series.astype(object)

    # --- Numeric ---
    if pd.api.types.is_numeric_dtype(series):
        non_null = series.dropna()
        max_val = non_null.abs().max()

        max_decimals = non_null.apply(
            lambda x: len(str(x).split('.')[-1].rstrip('0')) if '.' in str(x) else 0
        ).max()

        has_real_decimals = (non_null % 1 != 0).any()

        # 1) GPS check — within coordinate bounds and enough precision
        if max_val <= 180 and max_decimals >= 3:
            exclusion_band = ((non_null >= -6.5) & (non_null <= 4.7) & (non_null != 0)).mean() #no land mass there so we can exclude variables with significant proportion of values there
            if exclusion_band > 0.10:
                return False, f"likely standardized index or similar ({exclusion_band:.0%} of values in -6.5–4.7 band)"
            return True, f"possible GPS coordinates (max={max_val}, decimals={max_decimals})"

        # 2) Small values — likert, counts etc.
        if max_val < 1000:
            return False, f"numeric with small values (max={max_val})"

        # 3) Has real decimals — standardized scores, indices etc. unlikely to be PII
        if has_real_decimals:
            return False, f"large float, unlikely to be ID (max={max_val})"

        # 4) Large integers — phone numbers, IDs, zip codes
        return True, f"large integer, possible ID or phone (max={max_val})"

    # --- String / object ---
    if series.dtype == object:
        non_null = series.dropna().astype(str)
        max_len = non_null.str.len().max()

        if max_len < 4:
            return False, f"strings too short (max length={max_len})"

        # skip if clearly categorical — every unique value appears at least 10 times + number of unique values 20 or less + n unique / n observations <0.1
        n_unique = non_null.nunique()
        min_count = non_null.value_counts().min()
        cardinality = n_unique / len(non_null)
        if n_unique <= 20 and min_count >= 10 and cardinality < 0.1:
            return False, f"likely categorical ({n_unique} unique values, min count={min_count})"

        return True, f"string candidate (max length={max_len})"

    # --- Unknown dtype --- check to be safe
    return True, f"unknown dtype ({series.dtype}), checking to be safe"
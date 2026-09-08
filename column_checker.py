# column_checker.py
# Checks a single column for PII using LLM.
# Builds a tabulation of values and sends it to the LLM for evaluation.

import re
import logging
import time
import pandas as pd
from llm_client import call_llm, parse_json_response, DEFAULT_PROVIDER, DEFAULT_MODEL
from pii_patterns import evaluate_patterns

# openpyxl (and the OOXML spec) forbids certain control characters in cell values.
# Data files can contain these (e.g. form-feed \x0c in text fields), so we strip
# them before they reach an Excel cell, to avoid IllegalCharacterError on export.
_ILLEGAL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

logger = logging.getLogger(__name__)


def sanitize_for_excel(text: str) -> str:
    """Strips control characters that openpyxl/OOXML forbid in cell values."""
    return _ILLEGAL_CHARS_RE.sub('', text)

# Tabulation settings
N_LONGEST = 10
N_LEAST_FREQ = 20
N_RANDOM = 20
MAX_UNIQUE_FULL = 50  # if n_unique <= this, send all values
MAX_PER_PATTERN_CATEGORY = 5

# Pattern checks used only to decide what gets sampled into the tabulation below
# (never a classification signal — see pii_patterns.py for the actual PII floor).
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}')
_WEB_RE = re.compile(r'(https?://|www\.)\S+', re.IGNORECASE)
_LONG_DIGIT_SPAN_RE = re.compile(r'(?<![\w.])[\d\-\s]+(?![\w.])')


def matches_long_digit_pattern(s: str, min_digits: int = 9) -> bool:
    """
    Broad, deliberately unvalidated: covers phone numbers, SSNs, bank/account
    numbers, zip+4, etc. in one pattern. Excludes dots, so a dotted numeric
    sequence (e.g. an IP address or version number) won't match this on its
    own. False positives are fine here — this only affects what gets shown
    to the LLM in the tabulation, never a classification.
    """
    for span in _LONG_DIGIT_SPAN_RE.finditer(s):
        if sum(c.isdigit() for c in span.group()) >= min_digits:
            return True
    return False


def find_pattern_matches(counts: pd.Series, max_per_category: int = MAX_PER_PATTERN_CATEGORY) -> dict:
    """
    Scans the unique values in `counts` (as produced by .value_counts(), so
    already deduplicated) for email, web-URL, and long-digit-span patterns.
    Returns up to max_per_category full original values per category —
    'email', 'web', 'long_digit', checked in that priority order per value so
    a value matching more than one pattern is only counted once, under
    whichever it matches first.

    Scans rarest-first (ascending by count), not in value_counts()'s default
    descending order: if more than max_per_category values match a category,
    a handful of common/boilerplate matches would otherwise crowd out the one
    genuinely rare, isolated value this section exists to catch — a rare
    match is exactly the case the longest/least-frequent/random groups are
    most likely to miss, while a frequent one is likely to surface there
    anyway.
    """
    matches = {'email': [], 'web': [], 'long_digit': []}

    for value in counts.sort_values(ascending=True).index:
        if all(len(matches[cat]) >= max_per_category for cat in matches):
            break
        if len(matches['email']) < max_per_category and _EMAIL_RE.search(value):
            matches['email'].append(value)
        elif len(matches['web']) < max_per_category and _WEB_RE.search(value):
            matches['web'].append(value)
        elif len(matches['long_digit']) < max_per_category and matches_long_digit_pattern(value):
            matches['long_digit'].append(value)

    return matches


def _build_tabulation(series: pd.Series, nrows: int) -> str:
    """
    Builds a tabulation of values for the LLM prompt.
    For columns with <= MAX_UNIQUE_FULL unique values, sends all.
    Otherwise, email/web/long-digit pattern matches are reserved first, then
    the top N longest, N least frequent, and N random values are sampled
    from whatever's left.

    Pattern matches are reserved before the other three groups claim
    anything (selection order), so that section reliably shows whatever
    matched regardless of coincidental overlap with the other groups — but
    it's still rendered last in the output text (display order), after the
    three normal groups, and under a generic "Other values" header, never
    labeled by pattern type. This is required, not stylistic: a descriptive
    label would tell the LLM what to look for before it evaluates the
    column, which this pipeline deliberately never does.
    """
    non_null = series.dropna().astype(str).map(sanitize_for_excel)
    counts = non_null.value_counts()
    n_unique = len(counts)

    def format_row(value, count):
        pct = count / nrows * 100
        return f"  {value:<50} {count:>6} ({pct:.1f}%)"

    # --- send all if small enough ---
    if n_unique <= MAX_UNIQUE_FULL:
        lines = ["[All values]"]
        for val, count in counts.items():
            lines.append(format_row(val, count))
        return "\n".join(lines)

    # --- otherwise send three sampled groups, plus a fourth for pattern matches ---
    pattern_matches = find_pattern_matches(counts)
    already = set()
    for vals in pattern_matches.values():
        already.update(vals)

    lines = []

    # 10 longest values (excluding anything reserved for Other values)
    longest = sorted((v for v in counts.index if v not in already), key=len, reverse=True)[:N_LONGEST]
    lines.append("[Longest values]")
    for val in longest:
        lines.append(format_row(val, counts[val]))
    already.update(longest)

    # 10 least frequent (excluding already selected)
    least_freq = [v for v in counts.index if v not in already][-N_LEAST_FREQ:]
    if least_freq:
        lines.append("\n[Least frequent values]")
        for val in least_freq:
            lines.append(format_row(val, counts[val]))
        already.update(least_freq)

    # 20 random from the rest
    remaining = [v for v in counts.index if v not in already]
    if remaining:
        import random
        sample = random.sample(remaining, min(N_RANDOM, len(remaining)))
        lines.append("\n[Random sample]")
        for val in sample:
            lines.append(format_row(val, counts[val]))

    # pattern matches, rendered last, same row format as every other group —
    # nothing distinguishes these rows from a normally-sampled one
    other_values = pattern_matches['email'] + pattern_matches['web'] + pattern_matches['long_digit']
    if other_values:
        lines.append("\n[Other values]")
        for val in other_values:
            lines.append(format_row(val, counts[val]))

    lines.append(f"\n  (Showing sample of {n_unique} unique values)")
    return "\n".join(lines)


def _build_prompt(series: pd.Series, col_name: str, label: str, nrows: int, file_name: str = '') -> str:
    """Builds the LLM prompt for PII detection."""
    dtype = str(series.dtype)
    tabulation = _build_tabulation(series, nrows)

    return f"""You are a PII detection assistant helping audit research datasets for personally identifiable information (PII).

    Analyze the following column and evaluate whether it contains personally identifiable information (PII).

    File name    : {file_name}
    Column name  : {col_name}
    Column label : {label}
    Data type    : {dtype}
    Total rows   : {nrows}

    Value tabulation:
    {tabulation}

    Classify the column as one of:
    - "direct_pii"         : contains data that directly identifies individuals (names, emails, phone numbers, precise locations, IP addresses)
    - "internal_id"        : unique internal identifier (respondent ID, household ID, etc.)
    - "possible_indirect"  : could help identify individuals in combination with other data (village names, detailed demographic characteristics, rare occupations, etc.)
    - "not_pii"            : categorical, coded, or clearly non-identifying data
    - "consent"            : column of the data that indicates whether respondents provided consent or any details regarding consent

    Notes:
    - It is not enough for variable name to indicate the data could be PII, the actual data have to include the relevant information too. For example if the tabulated value of column "respondent name" is "REMOVED" rather than real names, the column is not pii
    - If values look likely to be latitude or longitude coordinate, classify as direct_pii — assume the paired coordinate exists in another column
    - Survey/administrative timestamps (e.g. submission or completion time) are not PII
    - First names only (with no surnames) are indirect not direct PII

    Respond with ONLY a JSON object. No explanation, no reasoning, no markdown outside the JSON, no text before or after. Your entire response must be exactly this:
    {{
        "reasoning": "one sentence explanation",
        "evaluation": "direct_pii" | "internal_id" | "possible_indirect" | "not_pii" | "consent"
    }}"""


def check_column(series: pd.Series, col_name: str, label: str, nrows: int,
                 file_name: str = '',
                 provider: str = DEFAULT_PROVIDER, model: str = DEFAULT_MODEL,
                 test_mode: bool = False) -> dict:
    """
    Checks a single column for PII using LLM.
    Returns a dict with evaluation results including the tabulation shown to the LLM.

    Independently of the LLM call, deterministic pattern checks (see
    pii_patterns.evaluate_patterns) run on the same column. If a pattern
    floor is met, the final evaluation is forced to direct_pii regardless of
    what the LLM concluded on its own — the LLM's own verdict is preserved
    in the reasoning text, not discarded. The LLM always gets exactly one
    unmodified standard prompt, with no pattern info injected.

    If test_mode=True, prints the prompt and pattern-check result, and
    returns a fake result without calling the API.
    """
    tabulation = _build_tabulation(series, nrows)
    pattern_result = evaluate_patterns(series, col_name, label)

    if test_mode:
        prompt = _build_prompt(series, col_name, label, nrows, file_name)
        print(f"\n{'=' * 60}")
        print(f"TEST MODE — column: {col_name} / label: {label} [STANDARD CHECK]")
        print(f"{'=' * 60}")
        print(prompt)
        print(f"{'=' * 60}")
        print(f"Pattern check: {pattern_result}")
        print(f"{'=' * 60}\n")
        return {
            'evaluation': 'test - not checked',
            'reasoning': 'test mode — no API call made',
            'tabulation': tabulation,
        }

    retry_delays = [1, 5, 10, 30]

    prompt = _build_prompt(series, col_name, label, nrows, file_name)
    for attempt in range(1, 5):
        try:
            response = call_llm(prompt, provider=provider, model=model)
            parsed = parse_json_response(response)

            if parsed and 'evaluation' in parsed:
                llm_evaluation = parsed['evaluation']
                llm_reasoning = parsed.get('reasoning', '')

                if pattern_result['floor_met']:
                    evaluation = 'direct_pii'
                    if llm_evaluation == 'direct_pii':
                        reasoning = f"{pattern_result['note']} LLM evaluation: {llm_reasoning}"
                    else:
                        reasoning = (f"{pattern_result['note']} LLM evaluation: {llm_reasoning} "
                                     f"(LLM independently classified as {llm_evaluation})")
                else:
                    evaluation = llm_evaluation
                    reasoning = llm_reasoning

                return {
                    'evaluation': evaluation,
                    'reasoning': sanitize_for_excel(reasoning),
                    'tabulation': tabulation,
                }

            logger.warning("Attempt %d: invalid response for column '%s'", attempt, col_name)

        except Exception as e:
            logger.warning("Attempt %d: LLM call failed for column '%s': %s", attempt, col_name, e)

        if attempt < 4:
            delay = retry_delays[attempt - 1]
            logger.info("Waiting %ds before retry...", delay)
            time.sleep(delay)

    logger.error("All attempts failed for column '%s'", col_name)
    if pattern_result['floor_met']:
        return {
            'evaluation': 'direct_pii',
            'reasoning': sanitize_for_excel(
                f"{pattern_result['note']} LLM evaluation unavailable: "
                f"returned unparseable response after 4 attempts"
            ),
            'tabulation': tabulation,
        }
    return {
        'evaluation': 'error',
        'reasoning': 'LLM returned unparseable response after 4 attempts',
        'tabulation': tabulation,
    }
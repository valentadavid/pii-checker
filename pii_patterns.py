# pii_patterns.py
# Deterministic regex/rule checks that establish a PII classification "floor"
# for platform participant IDs (Prolific/MTurk) and IP addresses.
#
# These checks never hint the LLM prompt — they run independently and, if
# met, force the final evaluation to direct_pii regardless of what the LLM
# concluded on its own. See pii_pattern_detection_spec.md for the full design.
#
# No dependency on the LLM client — pure regex/rule logic, independently
# testable.

import re
import pandas as pd

_PROLIFIC_RE = re.compile(r'^[0-9a-f]{24}$', re.IGNORECASE)
_MTURK_RE = re.compile(r'^A[A-Z0-9]{9,20}$', re.IGNORECASE)
_IP_SHAPE_RE = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')


def is_platform_id_candidate(col_name: str, label: str = '') -> bool:
    """
    Returns True if the column name or label suggests a Prolific or MTurk participant ID:
      - exact name "workerid" (any capitalisation)
      - contains "prolific"
      - contains "mturk" but not "mturkcode"
    """
    for text in [col_name, label]:
        if not text:
            continue
        t = text.lower().strip()
        if t == 'workerid':
            return True
        if 'prolific' in t:
            return True
        if 'mturk' in t and 'mturkcode' not in t:
            return True
    return False


def platform_id_match_stats(series: pd.Series) -> dict:
    """
    Counts how many (row-weighted) values in series match the Prolific or
    MTurk participant ID formats.
    """
    non_null = series.dropna().astype(str).str.strip()
    counts = non_null.value_counts()
    n_nonnull = len(non_null)

    prolific_n = sum(c for v, c in counts.items() if _PROLIFIC_RE.match(v))
    mturk_n = sum(c for v, c in counts.items() if _MTURK_RE.match(v))

    return {
        'n_nonnull': n_nonnull,
        'prolific_n': prolific_n,
        'mturk_n': mturk_n,
        'prolific_frac': prolific_n / n_nonnull if n_nonnull else 0.0,
        'mturk_frac': mturk_n / n_nonnull if n_nonnull else 0.0,
    }


def is_private_ip(a: int, b: int, c: int, d: int) -> bool:
    """
    Returns True if the given octets fall in a private, reserved, loopback,
    link-local, or CGNAT range — addresses that don't identify a specific
    person even though they are syntactically valid IPs.
    """
    if (a, b, c, d) == (0, 0, 0, 0):
        return True                          # unspecified/placeholder
    if a == 10:
        return True                          # 10.0.0.0/8
    if a == 172 and 16 <= b <= 31:
        return True                          # 172.16.0.0/12
    if a == 192 and b == 168:
        return True                          # 192.168.0.0/16
    if a == 127:
        return True                          # loopback, 127.0.0.0/8
    if a == 169 and b == 254:
        return True                          # link-local, 169.254.0.0/16
    if a == 100 and 64 <= b <= 127:
        return True                          # CGNAT shared space, 100.64.0.0/10
    return False


def ip_match_stats(series: pd.Series) -> dict:
    """
    Counts how many (row-weighted) values in series look like IP addresses,
    at three levels of strictness: loose shape, strictly valid (0-255 octets),
    and public (strictly valid and not private/reserved/loopback/link-local).

    IPv6 is explicitly out of scope — no evidence of IPv6 appearing in real
    replication packages so far; revisit if that changes.
    """
    non_null = series.dropna().astype(str).str.strip()
    counts = non_null.value_counts()
    n_nonnull = len(non_null)

    loose_match_n = 0
    strict_valid_n = 0
    public_valid_n = 0

    for value, count in counts.items():
        m = _IP_SHAPE_RE.match(value)
        if not m:
            continue
        loose_match_n += count

        octets = [int(g) for g in m.groups()]
        if all(0 <= o <= 255 for o in octets):
            strict_valid_n += count
            if not is_private_ip(*octets):
                public_valid_n += count

    strict_ratio = strict_valid_n / loose_match_n if loose_match_n > 0 else 0.0
    public_ratio = public_valid_n / loose_match_n if loose_match_n > 0 else 0.0

    return {
        'n_nonnull': n_nonnull,
        'loose_match_n': loose_match_n,
        'strict_valid_n': strict_valid_n,
        'public_valid_n': public_valid_n,
        'strict_ratio': strict_ratio,
        'public_ratio': public_ratio,
    }


def _volume_floor_met(match_n: int, n_nonnull: int) -> bool:
    """
    Volume-only threshold used when the column name doesn't confirm a
    platform-ID convention: a fraction-based bar (>10%) for larger columns,
    and a fixed absolute-count bar (>3) for columns under 30 observations,
    where a fraction alone would be too noisy (one coincidental match could
    already exceed 10%). The two bars agree at n=30 (>10% of 30 is >3), so
    there's no discontinuity at the boundary.
    """
    if n_nonnull < 30:
        return match_n > 3
    return n_nonnull > 0 and (match_n / n_nonnull) > 0.10


def evaluate_patterns(series: pd.Series, col_name: str, label: str = '') -> dict:
    """
    Runs all deterministic pattern checks on a column and returns whether a
    classification floor is met.

    Platform ID floor — two independent paths, either is sufficient:
      - Name path: name/label matches a platform-ID convention AND at least
        one value matches the Prolific or MTurk format.
      - Volume path: regardless of name/label, a large enough share of
        values match the format on their own (see _volume_floor_met) — a
        safety net for platform IDs sitting in an unexpectedly-named column,
        gated by volume specifically so a single coincidental match (with no
        name confirmation) can't trigger it alone.

    IP floor — no name signal at all:
      - At least one loose IP-shape match.
      - At least half of those loose matches are strictly valid (0-255
        octets) — confirms the column is actually IP-shaped data, not just
        coincidental dot-separated numbers.
      - At least one of the strictly-valid matches is public (not
        private/reserved/loopback/link-local/CGNAT) — a column that's
        entirely private-range IPs doesn't identify anyone and shouldn't
        float, but if even one genuine public IP is mixed in among many
        private ones, that alone is enough (no separate ratio on the public
        count — diluting a single real match among private ones must not
        hide it).

    Returns:
        {
            'floor_met': bool,
            'pii_type': 'prolific' | 'mturk' | 'ip' | None,
            'note': str,
        }
    """
    notes = []
    pii_type = None
    floor_met = False

    name_match = is_platform_id_candidate(col_name, label)
    stats = platform_id_match_stats(series)

    prolific_hit = (name_match and stats['prolific_n'] >= 1) or \
        _volume_floor_met(stats['prolific_n'], stats['n_nonnull'])
    mturk_hit = (name_match and stats['mturk_n'] >= 1) or \
        _volume_floor_met(stats['mturk_n'], stats['n_nonnull'])

    if prolific_hit or mturk_hit:
        floor_met = True
        # prefer whichever format has more matches; ties favor prolific
        if stats['prolific_n'] >= stats['mturk_n'] and prolific_hit:
            pii_type = 'prolific'
            via_name = name_match and stats['prolific_n'] >= 1
            frac, n, total = stats['prolific_frac'], stats['prolific_n'], stats['n_nonnull']
            fmt = 'Prolific'
        else:
            pii_type = 'mturk'
            via_name = name_match and stats['mturk_n'] >= 1
            frac, n, total = stats['mturk_frac'], stats['mturk_n'], stats['n_nonnull']
            fmt = 'MTurk'

        if via_name:
            notes.append(
                f"[Pattern detected: name matches platform-ID convention, "
                f"{frac:.0%} ({n}/{total}) matched {fmt} ID format]"
            )
        else:
            notes.append(
                f"[Pattern detected: {frac:.0%} ({n}/{total}) of values matched "
                f"{fmt} ID format despite no name/label match]"
            )

    ip_stats = ip_match_stats(series)
    if (ip_stats['loose_match_n'] >= 1
            and ip_stats['strict_ratio'] >= 0.5
            and ip_stats['public_valid_n'] >= 1):
        floor_met = True
        if pii_type is None:
            pii_type = 'ip'
        notes.append(
            f"[Pattern detected: {ip_stats['strict_ratio']:.0%} "
            f"({ip_stats['strict_valid_n']}/{ip_stats['loose_match_n']}) "
            f"matched valid IP format, {ip_stats['public_valid_n']} public]"
        )

    return {
        'floor_met': floor_met,
        'pii_type': pii_type,
        'note': ' '.join(notes),
    }

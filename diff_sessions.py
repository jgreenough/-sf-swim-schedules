"""Compare parsed/{pool_id}.json against parsed/{pool_id}_expected.json
and report missing, extra, and type-mismatched sessions.

Sessions are matched on (day_of_week, start_time, end_time) — that's the
primary key. swim_type and raw_swim_type differences on matched sessions
are reported separately so you can tell a parser-detection failure
(missing/extra) apart from a normalization failure (right time, wrong
type label).

Exit code 0 = parser output matches expected exactly.
Exit code 1 = any discrepancy.

Usage:
    python3 diff_sessions.py balboa
"""

import json
import sys
from pathlib import Path

DEFAULT_PARSED_DIR = Path(__file__).parent / "parsed"


def load_sessions(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("sessions", [])


def session_key(session):
    return (session["day_of_week"], session["start_time"], session["end_time"])


_DAY_ORDER = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def sorted_keys(keys):
    return sorted(keys, key=lambda k: (_DAY_ORDER.get(k[0], 99), k[1], k[2]))


def main(pool_id):
    parsed_path = DEFAULT_PARSED_DIR / f"{pool_id}.json"
    expected_path = DEFAULT_PARSED_DIR / f"{pool_id}_expected.json"

    if not parsed_path.exists():
        print(
            f"ERROR: {parsed_path.name} not found. Run parse_schedule.py first.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not expected_path.exists():
        print(
            f"ERROR: {expected_path.name} not found. No expected fixture available for this pool.",
            file=sys.stderr,
        )
        sys.exit(2)

    parsed = load_sessions(parsed_path)
    expected = load_sessions(expected_path)

    parsed_by_key = {}
    for session in parsed:
        # If the parser produces duplicate keys (multiple sessions at the same
        # day+time), keep them all so we can report the duplication.
        parsed_by_key.setdefault(session_key(session), []).append(session)
    expected_by_key = {}
    for session in expected:
        expected_by_key.setdefault(session_key(session), []).append(session)

    missing = [k for k in expected_by_key if k not in parsed_by_key]
    extra = [k for k in parsed_by_key if k not in expected_by_key]
    common = [k for k in expected_by_key if k in parsed_by_key]

    type_mismatches = []
    for k in common:
        # If either side has multiples at this key, compare the first.
        p_type = parsed_by_key[k][0]["swim_type"]
        e_type = expected_by_key[k][0]["swim_type"]
        if p_type != e_type:
            type_mismatches.append((k, p_type, e_type))

    print(f"Comparison for {pool_id}:")
    print(f"  Expected sessions: {len(expected)}")
    print(f"  Parsed sessions:   {len(parsed)}")
    print(f"  Matched (by day+time): {len(common)} of {len(expected)}")
    print()

    if missing:
        print(f"MISSING ({len(missing)}) — present in expected, absent in parsed:")
        for k in sorted_keys(missing):
            day, start, end = k
            session = expected_by_key[k][0]
            print(
                f"  - {day:<10} {start}-{end}  {session['swim_type']:<18} "
                f"\"{session['raw_swim_type']}\""
            )
        print()

    if extra:
        print(f"EXTRA ({len(extra)}) — present in parsed, absent in expected:")
        for k in sorted_keys(extra):
            day, start, end = k
            session = parsed_by_key[k][0]
            print(
                f"  + {day:<10} {start}-{end}  {session['swim_type']:<18} "
                f"\"{session['raw_swim_type']}\""
            )
        print()

    if type_mismatches:
        print(
            f"TYPE MISMATCHES ({len(type_mismatches)}) — matched on day+time but swim_type differs:"
        )
        for (k, p_type, e_type) in sorted(type_mismatches, key=lambda x: (_DAY_ORDER.get(x[0][0], 99), x[0][1])):
            day, start, end = k
            print(
                f"  ~ {day:<10} {start}-{end}  parsed={p_type:<18} expected={e_type}"
            )
        print()

    if missing or extra or type_mismatches:
        print(
            f"DIFF: {len(missing)} missing, {len(extra)} extra, "
            f"{len(type_mismatches)} type mismatch(es)"
        )
        sys.exit(1)

    print("PASS — parser output matches expected exactly.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 diff_sessions.py <pool_id>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])

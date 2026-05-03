"""M2 multi-pool runner (per PRD section 11): parse every active pool in
pools.json and print a summary table for spot-checking.

Pools flagged `maintenance: true` (currently Sava) are skipped politely.
For each remaining pool we shell out to `parse_schedule.py {pool_id}`,
which downloads the chosen PDF and writes parsed/{pool_id}.json. We then
read that JSON and aggregate session counts into one table.

Per-pool sanity checks flag at-risk pools without needing user input:
  - Total session count below a minimum
  - Fewer than 3 days have any sessions
  - No Lap Swim sessions found
  - No Family Swim sessions found

Usage:
    python3 parse_all.py
    python3 parse_all.py --diff      # also run diff_sessions.py per pool
                                       (only meaningful for pools with
                                       parsed/{pool_id}_expected.json)
"""

import json
import subprocess
import sys
from pathlib import Path

DEFAULT_INVENTORY_PATH = Path(__file__).parent / "pools.json"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "parsed"

# Sanity-check thresholds.
_MIN_TOTAL_SESSIONS = 10
_MIN_ACTIVE_DAYS = 3
_MUST_HAVE_TYPES = ("lap_swim", "family_swim")


def run_parse(pool_id):
    """Shell out to parse_schedule.py to download + parse one pool. Returns
    (return_code, stdout_tail). Captures stdout/stderr to keep this script's
    output tidy."""
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "parse_schedule.py"), pool_id],
        capture_output=True,
        text=True,
    )
    # Show only the final summary lines from parse_schedule per pool.
    tail = "\n".join(result.stdout.strip().splitlines()[-3:])
    if result.returncode != 0:
        tail = (tail + "\n" + result.stderr.strip()).strip()
    return result.returncode, tail


def load_parsed(pool_id, output_dir=DEFAULT_OUTPUT_DIR):
    path = output_dir / f"{pool_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def maybe_run_diff(pool_id):
    """Run diff_sessions.py if an expected fixture exists. Returns
    (status_string, return_code) — status is 'PASS', 'FAIL', or 'no fixture'."""
    fixture = DEFAULT_OUTPUT_DIR / f"{pool_id}_expected.json"
    if not fixture.exists():
        return ("no fixture", None)
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "diff_sessions.py"), pool_id],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return ("PASS", 0)
    # Pull the DIFF: ... summary line if present
    last = ""
    for line in result.stdout.strip().splitlines()[::-1]:
        if line.startswith("DIFF:"):
            last = line
            break
    return (last or "FAIL", result.returncode)


def sanity_warnings(parsed):
    sessions = parsed.get("sessions", [])
    warnings = []
    if len(sessions) < _MIN_TOTAL_SESSIONS:
        warnings.append(f"only {len(sessions)} session(s)")
    by_day = {s["day_of_week"] for s in sessions}
    if len(by_day) < _MIN_ACTIVE_DAYS:
        warnings.append(f"only {len(by_day)} day(s) active")
    types_present = {s["swim_type"] for s in sessions}
    for required in _MUST_HAVE_TYPES:
        if required not in types_present:
            warnings.append(f"no {required}")
    return warnings


def main(argv):
    do_diff = "--diff" in argv

    inventory = json.loads(DEFAULT_INVENTORY_PATH.read_text(encoding="utf-8"))
    rows = []

    for pool in inventory:
        pool_id = pool["id"]
        name = pool["name"]
        if pool.get("maintenance"):
            print(f"SKIP {name} ({pool_id}) — maintenance: {pool.get('notes', '').strip() or 'no note'}")
            rows.append(
                {"pool_id": pool_id, "name": name, "status": "skipped", "reason": "maintenance"}
            )
            continue

        print(f"--- {name} ({pool_id}) ---")
        rc, tail = run_parse(pool_id)
        if rc != 0:
            print(f"  FAILED to parse:\n{tail}")
            rows.append({"pool_id": pool_id, "name": name, "status": "parse_failed", "tail": tail})
            continue
        parsed = load_parsed(pool_id)
        if parsed is None:
            print(f"  parse_schedule reported success but parsed/{pool_id}.json is missing")
            rows.append({"pool_id": pool_id, "name": name, "status": "missing_output"})
            continue

        sessions = parsed["sessions"]
        by_day = {}
        for s in sessions:
            by_day[s["day_of_week"]] = by_day.get(s["day_of_week"], 0) + 1
        warnings = sanity_warnings(parsed)
        diff_status, _ = maybe_run_diff(pool_id) if do_diff else ("not run", None)

        rows.append(
            {
                "pool_id": pool_id,
                "name": name,
                "status": "parsed",
                "total": len(sessions),
                "by_day": by_day,
                "warnings": warnings,
                "diff": diff_status,
            }
        )
        print(f"  parsed {len(sessions)} sessions" + (f"  WARN: {'; '.join(warnings)}" if warnings else ""))

    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    full = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    header = f"{'Pool':<28} {'Total':>5} " + " ".join(f"{d:>4}" for d in days) + "  Notes"
    print(header)
    print("-" * len(header))
    for row in rows:
        if row["status"] == "skipped":
            note = f"skipped: {row['reason']}"
            print(f"{row['name']:<28} {'-':>5} " + " ".join(f"{'-':>4}" for _ in days) + f"  {note}")
            continue
        if row["status"] != "parsed":
            print(f"{row['name']:<28} {'?':>5} " + " ".join(f"{'?':>4}" for _ in days) + f"  {row['status']}")
            continue
        bd = row["by_day"]
        cells = " ".join(f"{bd.get(full[i], 0):>4}" for i in range(7))
        notes = []
        if row["warnings"]:
            notes.append("WARN: " + "; ".join(row["warnings"]))
        if do_diff:
            notes.append(f"diff: {row['diff']}")
        print(f"{row['name']:<28} {row['total']:>5} {cells}  {'; '.join(notes)}")

    print()
    parsed_count = sum(1 for r in rows if r["status"] == "parsed")
    failed_count = sum(1 for r in rows if r["status"] in ("parse_failed", "missing_output"))
    skipped_count = sum(1 for r in rows if r["status"] == "skipped")
    warned_count = sum(1 for r in rows if r["status"] == "parsed" and r["warnings"])
    print(
        f"Parsed: {parsed_count}  |  Failed: {failed_count}  |  Skipped: {skipped_count}  |  With warnings: {warned_count}"
    )

    # Exit code semantics:
    #   0 = every active pool parsed successfully (warnings are informational
    #       but don't fail the run — they're worth reviewing manually but
    #       shouldn't block the scheduler from committing whatever data we
    #       did get).
    #   1 = at least one active pool failed to parse, which means the source
    #       page or PDF is unreachable / unparseable and an operator should
    #       investigate. The scheduler will surface this as a workflow
    #       failure email from GitHub.
    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])

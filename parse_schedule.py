"""M1 parser (per PRD section 11): parse one pool's swim schedule PDF
into structured JSON. Pilot pool: Balboa.

Usage:
    pip3 install pdfplumber           # required (also used by check_pool_pages)
    python3 parse_schedule.py balboa
    python3 diff_sessions.py balboa   # validate against expected fixture

What it does:
  1. Looks up the pool in pools.json. Skips politely if `maintenance: true`.
  2. Re-uses check_pool_pages.py's discovery to find the chosen schedule
     PDF (anchor + content + English filters all applied).
  3. Downloads the PDF to pdfs/{pool_id}.pdf so we have a local archive
     and can re-parse without re-fetching.
  4. Parses each page using pdfplumber's word-level extraction. We use
     pdfplumber (not PyMuPDF) because it correctly applies the page's
     declared rotation — SF Rec & Park PDFs are stored rotated and
     pdfplumber respects it; PyMuPDF reports unrotated coordinates and
     made the algorithm fight the layout.
  5. Algorithm:
       a. Find day-of-week words; cluster them by their top y-coord and
          pick the cluster with the most distinct day names. This is the
          header row, and rejects body matches like the word "Thursday"
          inside "Closed every 3rd Thursday of the month for training."
       b. Build column boundaries with midpoints between adjacent day
          centers (Voronoi style). Cap the leftmost/rightmost edges at
          one half-average-gap from the outermost day centers, so the
          last column doesn't extend to the page edge and absorb the
          right-hand legend panel.
       c. Bucket every body word into a column by its x-center.
       d. Sort each column's words by top y, group into lines (small y
          tolerance), then walk the lines. For each time-range line,
          scan UPWARD collecting non-time lines as the label, stopping
          at another time line or a vertical gap larger than
          MAX_LABEL_GAP_PT.
       e. Normalize the label into a swim_type via _SWIM_TYPE_RULES.
  6. Post-process all extracted text to replace the `(cid:415)` font
     artifact with `ti` (the ligature pdfplumber can't decode in this
     font), and strip other unrecognized `(cid:NNN)` markers.
  7. Writes parsed/{pool_id}.json with one record per session, plus
     parsed/{pool_id}_raw.json with pdfplumber's raw extraction for
     debugging.
"""

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print(
        "ERROR: pdfplumber is required. Install with `pip3 install pdfplumber`.",
        file=sys.stderr,
    )
    sys.exit(2)

from check_pool_pages import (  # noqa: E402  (import after pdfplumber check)
    USER_AGENT,
    TIMEOUT_SECONDS,
    choose_schedule_anchor,
    discover_pdf_anchors,
    fetch_page,
    verify_pdf_content,
)

DEFAULT_INVENTORY_PATH = Path(__file__).parent / "pools.json"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "parsed"
DEFAULT_PDF_DIR = Path(__file__).parent / "pdfs"


# ---------- regexes (local; no global mutation) ----------

_DAY_PATTERN = re.compile(
    r"^(?:mon|monday|tue|tues|tuesday|wed|wednesday|thu|thur|thurs|thursday|fri|friday|sat|saturday|sun|sunday)$",
    re.IGNORECASE,
)
_DAY_NORMALIZE = {
    "mon": "monday", "monday": "monday",
    "tue": "tuesday", "tues": "tuesday", "tuesday": "tuesday",
    "wed": "wednesday", "wednesday": "wednesday",
    "thu": "thursday", "thur": "thursday", "thurs": "thursday", "thursday": "thursday",
    "fri": "friday", "friday": "friday",
    "sat": "saturday", "saturday": "saturday",
    "sun": "sunday", "sunday": "sunday",
}

_TIME_RANGE_PATTERN = re.compile(
    r"(?P<sh>\d{1,2})[:.](?P<sm>\d{2})\s*(?P<sap>am|pm)?"
    r"\s*[-–—]+\s*"
    r"(?P<eh>\d{1,2})[:.](?P<em>\d{2})\s*(?P<eap>am|pm)?",
    re.IGNORECASE,
)

_NUMERIC_DATE_RANGE_PATTERN = re.compile(
    r"(?P<m1>\d{1,2})/(?P<d1>\d{1,2})/(?P<y1>20\d{2})\s*[-–—to ]+\s*"
    r"(?P<m2>\d{1,2})/(?P<d2>\d{1,2})/(?P<y2>20\d{2})"
)
_TEXTUAL_DATE_RANGE_PATTERN = re.compile(
    r"(?P<m1>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z.]*\s+"
    r"(?P<d1>\d{1,2})\s*[-–—to ]+\s*"
    r"(?P<m2>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z.]*\s+"
    r"(?P<d2>\d{1,2})"
    r"(?:[,\s]*(?P<year>20\d{2}))?",
    re.IGNORECASE,
)

# Order matters; first match wins. Family check first so compound cells
# like "REC/FAMILY SWIM/ LAP SWIM" classify as family_swim.
_SWIM_TYPE_RULES = [
    (re.compile(r"\bfamily(?:\s*rec)?\s*swim\b|\brec\s*/\s*family\b|\bfamily\b", re.IGNORECASE), "family_swim"),
    # SFUSD label can appear with or without the literal word "class".
    (re.compile(r"\bsfusd\b", re.IGNORECASE), "school_class"),
    (re.compile(r"\b(?:adult|youth)\s*(?:adv\.?\s*)?(?:swim\s*)?lessons\b|\blearn\s*to\s*swim\b|\bswim\s*lesson|\blessons\b|\bpre[-\s]school\s*lessons\b", re.IGNORECASE), "lessons"),
    # Aqua-fitness covers H2O aerobics, water aerobics, aquafit, exercise,
    # and "self guided exercise"/"execise" (the second-R-missing typo seen
    # at North Beach). The `r?` makes the R in "exer(r)cise" optional.
    (re.compile(r"\b(?:water\s*aerobics|h2o(?:\s*aerobics)?|aquafit|aqua\s*fit|water\s*exercise|fitness|self[-\s]guided\s*exer?cise|exer?cise)\b", re.IGNORECASE), "aqua_fitness"),
    # Team-practice: masters, HS swim team, synchro, named teams (Piranhas),
    # competitive events (swim meet), water polo.
    (re.compile(r"\b(?:masters|swim\s*team|swim\s*meet|practice|synchro|piranhas|water\s*polo)\b", re.IGNORECASE), "team_practice"),
    (re.compile(r"\b(?:senior|therapy|access)\b", re.IGNORECASE), "senior_therapy"),
    (re.compile(r"\bparent\s*(?:and|&|/|n)?\s*(?:child|tot)\b|\btot\s*swim\b", re.IGNORECASE), "parent_and_tot"),
    (re.compile(r"\blap\s*swim\b|\blap\b", re.IGNORECASE), "lap_swim"),
    (re.compile(r"\b(?:recreation|rec)\s*swim\b|\bopen\s*swim\b|\bsafety\s*swim\b|\bsplash\b", re.IGNORECASE), "recreation_swim"),
    (re.compile(r"\b(?:closed|no\s*swim|closure|holiday)\b", re.IGNORECASE), "closed"),
]

# A label is "real" if it has enough alphabetic content and isn't a date
# fragment. Used to reject parser artifacts like "( )", "April 16,", and
# stray label tails that would otherwise show up as bogus unknown sessions.
_LABEL_DATE_FRAGMENT_PATTERN = re.compile(
    r"^\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d+[,\s]*$",
    re.IGNORECASE,
)
_LABEL_MIN_ALPHA = 3


def _is_real_label(text):
    if not text:
        return False
    alpha_count = sum(1 for c in text if c.isalpha())
    if alpha_count < _LABEL_MIN_ALPHA:
        return False
    if _LABEL_DATE_FRAGMENT_PATTERN.match(text.strip()):
        return False
    return True

_CID_PATTERN = re.compile(r"\(cid:(\d+)\)")
# Known font-CID-to-glyph mappings observed in SF Rec & Park PDFs.
# 415 is the `ti` ligature ("un(cid:415)l" -> "until", "Aqua(cid:415)cs" -> "Aquatics").
_CID_REPLACEMENTS = {
    "415": "ti",
}

# Layout tuning constants.
_LINE_GROUP_TOLERANCE_PT = 3
_MAX_LABEL_GAP_PT = 35
_HEADER_PADDING_PT = 4
_DAY_HEADER_TOP_LIMIT_PT = 200  # day-header words must appear above this y


# ---------- text cleaning ----------


def clean_text(text):
    """Replace known PDF font CID artifacts with their glyph equivalents,
    and strip unknown CIDs entirely."""
    if not text:
        return text

    def replace(match):
        return _CID_REPLACEMENTS.get(match.group(1), "")

    return _CID_PATTERN.sub(replace, text)


# ---------- discovery + download (re-uses check_pool_pages) ----------


def find_pool(inventory_path, pool_id):
    pools = json.loads(inventory_path.read_text(encoding="utf-8"))
    matches = [p for p in pools if p["id"] == pool_id]
    if not matches:
        available = ", ".join(p["id"] for p in pools)
        raise SystemExit(
            f"No pool with id '{pool_id}' in {inventory_path.name}. Available: {available}"
        )
    return matches[0]


def discover_chosen_pdf(pool):
    page_status, body, page_error = fetch_page(pool["source_page_url"])
    if page_status != 200 or body is None:
        raise SystemExit(
            f"Failed to fetch source page for {pool['id']}: status={page_status}, error={page_error}"
        )
    candidates = discover_pdf_anchors(body, pool["source_page_url"])
    if not candidates:
        raise SystemExit(f"No PDF candidates found on {pool['source_page_url']}")
    verified = []
    for candidate in candidates:
        cv = verify_pdf_content(candidate["url"])
        if cv["ran"] and cv["looks_like_schedule"]:
            verified.append(candidate)
    pool_for_choice = verified if verified else candidates
    chosen = choose_schedule_anchor(
        [{"url": c["url"], "text": c["text"]} for c in pool_for_choice]
    )
    if not chosen:
        raise SystemExit(f"Disambiguation produced no chosen URL for {pool['id']}")
    return chosen


def download_pdf(url, dest_path):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        dest_path.write_bytes(response.read())
    return dest_path


# ---------- shared normalization ----------


def normalize_swim_type(cell_text):
    if cell_text is None:
        return None
    cleaned = " ".join(cell_text.split())
    if not cleaned:
        return None
    for pattern, label in _SWIM_TYPE_RULES:
        if pattern.search(cleaned):
            return (label, cleaned)
    return ("unknown", cleaned)


def _to_24h(hour, ampm):
    if not ampm:
        if 1 <= hour <= 7:
            return hour + 12
        return hour
    if ampm == "am":
        return 0 if hour == 12 else hour
    if ampm == "pm":
        return 12 if hour == 12 else hour + 12
    return hour


def normalize_time_range(text):
    if not text:
        return (None, None)
    match = _TIME_RANGE_PATTERN.search(text)
    if not match:
        return (None, None)
    sh, sm = int(match.group("sh")), int(match.group("sm"))
    eh, em = int(match.group("eh")), int(match.group("em"))
    sap = (match.group("sap") or "").lower()
    eap = (match.group("eap") or "").lower()

    # When only one side has an am/pm marker, infer the other smartly:
    # try the same period first, and fall back to the opposite if that
    # produces a backwards range (which means the range crosses noon —
    # e.g., "10:30 - 12:00 pm" actually means 10:30 AM to 12:00 PM, not
    # 10:30 PM to 12:00 PM. Many SF Rec & Park PDFs mark only the end).
    def total_min(h, m):
        return h * 60 + m

    if sap and not eap:
        if total_min(_to_24h(sh, sap), sm) < total_min(_to_24h(eh, sap), em):
            eap = sap
        else:
            eap = "pm" if sap == "am" else "am"
    elif eap and not sap:
        if total_min(_to_24h(sh, eap), sm) < total_min(_to_24h(eh, eap), em):
            sap = eap
        else:
            sap = "pm" if eap == "am" else "am"

    sh24 = _to_24h(sh, sap)
    eh24 = _to_24h(eh, eap)

    # Final guard: if neither side had an explicit marker and the range is
    # still backwards, bump the end to PM (the existing heuristic).
    if (
        eh24 is not None
        and sh24 is not None
        and eh24 < sh24
        and not match.group("eap")
        and not match.group("sap")
    ):
        eh24 = (eh % 12) + 12

    start = f"{sh24:02d}:{sm:02d}" if sh24 is not None else None
    end = f"{eh24:02d}:{em:02d}" if eh24 is not None else None
    return (start, end)


def find_effective_dates(text):
    if not text:
        return (None, None)
    numeric = _NUMERIC_DATE_RANGE_PATTERN.search(text)
    if numeric:
        try:
            start = datetime(
                int(numeric.group("y1")), int(numeric.group("m1")), int(numeric.group("d1"))
            ).date()
            end = datetime(
                int(numeric.group("y2")), int(numeric.group("m2")), int(numeric.group("d2"))
            ).date()
            return (start.isoformat(), end.isoformat())
        except ValueError:
            pass
    textual = _TEXTUAL_DATE_RANGE_PATTERN.search(text)
    if textual:
        year = int(textual.group("year")) if textual.group("year") else datetime.now().year
        try:
            start = datetime.strptime(
                f"{textual.group('m1')[:3]} {textual.group('d1')} {year}", "%b %d %Y"
            ).date()
            end = datetime.strptime(
                f"{textual.group('m2')[:3]} {textual.group('d2')} {year}", "%b %d %Y"
            ).date()
            if end < start:
                end = end.replace(year=year + 1)
            return (start.isoformat(), end.isoformat())
        except ValueError:
            return (None, None)
    return (None, None)


# ---------- pdfplumber parser ----------


def find_header_row(words):
    """Cluster day-of-week words by top-y; pick the cluster with the most
    distinct day names. Rejects body matches like the word 'Thursday' inside
    'Closed every 3rd Thursday of the month for training.'"""
    candidates = [
        w
        for w in words
        if _DAY_PATTERN.match(w["text"]) and w["top"] < _DAY_HEADER_TOP_LIMIT_PT
    ]
    if not candidates:
        return []
    clusters = {}
    for w in candidates:
        bucket = round(w["top"] / 3) * 3
        clusters.setdefault(bucket, []).append(w)
    best = None
    best_distinct = 0
    for cluster in clusters.values():
        distinct = len({_DAY_NORMALIZE[w["text"].lower()] for w in cluster})
        if distinct > best_distinct:
            best_distinct = distinct
            best = cluster
    if not best or best_distinct < 3:
        return []
    seen = {}
    for w in best:
        day = _DAY_NORMALIZE[w["text"].lower()]
        if day not in seen:
            seen[day] = w
    out = list(seen.values())
    out.sort(key=lambda w: w["x0"])
    return out


def _build_column_boundaries(headers, page_x_max):
    """Voronoi-style midpoint boundaries between day centers, with the
    leftmost/rightmost outer edges capped at one half-average-gap from
    the outermost day centers (NOT extending to the page edge — that
    would absorb side-panel content like the SF Rec & Park legend)."""
    centers = [(w["x0"] + w["x1"]) / 2 for w in headers]
    if len(centers) >= 2:
        gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        avg_gap = sum(gaps) / len(gaps)
    else:
        avg_gap = 100.0
    half = avg_gap / 2
    boundaries = [max(0.0, centers[0] - half)]
    for i in range(len(centers) - 1):
        boundaries.append((centers[i] + centers[i + 1]) / 2)
    boundaries.append(min(page_x_max, centers[-1] + half))
    return boundaries


def parse_page(page):
    words = page.extract_words() or []
    headers = find_header_row(words)
    if not headers:
        return [], {
            "word_count": len(words),
            "header_found": False,
            "days_found": [],
        }

    days = [_DAY_NORMALIZE[w["text"].lower()] for w in headers]
    boundaries = _build_column_boundaries(headers, page.bbox[2])
    header_bottom = max(w["bottom"] for w in headers)

    def column_for(x_center):
        for i in range(len(days)):
            if boundaries[i] <= x_center < boundaries[i + 1]:
                return i
        return None

    columns = {i: [] for i in range(len(days))}
    for w in words:
        if w["top"] <= header_bottom + _HEADER_PADDING_PT:
            continue
        x_center = (w["x0"] + w["x1"]) / 2
        col = column_for(x_center)
        if col is None:
            continue
        columns[col].append(w)

    sessions = []
    for col_index, col_words in columns.items():
        if not col_words:
            continue
        col_words.sort(key=lambda w: (w["top"], w["x0"]))

        lines = []
        current_line = []
        last_top = None
        for w in col_words:
            if last_top is None or abs(w["top"] - last_top) <= _LINE_GROUP_TOLERANCE_PT:
                current_line.append(w)
            else:
                lines.append(current_line)
                current_line = [w]
            last_top = w["top"]
        if current_line:
            lines.append(current_line)

        line_records = []
        for line in lines:
            sorted_line = sorted(line, key=lambda w: w["x0"])
            text = clean_text(" ".join(w["text"] for w in sorted_line))
            line_records.append(
                {
                    "text": text,
                    "top": min(w["top"] for w in line),
                    "bottom": max(w["bottom"] for w in line),
                    "has_time": bool(_TIME_RANGE_PATTERN.search(text)),
                }
            )

        for i, line in enumerate(line_records):
            if not line["has_time"]:
                continue
            time_match = _TIME_RANGE_PATTERN.search(line["text"])
            text_without_time = _TIME_RANGE_PATTERN.sub("", line["text"]).strip()

            # Always scan UP for label context, even if the time line has its
            # own non-time text. This catches notice cells like Rossi's
            # "3rd Thursday of month pool closed 11:00-2:00 for training":
            # taking only the same-line "for training" fragment loses the
            # "closed" signal that lets us correctly classify and filter it.
            label_parts = []
            ref_top = line["top"]
            for j in range(i - 1, -1, -1):
                prev = line_records[j]
                if prev["has_time"]:
                    break
                if (ref_top - prev["bottom"]) > _MAX_LABEL_GAP_PT:
                    break
                label_parts.insert(0, prev["text"])
                ref_top = prev["top"]

            full_label_parts = list(label_parts)
            if text_without_time:
                full_label_parts.append(text_without_time)
            label = " ".join(full_label_parts).strip()

            if not label or not _is_real_label(label):
                continue
            normalized = normalize_swim_type(label)
            if not normalized:
                continue
            swim_type, raw = normalized
            # Closure / notice cells aren't real swim sessions — drop them.
            if swim_type == "closed":
                continue
            start_time, end_time = normalize_time_range(time_match.group(0))
            if not start_time or not end_time:
                continue
            sessions.append(
                {
                    "day_of_week": days[col_index],
                    "start_time": start_time,
                    "end_time": end_time,
                    "swim_type": swim_type,
                    "raw_swim_type": raw,
                    "notes": "",
                }
            )

    page_diagnostics = {
        "word_count": len(words),
        "header_found": True,
        "days_found": days,
        "column_boundaries": boundaries,
    }
    return sessions, page_diagnostics


def parse_pdf(pdf_path, page_filter=None):
    """Parse the PDF. If `page_filter` is set, only pages whose extracted
    text contains the filter string (case-insensitive) are processed —
    used for multi-pool facility PDFs (e.g., North Beach has separate
    WARM POOL and COOL POOL pages in one document)."""
    sessions = []
    raw_pages = []
    filter_lower = page_filter.lower() if page_filter else None
    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            cleaned_text = clean_text(page_text)
            page_record = {
                "page_number": page_index + 1,
                "text": cleaned_text,
            }
            if filter_lower and filter_lower not in page_text.lower():
                page_record["skipped"] = f"page_filter '{page_filter}' did not match"
                raw_pages.append(page_record)
                continue
            page_sessions, diag = parse_page(page)
            sessions.extend(page_sessions)
            page_record.update(diag)
            raw_pages.append(page_record)

    seen = set()
    unique = []
    for session in sessions:
        key = (
            session["day_of_week"],
            session["start_time"],
            session["end_time"],
            session["swim_type"],
            session["raw_swim_type"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(session)
    return unique, raw_pages


# ---------- main ----------


def main(pool_id, inventory_path=DEFAULT_INVENTORY_PATH, output_dir=DEFAULT_OUTPUT_DIR):
    pool = find_pool(inventory_path, pool_id)

    if pool.get("maintenance"):
        print(
            f"Pool '{pool_id}' is flagged maintenance; skipping. "
            f"Note: {pool.get('notes', '')}"
        )
        return

    print(f"Discovering schedule PDF for {pool['name']}...")
    chosen = discover_chosen_pdf(pool)
    print(f"  chosen: \"{chosen['text']}\"")
    print(f"  url:    {chosen['url']}")

    pdf_path = DEFAULT_PDF_DIR / f"{pool_id}.pdf"
    print(f"Downloading to {pdf_path.relative_to(Path(__file__).parent)} ...")
    download_pdf(chosen["url"], pdf_path)
    print(f"  {pdf_path.stat().st_size:,} bytes written")

    page_filter = pool.get("pdf_page_filter")
    if page_filter:
        print(f"Parsing with pdfplumber (page filter: {page_filter!r})...")
    else:
        print("Parsing with pdfplumber (rotation-aware word extraction)...")
    sessions, raw = parse_pdf(pdf_path, page_filter=page_filter)

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / f"{pool_id}_raw.json"
    raw_path.write_text(
        json.dumps({"pages": raw}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  raw extraction -> {raw_path.relative_to(Path(__file__).parent)}")

    full_text = "\n".join(p["text"] for p in raw)
    effective_start, effective_end = find_effective_dates(full_text)

    output = {
        "pool_id": pool["id"],
        "pool_name": pool["name"],
        "source_page_url": pool["source_page_url"],
        "source_pdf_url": chosen["url"],
        "source_pdf_anchor_text": chosen["text"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "effective_start": effective_start,
        "effective_end": effective_end,
        "session_count": len(sessions),
        "sessions": sessions,
    }
    parsed_path = output_dir / f"{pool_id}.json"
    parsed_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  parsed sessions -> {parsed_path.relative_to(Path(__file__).parent)}")

    print()
    print(f"Effective dates: {effective_start} → {effective_end}")
    print(f"Total sessions: {len(sessions)}")

    by_day = {}
    by_type = {}
    for session in sessions:
        by_day[session["day_of_week"]] = by_day.get(session["day_of_week"], 0) + 1
        by_type[session["swim_type"]] = by_type.get(session["swim_type"], 0) + 1
    if by_day:
        print("By day of week:")
        order = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for day in order:
            if day in by_day:
                print(f"  {day:<10} {by_day[day]}")
    if by_type:
        print("By swim type:")
        for swim_type, count in sorted(by_type.items(), key=lambda kv: -kv[1]):
            print(f"  {swim_type:<20} {count}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 parse_schedule.py <pool_id>", file=sys.stderr)
        print("Example: python3 parse_schedule.py balboa", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])

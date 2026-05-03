"""M0 test condition (per PRD section 11): for every pool in pools.json,
walk the per-poll discovery sequence end-to-end:

  1. GET the source_page_url, assert HTTP 200.
  2. Parse the HTML, extract every anchor's href + visible text, and keep
     the ones whose href looks like a PDF candidate (either ends in .pdf or
     matches /DocumentCenter/View/{id}[/{slug}]).
  3. Disambiguate to a single chosen anchor (four-tier rule per PRD section 5.1):
       Tier 1: anchor text contains "schedule" (case-insensitive)
       Tier 2: URL slug contains "schedule"
       Tier 3: exclude candidates whose text/URL looks like a non-schedule
               document (rules, flyer, registration, policy, etc.)
       Tier 4: first remaining candidate (page order)
     Tiebreak within tiers 1 and 2 by highest 4-digit year in text or URL.
  4. Follow the chosen PDF URL, assert HTTP 200 with a PDF content-type.
     Falls back to GET if the server misbehaves on HEAD (CivicPlus does).
  5. (Optional, requires `pip install pypdf`) Download the chosen PDF,
     extract first-page text, and verify it looks like a swim schedule
     (multiple time patterns + day-of-week mentions, or explicit
     "Lap Swim"/"Family Swim" labels). Catches source-site link
     mislabeling that no URL/text heuristic can detect. If pypdf is not
     installed, this step is skipped with a warning.

Exit code 0 => all required pools pass. Exit code 1 => one or more failures.
Pools flagged `seasonal: true` may legitimately fail outside swim season.

The script also reports "suspicious" choices independently of pass/fail:
a chosen URL whose text/URL contains an excluded term, a chosen anchor
with no visible text, or a chosen URL that's shared with another pool's
choice (the last is a strong signal of a global footer link being picked
instead of a per-pool schedule). Suspicious pools always print their full
candidate list so disambiguation problems are visible at a glance.

Usage:
    pip3 install pypdf            # one-time, optional but recommended
    python3 check_pool_pages.py
    python3 check_pool_pages.py path/to/pools.json   # optional override

No third-party dependencies are required for the basic check; pypdf is
optional and gates the content-verification step.
"""

import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

# All variables are defined locally inside main() / per-call functions
# to keep state contained, per project guidance.

try:
    import pypdf  # type: ignore

    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

DEFAULT_INVENTORY_PATH = Path(__file__).parent / "pools.json"
USER_AGENT = "swim-schedule-inventory-check/0.4 (M0 test condition; PRD section 11)"
TIMEOUT_SECONDS = 30  # raised because PDF downloads are larger than HTML pages

# A href is a PDF candidate if it ends in .pdf OR matches the SF Rec & Park
# CivicPlus Document Center pattern /DocumentCenter/View/{id}[/{slug}].
_PDF_EXTENSION_PATTERN = re.compile(r"\.pdf(\?|#|$)", re.IGNORECASE)
_DOC_CENTER_PATTERN = re.compile(r"/DocumentCenter/View/\d+", re.IGNORECASE)
_YEAR_PATTERN = re.compile(r"(20\d{2})")

# Anchor text or slug terms that indicate a non-schedule document.
_NON_SCHEDULE_TERMS = (
    "rules",
    "flyer",
    "registration",
    "policy",
    "application",
    "waiver",
    "permit",
    "rental",
)

# Language preference. We prefer English documents when the source publishes
# multi-language variants (the SF Rec & Park Sava page, for example, links
# deck rules in English, Chinese, and Spanish). Markers are matched on
# anchor text + URL, case-insensitive.
_ENGLISH_MARKER = "english"
_NON_ENGLISH_LANGUAGE_TERMS = (
    "chinese",
    "spanish",
    "español",
    "espanol",
    "中文",
    "繁體",
    "简体",
    "tagalog",
    "vietnamese",
    "russian",
)

# Content-verification heuristics. A schedule-like PDF should have many
# time patterns (e.g., "6:00 AM"), several day-of-week mentions (Mon/Tue/...),
# and ideally explicit swim-type labels.
_TIME_PATTERN = re.compile(
    r"\b\d{1,2}:\d{2}\s?(?:am|pm|AM|PM)?", re.IGNORECASE
)
_DAY_PATTERN = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day|s)?\b", re.IGNORECASE
)
_SCHEDULE_KEYWORDS = (
    "lap swim",
    "family swim",
    "recreation swim",
    "open swim",
    "parent and tot",
    "tot swim",
    "schedule",
)

# Thresholds for the schedule classifier. Conservative on purpose: a typical
# pool schedule has dozens of time slots and many day mentions, while a
# rules/flyer document has very few of either.
_MIN_TIME_HITS = 5
_MIN_DAY_HITS = 3
_MIN_KEYWORD_HITS_ALONE = 2  # 2+ schedule keywords pass even without time/day


# ---------- HTML parsing ----------


class _AnchorExtractor(HTMLParser):
    """Walk an HTML document and collect each anchor's href + visible text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []  # list of dicts: {"href": str, "text": str}
        self._href_stack = []
        self._text_stack = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            attr_dict = dict(attrs)
            self._href_stack.append(attr_dict.get("href"))
            self._text_stack.append([])

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href_stack:
            href = self._href_stack.pop()
            parts = self._text_stack.pop()
            text = " ".join("".join(parts).split())
            if href is not None:
                self.anchors.append({"href": href, "text": text})

    def handle_data(self, data):
        if self._text_stack:
            self._text_stack[-1].append(data)


def _is_candidate_pdf_href(href):
    if not href:
        return False
    if _PDF_EXTENSION_PATTERN.search(href):
        return True
    if _DOC_CENTER_PATTERN.search(href):
        return True
    return False


# ---------- HTTP ----------


def fetch_page(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body, None
    except urllib.error.HTTPError as http_error:
        return http_error.code, None, f"HTTP {http_error.code}: {http_error.reason}"
    except urllib.error.URLError as url_error:
        return None, None, f"URL error: {url_error.reason}"
    except Exception as exc:  # pragma: no cover - defensive
        return None, None, f"Unexpected error: {exc!r}"


def head_url(url):
    head_request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(head_request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.headers.get("Content-Type", ""), None
    except urllib.error.HTTPError as head_error:
        if head_error.code in (403, 404, 405, 501):
            return _get_for_headers(url)
        return head_error.code, "", f"HTTP {head_error.code}: {head_error.reason}"
    except urllib.error.URLError as url_error:
        return None, "", f"URL error: {url_error.reason}"
    except Exception as exc:  # pragma: no cover - defensive
        return None, "", f"Unexpected error: {exc!r}"


def _get_for_headers(url):
    get_request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(get_request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.headers.get("Content-Type", ""), None
    except urllib.error.HTTPError as http_error:
        return http_error.code, "", f"HTTP {http_error.code}: {http_error.reason}"
    except urllib.error.URLError as url_error:
        return None, "", f"URL error: {url_error.reason}"
    except Exception as exc:  # pragma: no cover - defensive
        return None, "", f"Unexpected error: {exc!r}"


def _download_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read()


# ---------- Discovery & disambiguation ----------


def discover_pdf_anchors(body, base_url):
    if not body:
        return []
    parser = _AnchorExtractor()
    try:
        parser.feed(body)
    except Exception:
        pass

    seen_urls = set()
    out = []
    for anchor in parser.anchors:
        href = anchor["href"]
        if not _is_candidate_pdf_href(href):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        if absolute in seen_urls:
            continue
        seen_urls.add(absolute)
        out.append({"url": absolute, "text": anchor["text"]})
    return out


def _latest_year_in(candidate):
    haystack = candidate["text"] + " " + candidate["url"]
    years = _YEAR_PATTERN.findall(haystack)
    return max((int(year) for year in years), default=0)


def _contains_term(candidate, term):
    return term in (candidate["text"] + " " + candidate["url"]).lower()


def _prefer_english(candidates):
    """Apply language preference: if any candidate is explicitly marked
    English, return only those; else exclude any explicitly marked as a
    non-English language; else keep all (assume English when there is no
    language signal at all)."""
    if not candidates:
        return candidates

    explicit_english = [
        c
        for c in candidates
        if _ENGLISH_MARKER in (c["text"] + " " + c["url"]).lower()
    ]
    if explicit_english:
        return explicit_english

    not_explicit_other = [
        c
        for c in candidates
        if not any(_contains_term(c, term) for term in _NON_ENGLISH_LANGUAGE_TERMS)
    ]
    return not_explicit_other if not_explicit_other else candidates


def choose_schedule_anchor(candidates):
    if not candidates:
        return None

    # Step 0: language preference. Prefer English; exclude explicitly
    # non-English variants. Applied before the tiered rule so that all
    # downstream tiers operate on a language-filtered set.
    candidates = _prefer_english(candidates)

    text_schedule = [c for c in candidates if "schedule" in c["text"].lower()]
    if text_schedule:
        return sorted(text_schedule, key=_latest_year_in, reverse=True)[0]

    url_schedule = [c for c in candidates if "schedule" in c["url"].lower()]
    if url_schedule:
        return sorted(url_schedule, key=_latest_year_in, reverse=True)[0]

    not_excluded = [
        c
        for c in candidates
        if not any(_contains_term(c, term) for term in _NON_SCHEDULE_TERMS)
    ]
    pool = not_excluded if not_excluded else candidates
    return pool[0]


# ---------- Content verification (optional, requires pypdf) ----------


def verify_pdf_content(pdf_url):
    """Returns dict with keys: ran (bool), looks_like_schedule (bool|None),
    evidence (str), error (str|None). Skips with ran=False if pypdf missing."""
    if not PYPDF_AVAILABLE:
        return {
            "ran": False,
            "looks_like_schedule": None,
            "evidence": "pypdf not installed",
            "error": None,
        }

    try:
        pdf_bytes = _download_bytes(pdf_url)
    except Exception as exc:
        return {
            "ran": False,
            "looks_like_schedule": None,
            "evidence": "",
            "error": f"download failed: {exc!r}",
        }

    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text_parts = []
        for page in reader.pages[:2]:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
        text = "\n".join(text_parts)
    except Exception as exc:
        return {
            "ran": False,
            "looks_like_schedule": None,
            "evidence": "",
            "error": f"PDF parse failed: {exc!r}",
        }

    text_lower = text.lower()
    time_hits = len(_TIME_PATTERN.findall(text))
    day_hits = len(_DAY_PATTERN.findall(text))
    keyword_hits = [kw for kw in _SCHEDULE_KEYWORDS if kw in text_lower]

    looks_like = (
        (time_hits >= _MIN_TIME_HITS and day_hits >= _MIN_DAY_HITS)
        or len(keyword_hits) >= _MIN_KEYWORD_HITS_ALONE
    )

    evidence = (
        f"{time_hits} time patterns, {day_hits} day mentions, "
        f"{len(keyword_hits)} schedule keyword(s)"
        + (f" [{', '.join(keyword_hits)}]" if keyword_hits else "")
    )
    return {
        "ran": True,
        "looks_like_schedule": looks_like,
        "evidence": evidence,
        "error": None,
    }


# ---------- Suspicion check ----------


def compute_suspicion_reasons(result, chosen_url_counts):
    chosen_url = result.get("chosen_url")
    if not chosen_url:
        return []

    reasons = []
    chosen_text = result.get("chosen_text") or ""
    haystack = (chosen_text + " " + chosen_url).lower()

    excluded_terms_present = [t for t in _NON_SCHEDULE_TERMS if t in haystack]
    if excluded_terms_present:
        reasons.append(
            f"chosen URL/text contains excluded term(s): {', '.join(excluded_terms_present)}"
        )

    if not chosen_text.strip():
        reasons.append("chosen anchor has no visible text")

    shared_count = chosen_url_counts.get(chosen_url, 0)
    if shared_count > 1:
        reasons.append(
            f"chosen URL is shared with {shared_count - 1} other pool(s) — likely a global link, not a per-pool schedule"
        )

    return reasons


# ---------- Per-pool check ----------


def check_one(pool):
    page_status, body, page_error = fetch_page(pool["source_page_url"])
    candidates = discover_pdf_anchors(body, pool["source_page_url"])

    # HEAD every candidate, and content-verify every reachable PDF. Verifying
    # all candidates (rather than only the chosen one) lets us use content
    # verification as a FILTER before disambiguation rather than a post-check
    # after it — which is necessary to defeat the source-site mislabeling
    # case where a fake "Pool Schedule" link beats the real schedule on
    # anchor-text matching.
    candidate_results = []
    for candidate in candidates:
        status, content_type, error = head_url(candidate["url"])
        cv = {
            "ran": False,
            "looks_like_schedule": None,
            "evidence": "",
            "error": None,
        }
        if status == 200 and "pdf" in (content_type or "").lower():
            cv = verify_pdf_content(candidate["url"])
        candidate_results.append(
            {
                "url": candidate["url"],
                "text": candidate["text"],
                "status": status,
                "content_type": content_type,
                "error": error,
                "content_verification": cv,
                "is_chosen": False,
            }
        )

    # Disambiguation: when content verification ran for at least one candidate
    # (i.e., pypdf is installed and at least one HEAD succeeded), prefer the
    # subset of candidates that content-verified as schedule-like. If none
    # verified positive, fall back to all candidates so we still produce a
    # "best guess" choice that the strict pass criteria can then fail on.
    any_cv_ran = any(cr["content_verification"]["ran"] for cr in candidate_results)
    if any_cv_ran:
        schedule_like = [
            cr
            for cr in candidate_results
            if cr["content_verification"]["looks_like_schedule"] is True
        ]
        pool_for_choice = schedule_like if schedule_like else candidate_results
    else:
        pool_for_choice = candidate_results

    chosen = choose_schedule_anchor(
        [{"url": cr["url"], "text": cr["text"]} for cr in pool_for_choice]
    )

    for cr in candidate_results:
        cr["is_chosen"] = chosen is not None and cr["url"] == chosen["url"]

    chosen_result = next((cr for cr in candidate_results if cr["is_chosen"]), None)
    chosen_cv = (
        chosen_result["content_verification"]
        if chosen_result
        else {
            "ran": False,
            "looks_like_schedule": None,
            "evidence": "",
            "error": None,
        }
    )

    return {
        "id": pool["id"],
        "name": pool["name"],
        "seasonal": pool.get("seasonal", False),
        "maintenance": pool.get("maintenance", False),
        "notes": pool.get("notes", ""),
        "page_status": page_status,
        "page_error": page_error,
        "pdf_link_count": len(candidates),
        "chosen_url": chosen["url"] if chosen else None,
        "chosen_text": chosen["text"] if chosen else None,
        "pdf_status": chosen_result["status"] if chosen_result else None,
        "pdf_content_type": chosen_result["content_type"] if chosen_result else "",
        "pdf_error": chosen_result["error"] if chosen_result else None,
        "candidate_results": candidate_results,
        "content_verification": chosen_cv,
    }


def passed(result):
    """Strict pass: page+PDF reachable AND (content verification not run OR
    content verification says it looks like a schedule)."""
    base_pass = (
        result["page_status"] == 200
        and result["pdf_link_count"] > 0
        and result["pdf_status"] == 200
        and "pdf" in result["pdf_content_type"].lower()
    )
    if not base_pass:
        return False
    cv = result["content_verification"]
    if cv["ran"]:
        return bool(cv["looks_like_schedule"])
    return True  # pypdf not available → fall back to base reachability check


# ---------- Output ----------


def main(inventory_path):
    with open(inventory_path, "r", encoding="utf-8") as handle:
        pools = json.load(handle)

    if not PYPDF_AVAILABLE:
        print(
            "WARNING: pypdf not installed — content verification will be SKIPPED.\n"
            "         Install with `pip3 install pypdf` to enable the strongest check.\n"
        )

    results = [check_one(pool) for pool in pools]

    chosen_url_counts = Counter(
        r["chosen_url"] for r in results if r["chosen_url"]
    )
    for result in results:
        result["suspicion_reasons"] = compute_suspicion_reasons(
            result, chosen_url_counts
        )

    print(
        f"{'Pool':<28} {'Page':<6} {'PDFs':>5} {'PDF':<6} {'Content':<8} Notes"
    )
    print("-" * 110)
    any_required_failed = False
    for result in results:
        notes_parts = []
        if result["page_error"]:
            notes_parts.append(result["page_error"])
        if result["pdf_link_count"] == 0 and not result["page_error"]:
            notes_parts.append("no PDF link found on page")
        if result["pdf_error"]:
            notes_parts.append(f"pdf: {result['pdf_error']}")
        if (
            result["pdf_status"] is not None
            and result["pdf_status"] == 200
            and "pdf" not in result["pdf_content_type"].lower()
        ):
            notes_parts.append(
                f"pdf returned 200 but content-type was '{result['pdf_content_type']}'"
            )

        cv = result["content_verification"]
        if cv["ran"] and not cv["looks_like_schedule"]:
            notes_parts.append(
                f"content does NOT look like a schedule ({cv['evidence']})"
            )
        elif cv["error"]:
            notes_parts.append(f"content check error: {cv['error']}")

        if result["suspicion_reasons"]:
            notes_parts.append("SUSPICIOUS: " + "; ".join(result["suspicion_reasons"]))

        ok = passed(result)
        if not ok:
            if result["maintenance"]:
                notes_parts.append("pool under maintenance — failure is OK")
                if result["notes"]:
                    notes_parts.append(f'note: "{result["notes"]}"')
            elif result["seasonal"]:
                notes_parts.append("seasonal pool — outside-season failure is OK")
            else:
                notes_parts.append("FAIL")
                any_required_failed = True

        if ok and result["chosen_url"]:
            label = result["chosen_text"] or "(no anchor text)"
            notes_parts.append(f'-> "{label}" {result["chosen_url"]}')

        notes = "; ".join(notes_parts) if notes_parts else "ok"
        page_text = (
            str(result["page_status"]) if result["page_status"] is not None else "n/a"
        )
        pdf_text = (
            str(result["pdf_status"]) if result["pdf_status"] is not None else "n/a"
        )
        if cv["ran"]:
            content_text = "sched" if cv["looks_like_schedule"] else "NOT"
        elif cv["error"]:
            content_text = "err"
        else:
            content_text = "skip"
        print(
            f"{result['name']:<28} {page_text:<6} {result['pdf_link_count']:>5} {pdf_text:<6} {content_text:<8} {notes}"
        )

        # Print every candidate when the pool is suspicious or failed —
        # but only when the missing-schedule state is NOT already expected
        # (no point dumping diagnostics for a pool we know is offline).
        failure_is_expected = result["seasonal"] or result["maintenance"]
        should_print_candidates = (
            (not failure_is_expected)
            and ((not ok) or bool(result["suspicion_reasons"]))
            and result["candidate_results"]
        )
        if should_print_candidates:
            for candidate in result["candidate_results"]:
                marker = "*" if candidate["is_chosen"] else " "
                cand_status = (
                    str(candidate["status"])
                    if candidate["status"] is not None
                    else "n/a"
                )
                cand_label = candidate["text"] or "(no anchor text)"
                print(
                    f'      {marker} [{cand_status}]  "{cand_label}"  {candidate["url"]}'
                )

    print()
    if any_required_failed:
        print("M0 test FAILED. See notes above. (* marks the disambiguation choice.)")
        sys.exit(1)
    print("M0 test PASSED.")


if __name__ == "__main__":
    inventory_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INVENTORY_PATH
    main(inventory_arg)

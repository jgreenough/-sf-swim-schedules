# PRD: SF Pool Swim Schedule Search

> **Status:** Draft template — sections marked `TODO` need your input before this is build-ready.
> **Owner:** _your name_
> **Last updated:** 2026-05-03

---

## 1. Problem

San Francisco Recreation & Parks publishes a separate swim schedule (as a PDF) for each public pool. The PDFs are updated periodically and there is no cross-pool search. A family that just wants to know "where can we do a Family Swim on Saturday morning?" or "which pool has Lap Swim before work?" has to open ~9 PDFs and read them by hand every time the schedules rotate.

## 2. Goals (v1)

1. **Cross-pool search** for **Lap Swim** and **Family Swim** sessions across all SF Rec & Park public pools.
2. **Stay current automatically** — re-fetch the source PDFs on a schedule and re-parse them when they change.
3. **Multiple consumption surfaces:**
   - A filterable web page (day, time window, pool, swim type).
   - A subscribable calendar feed (`.ics`) so matching sessions appear in Google/Apple Calendar.
   - Change alerts when a pool publishes a new PDF or a slot you care about appears/disappears.

## 3. Non-goals (v1)

- Booking or reservations (SF Rec & Park handles this elsewhere).
- Schedule data for non-SF cities or private pools.
- Pool reviews, ratings, or social features.
- Other rec activities (classes, yoga, basketball, etc.).
- Native mobile apps. Mobile-friendly web is enough.

## 4. Users

| Phase | Audience | Notes |
|---|---|---|
| v1 | Just my household | No accounts, no signup. Tool runs and I use it. |
| v2 | Friends/family I share with | Same tool, just sharing the URL. Still no accounts. |
| v3 (optional) | Any SF swimmer (public) | Adds requirements around uptime, accessibility, and a privacy notice. |

### Primary user stories

- *As a parent,* I want to see every **Family Swim** slot this Saturday across all pools, sorted by start time, so I can pick one that fits our day.
- *As a lap swimmer,* I want a calendar that shows every **Lap Swim** session Mon–Fri 6–8am, so I can decide where to go without re-checking PDFs.
- *As a regular user,* I want to be notified when a pool I care about changes its schedule, so I'm not surprised at the door.

## 5. Functional requirements

### 5.0 Pools in scope

V1 ingests the following SF Rec & Park public pools:

1. Balboa
2. Coffman
3. Garfield
4. Hamilton
5. Martin Luther King Jr. (MLK)
6. Mission (note: outdoor / seasonal — may not publish a year-round PDF)
7. North Beach
8. Rossi
9. Sava

Adding or removing a pool is a one-line change to the pool inventory plus supplying a `source_page_url` (see §13). Pools that are temporarily not publishing a schedule should remain in the inventory but be flagged so the scheduler doesn't treat the absence as a failure. Two flag values are recognized:

- `seasonal: true` — pool is seasonal (e.g., outdoor, summer-only). Missing schedule outside the swim season is expected.
- `maintenance: true` — pool is temporarily closed for maintenance. Missing schedule for the duration of the closure is expected. Clear the flag when the pool reopens and a schedule reappears.

Both flags are independent and may coexist with an optional `notes` string for human context (date the flag was set, expected reopen window, etc.). Pools without either flag are treated as `active` and a missing schedule is a real failure.

The pool inventory lives at **`pools.json`** in the repo root. Each entry has `id`, `name`, `source_page_url`, and optional `seasonal`, `maintenance`, `address`, `pdf_page_filter`, and `notes` fields.

**Multi-pool facilities** (added in M2): some facilities publish a single PDF that covers more than one physical pool. North Beach has separate WARM and COOL pools on pages 1 and 2 of one schedule PDF. These are modeled as **two pool entries** sharing the same `source_page_url` and `address`, distinguished by `pdf_page_filter` — a string the parser searches for in each page's extracted text to decide whether that page belongs to this entry. For North Beach the filters are `86°F` (warm pool's temperature line) and `76°F` (cool pool's), since those text strings reliably distinguish the pages while the colored "WARM POOL"/"COOL POOL" headers don't survive PDF text extraction.

### 5.1 Ingestion

The scheduler runs every pool through the same per-poll sequence (default cadence: daily). Each step has a defined failure outcome that gets logged so the operator can see at a glance whether the pipeline is healthy.

**Per-poll sequence (per pool):**

1. **Fetch the source page.** GET the pool's `source_page_url` from `pools.json`. On non-200 or network failure, log `page_unreachable` and stop. The `source_page_url` is the *page* on the SF Rec & Park site (e.g., `…/Facility/Details/Balboa-Pool-212`), **not** a direct PDF URL — PDF filenames change between schedule revisions, so the page is the only stable handle.
2. **Discover the schedule PDF link on the page.** Parse the page HTML with an **anchor-element extractor** (stdlib `html.parser`) that captures every `<a>` element's `href` **and** its visible inner text. Anchor text is essential for disambiguation when the URL alone is opaque (see below). Keep only anchors whose `href` is a PDF candidate: either ends in `.pdf` (general fallback) or matches `/DocumentCenter/View/\d+` (the SF Rec & Park CivicPlus pattern). The CivicPlus path has an **optional** trailing `/{slug}` and optional query string; both `https://sfrecpark.org/DocumentCenter/View/28495` (bare ID) and `https://sfrecpark.org/DocumentCenter/View/28547/2026-Balboa-Pool-Spring-Schedule?bidId=` (slugged) appear in real pages and resolve to the same document on the server. Resolve relative URLs against the page URL. If multiple candidates are present, select the schedule using the disambiguation rule below. On zero candidates found, log `no_pdf_link_found` and stop.
3. **Download the PDF.** GET the resolved PDF URL. On non-200 or network failure, log `fetch_failed` and stop.
4. **Extract text and hash it.** Run text extraction over the downloaded PDF and SHA-256 the extracted text. Compare to the previously stored hash for this pool.
5. **Short-circuit on no change.** If the text hash matches the prior hash, log `unchanged` and stop. The structured parsing, `.ics` regeneration, and RSS emission steps below are the expensive parts and they only need to run when something real changed.
6. **Parse, regenerate, archive.** On a real change: parse into structured sessions (per §5.2), regenerate the two `.ics` feeds (per §5.4), emit a change event for the RSS alerts feed, archive the new PDF (committed to the repo, named `{pool_id}-{YYYYMMDD}.pdf`), update the stored hash, and log `changed`. On a parsing failure, log `parse_failed` and keep the prior published outputs untouched (better stale than broken).

**PDF link disambiguation (filters then tiered heuristic).** SF Rec & Park pages typically host 3–4 PDFs (schedule, deck rules, flyer, registration form, language variants, etc.). Disambiguation applies two pre-filters in order, then a four-tier preference rule on whatever survives:

**Pre-filter A — content verification (the strongest signal).** Download every reachable PDF candidate, extract first-page text, and classify it as schedule-like by counting time patterns (e.g., `6:00 AM`), day-of-week mentions (`Mon`, `Tue`, ...), and explicit swim-type keywords (`Lap Swim`, `Family Swim`, `Recreation Swim`, ...). Keep only candidates that look like schedules. This is the only filter that can defeat the source-site mislabeling case where an editor labels a deck-rules link as `"pool schedule (PDF)"` — anchor text alone is fooled, content cannot be. If no candidate verifies as schedule-like (or pypdf is not installed), fall through to all candidates and let the strict pass criteria fail visibly.

**Pre-filter B — English language preference.** When the source publishes multi-language variants (the SF Rec & Park Sava page links deck rules in English, Chinese, and Spanish), prefer the English document. Specifically: if any candidate has `english` in its text or URL, keep only those; else exclude any candidate explicitly marked as a known non-English language (`chinese`, `spanish`/`español`, `中文`, `tagalog`, `vietnamese`, `russian`, etc.); else keep all (assume English when there is no language signal at all).

**Tiered preference rule** (applied to whatever survives both filters):

1. **Anchor text contains `schedule`** (case-insensitive). Tiebreak by highest 4-digit year present in the anchor text or URL.
2. **URL slug contains `schedule`.** Same tiebreak.
3. **Exclude obvious non-schedule documents.** From the remaining candidates, drop any whose anchor text or URL contains `rules`, `flyer`, `registration`, `policy`, `application`, `waiver`, `permit`, or `rental`.
4. **First remaining candidate** in page order. Last-resort fallback when no other signal is available.

The chosen URL, the chosen anchor text, **and** the content-verification result must all be logged on every successful poll for at-a-glance auditing.

**Change detection uses text hashes, not byte hashes.** PDFs embed non-content metadata (creation timestamps, generator strings) that change on every re-export, so a raw-byte hash gives false positives. Hashing extracted text is robust to those re-exports while still catching real schedule edits.

**Outcome log.** Every poll terminates in exactly one of `unchanged`, `changed`, `page_unreachable`, `no_pdf_link_found`, `fetch_failed`, `parse_failed`. `page_unreachable` and `no_pdf_link_found` mean the SF Rec & Park page itself moved or restructured, which means the `source_page_url` in `pools.json` needs human review.

### 5.2 Parsing
- Convert each PDF into structured records: `pool, effective_start, effective_end, day_of_week (or date), start_time, end_time, swim_type, notes`.
- Normalize swim-type labels across pools (e.g., "Family Rec Swim" vs "Family Swim").
- v1 must reliably parse **Lap Swim** and **Family Swim**. Other types may be parsed best-effort and stored, but are not surfaced in v1 UI.

### 5.3 Search / web view
- Filters: pool (multi-select), day(s) of week, time window, swim type.
- Default view: "this week" across all pools, both swim types.
- Shareable URL that encodes the filter state (so a permalink like "Lap Swim, MWF, 6–8am" can be bookmarked).

### 5.4 Calendar feeds (`.ics`)

V1 ships exactly **two** static `.ics` feeds:

1. **Lap Swim — all SF pools.** Every Lap Swim session at every in-scope pool.
2. **Family Swim — all SF pools.** Every Family Swim session at every in-scope pool.

Per-pool feeds and arbitrary filter combinations are explicitly **out of scope for v1** — they add code paths and UI without enough additional value for the household use case.

Implementation notes:
- Both feeds are regenerated as static files by the same scheduled job that fetches and parses the PDFs.
- URLs are not secret but use unguessable slugs — see §9.
- Subscribable in Google Calendar and Apple Calendar.
- **Stable event UIDs are required.** Each session's `UID` must be a deterministic hash of `(pool + day_of_week + start_time + swim_type)` so that regenerating the feed does not create duplicate events in subscribers' calendars. Schedule transitions are handled by setting `RRULE ... UNTIL` to the schedule's effective end date, so old recurrences expire cleanly without manual cleanup.

### 5.5 Change alerts
- No-PII delivery only (see §9): RSS feed and/or web push.
- Alert types:
  - "Pool X published a new schedule effective DATE."
  - (Stretch) "A slot matching your saved filter was added/removed."

## 6. Non-functional requirements

| Area | Requirement |
|---|---|
| Refresh cadence | Re-fetch source PDFs at least once per day. |
| Alert latency | Within 24 hours of a source PDF change. |
| Hosting cost | Free tier now; willing to spend <$10/month later if it unlocks meaningful reliability or features. |
| Accessibility (v3 only) | WCAG 2.1 AA basics — keyboard navigation, color contrast, semantic HTML. |
| Reliability | OK to be down briefly. Stale data is worse than downtime — show "last updated" timestamps everywhere. |
| Maintainability | Owner has minimal networking/deployment knowledge. Prefer managed services and static-file output over self-hosted servers. |

## 7. Data model (sketch)

```
Pool
  id, name, address, lat, lng, source_page_url

Schedule
  id, pool_id, effective_start, effective_end,
  source_pdf_url, source_pdf_hash, fetched_at

Session
  id, schedule_id,
  day_of_week | specific_date,
  start_time, end_time,
  swim_type (normalized), raw_label (verbatim from PDF),
  notes
```

## 8. Suggested architecture (non-binding — design choice in build phase)

A "boring" stack that fits the cost / skill posture:

- **Scraper + parser:** Python script (`pdfplumber` or `unstructured` for PDFs) run on **GitHub Actions cron** (free).
- **Storage:** SQLite file committed to the same Git repo, or a JSON snapshot per pool. No database server to manage.
- **Web frontend:** Static site (Astro, Eleventy, or plain HTML) reading the JSON, hosted on **Cloudflare Pages** or **GitHub Pages** (free).
- **Calendar feeds:** `.ics` files generated by the same Action and served as static assets.
- **Alerts:** Static **RSS feed** generated by the same Action; optionally a web-push integration later.

Trade-off: this entire stack avoids running a server, avoids a database, and avoids collecting any user data. The scraper job is the only "moving part."

## 9. Privacy & infosec posture

Per project guidance, default to collecting **no PII**:

- No accounts, no email collection, no phone numbers in v1 or v2.
- Alerts ship via RSS and/or web push — both work without storing identifiers.
- `.ics` feeds at random unguessable URLs are still effectively public — **never embed personal info in the feed name or path**.
- If a "near me" filter is added, store the home location **client-side only** (in the browser); never send it to a server or include it in shareable links.
- If hosted on a public Git repo (e.g., GitHub Pages), the scraped PDFs and parsed data become publicly visible. That's fine for SF Rec & Park's public schedules, but it's worth being explicit.
- v3 (public launch) requires: a short privacy notice, a `robots.txt`, and a contact email that is **not** the owner's personal address (suggest a forwarding alias).

## 10. Risks & open questions

- **PDF format drift.** Different pools format their PDFs differently, and a single pool may change layout between updates. Parser must degrade gracefully and log unparseable rows for review.
- **"Same session" identity across versions.** To say "this slot was removed," we need a stable identity for a session across schedule revisions. Day-of-week + time + swim-type is a reasonable v1 key. (The same identity rule also drives stable `.ics` UIDs — see §5.4.)
- **Holidays / one-off closures.** PDFs sometimes note holiday changes inline. v1 may simply surface the raw `notes` field; a richer model can come later.
- **Time zone / DST.** All times are America/Los_Angeles. Make this explicit everywhere; don't trust the scraper environment's default.
- **Source ToS / rate limits.** A daily fetch of ~9 small PDFs is well within reasonable use; if SF Rec & Park asks us to stop, we stop.
- **CMS-wrapped PDF URLs.** SF Rec & Park serves PDFs through a `/DocumentCenter/View/{numeric_id}/{slug}` endpoint (this is the standard CivicPlus Document Center pattern, common to many municipal websites). The numeric ID changes when documents are re-uploaded — which is exactly when we care about a change — so the page-scrape strategy in §5.1 is the only viable approach; there is no stable direct-PDF URL to bookmark. The matcher must explicitly recognize this URL shape, not just `.pdf` extensions.
- **Open question:** if v3 goes public, does the owner want analytics? Recommend a **no-cookie, no-PII** option (e.g., Plausible, Cloudflare Web Analytics) or none at all.

## 11. Milestones

| # | Milestone | Definition of done |
|---|---|---|
| M0 | Data inventory | All in-scope pool *page* URLs (per §5.0) collected and committed to the pool inventory (`pools.json` — done). One sample PDF saved per pool. **Test condition: `check_pool_pages.py` walks the per-poll discovery sequence end-to-end for every pool — (1) GET the `source_page_url` and assert HTTP 200, (2) parse the HTML, extract anchors with their visible text, and assert at least one PDF link is discoverable, (3) follow the chosen PDF link and assert it returns HTTP 200 with a PDF content-type, (4) **content-verify the chosen PDF** by extracting first-page text and confirming it looks like a schedule (multiple time patterns + day-of-week mentions, or explicit `Lap Swim`/`Family Swim` labels). Step 4 requires `pip3 install pypdf` (one-time, optional but recommended); when pypdf is missing the step is skipped with a warning rather than failing the run. The script also independently flags **suspicious choices** — chosen URLs whose text/URL contains an excluded term, anchors with no visible text, or URLs shared with another pool's choice — and prints the full candidate list for any suspicious or failing pool, so disambiguation problems are visible at a glance. Required pools must all pass; pools flagged `seasonal: true` may pass with no PDF outside their swim season. The check runs on every change to `pools.json` (e.g., as a CI step) and is what marks M0 done.** |
| M1 | One pool, end-to-end | **DONE 2026-05-03.** Pilot: Balboa. Parser `parse_schedule.py` produces all 29 sessions (Tue 7, Wed 5, Thu 6, Fri 6, Sat 5) with correct times, correct normalized `swim_type`, and verbatim `raw_swim_type`. `diff_sessions.py balboa` exits 0 against the hand-transcribed `parsed/balboa_expected.json` ground-truth fixture. Acceptance approach was promoted from option (a) eyeball to option (c) structural assertions once the owner supplied per-day session counts; that gave us a precise diff target instead of a "looks about right" judgment. |
| M2 | All pools (parser generalization) | **DONE 2026-05-03.** Parser generalizes across all 9 active pool entries (Sava skipped — `maintenance: true`) with **zero unknown labels** and zero suspect time ranges. Total: 241 sessions parsed cleanly. North Beach split into TWO pool entries (`north_beach_warm`, `north_beach_cool`) because the facility has separate warm and cool pools with their own schedules on a 2-page PDF; the inventory schema gained an optional `pdf_page_filter` field for multi-pool facilities, and the parser routes each entry to its own page via temperature-string matching. Schema also gained an optional `address` field (populated for North Beach so far). Several parser improvements landed during M2: smart time-period inference for ranges that cross noon, label-fragment filtering (`( )`, date strings, low-alpha-count text), notice-cell detection (cells with "closed" / "training" text are no longer emitted as sessions), and broader swim-type rules (water polo, swim meet, safety swim, "self guided exercise" with the observed `EXECISE` typo). Balboa M1 diff still exits 0, confirming no regression. |
| M3 | Scheduler (daily refresh) | **DONE 2026-05-03.** Project pushed to its GitHub repo; workflow `.github/workflows/refresh-schedules.yml` runs `parse_all.py` daily at 13:37 UTC and commits any changed `parsed/*.json` / `pdfs/*.pdf` back to the default branch as github-actions[bot]. Manual trial run via the Actions tab completed green end-to-end. From this point forward the data refreshes itself; an operator only needs to intervene if a workflow run fails (which surfaces as a GitHub email notification). |
| M4 | Web view (filterable schedule browser) | Per-owner direction during M4 kickoff: M4 was rescoped to **web-view only**; the `.ics` feed generation was pushed to M5. Hosting: GitHub Pages on the same repo. **Implementation in progress 2026-05-03.** New file `index.html` at the repo root: plain HTML + vanilla JS, no build step or framework. Loads `pools.json` and every active pool's `parsed/*.json` in parallel via `fetch()`, builds filter chips for swim type / day / pool, supports a start-time range, syncs filter state to the URL hash so links are shareable, and renders results grouped by day-of-week with color-coded swim-type chips. Defaults to Lap Swim + Family Swim selected (per §5.3). Mobile-friendly via media queries. Supports both light and dark mode via `prefers-color-scheme`. M4 done when the page is deployed and reachable at the GitHub Pages URL. |
| M5 | Change detection + alerts | RSS feed of schedule changes published; visible "last updated" stamps. (Bumped from M4 per the M3 split.) |
| M6 (optional) | Public launch | Privacy notice, contact alias, accessibility pass, public URL announced. (Bumped from M5.) |

## 12. Success metrics

- **v1 (personal):** I can find a workable session in **<30 seconds** any given week without opening a single PDF.
- **v2 (shared):** at least one other household uses the link weekly.
- **v3 (public, if launched):** unique pools covered = 100%; weekly returning visitors trend up; zero PII collected.

---

## 13. Inputs needed from you (TODO before build)

These are the gaps I couldn't fill from our discussion:

- [x] **Per-pool source *page* URLs.** The URL of the **webpage** on the SF Rec & Park site that lists/links the current schedule PDF for each pool — **not** a direct link to a PDF file (PDF filenames change between updates, so the page URL is the only stable handle). Supplied 2026-05-03 and committed to `pools.json`; Mission is flagged `seasonal: true`. The scraper resolves the current PDF link from the page on every poll. M0 reachability test (`check_pool_pages.py`) is **pending execution** — see §14.
- [ ] **Default "useful" filter.** What time windows and days should the homepage default to (e.g., "weekday mornings + weekend mornings")?
- [ ] **Domain name.** Do you want a custom domain for v1, or is a `*.pages.dev` / `*.github.io` URL fine to start?
- [ ] **GitHub account / org** to host the repo and the scheduled job under.
- [ ] **"Near me" filter.** In scope for v1, or defer to v2/v3?

## 14. Pending actions

| # | Action | Status | Notes |
|---|---|---|---|
| P1 | Run `check_pool_pages.py` against `pools.json` | **Passed 2026-05-03 (with one caveat)** | All 9 pool pages return HTTP 200 and a reachable PDF with `application/pdf` content-type, satisfying the strict M0 acceptance criteria. Caveat: Sava's chosen URL is `/View/19018/Facility--deck-rules-in-English` — the deck rules document, **not** the swim schedule. Disambiguation picked wrong because none of Sava's candidates have `schedule` in the slug, so the heuristic fell back to "first candidate." Balboa's chosen ID `28547` matches the schedule URL supplied earlier, so Balboa is verifiably correct; the other 7 chosen URLs are *probably* schedules (IDs cluster in the same recently-uploaded range) but unverified without opening each PDF. See P2. |
| P2 | Fix disambiguation so Sava picks the schedule, not deck rules | **Partially resolved 2026-05-03 — see P3** | Anchor-text disambiguation (option b) implemented in `check_pool_pages.py`. Re-run results: **5 pools verifiably correct** (Balboa, Garfield, Hamilton, Mission, Rossi — anchor text contains pool name + year + "Schedule" + date range). **1 pool probably correct** (North Beach — anchor text shows a clear date-ranged schedule even though the literal word "schedule" is missing). **3 pools wrong**, all picking the same `/View/19018/Facility--deck-rules-in-English` document, which divides into two distinct root causes captured under P3 and P4. |
| P3 | Detect source-site link mislabeling (Coffman, MLK) | **Resolved 2026-05-03** | Restructured content verification from a post-check on the chosen URL into a pre-filter on all candidates. Coffman now correctly picks `/View/28927` ("Coffman Pool_Spring26_Apr21_June6") and MLK picks `/View/28771` ("MLK_Spring26_Apr07_Jun06_FINAL v2.0"). Both pass content verification with `sched` and no SUSPICIOUS flag. The mislabeled `"pool schedule (PDF)"` link still exists on the source pages but is now eliminated from consideration before disambiguation runs. |
| P4 | Diagnose Sava | **Resolved 2026-05-03 — confirmed data issue, not script issue** | Diagnostic confirms Sava's facility page genuinely links **no swim schedule PDF**. Its only 3 PDF candidates are the global deck-rules document in English, Chinese, and Spanish (`/View/19018`, `/View/19020`, `/View/19019` — the same global document linked from Coffman and MLK pages, where it gets correctly bypassed). The script behaves correctly: picks the English variant (per Step 0 language preference), content-verifies it as not-a-schedule, marks suspicious, fails. The remaining question is a product-side decision about Sava — see P5. |
| P5 | Decide what to do about Sava (no schedule PDF on its page) | **Resolved 2026-05-03** | Confirmed Sava is currently closed for maintenance, which is why no schedule is published. Modeled this as a new `maintenance: true` flag in the pool inventory schema (parallel to the existing `seasonal: true`) plus an optional `notes` field for context. `pools.json` updated accordingly; `check_pool_pages.py` updated to treat maintenance pools the same as seasonal ones — failure is allowed, the test still passes overall, and candidate dumps are suppressed since the absence is expected. The pool stays in the inventory so it'll re-validate automatically when Sava reopens; an operator clears the `maintenance` flag at that point. |

## 15. Appendix: change log

| Date | Change | Author |
|---|---|---|
| 2026-05-03 | Initial template drafted from kickoff Q&A | _you_ |
| 2026-05-03 | Scoped `.ics` to two feeds only (Lap Swim, Family Swim — both across all pools); per-pool feeds and arbitrary filters out of scope for v1 | _you_ |
| 2026-05-03 | Made skip-on-no-change explicit in §5.1: hash extracted text (not raw PDF bytes) and short-circuit parsing + regeneration when unchanged; archive every fetched PDF for audit and re-parse | _you_ |
| 2026-05-03 | Clarified §13: the per-pool URLs to be supplied are *webpage* URLs that list the PDFs, not direct PDF URLs (PDF filenames change between updates) | _you_ |
| 2026-05-03 | Promoted the pool list to a definitive scope subsection (§5.0) including Balboa; flagged Mission as seasonal; added a CI test condition under M0 that fetches every `source_page_url` and asserts HTTP 200 + a discoverable PDF link; added `page_unreachable` and `no_pdf_link_found` to the per-poll outcome log in §5.1 | _you_ |
| 2026-05-03 | URLs supplied for all 9 pools and committed to `pools.json`; M0 test condition implemented as `check_pool_pages.py` (stdlib only, no dependencies); marked §13 URL bullet done; added §14 Pending actions to track the still-unrun M0 test (blocked by network allowlist in this session) | _you_ |
| 2026-05-03 | §5.1 rewritten as an explicit per-poll sequence: GET page → parse HTML for PDF link (with disambiguation rule TBD against a real sample) → GET PDF → text-hash → short-circuit-or-parse → archive. Added a TODO for disambiguation under M1. M0 test condition (§11) extended to also follow the discovered PDF link and assert HTTP 200 + a PDF content-type; `check_pool_pages.py` updated to match (resolves relative URLs, HEADs the PDF, falls back to GET if HEAD is rejected) | _you_ |
| 2026-05-03 | Sample PDF URL (`/DocumentCenter/View/28547/2026-Balboa-Pool-Spring-Schedule?bidId=`) supplied. Discovered SF Rec & Park serves PDFs through CivicPlus Document Center — URLs do **not** end in `.pdf`. §5.1 step 2 updated to require matching `/DocumentCenter/View/\d+/` paths in addition to the `.pdf` extension. Disambiguation rule promoted from TODO to a concrete heuristic: prefer slugs containing "schedule," tiebreak by highest 4-digit year. §10 risks adds a CMS-pattern note. `check_pool_pages.py` regex and disambiguation function updated to match | _you_ |
| 2026-05-03 | Second sample (`/DocumentCenter/View/28495`) revealed the slug is **optional** — pool pages link both the bare-ID and slugged forms. Regex relaxed in §5.1 and in `check_pool_pages.py` to make the slug optional. Disambiguation behavior documented for the slug-absent case: fall back to first candidate. Added an M1 open question to upgrade discovery to capture anchor text for richer disambiguation when slugs are missing | _you_ |
| 2026-05-03 | First end-to-end run found 4 PDF candidates per page (3 for Sava) but the chosen URL returned 404 across the board. Two probable causes (CivicPlus servers misbehaving on HEAD; or disambiguation picking the wrong link when slugs are absent). Updated `check_pool_pages.py` to (a) add 404 to the HEAD-fallback list so a HEAD-404 retries with GET, and (b) HEAD every candidate and print all of them with their statuses on failure, marking the disambiguation choice with `*`. This makes it diagnosable in one run which of the two causes is happening | _you_ |
| 2026-05-03 | M0 test PASSED on the strict acceptance criteria — all 9 pages reachable, all chosen PDFs reachable with PDF content-type. Confirmed root cause was CivicPlus's broken HEAD handler (the HEAD→GET fallback fixed it). Sava's chosen URL is the deck rules document, not the schedule — disambiguation gap surfaced as expected. §14 P1 marked passed-with-caveat; new P2 added to track the disambiguation fix | _you_ |
| 2026-05-03 | Anchor-text disambiguation (option b) implemented. `check_pool_pages.py` switched from regex-only to `html.parser` for anchor extraction; each anchor's visible text is captured alongside its href. Disambiguation in `choose_schedule_anchor` is now a four-tier rule (anchor text → URL slug → exclusion list → page order) with year-based tiebreaks. §5.1 step 2 and the disambiguation paragraph rewritten to document this; the prior "open question for M1" framing is removed. Output now shows chosen anchor text on success lines for at-a-glance auditing | _you_ |
| 2026-05-03 | Anchor-text re-run resolved 6 of 9 pools cleanly. Surfaced two distinct new failure modes: (1) source-site link mislabeling — Coffman & MLK have anchor text "pool schedule (PDF)" pointing at deck rules; only content verification can catch this (now P3). (2) Sava's chosen URL is suspicious (empty anchor text + excluded URL term) suggesting all candidates on its page may be non-schedule documents (now P4). §14 reorganized: P2 partially resolved, P3 and P4 added | _you_ |
| 2026-05-03 | Both P3 and P4 implemented in `check_pool_pages.py`. (P3) Content verification: optional `pypdf` import, first-page text extraction, schedule classifier counting time patterns, day-of-week mentions, and swim-type keywords with conservative thresholds; pass criteria extended to require schedule-like content when verification runs; gracefully skips with a warning when pypdf isn't installed. (P4) Suspicious-choice diagnostic: per-pool flag triggered by excluded term, empty anchor text, or shared chosen URL across pools; full candidate list printed for any suspicious or failing pool. M0 test condition in §11 extended accordingly | _you_ |
| 2026-05-03 | Diagnostic re-run revealed the actual root cause for Coffman/MLK: their REAL schedules are candidates #1 (`/View/28927` and `/View/28771`) but Tier 1 fired on a mislabeled `"pool schedule (PDF)"` link. Sava's page genuinely links no schedule PDF (3 deck-rules variants only). Restructured `check_pool_pages.py` to content-verify ALL candidates (not just chosen) and use that as a pre-filter BEFORE the tiered disambiguation rule — the deck-rules link is now eliminated from consideration before Tier 1 runs. Per follow-up request, also added an English-language pre-filter (Step 0): prefer candidates marked English, exclude candidates explicitly marked non-English (Chinese/Spanish/etc.), default to all when no language signal exists. §5.1 disambiguation paragraph rewritten as "filters then tiered heuristic" | _you_ |
| 2026-05-03 | Re-run confirmed: 8 of 9 pools pass with content verification AND English preference both active. Coffman, MLK, and the previously-unverified six all return `sched` from content verification. Sava is the only fail, and its diagnostic output proves the failure is a real-world data issue (Sava's page links zero schedule PDFs) — not a script bug. P3 marked resolved; P4 reframed as resolved-but-revealed-product-question; new P5 added for the Sava decision (investigate / mark seasonal / drop) | _you_ |
| 2026-05-03 | Confirmed Sava is closed for maintenance. Added a `maintenance: true` flag to the pool inventory schema (parallel to `seasonal: true`) plus an optional `notes` field for human context. §5.0 documents both flags. `pools.json` Sava entry updated. `check_pool_pages.py` updated to treat maintenance like seasonal: failure tolerated, test still passes overall, candidate diagnostics suppressed since the absence is expected. P5 resolved — Sava stays in the inventory and will auto-revalidate when the maintenance flag is cleared | _you_ |
| 2026-05-03 | M0 marked done; M1 started. Pilot pool: Balboa (per kickoff Q&A). Acceptance approach: eyeball validation. PDF library: pdfplumber. New file `parse_schedule.py` implements: (a) re-uses M0 discovery to find the chosen PDF, (b) downloads to `pdfs/{pool_id}.pdf` for local archive, (c) extracts via pdfplumber and dumps raw text + tables to `parsed/{pool_id}_raw.json` for debugging the layout assumptions, (d) runs a best-effort day-grid table parser, (e) writes `parsed/{pool_id}.json` with normalized session records (incl. verbatim `raw_swim_type` per §5.2). M1 milestone in §11 updated with full scope | _you_ |
| 2026-05-03 | First M1 run on Balboa returned 0 sessions despite finding 23 tables. Root cause (visible from reading the actual PDF): each session is rendered as TWO visual sub-boxes (label sub-box stacked above time-range sub-box) inside a day column, which fragments pdfplumber's table detection into 22 micro-tables plus a confused main table. Also discovered the pool is closed Sunday/Monday (only Tue–Sat columns present) and the effective-date range is in numeric form (`3/17/2026-6/06/2026`) which my textual-date regex didn't match. Rewrote `parse_schedule.py` around a word-coordinate clustering strategy: extract every word with (x,y), find day-header words to define column boundaries, bucket body words by column, group consecutive same-column words into lines (small y-tolerance) then into cells (larger y-gap separates cells), pair label cells with time-range cells. Added a numeric date-range pattern. Output now reports session counts by day-of-week and by swim type for at-a-glance auditing | _you_ |
| 2026-05-03 | Second M1 run on Balboa returned 13 sessions vs ~30–40 expected. Diagnosed two structural bugs: (1) rightmost column extended to +∞ so the entire legend panel right of Saturday got bucketed into the Saturday column, producing one giant garbage session with the legend text as label; (2) cell-clustering threshold was unreliable because label-to-time gaps and time-to-next-label gaps are similar in magnitude, so cells split or merged unpredictably (e.g., "PARENT/CHILD INTRO" got split off from "(shallow pool)" and the time then paired with only the second sub-line). Replaced cell-clustering with **scan-upward-from-each-time-line** label collection (walk up until you hit another time line or a too-large gap), and bounded each column's width to the average inter-day spacing so the rightmost column no longer absorbs the legend area. Also noted but did not auto-correct an upstream typo in the Wednesday Balboa schedule (`12:30 am - 3:00 pm` should be `12:30 pm - 3:00 pm`) — the parser faithfully reflects what the PDF says | _you_ |
| 2026-05-03 | Third M1 run regressed to 10 sessions (with 3 unknown), confirming the word-clustering approach has a structural ceiling on this layout that no threshold tuning will lift. Per user direction ("try other pdf parser"), swapped pdfplumber out for **PyMuPDF** (`fitz`) and switched from word-level clustering to **block-based parsing**: PyMuPDF returns text as visual blocks that often correspond 1:1 to cells in a structured layout, eliminating the need for fragile gap-clustering heuristics. New pipeline: extract blocks per page → identify day-header blocks → bucket body blocks into columns by x-center → in each column, walk blocks sorted by y looking for time-range blocks, with the immediately-preceding block as the label. Raw extraction now also dumps the block list (with bounding boxes) to `parsed/{pool_id}_raw.json` for debugging. Adds one new dependency: `pip3 install pymupdf` | _you_ |
| 2026-05-03 | Owner supplied per-day session counts (Tue 7, Wed 5, Thu 6, Fri 6, Sat 5 — total 29) as the M1 ground truth. Promoted the M1 acceptance approach from option (a) "eyeball validation" to option (c) "structural assertions" since we now have authoritative counts. Hand-transcribed the full 29-session ground truth into `parsed/balboa_expected.json` by reading the source PDF directly — every session has day, start/end (24h), normalized swim_type, verbatim raw_swim_type, and notes. Notable corner cases captured in the fixture: the Wednesday `12:30 am - 3:00 pm` typo (faithfully transcribed as 00:30-15:00 with a per-session note explaining the suspected source error), the Tuesday compound REC/FAMILY+LAP cell, and the Saturday concurrent LAP+LEARN-TO-SWIM at 9:00 (different pool sections). Added `diff_sessions.py` that compares parsed vs expected on (day, start, end) and reports missing / extra / type-mismatched sessions with exit code 0 = exact match. Workflow is now: run parser → run diff → iterate until diff exits 0 | _you_ |
| 2026-05-03 | First PyMuPDF block-based parser run on Balboa returned 0 sessions despite finding 45 blocks. Diagnosed from the raw extraction: **the source PDF is internally rotated** — drawn landscape but stored as portrait — and PyMuPDF reports unrotated coordinates. The day-header row (TUESDAY…SATURDAY) appears as a **single block** at constant x with day names spread vertically across y. Every previous parser (pdfplumber word-clustering AND PyMuPDF blocks) silently assumed day-axis=X and was operating on the wrong axis the whole time. Rewrote `parse_via_pymupdf` around `get_text("words")` (not blocks, so each day name is its own positioned token) with **auto-detected page orientation**: find day-header words, measure their x-spread vs y-spread, and use the higher-spread axis as the day axis. All downstream logic (column bucketing, label-scan-back-along-time-axis) is now axis-agnostic. Raw extraction also records the detected day_axis so any future orientation surprises are visible at a glance | _you_ |
| 2026-05-03 | PyMuPDF orientation auto-detection still produced 2 garbled sessions because PyMuPDF gives unrotated coordinates regardless. Discovered via direct sandbox inspection that **pdfplumber respects the PDF's declared 90° rotation and gives correct visual coordinates out of the box** — `page.rotation == 90` is honored by `extract_words()`. Reverted to pdfplumber and rewrote the parser with three corrections from earlier failures: (1) **header-row clustering** — find all day-name words, cluster by their top-y, pick the cluster with the most distinct days, which rejects body matches like the word "Thursday" inside "Closed every 3rd Thursday of the month for training"; (2) **Voronoi column boundaries** with the leftmost/rightmost outer edges capped at one half-average-gap from the outermost day centers, so the rightmost column doesn't extend to the page edge and absorb the right-hand legend panel; (3) `MAX_LABEL_GAP_PT` raised from 22 → 35 so labels separated from their times by visual cell padding still pair correctly (Wednesday's LEARN TO SWIM was 27pt above its time and was being missed). Also added `(cid:415)` → `ti` substitution for the font ligature artifact in the SF Rec & Park PDFs. **Result: 29/29 sessions with correct times and swim_types; `diff_sessions.py balboa` exits 0; M1 done** | _you_ |
| 2026-05-03 | M2 started. Per kickoff Q&A: validation = session counts + sanity checks (not full hand-transcription); scheduler deferred to M3. New runner `parse_all.py` shells out to `parse_schedule.py` for every active pool in the inventory (skips maintenance-flagged Sava), reads the resulting JSONs, prints a summary table with total + per-day session counts, and flags any pool that fails sanity checks (`_MIN_TOTAL_SESSIONS=10`, ≥3 active days, must have lap_swim AND family_swim). Optional `--diff` flag also runs `diff_sessions.py` per pool against any expected fixtures that exist (currently only Balboa). Exits 1 if any pool failed or warned, so it's CI-friendly | _you_ |
| 2026-05-03 | First M2 multi-pool run found: 7 pools clean, North Beach at 52 (outlier — investigation revealed it's a **dual-pool facility** with warm + cool pools on a 2-page schedule; counts are real). Diagnosed and fixed three classes of issue surfaced by the larger pool corpus: (1) **time-period inference bug** — when only end has am/pm, propagating to start naively breaks ranges that cross noon (e.g., "10:30 - 12:00 pm" was parsed as 22:30-12:00 instead of 10:30-12:00); fixed by trying same-period first and falling back to opposite if the range goes backwards; (2) **junk labels** — Garfield emitted "( )" sessions, MLK emitted date-fragment sessions ("April 16,"); added `_is_real_label` filter on min-alpha-count and date-fragment patterns; (3) **notice cells** — Rossi has a "3rd Thursday of month pool closed 11:00-2:00 for training" cell that was being parsed as a real session with label "for training"; fixed by always combining same-line text with scan-upward context (so the full "...closed for training" text reaches normalization), and dropping any session normalized as `swim_type: closed`. Also broadened swim-type rules: water polo, swim meet, safety swim/splash, self-guided exer(c)ise with the observed typo. **Result: zero unknown labels, zero suspect times across all 9 pool entries; M2 done** | _you_ |
| 2026-05-03 | Per owner direction, North Beach split into two pool entries (`north_beach_warm`, `north_beach_cool`) sharing the same `source_page_url` and `address`. Pool inventory schema gained an optional `pdf_page_filter` string; `parse_pdf` accepts an optional `page_filter` parameter and skips pages whose extracted text doesn't contain the marker. Initial filters were `WARM POOL` / `COOL POOL` but those colored-text headers don't survive pdfplumber extraction; switched to temperature markers `86°F` / `76°F` which are reliably present on each page. Schema also gained an optional `address` field, populated for North Beach with the shared physical address ("661 Lombard St, San Francisco, CA 94133"). Future multi-pool facilities can use the same pattern; future pools can have address backfilled when convenient | _you_ |
| 2026-05-03 | M3 done. GitHub repo created and project pushed; `.github/workflows/refresh-schedules.yml` workflow's manual trial run completed green end-to-end. Daily cron at 13:37 UTC takes over from here | _you_ |
| 2026-05-03 | M4 started. Per kickoff Q&A: web-view only (no `.ics` this milestone, those are M5); hosting on GitHub Pages on the same repo. New file `index.html` at the repo root (single-file HTML + vanilla JS, no framework, no build step) reads `pools.json` and every active pool's `parsed/*.json` at page load and renders a filter UI (swim type / day / pool / time range) with results grouped by day. Filter state syncs to URL hash for shareable links. Default filter selects Lap Swim + Family Swim only per §5.3. Mobile-responsive; supports light + dark mode via `prefers-color-scheme`. Also regenerated `parsed/north_beach_warm.json` and `parsed/north_beach_cool.json` since splitting the pool inventory entry hadn't yet been followed by re-running the parser; stale `parsed/north_beach.json` (combined, no longer matched by any pool ID) can be deleted manually | _you_ |

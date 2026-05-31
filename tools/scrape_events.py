"""Scrape PE/PD/alts event listings from a list of host sites, LLM-extract
structured events, and persist to data/events.csv.

Each source page is fetched via Firecrawl (plain HTTP first if cheap), the
resulting markdown is sent to DeepSeek for extraction into a strict JSON
schema, and the events are merged into the master CSV with
deduplication by (host, title, date_start).

Filter at extraction time: keep only Hong Kong events (or events with a
clear HK chapter / Asia regional that includes HK).

Usage:
  python tools/scrape_events.py                    # scrape all confirmed sources
  python tools/scrape_events.py --include-unconfirmed   # also try best-guess URLs
  python tools/scrape_events.py --source AVCJ      # one source only
  python tools/scrape_events.py --dry-run          # print plan, no API calls
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import os as _os
import requests as _requests
from classify_strategy import scrape_firecrawl, _scrape_firecrawl_only, FIRECRAWL_KEY  # noqa: E402
from llm_router import llm_call  # noqa: E402

# For SPA-heavy event sites, the default 1500ms wait isn't enough — the event
# listing renders client-side and we'd get only the page shell. Use this
# longer wait when requires_js=yes in event_sources.csv.
JS_HEAVY_WAIT_MS = 4000   # lowered from 6000 to leave more buffer inside the 15s total timeout


def _scrape_firecrawl_aggressive(url: str) -> str:
    """Like _scrape_firecrawl_only but uses Firecrawl 'actions' to wait, scroll
    several times, and wait again — triggers lazy-loaded event lists that
    require scroll-into-view OR don't render until after first scroll event.

    Used as a second-pass fallback when standard JS-render wait returned
    page-chrome only."""
    if not FIRECRAWL_KEY:
        return ""
    body = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "timeout": 45000,                # longer overall timeout for the actions
        "actions": [
            {"type": "wait", "milliseconds": 4000},
            {"type": "scroll", "direction": "down"},
            {"type": "wait", "milliseconds": 2500},
            {"type": "scroll", "direction": "down"},
            {"type": "wait", "milliseconds": 2500},
            {"type": "scroll", "direction": "down"},
            {"type": "wait", "milliseconds": 3000},
        ],
    }
    try:
        r = _requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {FIRECRAWL_KEY}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
    except _requests.RequestException as e:
        print(f"  [firecrawl-aggressive] network error: {e}", file=sys.stderr)
        return ""
    if not r.ok:
        print(f"  [firecrawl-aggressive] HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return ""
    try:
        return (r.json().get("data", {}).get("markdown") or "")[:14000]
    except ValueError:
        return ""

SOURCES_PATH = PROJECT_ROOT / "data" / "event_sources.csv"
DETAIL_PAGE_WAIT_MS = 3000   # individual event detail pages render fast
EVENTS_PATH = PROJECT_ROOT / "data" / "events.csv"
RAW_CACHE_DIR = PROJECT_ROOT / "data" / ".tmp_event_pages"

EVENT_FIELDS = [
    "host", "title", "topic", "date_start", "date_end",
    "time", "venue", "city", "is_hk", "is_virtual",
    "url", "speakers", "price", "audience",
    "source_url", "scraped_at_utc",
]


EXTRACTION_PROMPT = """You are extracting structured event listings from a scraped event page.

Source: {host} ({source_url})

For EACH event you can identify on the page, output one row. Skip:
  - past events (start date before today {today})
  - events with no clear Hong Kong relevance (not in HK, not Asia-regional including HK, not virtual-and-HK-accessible)
  - events that are clearly not finance / PE / PD / hedge funds / asset management / audit / regulatory

Output STRICT JSON only — no commentary, no markdown fence:
{{
  "events": [
    {{
      "title": "...",                  // headline of the event
      "topic": "...",                  // short tag: 'private_equity' | 'private_debt' | 'hedge_funds' | 'real_estate' | 'audit' | 'regulatory' | 'mixed'
      "date_start": "YYYY-MM-DD",      // ISO date, no timezone
      "date_end": "YYYY-MM-DD",        // optional, blank if single-day
      "time": "...",                   // e.g. '08:30-10:00 HKT', blank if unknown
      "venue": "...",                  // physical venue name + address fragment
      "city": "Hong Kong",             // or 'Virtual', 'Singapore', etc.
      "is_hk": true,                   // boolean — physically in HK or HK-chapter event
      "is_virtual": false,             // boolean
      "url": "...",                    // direct registration link if visible
      "speakers": "...",               // comma-separated, blank if not listed
      "price": "...",                  // e.g. 'HKD 500 / free for members'
      "audience": "..."                // e.g. 'CPAs, asset managers, family offices'
    }}
  ]
}}

If no Hong Kong-relevant events on the page, output {{"events": []}}.

Source markdown (truncated to first 12000 chars):
---
{markdown}
---
"""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:80]


def _read_sources(only_source: str | None, include_unconfirmed: bool) -> list[dict]:
    if not SOURCES_PATH.exists():
        raise SystemExit(f"missing {SOURCES_PATH}")
    out = []
    with SOURCES_PATH.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if only_source and r["host"] != only_source:
                continue
            if not include_unconfirmed and r.get("confirmed", "").lower() != "yes":
                continue
            out.append(r)
    return out


def _existing_events() -> list[dict]:
    if not EVENTS_PATH.exists():
        return []
    with EVENTS_PATH.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _event_key(e: dict) -> tuple:
    return (e.get("host", "").strip().lower(),
            (e.get("title", "") or "").strip().lower(),
            (e.get("date_start", "") or "").strip())


def _is_keepable(e: dict) -> tuple[bool, str]:
    """Belt-and-braces filter applied AFTER the LLM extraction. The LLM is
    instructed to skip past + non-HK events but occasionally slips up.
    Returns (keep?, reason_if_dropped)."""
    today_iso = dt.date.today().isoformat()
    ds = (e.get("date_start") or "").strip()
    if ds and ds < today_iso:
        # Check date_end too — if multi-day event ends in the future, keep it
        de = (e.get("date_end") or "").strip()
        if not (de and de >= today_iso):
            return False, f"past event (date_start={ds})"
    # Drop physical-only events outside HK. Virtual events from anywhere are OK.
    is_hk = str(e.get("is_hk", "")).lower() == "true"
    is_virtual = str(e.get("is_virtual", "")).lower() == "true"
    if not is_hk and not is_virtual:
        return False, f"not HK / not virtual (city={e.get('city','')})"
    return True, ""


def _atomic_write_events(rows: list[dict]) -> None:
    tmp = EVENTS_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in EVENT_FIELDS})
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(EVENTS_PATH)


def _llm_extract(host: str, source_url: str, markdown: str) -> list[dict]:
    """Send the markdown to DeepSeek and parse the JSON response. Truncates
    to 12k chars (Firecrawl already caps but defense in depth)."""
    prompt = EXTRACTION_PROMPT.format(
        host=host,
        source_url=source_url,
        today=dt.date.today().isoformat(),
        markdown=(markdown or "")[:12000],
    )
    raw = llm_call(
        system="You extract event listings into strict JSON. Output JSON only.",
        user_content=prompt,
        max_tokens=4000,
    )
    if not raw:
        return []
    # Strip any code-fence noise
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [llm] JSON parse error: {e}", file=sys.stderr)
        print(f"  [llm] first 400 chars: {raw[:400]}", file=sys.stderr)
        return []
    events = data.get("events") or []
    if not isinstance(events, list):
        return []
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="", help="run only this host (case-sensitive name in csv)")
    ap.add_argument("--include-unconfirmed", action="store_true",
                    help="also try best-guess URLs that haven't been verified")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--aggressive", action="store_true",
                    help="for requires_js=yes sources, use Firecrawl actions (scroll+wait) "
                         "to trigger lazy-loaded event lists. Costs ~3-5× normal Firecrawl credits.")
    args = ap.parse_args()

    sources = _read_sources(args.source or None, args.include_unconfirmed)
    if not sources:
        print("no sources selected (use --include-unconfirmed for best-guess URLs)",
              file=sys.stderr); sys.exit(1)

    print(f"[events] scraping {len(sources)} source(s)", file=sys.stderr)
    if args.dry_run:
        for s in sources:
            print(f"  {s['host']:30s} {s['listing_url']}")
        return

    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    existing = _existing_events()
    existing_keys = {_event_key(e) for e in existing}
    new_rows = list(existing)
    new_count = 0
    skipped_count = 0
    failed_sources = []

    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    for s in sources:
        host = s["host"]
        url = s["listing_url"]
        detail_re = (s.get("detail_url_regex") or "").strip()
        print(f"\n[events] === {host} ===", file=sys.stderr)
        print(f"  url: {url}", file=sys.stderr)

        # Two-pass mode: index page gives URLs only; the dates / venues are on
        # individual event detail pages. Fetch index, regex-match detail URLs,
        # scrape each, LLM-extract per page. Used for HKVCA (homepage has the
        # upcoming-event URLs).
        if detail_re:
            # Explicitly opted-in source: bypass robots wrapper and force Firecrawl.
            # Many JS event sites disallow generic crawlers in robots but we have
            # author intent here (the host is listed in event_sources.csv).
            try:
                # 3s is enough for an index page to show event URLs (we only
                # need the links rendered, not the full event detail).
                index_md = _scrape_firecrawl_only(url, wait_ms=3000)
            except Exception as e:
                print(f"  [scrape] index error: {e}", file=sys.stderr)
                failed_sources.append((host, f"index scrape error: {e}"))
                continue
            urls = sorted(set(re.findall(detail_re, index_md or "")))
            print(f"  [two-pass] index gave {len(urls)} detail URLs", file=sys.stderr)
            urls = urls[:20]                  # cap per source to keep Firecrawl spend bounded
            kept_total = 0
            for du in urls:
                try:
                    dmd = _scrape_firecrawl_only(du, wait_ms=DETAIL_PAGE_WAIT_MS)
                except Exception as e:
                    print(f"    [detail] {du[:80]} error: {e}", file=sys.stderr)
                    continue
                if not dmd or len(dmd) < 200:
                    continue
                try:
                    events = _llm_extract(host, du, dmd)
                except Exception as e:
                    print(f"    [llm] {du[:80]} error: {e}", file=sys.stderr)
                    continue
                for ev in events:
                    ev = {**ev}
                    ev["host"] = host
                    ev["source_url"] = du
                    ev["scraped_at_utc"] = now
                    keep, why = _is_keepable(ev)
                    if not keep:
                        skipped_count += 1
                        continue
                    key = _event_key(ev)
                    if key in existing_keys:
                        skipped_count += 1
                        continue
                    existing_keys.add(key)
                    new_rows.append(ev)
                    new_count += 1
                    kept_total += 1
                time.sleep(0.3)
            print(f"  [two-pass] kept {kept_total} events from {len(urls)} pages", file=sys.stderr)
            continue

        requires_js = (s.get("requires_js") or "").lower() in ("yes", "y", "true", "1")
        try:
            if requires_js and args.aggressive:
                md = _scrape_firecrawl_aggressive(url)
            elif requires_js:
                # Force Firecrawl path with long JS wait so SPA event lists render.
                md = _scrape_firecrawl_only(url, wait_ms=JS_HEAVY_WAIT_MS)
            else:
                md = scrape_firecrawl(url)
        except Exception as e:
            print(f"  [scrape] error: {e}", file=sys.stderr)
            failed_sources.append((host, f"scrape error: {e}"))
            continue
        if not md or len(md) < 200:
            print(f"  [scrape] thin content ({len(md or '')} chars) — skipping", file=sys.stderr)
            failed_sources.append((host, f"thin content ({len(md or '')} chars)"))
            continue
        cache_path = RAW_CACHE_DIR / f"{_slug(host)}.md"
        cache_path.write_text(md, encoding="utf-8")

        try:
            events = _llm_extract(host, url, md)
        except Exception as e:
            print(f"  [llm] error: {e}", file=sys.stderr)
            failed_sources.append((host, f"llm error: {e}"))
            continue

        kept = 0
        for ev in events:
            ev = {**ev}                       # copy
            ev["host"] = host
            ev["source_url"] = url
            ev["scraped_at_utc"] = now
            keep, why = _is_keepable(ev)
            if not keep:
                skipped_count += 1
                continue
            key = _event_key(ev)
            if key in existing_keys:
                skipped_count += 1
                continue
            existing_keys.add(key)
            new_rows.append(ev)
            new_count += 1
            kept += 1
        print(f"  [extract] {len(events)} events on page, {kept} new", file=sys.stderr)
        time.sleep(0.5)   # be polite

    _atomic_write_events(new_rows)

    print(f"\n[events] done.", file=sys.stderr)
    print(f"  total in events.csv:  {len(new_rows)}", file=sys.stderr)
    print(f"  new this run:         {new_count}", file=sys.stderr)
    print(f"  duplicates skipped:   {skipped_count}", file=sys.stderr)
    if failed_sources:
        print(f"  sources failed:       {len(failed_sources)}", file=sys.stderr)
        for h, why in failed_sources:
            print(f"    - {h}: {why}", file=sys.stderr)


if __name__ == "__main__":
    main()

"""Classify each SFC Type 9 corporation by what's actually useful for Kroll PortVal BD.

v2 schema is multi-dimensional (BD-pragmatic, per QC review):
  asset_classes[]            What asset types they invest in (multi-value)
  style                      How they trade (directional/L-S/macro/etc.)
  illiquid_book_likelihood   THE actual BD signal: how likely they hold illiquid positions
  operational_status         active vs placeholder vs no_site vs dormant
  cayman_fund_signals        proxy for "needs an audit → PortVal client"
  us_lp_signals              proxy for US-LP-driven valuation governance
  family_office_type         single / multi / n/a
  parent_org + parent_relationship
  aum_raw_string + aum_currency + aum_usd_m  (structured, no silent FX conversion)
  geographic_focus, sector_focus
  also_holds_type_1          Derived from SFC /details (free; no scrape)
  also_holds_type_4
  name_disambiguation_status
  linkedin_url, ir_email
  evidence_strength          Replaces self-reported "confidence" (which is theatre)
  source_markdown_path       Path to cached raw page markdown (gitignored)

Pipeline per firm:
  1. SerpAPI: find website (HK-biased query)
  2. robots.txt: respect disallow
  3. Firecrawl: scrape homepage; if thin, try /about
  4. SFC /details: pull full RA-type list (Type 1 = dealing, Type 4 = advising, etc.)
  5. DeepSeek (via llm_router): single JSON classification

Idempotent — skips ceref already in the output CSV.
Markdowns cached under data/firm_pages/{ceref}.md (gitignored).
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
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # repo root
SNAP_DIR = PROJECT_ROOT / "data" / "snapshots"
OUT_PATH = PROJECT_ROOT / "data" / "strategy_classification.csv"
PAGES_DIR = PROJECT_ROOT / "data" / "firm_pages"
GITIGNORE_PATH = PROJECT_ROOT / "data" / ".gitignore"

sys.path.insert(0, str(PROJECT_ROOT / "tools"))
load_dotenv(PROJECT_ROOT / ".env")

from llm_router import llm_call  # noqa: E402

SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
FIRECRAWL_KEY = os.environ.get("FIRECRAWL_API_KEY")
CLASSIFIER_VERSION = "v2-2026-05-27"
UA = "krollBD-classifier/2.0 (contact: fengelh@gmail.com)"

# Fields that downstream tools (deep_scrape, hunter) populate and that
# classify_strategy must MERGE FORWARD on re-classification, never overwrite.
# See workflows/enrichment_cascade.md "never downgrade confidence" rule.
STICKY_FIELDS = (
    "emails_on_site",          # deep_scrape may augment; merge sets
    "generic_emails_on_site",  # deep_scrape may augment; merge sets
    "deep_scrape_attempted_utc",
    "deep_scrape_status",
    "ir_email",                # may be hand-corrected by user
)

# When a website CHANGES (new URL via override or SFC update), these stamps
# are invalidated and the downstream tools should re-run on the new content.
STAMPS_CLEARED_ON_WEBSITE_CHANGE = (
    "deep_scrape_attempted_utc",
    "deep_scrape_status",
)


FIELDS = [
    "ceref", "name_en", "website_url", "name_disambiguation_status",
    "operational_status",
    "asset_classes", "style", "sub_strategy",
    "illiquid_book_likelihood",
    "cayman_fund_signals", "us_lp_signals",
    "geographic_focus", "sector_focus",
    "family_office_type",
    "parent_org", "parent_relationship",
    "aum_raw_string", "aum_currency", "aum_usd_m",
    "linkedin_url", "ir_email",
    "emails_on_site", "generic_emails_on_site",
    "also_holds_type_1", "also_holds_type_4", "all_sfc_ra_types",
    "evidence_strength",
    "rationale",
    "classification_source",
    "serpapi_kg_summary",
    "source_markdown_path",
    "classified_at_utc", "classifier_version",
]

# Canonical ORDER for columns added by downstream tools after the core FIELDS.
# Any new derived column should be added here in append-order so column
# layout stays stable regardless of which tool ran last.
EXTRA_FIELDS_ORDER = [
    "email_extraction_cleared",         # verify_emails_against_disambig
    "website_accuracy",                 # derive_website_accuracy
    "deep_scrape_attempted_utc",        # deep_scrape_contact_pages
    "deep_scrape_status",               # deep_scrape_contact_pages
    "hunter_email",                     # hunter_for_cohort / publisher
    "hunter_score",
    "hunter_status",
    "hunter_attempted_utc",
]


def canonical_fieldnames(seen: list[str]) -> list[str]:
    """Return seen fieldnames reordered into canonical layout:
       1. FIELDS (core, in declared order, only ones actually present)
       2. EXTRA_FIELDS_ORDER (derived, in declared order, only ones present)
       3. Any remaining unknown columns, in their original seen order
    Any column declared in EXTRA_FIELDS_ORDER but not present in `seen` is
    silently dropped so writers don't accidentally add empty columns.
    """
    seen_set = set(seen)
    out = [c for c in FIELDS if c in seen_set]
    out += [c for c in EXTRA_FIELDS_ORDER if c in seen_set]
    known = set(out)
    out += [c for c in seen if c not in known]
    return out


# ---------------- email extraction from scraped markdown ----------------

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# locals that look like generic inboxes (not named people)
GENERIC_LOCALS = {
    "info", "contact", "enquiry", "enquiries", "hello", "support",
    "compliance", "regulatory", "legal", "admin", "office", "general",
    "hr", "recruitment", "careers", "jobs", "press", "media", "pr",
    "ir", "investorrelations", "investor_relations", "investor-relations",
    "sales", "marketing", "service", "services", "customerservice",
    "client", "clients", "clientservices", "feedback", "webmaster",
    "noreply", "no-reply", "donotreply",
}


def extract_emails_from_md(md: str, firm_domain: str | None = None) -> tuple[list[str], list[str]]:
    """Return (named_emails, generic_emails). Optionally restrict to firm_domain."""
    found = set()
    for m in EMAIL_RE.finditer(md or ""):
        em = m.group(0).lower()
        # noise filters
        if any(x in em for x in ("example.com", "yourdomain", "domain.com", "test.com",
                                  ".png", ".jpg", ".gif", "sentry.io", "wixpress.com")):
            continue
        # If we know the firm's domain, only keep matching emails
        if firm_domain:
            host = em.split("@", 1)[1]
            fd = re.sub(r"^(www\.)?", "", firm_domain.lower())
            fd_root = ".".join(fd.split(".")[-2:]) if "." in fd else fd
            if not (host == fd or host.endswith("." + fd_root) or host == fd_root):
                continue
        found.add(em)
    named, generic = [], []
    for em in sorted(found):
        local = em.split("@", 1)[0]
        if local in GENERIC_LOCALS or any(g in local for g in ("info", "contact", "compliance")):
            generic.append(em)
        else:
            named.append(em)
    return named, generic


# ---------------- gitignore for firm_pages/ ----------------

def ensure_gitignore() -> None:
    """Add data/firm_pages/ to data/.gitignore — markdowns are regenerable + bulky."""
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    existing = GITIGNORE_PATH.read_text(encoding="utf-8") if GITIGNORE_PATH.exists() else ""
    if "firm_pages/" not in existing:
        with GITIGNORE_PATH.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("# Cached firm pages — regenerable, bulky, marketing copy aggregated\n")
            f.write("firm_pages/\n")


# ---------------- find website via SerpAPI ----------------

EXCLUDE_DOMAINS = {
    # social / news
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "youtube.com",
    "wikipedia.org", "bloomberg.com", "reuters.com", "wsj.com",
    "ft.com", "scmp.com", "asianinvestor.net", "avcj.com", "pei.media",
    "instagram.com", "tiktok.com", "github.com", "google.com",
    # regulators / official registers (we already use SFC directly)
    "sfc.hk", "apps.sfc.hk", "hkex.com.hk", "hkexnews.hk",
    # directories / business-listing sites (the big offenders)
    "dnb.com", "ltddir.com", "globalfinreg.com", "0xmd.com", "webbsite.0xmd.com",
    "irasia.com", "hktdc.com", "yellowpages.com.hk", "jobsdb.com",
    "fastbull.com", "brokersview.com", "lei.info", "gleif.org",
    "opencorporates.com", "companiesintheuk.co.uk", "endole.co.uk",
    "northdata.com", "rocketreach.co", "zoominfo.com", "crunchbase.com",
    "panjiva.com", "importgenius.com", "hktradeservices.com",
    "hkcomdir.com", "hong-kong-companies.com", "ltddir.com",
    "trade-leads.com", "hkdir.com", "hong-kong-corp.com",
    "pitchbook.com", "preqin.com",
}


def _root_url(url: str) -> str:
    """Normalize URL to its root (https://host/) — discards path/query/hash."""
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return url
    return f"{p.scheme}://{p.netloc}/"


def name_slugs(name: str) -> list[str]:
    """Generate candidate URL slugs from a firm name (most-likely first)."""
    s = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    s = re.sub(r"\s+", " ", s).strip()
    words = s.split()
    drop = {"hong", "kong", "asia", "asian", "limited", "ltd", "capital", "management",
            "investment", "investments", "advisors", "advisers", "partners",
            "group", "holdings", "company", "co", "the", "of"}
    short = [w for w in words if w not in drop]
    cands = {
        ("".join(short) or "".join(words))[:24],
        (short[0] if short else words[0])[:24],
        "".join(words[:2])[:30],
        "".join(short[:2])[:30] if len(short) >= 2 else "",
        # add slug + "capital" / "investment" since many firms include these
        ((short[0] if short else words[0]) + "capital")[:30],
        ((short[0] if short else words[0]) + "investment")[:30],
    }
    return [c for c in cands if c]


def guess_domain_urls(name: str) -> list[str]:
    """Fabricate plausible firm URLs from the name. Top-of-list first."""
    out: list[str] = []
    for slug in name_slugs(name):
        for tld in (".com.hk", ".com", "hk.com", ".asia"):
            out.append(f"https://www.{slug}{tld}/")
            out.append(f"https://{slug}{tld}/")
    # dedup preserving order
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq[:12]


_SERPAPI_KG_CACHE: dict[str, str] = {}


_SERPAPI_EXHAUSTED = False


def _serpapi_search(query: str) -> list[str]:
    """Return list of root URLs from SerpAPI for one query (blocklist applied).

    Side effect: caches Google Knowledge Graph + answer_box text in
    _SERPAPI_KG_CACHE[query] for later triangulation.

    Circuit breaker: once SerpAPI returns 429 (monthly-quota exhausted),
    every subsequent call short-circuits to [] instead of round-tripping
    through requests. Prevents ~20s dead-time per call across hundreds of
    firms during a weekly publish.
    """
    global _SERPAPI_EXHAUSTED
    if _SERPAPI_EXHAUSTED:
        return []
    try:
        r = requests.get("https://serpapi.com/search.json",
                         params={"q": query, "api_key": SERPAPI_KEY, "num": 10,
                                 "hl": "en", "gl": "hk"},
                         timeout=20)
        if r.status_code == 429:
            print(f"  [serpapi] 429 on {query} — tripping circuit breaker for rest of run", file=sys.stderr)
            _SERPAPI_EXHAUSTED = True
            return []
        r.raise_for_status()
        data = r.json()
        # Stash KG/answer-box for later cross-check
        kg_bits: list[str] = []
        kg = data.get("knowledge_graph") or {}
        if kg.get("title"): kg_bits.append(f"KG title: {kg['title']}")
        if kg.get("type"): kg_bits.append(f"KG type: {kg['type']}")
        if kg.get("description"): kg_bits.append(f"KG desc: {kg['description']}")
        for k in ("founder", "founders", "ceo", "headquarters", "parent_organization",
                  "subsidiaries", "industry", "number_of_employees", "founded"):
            if kg.get(k):
                kg_bits.append(f"KG {k}: {kg[k]}")
        ab = data.get("answer_box") or {}
        if ab.get("answer"): kg_bits.append(f"AnswerBox: {ab['answer']}")
        if ab.get("snippet"): kg_bits.append(f"AnswerBoxSnippet: {ab['snippet']}")
        ai_ov = data.get("ai_overview") or {}
        if ai_ov.get("text_blocks"):
            txt = " ".join(b.get("snippet") or "" for b in ai_ov["text_blocks"])[:500]
            if txt.strip(): kg_bits.append(f"AI overview: {txt}")
        _SERPAPI_KG_CACHE[query] = "\n".join(kg_bits)
        urls: list[str] = []
        for item in data.get("organic_results", []):
            link = item.get("link") or ""
            if not link.startswith("http"):
                continue
            host = re.sub(r"^https?://(www\.)?", "", link).split("/")[0].lower()
            if any(host == d or host.endswith("." + d) for d in EXCLUDE_DOMAINS):
                continue
            root = _root_url(link)
            if root not in urls:
                urls.append(root)
        return urls
    except Exception as e:
        print(f"  [serpapi] {query}: {e}", file=sys.stderr)
        return []


def get_kg_summary(name: str) -> str:
    """Pull cached KG text for whichever query strings ran for this name."""
    bits = []
    for q in (f'"{name}" Hong Kong', f'"{name}" asset management Hong Kong'):
        if q in _SERPAPI_KG_CACHE and _SERPAPI_KG_CACHE[q]:
            bits.append(_SERPAPI_KG_CACHE[q])
    return "\n\n".join(bits)[:1500]  # cap for CSV sanity


def find_website(name: str) -> tuple[str | None, list[str]]:
    """Multi-query SerpAPI: run two variants, merge results.

    Query A:  '"<name>" Hong Kong'                 — works well for global brands
    Query B:  '"<name>" asset management Hong Kong' — works better for ambiguous names
                                                      (disambiguates from hotels, brokers, etc.)
    Each query also gets results re-ranked by domain-name match upstream.
    """
    if not SERPAPI_KEY:
        return None, []
    primary = _serpapi_search(f'"{name}" Hong Kong')
    # Smart fallback: only burn a 2nd SerpAPI call if the primary didn't return
    # a candidate whose domain at least loosely contains a name word.
    needs_fallback = True
    for url in primary[:3]:
        if _domain_name_score(url, name) > 0:
            needs_fallback = False
            break
    if needs_fallback:
        secondary = _serpapi_search(f'"{name}" asset management Hong Kong')
    else:
        secondary = []

    # Query C — Chinese language. Many HK boutiques (esp. mainland-affiliated)
    # have zh-Hant-only sites that English queries miss. Only fire if neither
    # primary nor secondary surfaced a name-matching domain.
    tertiary: list[str] = []
    if not any(_domain_name_score(u, name) > 0 for u in (primary[:3] + secondary[:3])):
        tertiary = _serpapi_search(f'"{name}" 香港')

    # Query D — SFC-PDF-biased. SFC publications often list the firm's official
    # site in the corporate-info table. Only fire if all 3 prior queries failed.
    quaternary: list[str] = []
    if not any(_domain_name_score(u, name) > 0
               for u in (primary[:3] + secondary[:3] + tertiary[:3])):
        quaternary = _serpapi_search(f'"{name}" SFC Type 9')

    # Interleave so each query's top result rides near the top of the merge.
    merged: list[str] = []
    lists = [primary, secondary, tertiary, quaternary]
    for i in range(max((len(l) for l in lists), default=0)):
        for l in lists:
            if i < len(l) and l[i] not in merged:
                merged.append(l[i])
    return (merged[0] if merged else None), merged


# ---------------- robots.txt compliance ----------------
# Hand-rolled permissive parser. Python 3.14's urllib.robotparser misreads
# legitimately permissive robots.txt files (e.g. KKR, Millennium both use
# `Allow: /` or empty Disallow: which the stdlib parser treats as block-all).
# This implementation follows RFC 9309: empty Disallow = no restriction.

_ROBOTS_CACHE_TEXT: dict[str, str] = {}


def _fetch_robots(root: str) -> str:
    if root in _ROBOTS_CACHE_TEXT:
        return _ROBOTS_CACHE_TEXT[root]
    try:
        r = requests.get(urljoin(root, "/robots.txt"),
                         headers={"User-Agent": UA}, timeout=10)
        text = r.text if r.status_code == 200 else ""
    except Exception:
        text = ""
    _ROBOTS_CACHE_TEXT[root] = text
    return text


def robots_allowed(url: str) -> bool:
    """Permissive RFC-9309 check. Only blocks when an explicit Disallow rule
    applies to '*' or our UA prefix AND the URL path matches it.
    No robots.txt, unreachable, or empty Disallow → allowed.
    """
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    text = _fetch_robots(root)
    if not text:
        return True
    path = parsed.path or "/"
    in_section = False
    disallow_paths: list[str] = []
    allow_paths: list[str] = []
    ua_lower = UA.lower()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            in_section = (val == "*") or (val.lower() in ua_lower)
        elif key == "disallow" and in_section and val:
            disallow_paths.append(val)
        elif key == "allow" and in_section and val:
            allow_paths.append(val)
    # Allow wins over Disallow for same-length matches; we keep it simple:
    if any(path.startswith(a) for a in allow_paths):
        return True
    return not any(path.startswith(d) for d in disallow_paths)


# ---------------- scrape ----------------

def _scrape_plain(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        if not r.ok:
            return ""
        text = re.sub(r"<script[^>]*>.*?</script>", "", r.text, flags=re.S | re.I)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text)[:12000]
    except Exception:
        return ""


# Session-level Firecrawl failure counters. Persistently >50% failure rate
# usually means a billing / quota issue, not a transient blip.
_FIRECRAWL_STATS = {"ok": 0, "fail_quota": 0, "fail_rate": 0, "fail_other": 0, "network": 0}


def _firecrawl_warn(reason: str) -> None:
    """Bump the failure counter and print once every 10 failures so the operator
    can spot a degraded-Firecrawl situation in long classify runs."""
    _FIRECRAWL_STATS[reason] = _FIRECRAWL_STATS.get(reason, 0) + 1
    total_fail = sum(v for k, v in _FIRECRAWL_STATS.items() if k != "ok")
    if total_fail % 10 == 0:
        print(f"  [firecrawl] cumulative stats: {dict(_FIRECRAWL_STATS)}", file=sys.stderr)


def _scrape_firecrawl_only(url: str, wait_ms: int = 1500) -> str:
    """Firecrawl scrape with a small JS-render wait so SPA pages get a chance
    to populate. wait_ms=0 disables the wait (cheaper, no JS wait).

    Returns empty string on failure but distinguishes the reason in
    _FIRECRAWL_STATS so degraded runs are diagnosable."""
    if not FIRECRAWL_KEY:
        return ""
    body = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "timeout": 15000,
    }
    if wait_ms:
        body["actions"] = [{"type": "wait", "milliseconds": int(wait_ms)}]
    try:
        r = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {FIRECRAWL_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=25,
        )
    except requests.RequestException as e:
        print(f"  [firecrawl] network error for {url}: {e}", file=sys.stderr)
        _firecrawl_warn("network")
        return ""
    if r.ok:
        _FIRECRAWL_STATS["ok"] += 1
        try:
            md = (r.json().get("data", {}).get("markdown") or "")
            return md[:12000]
        except ValueError:
            _firecrawl_warn("fail_other")
            return ""
    # Diagnose non-OK responses
    if r.status_code in (402,):
        print(f"  [firecrawl] HTTP 402 quota/billing — check firecrawl.dev dashboard", file=sys.stderr)
        _firecrawl_warn("fail_quota")
    elif r.status_code == 429:
        print(f"  [firecrawl] HTTP 429 rate-limited on {url}", file=sys.stderr)
        _firecrawl_warn("fail_rate")
    else:
        print(f"  [firecrawl] HTTP {r.status_code} on {url}: {r.text[:160]}", file=sys.stderr)
        _firecrawl_warn("fail_other")
    return ""


def scrape_firecrawl(url: str) -> str:
    """Plain HTTP first; Firecrawl only if plain returns thin content (likely JS SPA).

    Saves ~60-70% of Firecrawl quota at the cost of slightly noisier output for
    SPA-heavy sites.
    """
    if not robots_allowed(url):
        print(f"  [robots] disallowed: {url}", file=sys.stderr)
        return ""
    plain = _scrape_plain(url)
    # Heuristics for "this looks like a JS SPA that didn't render": <500 chars,
    # or content is mostly nav (no descriptive sentences).
    if plain and len(plain) >= 500 and plain.count(".") >= 5:
        return plain
    fc = _scrape_firecrawl_only(url)
    return fc if fc else plain


# Gate-page signature: short page (mostly disclaimers) + agreement keywords.
# Common on HK PE/PD firms that require professional-investor confirmation
# before showing real content.
_GATE_KEYWORDS = re.compile(
    r"(?i)\b(professional\s+investor|i\s+agree|terms\s+of\s+use|cookie\s+notice|"
    r"disclaimer|confirm.*not.*resident|click\s+(?:agree|accept|continue)|"
    r"by\s+(?:clicking|accepting|continuing)|"
    r"\bagree\b.*\bdecline\b|"
    r"this\s+website\s+(?:is\s+)?intended\s+for|are\s+you\s+a)\b"
)


def _looks_like_gate(md: str) -> bool:
    """Page is a professional-investor / TOS gate, not real content."""
    if not md:
        return False
    # Real content pages typically don't have 3+ gate keywords in <2000 chars.
    if len(md) > 4000:
        return False
    hits = len(_GATE_KEYWORDS.findall(md))
    return hits >= 3


def _post_gate_url_candidates(url: str) -> list[str]:
    """Common post-gate URL patterns HK PE/PD firms use after the click-through.

    APInvest hides real content at content.{domain}; some firms use pi., app.,
    portal., members. Try the obvious ones; classify_strategy keeps the best
    page it gets back.
    """
    from urllib.parse import urlparse
    p = urlparse(url if "://" in url else "https://" + url)
    host = re.sub(r"^www\.", "", p.netloc.lower())
    if not host:
        return []
    scheme = p.scheme or "https"
    return [
        f"{scheme}://content.{host}",
        f"{scheme}://pi.{host}",
        f"{scheme}://portal.{host}",
        f"{scheme}://members.{host}",
        f"{scheme}://app.{host}",
        f"{scheme}://www.{host}/content",
        f"{scheme}://www.{host}/private",
        f"{scheme}://www.{host}/pi",
    ]


def scrape_with_fallback(url: str, name: str) -> tuple[str, str]:
    """Try homepage; fall back to /about variants if homepage is thin OR a
    professional-investor gate page (common on HK PE/PD firms).

    Order of escalation:
      1. homepage
      2. if homepage is a gate → try common post-gate subdomains (content./pi.)
         and /content, /private, /pi paths
      3. if still thin → try /about, /about-us, /firm
    """
    md = scrape_firecrawl(url)
    if md and len(md) >= 300 and not _looks_like_gate(md):
        return md, url

    # Step 2: if it's a gate, try post-gate URLs
    if md and _looks_like_gate(md):
        for candidate in _post_gate_url_candidates(url):
            if not robots_allowed(candidate):
                continue
            try:
                better = scrape_firecrawl(candidate)
            except Exception:
                continue
            if better and len(better) >= 600 and not _looks_like_gate(better):
                # Use post-gate content; discard the gate page
                return better, candidate

    # Step 3: /about-style fallbacks
    base = url.rstrip("/")
    for path in ("/about", "/about-us", "/firm"):
        candidate = base + path
        if not robots_allowed(candidate):
            continue
        more = scrape_firecrawl(candidate)
        if more and len(more) >= 300 and not _looks_like_gate(more):
            combined = (md + "\n\n---\n\n" + more) if md else more
            return combined, candidate
    return md or "", url


# ---------------- SFC /details to get all RA types ----------------

SFC_DETAIL_URL = "https://apps.sfc.hk/publicregWeb/corp/{ceref}/details?locale=en"
RA_DATA_RE = re.compile(r"var\s+raDetailData\s*=\s*(\[.*?\]);", re.S)


def sfc_ra_types(ceref: str) -> tuple[set[int], list[int]]:
    """Pull the full active RA-type list for a corp. Returns (set, sorted list)."""
    try:
        r = requests.get(SFC_DETAIL_URL.format(ceref=ceref),
                         headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        m = RA_DATA_RE.search(r.text)
        if not m:
            return set(), []
        items = json.loads(m.group(1))
        active = {int(d["actType"]) for d in items
                  if d.get("actType") is not None and d.get("status") == "A"}
        return active, sorted(active)
    except Exception:
        return set(), []


# ---------------- LLM ----------------

SYSTEM_PROMPT = """\
You classify Hong Kong-licensed asset managers based on text from their website.
Output a SINGLE JSON object — no markdown, no prose, no code fences.

## Important: classify the PARENT GROUP, not the narrow HK legal entity
Most SFC Type 9 entities (e.g. "BlackRock Asset Management North Asia Limited",
"Goldman Sachs Asset Management (Hong Kong) Limited", "PIMCO Asia Limited",
"KKR Capital Markets Asia Limited") are subsidiaries of a global parent group.
The website you are scraping is almost always the PARENT's site, not the
narrow HK entity's. That is intentional.

For BD purposes, the relationship is with the parent group:
- asset_classes = the parent group's FULL menu of asset classes, not just
  what the local HK entity narrowly executes. If BlackRock Group runs both
  liquid ETFs AND a large private-credit business, mark BOTH — even if the
  HK entity might in practice be ETF-only.
- aum = the parent group's AUM (often headline figure on homepage).
- illiquid_book_likelihood = the parent group's posture. BlackRock, Goldman,
  PIMCO, Morgan Stanley, Allianz, UBS, Fidelity etc. all run material
  illiquid books → "medium" minimum even if the scraped page emphasizes
  retail/ETF/MPF distribution.
- parent_org = name the parent group explicitly when the firm appears to be
  a subsidiary.

If the firm has NO parent group (standalone HK-only manager like a boutique
fund), classify the standalone entity normally.

The user wants to filter to firms that hold ILLIQUID PRIVATE positions
(private equity, private credit, real assets, secondaries) because those
firms need third-party fair-value opinions for accounting. Be honest about
what the source supports.

Schema (every field required; use null for unknowns):

{
  "asset_classes": [array of: "private_equity","private_credit","secondaries","real_assets","public_equity","public_credit","fund_of_funds","multi","unknown"],
  "style": one of "directional_long","long_short","relative_value","macro","systematic","passive","buy_and_hold","mixed","unknown",
  "sub_strategy": short qualifier like "growth equity" or "Asia direct lending", or "",
  "illiquid_book_likelihood": "high","medium","low","none", or "unknown",
  "operational_status": "active","placeholder_site","no_site_found","dormant_signals","unknown",
  "cayman_fund_signals": true / false / null,
  "us_lp_signals": true / false / null,
  "geographic_focus": short string e.g. "greater_china","asia","global","sea","japan","korea", or null,
  "sector_focus": array of e.g. "healthcare","tech","infrastructure","real_estate","consumer","financials","generalist","none",
  "family_office_type": "single","multi","not_applicable",
  "parent_org": null or string,
  "parent_relationship": "subsidiary","affiliate","alumni_founded","joint_venture","n_a",
  "aum_raw_string": verbatim quote with currency if mentioned, else null,
  "aum_currency": "USD","HKD","CNY","EUR","GBP","JPY","unknown" or null,
  "aum_usd_m": numeric value or null if not stated or you'd have to guess FX rate,
  "linkedin_url": full URL or null,
  "ir_email": email or null,
  "evidence_strength": "multiple_pages_corroborate","one_clear_statement","inferred_from_name_or_parent","guessed","no_evidence",
  "rationale": single sentence, what made you classify this way
}

Rules:
- If the source text is empty or unrelated to the named firm: operational_status="no_site_found", evidence_strength="no_evidence", asset_classes=["unknown"], everything else null.
- If the source is a placeholder ("coming soon", under construction, contact only): operational_status="placeholder_site".
- For aum_usd_m: ONLY fill if the page states USD explicitly. Do NOT silently convert HK$ → USD or RMB → USD — return null instead.
- illiquid_book_likelihood (PARENT-GROUP view): "high" if private investments are a primary business line; "medium" if mixed firms (e.g. major bank/AM with both liquid and alts arms); "low" only for pure-public-markets shops with no alts at all; "none" extremely rarely.
- evidence_strength: use "guessed" or "no_evidence" liberally. The user prefers honest "unknown" over confident wrong.
"""


def classify_with_llm(name: str, source_text: str) -> dict:
    user = (
        f"Firm name: {name}\n\n"
        f"Source text from firm website (may be empty):\n---\n{source_text[:8000]}\n---\n\n"
        "Return only the JSON object."
    )
    try:
        raw = llm_call(SYSTEM_PROMPT, user, max_tokens=600)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {"operational_status": "unknown", "evidence_strength": "no_evidence",
                    "rationale": "llm returned no JSON"}
        return json.loads(m.group(0))
    except Exception as e:
        return {"operational_status": "unknown", "evidence_strength": "no_evidence",
                "rationale": f"llm error: {e}"}


# ---------------- name disambiguation ----------------

def disambiguation_status(name: str, source_text: str) -> str:
    """Did the scraped page actually mention this firm?"""
    if not source_text:
        return "no_match"
    words = [w.lower() for w in re.split(r"[\s,]+", name) if len(w) >= 4]
    text = source_text.lower()
    hits = sum(1 for w in words if w in text)
    if hits >= 2 or any(w in text for w in words if "hong kong" in name.lower()):
        return "high_confidence"
    if hits >= 1:
        return "medium_confidence"
    return "ambiguous"


# ---------------- main loop ----------------

def load_existing() -> dict[str, dict]:
    if not OUT_PATH.exists():
        return {}
    with OUT_PATH.open(encoding="utf-8-sig") as f:
        return {r["ceref"]: r for r in csv.DictReader(f)}


def _atomic_write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """Write CSV via tmp file + fsync + atomic replace. utf-8 (no BOM) standard."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def write_row(row: dict) -> None:
    write_header = not OUT_PATH.exists()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Coerce list / bool to strings for CSV
    safe = {}
    for k in FIELDS:
        v = row.get(k)
        if isinstance(v, list):
            safe[k] = ",".join(str(x) for x in v)
        elif isinstance(v, bool):
            safe[k] = "true" if v else "false"
        elif v is None:
            safe[k] = ""
        else:
            safe[k] = v
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerow(safe)


def save_markdown(ceref: str, url: str, md: str) -> Path:
    p = PAGES_DIR / f"{ceref}.md"
    header = f"<!-- source: {url}\n     scraped: {dt.datetime.utcnow().isoformat()}Z -->\n\n"
    p.write_text(header + md, encoding="utf-8")
    return p


def latest_corps_csv() -> Path:
    # New canonical name first; fall back to dated files if not yet migrated
    canonical = SNAP_DIR / "sfc_t9_corps_latest.csv"
    if canonical.exists():
        return canonical
    files = sorted(SNAP_DIR.glob("sfc_t9_corps_*.csv"))
    if not files:
        raise SystemExit("No corps snapshot found in data/snapshots/")
    return files[-1]


_WRITE_LOCK = None  # set in main() when concurrent


def name_words_match(name: str, text: str) -> int:
    """Count significant name words (>=4 chars, not noise) found in text."""
    noise = {"hong", "kong", "asia", "asian", "limited", "ltd", "group", "the",
             "capital", "management", "investment", "investments", "company", "co",
             "advisors", "advisers", "partners", "holdings", "of"}
    words = [w.lower() for w in re.split(r"[\s,.()&]+", name) if len(w) >= 4]
    sig = [w for w in words if w not in noise]
    text_l = text.lower()
    return sum(1 for w in sig if w in text_l)


def _alive(url: str, timeout: float = 2.0) -> bool:
    """Fast HEAD probe. GET fallback for servers that 405 on HEAD."""
    try:
        r = requests.head(url, headers={"User-Agent": UA}, timeout=timeout,
                          allow_redirects=True)
        if r.status_code < 400:
            return True
        if r.status_code in (405, 403):
            r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout,
                             stream=True)
            return r.status_code < 400
    except Exception:
        pass
    return False


def _domain_name_score(url: str, name: str) -> int:
    """+3 per significant name word found inside the domain. Heavily favours
    real firm sites (hillhouseinvestment.com) over directories (legal500.com).
    """
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
    noise = {"hong", "kong", "asia", "asian", "limited", "ltd", "group", "the",
             "capital", "management", "investment", "investments", "company", "co",
             "advisors", "advisers", "partners", "holdings", "of"}
    words = [w.lower() for w in re.split(r"[\s,.()&]+", name) if len(w) >= 4 and w.lower() not in noise]
    return sum(15 for w in words if w in host)  # weighted to beat SerpAPI rank


def find_best_site(name: str) -> tuple[str, str, str]:
    """Domain-name-aware site discovery:
      1) Build candidates from SerpAPI + slug-based guesses
      2) Rank by: domain-contains-firm-name-words (heaviest), then SerpAPI rank
      3) HEAD-probe ranked candidates; scrape top 4 live ones
      4) Pick whichever scrape has the most firm-name words in CONTENT
    """
    serp_top, serp_alts = find_website(name)
    ordered: list[tuple[str, int]] = []
    # SerpAPI results: rank-based base score (higher rank = lower number)
    for i, alt in enumerate([serp_top] + serp_alts if serp_top else serp_alts):
        if alt:
            score = max(0, 5 - i) + _domain_name_score(alt, name)
            ordered.append((alt, score))
    # Slug guesses: low base score, but domain-name match will boost
    for g in guess_domain_urls(name):
        ordered.append((g, _domain_name_score(g, name)))

    # dedup keeping highest score
    best_score: dict[str, int] = {}
    for url, s in ordered:
        if s > best_score.get(url, -1):
            best_score[url] = s
    ranked = sorted(best_score.items(), key=lambda kv: -kv[1])

    # HEAD-prune (cap at 2 live candidates; first is usually right)
    live: list[str] = []
    for url, _ in ranked[:10]:
        if _alive(url):
            live.append(url)
            if len(live) >= 2:
                break

    # Scrape ranked-live candidates
    best_md, best_used, best_url, best_match = "", "", "", -1
    for url in live:
        try:
            md, used = scrape_with_fallback(url, name)
        except Exception:
            md, used = "", url
        if not md:
            continue
        match = name_words_match(name, md)
        if match > best_match:
            best_match, best_md, best_used, best_url = match, md, used, url
        if best_match >= 2 and len(best_md) >= 800:
            break
    return best_url, best_used, best_md


def classify_from_kg_only(name: str, kg_text: str) -> dict:
    """Same prompt, but the 'website excerpt' is replaced by Google KG/answer-box."""
    if not kg_text.strip():
        return {"operational_status": "no_site_found",
                "evidence_strength": "no_evidence",
                "rationale": "no KG / answer-box / AI overview available"}
    user = (
        f"Firm name: {name}\n\n"
        f"Source: Google Knowledge Graph / answer-box / AI overview (NOT the firm's own site):\n---\n{kg_text}\n---\n\n"
        "Return only the JSON object. Treat KG/AI summaries as moderately reliable but not authoritative."
    )
    try:
        raw = llm_call(SYSTEM_PROMPT, user, max_tokens=600)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {"operational_status": "unknown", "evidence_strength": "no_evidence",
                    "rationale": "no JSON in KG response"}
        return json.loads(m.group(0))
    except Exception as e:
        return {"operational_status": "unknown", "evidence_strength": "no_evidence",
                "rationale": f"KG llm error: {e}"}


def classify_from_name_only(name: str) -> dict:
    """No source content — let the LLM infer from name alone (honest abstention if unknown)."""
    user = (
        f"Firm name: {name}\n\n"
        "No source text is available — no website found, no Google KG. "
        "If you genuinely recognise this firm name as a well-known asset manager, "
        "classify based on your knowledge. Otherwise return operational_status='no_site_found', "
        "asset_classes=['unknown'], illiquid_book_likelihood='unknown', and evidence_strength='no_evidence'. "
        "Be conservative — only classify if you have HIGH confidence the firm is a known entity.\n\n"
        "Return only the JSON object."
    )
    try:
        raw = llm_call(SYSTEM_PROMPT, user, max_tokens=600)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {"operational_status": "no_site_found", "evidence_strength": "no_evidence"}
        return json.loads(m.group(0))
    except Exception as e:
        return {"operational_status": "no_site_found", "evidence_strength": "no_evidence",
                "rationale": f"llm error: {e}"}


def _is_strong(result: dict) -> bool:
    return (result.get("operational_status") not in (None, "no_site_found", "unknown")
            and result.get("illiquid_book_likelihood") not in (None, "unknown"))


def _merge_triangulated(name: str, web: dict, kg: dict, name_only: dict) -> tuple[dict, str]:
    """Pick the best result; tag its source. If multiple agree on illiq tier,
    promote evidence_strength to 'multiple_pages_corroborate'.
    """
    strong_web = _is_strong(web)
    strong_kg = _is_strong(kg)
    strong_no = _is_strong(name_only)

    if strong_web:
        # Did KG / name_only agree on illiq tier? Promote confidence.
        agreements = sum(1 for r in (kg, name_only)
                         if r.get("illiquid_book_likelihood") == web.get("illiquid_book_likelihood"))
        result = dict(web)
        if agreements >= 1:
            result["evidence_strength"] = "multiple_pages_corroborate"
        return result, ("consensus_website_plus" if agreements >= 1 else "website")
    if strong_kg and strong_no:
        # Two non-site sources agree?
        if kg.get("illiquid_book_likelihood") == name_only.get("illiquid_book_likelihood"):
            result = dict(kg)
            result["evidence_strength"] = "inferred_from_name_or_parent"
            return result, "consensus_no_site"
        # Disagree — prefer KG (richer source)
        return dict(kg), "conflicted_kg_vs_llm"
    if strong_kg:
        return dict(kg), "serpapi_kg"
    if strong_no:
        return dict(name_only), "llm_on_name"
    # Nobody had a confident answer
    fallback = web if web.get("operational_status") else (kg or name_only)
    return fallback, "none"


_OVERRIDES_PATH = PROJECT_ROOT / "data" / "website_overrides.csv"
_OVERRIDES_CACHE: dict[str, str] | None = None


def _load_overrides() -> dict[str, str]:
    global _OVERRIDES_CACHE
    if _OVERRIDES_CACHE is not None:
        return _OVERRIDES_CACHE
    out: dict[str, str] = {}
    if _OVERRIDES_PATH.exists():
        with _OVERRIDES_PATH.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                ce = (r.get("ceref") or "").strip()
                url = (r.get("corrected_url") or "").strip()
                if ce and url:
                    out[ce] = url
    _OVERRIDES_CACHE = out
    return out


def classify_one(c: dict) -> dict | None:
    legal = c["name_en"]
    natural = c.get("name_natural") or legal
    ceref = c["ceref"]
    name = legal  # legal still used in rows so it's traceable

    # 1st priority: manual override (data/website_overrides.csv) — takes priority
    # over SFC and SerpAPI. Use when you spot a wrong-firm SerpAPI hit.
    overrides = _load_overrides()
    override_site = overrides.get(ceref, "")

    if override_site:
        try:
            md, used_url = scrape_with_fallback(override_site, natural)
        except Exception:
            md, used_url = "", override_site
        site = override_site
    else:
        # 2nd: SFC-registered website (authoritative, no guessing)
        sfc_site = (c.get("website_url_sfc") or "").strip()
        if sfc_site:
            try:
                md, used_url = scrape_with_fallback(sfc_site, natural)
            except Exception:
                md, used_url = "", sfc_site
            site = sfc_site
        else:
            # 3rd: SerpAPI website discovery
            site, used_url, md = find_best_site(natural)
    md_path = ""
    if md:
        md_path = str(save_markdown(ceref, used_url, md).relative_to(PROJECT_ROOT))
    ra_set, ra_list = sfc_ra_types(ceref)
    # LLM gets both legal + natural name for context
    llm_input_name = f"{legal} (commonly: {natural})" if natural != legal else legal

    # Three sources for triangulation
    web_result = classify_with_llm(llm_input_name, md) if md else {
        "operational_status": "no_site_found", "evidence_strength": "no_evidence"
    }
    kg_text = get_kg_summary(natural)
    kg_result = classify_from_kg_only(llm_input_name, kg_text)
    name_only_result = classify_from_name_only(llm_input_name)
    result, source_tag = _merge_triangulated(llm_input_name, web_result, kg_result, name_only_result)

    disambig = disambiguation_status(natural, md)

    # Pull any email addresses the firm publishes on its own pages.
    firm_domain = ""
    if site:
        firm_domain = urlparse(site).netloc.lower().lstrip("www.")
    named_emails, generic_emails = extract_emails_from_md(md or "", firm_domain or None)
    return {
        "ceref": ceref, "name_en": name, "website_url": site or "",
        "name_disambiguation_status": disambig,
        "operational_status": result.get("operational_status", "unknown"),
        "asset_classes": result.get("asset_classes", ["unknown"]),
        "style": result.get("style", "unknown"),
        "sub_strategy": result.get("sub_strategy", ""),
        "illiquid_book_likelihood": result.get("illiquid_book_likelihood", "unknown"),
        "cayman_fund_signals": result.get("cayman_fund_signals"),
        "us_lp_signals": result.get("us_lp_signals"),
        "geographic_focus": result.get("geographic_focus", ""),
        "sector_focus": result.get("sector_focus", []),
        "family_office_type": result.get("family_office_type", "not_applicable"),
        "parent_org": result.get("parent_org", ""),
        "parent_relationship": result.get("parent_relationship", "n_a"),
        "aum_raw_string": result.get("aum_raw_string", ""),
        "aum_currency": result.get("aum_currency", ""),
        "aum_usd_m": result.get("aum_usd_m"),
        "linkedin_url": result.get("linkedin_url", ""),
        "ir_email": result.get("ir_email", ""),
        "emails_on_site": ",".join(named_emails),
        "generic_emails_on_site": ",".join(generic_emails),
        "also_holds_type_1": 1 in ra_set,
        "also_holds_type_4": 4 in ra_set,
        "all_sfc_ra_types": ",".join(str(x) for x in ra_list),
        "evidence_strength": result.get("evidence_strength", "no_evidence"),
        "rationale": result.get("rationale", ""),
        "classification_source": source_tag,
        "serpapi_kg_summary": kg_text[:600],
        "source_markdown_path": md_path,
        "classified_at_utc": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "classifier_version": CLASSIFIER_VERSION,
    }


def write_row_safe(row: dict) -> None:
    """Thread-safe wrapper around write_row."""
    if _WRITE_LOCK:
        with _WRITE_LOCK:
            write_row(row)
    else:
        write_row(row)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--include-inactive", action="store_true")
    ap.add_argument("--ceref", help="Classify a single firm (debug).")
    ap.add_argument("--cerefs", help="Comma-separated list of CE refs (e.g. 'AAB444,ASY362,ARH147').")
    ap.add_argument("--force", action="store_true", help="Re-classify even if in CSV.")
    ap.add_argument("--workers", type=int, default=1, help="Parallel workers (default 1).")
    args = ap.parse_args()

    ensure_gitignore()

    corps_path = latest_corps_csv()
    print(f"Loading corps from {corps_path.name}", file=sys.stderr)
    with corps_path.open(encoding="utf-8-sig") as f:
        corps = list(csv.DictReader(f))

    if args.cerefs:
        wanted = {c.strip() for c in args.cerefs.split(",") if c.strip()}
        corps = [c for c in corps if c["ceref"] in wanted]
    elif args.ceref:
        corps = [c for c in corps if c["ceref"] == args.ceref]
    elif not args.include_inactive:
        corps = [c for c in corps if c["has_active_licence"] == "Y"]

    existing = load_existing()
    todo = []
    skipped = 0
    refresh_reasons: dict[str, str] = {}
    for c in corps:
        ceref = c["ceref"]
        prev = existing.get(ceref)
        if args.force or prev is None:
            todo.append(c)
            continue
        # Already classified — but check if the SFC-registered website has changed
        # since last classification. If so, re-classify (the firm updated their site,
        # which often signals a rebrand / new strategy).
        new_site = (c.get("website_url_sfc") or "").strip().rstrip("/").lower()
        old_site = (prev.get("website_url") or "").strip().rstrip("/").lower()
        if new_site and new_site != old_site:
            todo.append(c)
            refresh_reasons[ceref] = f"website changed: {old_site or '(none)'} → {new_site}"
            continue
        skipped += 1
    if args.limit:
        todo = todo[: args.limit]

    print(f"Will classify {len(todo)} firms "
          f"({skipped} already-classified skipped, {len(refresh_reasons)} re-classifying due to website change).",
          file=sys.stderr)
    for ceref, reason in list(refresh_reasons.items())[:5]:
        print(f"  refresh: {ceref}  ({reason})", file=sys.stderr)

    if args.workers <= 1:
        for i, c in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {c['ceref']} {c['name_en']}", file=sys.stderr)
            row = classify_one(c)
            if row:
                write_row(row)
    else:
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        global _WRITE_LOCK
        _WRITE_LOCK = threading.Lock()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(classify_one, c): c for c in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                c = futures[fut]
                try:
                    row = fut.result()
                except Exception as e:
                    print(f"  [err] {c['ceref']} {c['name_en']}: {e}", file=sys.stderr)
                    continue
                if row:
                    write_row_safe(row)
                if i % 25 == 0 or i == len(todo):
                    print(f"  progress: {i}/{len(todo)}", file=sys.stderr)

    # Dedup CSV: keep latest row per ceref (newest classified_at_utc wins),
    # BUT merge STICKY_FIELDS forward from older rows so downstream-discovered
    # data (e.g. emails added by deep_scrape) survives a re-classification.
    # See workflows/enrichment_cascade.md "never downgrade confidence".
    if OUT_PATH.exists():
        with OUT_PATH.open(encoding="utf-8-sig") as f:
            rdr = csv.DictReader(f)
            fieldnames = list(rdr.fieldnames or [])
            all_rows = list(rdr)
        # Ensure sticky columns exist on every row so the merge step is safe.
        for col in STICKY_FIELDS + STAMPS_CLEARED_ON_WEBSITE_CHANGE:
            if col not in fieldnames:
                fieldnames.append(col)
                for r in all_rows:
                    r.setdefault(col, "")

        # Group rows per-ceref, oldest→newest.
        by_ceref: dict[str, list[dict]] = {}
        for r in all_rows:
            ce = r.get("ceref")
            if not ce:
                continue
            by_ceref.setdefault(ce, []).append(r)
        for ce in by_ceref:
            by_ceref[ce].sort(key=lambda x: x.get("classified_at_utc", ""))

        best: dict[str, dict] = {}
        merged_count = 0
        cleared_count = 0
        for ce, group in by_ceref.items():
            winner = group[-1]   # newest classified_at_utc
            # Detect website change vs immediate predecessor — if so, clear stamps.
            website_changed = False
            if len(group) >= 2:
                prev = group[-2]
                old_url = (prev.get("website_url") or "").strip().rstrip("/").lower()
                new_url = (winner.get("website_url") or "").strip().rstrip("/").lower()
                if old_url and new_url and old_url != new_url:
                    website_changed = True
            # Merge sticky fields forward from older rows when winner's are empty.
            for older in group[:-1]:
                for col in STICKY_FIELDS:
                    if col in STAMPS_CLEARED_ON_WEBSITE_CHANGE and website_changed:
                        # Don't carry stale stamps across a website change.
                        continue
                    new_val = (winner.get(col) or "").strip()
                    old_val = (older.get(col) or "").strip()
                    if not old_val:
                        continue
                    if not new_val:
                        winner[col] = old_val
                        merged_count += 1
                    elif col in ("emails_on_site", "generic_emails_on_site"):
                        # Union the two comma-lists (filter to '@'-bearing tokens).
                        a = {e.strip().lower() for e in new_val.split(",") if "@" in e}
                        b = {e.strip().lower() for e in old_val.split(",") if "@" in e}
                        union = sorted(a | b)
                        if union != sorted(a):
                            winner[col] = ",".join(union)
                            merged_count += 1
            if website_changed:
                for col in STAMPS_CLEARED_ON_WEBSITE_CHANGE:
                    if (winner.get(col) or "").strip():
                        winner[col] = ""
                        cleared_count += 1
            best[ce] = winner

        if len(best) != len(all_rows) or merged_count or cleared_count:
            print(
                f"Dedup: {len(all_rows)} rows → {len(best)} unique cerefs "
                f"(merged {merged_count} sticky fields forward, "
                f"cleared {cleared_count} stamps on website change).",
                file=sys.stderr,
            )
            _atomic_write_csv(OUT_PATH, canonical_fieldnames(fieldnames),
                              [best[ce] for ce in sorted(best.keys())])

    print(f"\nDone.", file=sys.stderr)


if __name__ == "__main__":
    main()

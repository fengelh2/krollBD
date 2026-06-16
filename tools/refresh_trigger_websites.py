"""Refresh website_url + website_accuracy for every open-trigger firm.

Handles three failure modes the bulk enricher can't:
  1. URL on file but accuracy=not_found — re-probe and classify the result
     (could be SSL-broken / wrong-match / parked / actually-fine)
  2. NO URL on file — try DuckDuckGo HTML endpoint to find one, validate
     against the firm name, set + classify
  3. Wrong-match cases (NC.com is NCSoft, not NC Mgmt) — null the URL out
     and stamp a reason so future classify-passes skip it

Output: updates data/strategy_classification.csv in place; appends
data/website_overrides.csv rows for confirmed-wrong matches so the
classifier never re-picks them.

Distinct from classify_strategy.py because:
  - No SerpAPI (it's exhausted) — uses DuckDuckGo HTML endpoint instead
  - Scope is only the firms currently behind open triggers (~40 typical)
  - Classification is rule-based, not LLM — fast + no DeepSeek spend
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRAT_PATH = PROJECT_ROOT / "data" / "strategy_classification.csv"
OVERRIDES_PATH = PROJECT_ROOT / "data" / "website_overrides.csv"
TIMEOUT = 12
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Directory / aggregator hosts to reject as a real corporate site
DIRECTORY_HOSTS = {
    "dnb.com", "ltddir.com", "hkcompany.org", "coltd.hk", "hongkong-corp.com",
    "infobel.com", "webbsite.0xmd.com", "hksecwiki.com", "hk.hksecwiki.com",
    "bloomberg.com", "reuters.com", "wsj.com", "linkedin.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "google.com", "bing.com", "duckduckgo.com", "wikipedia.org",
    "apps.sfc.hk", "sfc.hk", "hkifa.org.hk", "hkvca.com.hk", "aima.org",
    "bvi.gov", "icris.cr.gov.hk", "info.gov.hk",
    "indeed.com", "glassdoor.com", "cpjobs.com",
    "intechopen.com", "hubbis.com", "alphasights.com",
    # Additional directories that slipped through on first run:
    "emis.com", "hkcorporationsearch.com", "companieshouse.hk",
    "thesfcnetwork.com", "databasesets.com", "opendatalei.com",
    "user.databasesets.com", "hkg.databasesets.com",
    "996co.com", "perennialscapital.com",
    "lei.report", "gleif.org", "leireg.com",
    "openleidata.com", "leilookup.com",
    "opencorporates.com", "rocketreach.co", "zoominfo.com",
    "crunchbase.com", "pitchbook.com", "preqin.com",
}

OUTCOMES = {
    "verified": "page resolves + multiple name tokens match",
    "probable": "page resolves + at least one name token matches",
    "browser_only": "page exists but our scraper can't fetch (SSL / 4xx / bot block)",
    "wrong_match": "page resolves but content is unrelated to firm",
    "parked": "page is a parking placeholder",
    "not_found": "no working URL could be confirmed",
}


def _open_trigger_cerefs() -> set[str]:
    out = subprocess.check_output(
        ["gh", "issue", "list", "--repo", "fengelh2/krollBD",
         "--state", "open", "--limit", "200", "--json", "body"],
        text=True, cwd=PROJECT_ROOT,
    )
    cerefs = set()
    for i in json.loads(out):
        m = re.search(r"`([A-Z]{3}\d{3})`", i["body"])
        if m: cerefs.add(m.group(1))
    return cerefs


def _name_tokens(name: str) -> list[str]:
    """Return discriminative tokens from a firm name (>=4 chars, not generic)."""
    stop = {"limited", "ltd", "holdings", "company", "asset", "capital",
            "management", "investment", "investments", "securities", "fund",
            "funds", "partners", "group", "hong", "kong", "china", "asia",
            "asian", "international", "global", "pacific", "hk", "the",
            "and", "co", "advisors", "services", "trust", "wealth",
            "consulting", "corporation"}
    toks = re.findall(r"[a-z]{4,}", (name or "").lower())
    return [t for t in toks if t not in stop]


def _probe_url(url: str, firm_name: str) -> tuple[str, str]:
    """Returns (outcome, evidence)."""
    if not url:
        return ("not_found", "no URL on file")
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA},
                         allow_redirects=True, verify=True)
    except requests.exceptions.SSLError as e:
        # Retry without TLS verification — some real corporate sites have
        # legitimate certs misconfigured (e.g. wrong intermediate chain).
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA},
                             allow_redirects=True, verify=False)
        except Exception as e2:
            return ("browser_only", f"SSL error + insecure retry failed: {e2}")
    except Exception as e:
        return ("browser_only", f"connection error: {e}")

    if r.status_code in (401, 403, 406, 429):
        return ("browser_only", f"HTTP {r.status_code} (likely bot block)")
    if r.status_code >= 400:
        return ("not_found", f"HTTP {r.status_code}")

    body = r.text or ""
    body_clean = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", body, flags=re.I)
    body_text = re.sub(r"<[^>]+>", " ", body_clean).lower()
    body_text = re.sub(r"\s+", " ", body_text)

    if len(body_text) < 200:
        return ("parked", f"body has {len(body_text)} bytes of text (parking page)")

    tokens = _name_tokens(firm_name)
    if not tokens:
        return ("probable", "page resolves; firm name has no discriminative tokens to match")
    hits = [t for t in tokens if t in body_text]
    if len(hits) >= 2:
        return ("verified", f"matches {hits[:3]}")
    if len(hits) == 1:
        return ("probable", f"single token match {hits!r}")
    return ("wrong_match", f"page text doesn't mention {tokens[:3]}")


def _ddg_search(firm_name: str) -> str | None:
    """Find a corporate website via DuckDuckGo HTML endpoint.
    Returns first non-directory non-social result, or None."""
    q = f'"{firm_name}" Hong Kong'
    try:
        r = requests.get("https://html.duckduckgo.com/html/", params={"q": q},
                         timeout=TIMEOUT, headers={"User-Agent": UA})
    except Exception:
        return None
    candidates = []
    for m in re.finditer(r'<a class="result__url"[^>]*href="([^"]+)"', r.text):
        raw = m.group(1)
        # DDG sometimes wraps the real URL in a redirector
        if "uddg=" in raw:
            raw = urllib.parse.unquote(re.sub(r"^.*uddg=([^&]+).*$", r"\1", raw))
        try:
            host = urllib.parse.urlparse(raw if "://" in raw else "https://" + raw).netloc
        except Exception:
            continue
        host = re.sub(r"^www\.", "", host).lower()
        if not host or host in DIRECTORY_HOSTS:
            continue
        if any(host.endswith("." + d) or host == d for d in DIRECTORY_HOSTS):
            continue
        candidates.append(("https://" + host if not raw.startswith("http") else raw, host))
    return candidates[0][0] if candidates else None


def _load_strat() -> tuple[list[dict], list[str]]:
    with STRAT_PATH.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        return list(reader), fieldnames


def _write_strat(rows: list[dict], fieldnames: list[str]) -> None:
    tmp = STRAT_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, STRAT_PATH)


def _append_override(ceref: str, corrected_url: str, reason: str) -> None:
    is_new = not OVERRIDES_PATH.exists()
    with OVERRIDES_PATH.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["ceref", "corrected_url", "reason", "marked_at", "skip_enrichment"])
        w.writerow([ceref, corrected_url, reason, dt.date.today().isoformat(), ""])


def main():
    cerefs = _open_trigger_cerefs()
    print(f"Open-trigger firms: {len(cerefs)}", flush=True)

    rows, fieldnames = _load_strat()
    by_ceref = {r["ceref"]: r for r in rows}

    summary = {"upgraded": 0, "no_change": 0, "wrong_to_null": 0,
               "ddg_found": 0, "ddg_missed": 0, "browser_only": 0}

    for i, ce in enumerate(sorted(cerefs), 1):
        r = by_ceref.get(ce)
        if not r: continue
        name = r.get("name_en", "")
        url = (r.get("website_url") or "").strip()
        acc = (r.get("website_accuracy") or "").strip()
        # Two scopes: URL-but-not-found, and no-URL
        if url and acc == "not_found":
            outcome, evid = _probe_url(url, name)
            print(f"[{i:3}/{len(cerefs)}] {ce} {name[:40]:40} URL: {url[:35]:35} -> {outcome}  ({evid[:60]})", flush=True)
            if outcome == "verified" or outcome == "probable":
                r["website_accuracy"] = outcome
                r["classification_source"] = (r.get("classification_source") or "") + "+website_reverify"
                summary["upgraded"] += 1
            elif outcome == "browser_only":
                r["website_accuracy"] = "browser_only"
                summary["browser_only"] += 1
                # Keep the URL — Hunter can still query the domain
            elif outcome in ("wrong_match", "parked"):
                # Null the URL out + record as override so it isn't re-suggested
                r["website_url"] = ""
                r["website_accuracy"] = "not_found"
                _append_override(ce, "", f"reverify {dt.date.today()}: {outcome} — {evid[:120]}")
                summary["wrong_to_null"] += 1
            else:
                summary["no_change"] += 1
            time.sleep(0.3)
        elif not url:
            print(f"[{i:3}/{len(cerefs)}] {ce} {name[:40]:40} no URL — DDG lookup", flush=True)
            found = _ddg_search(name)
            if found:
                # Validate via probe
                outcome, evid = _probe_url(found, name)
                print(f"          DDG -> {found[:50]} -> {outcome} ({evid[:60]})", flush=True)
                if outcome in ("verified", "probable"):
                    r["website_url"] = found
                    r["website_accuracy"] = outcome
                    r["classification_source"] = (r.get("classification_source") or "") + "+ddg_discovery"
                    summary["ddg_found"] += 1
                else:
                    summary["ddg_missed"] += 1
            else:
                summary["ddg_missed"] += 1
            time.sleep(0.5)

    _write_strat(rows, fieldnames)
    print()
    print("== summary ==")
    for k, v in summary.items():
        print(f"  {k:20} {v}")


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    main()

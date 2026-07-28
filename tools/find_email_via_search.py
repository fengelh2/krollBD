"""On-demand RO-email finder via Google search snippets.

Strategy:
    Google search for `"<Person Name>" "<Firm>" email`
    Email-finder aggregator sites (SignalHire, Hunter, RocketReach, ContactOut,
    Lusha) often surface in results with **partially masked** email addresses
    in their snippets, e.g. `f***.n***@amundi.com`.

    From the mask shape we infer the firm's email format:
        f***.n***@firm.com    →    {first}.{last}@firm.com
        f***@firm.com         →    {first}@firm.com   (or {f}@firm.com — ambiguous)
        flo****.n***@firm.com →    {first}.{last}@firm.com   (long first name)

    Combine the inferred pattern with the RO's parsed first/last names
    (already in our snapshot CSV) → construct the actual email.

Usage:
    from find_email_via_search import find_email_pattern
    result = find_email_pattern("NETO Florian Andre Jean", "Amundi",
                                first_short="Florian", last="Neto",
                                verified_domain="amundi.com")
    # → {"pattern": "{first}.{last}", "domain": "amundi.com",
    #    "constructed": "florian.neto@amundi.com",
    #    "confidence": "high", "snippets": [...]}

Honest caveats:
    - Patterns inferred from masked snippets are usually right but NOT verified deliverable
    - Catch-all domains will accept anything; SMTP probes lie; only sending verifies
    - 1 SerpAPI call per lookup → run on-demand only, not bulk
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # repo root
load_dotenv(PROJECT_ROOT / ".env")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY")


# ---- SerpAPI retry wrapper -------------------------------------------------
# Retries 429 (rate limit) and 5xx with exponential backoff + jitter. Returns
# (data_dict, status). Status is one of: "ok" | "rate_limited" | "server_error"
# | "client_error" | "network_error" | "no_key" — so callers can distinguish
# "empty results" from "couldn't reach SerpAPI" and surface a useful warning.

import random as _random
import time as _time

_SERPAPI_URL = "https://serpapi.com/search.json"
_SERPAPI_MAX_RETRIES = 5  # bumped from 2 — SerpAPI 429 windows are ~15-30s
_SERPAPI_BASE_BACKOFF = 4.0  # seconds, then ×2 with jitter


# Circuit breaker: once SerpAPI returns 429 (monthly-quota exhausted), stop
# calling it for the rest of the process. Weekly publish had been burning
# 3+ hours retrying every call ~2min through exp backoff.
_SERPAPI_QUOTA_EXHAUSTED = False


def _serpapi_get(params: dict, *, timeout: int = 20) -> tuple[dict, str]:
    global _SERPAPI_QUOTA_EXHAUSTED
    if not SERPAPI_KEY:
        return {}, "no_key"
    if _SERPAPI_QUOTA_EXHAUSTED:
        return {}, "rate_limited"
    params = {**params, "api_key": SERPAPI_KEY}
    last_status = "network_error"
    for attempt in range(_SERPAPI_MAX_RETRIES + 1):
        try:
            r = requests.get(_SERPAPI_URL, params=params, timeout=timeout)
        except requests.RequestException as e:
            last_status = "network_error"
            if attempt >= _SERPAPI_MAX_RETRIES:
                print(f"  [serpapi] network error after {attempt+1} attempts: {e}", file=sys.stderr)
                return {}, last_status
            backoff = _SERPAPI_BASE_BACKOFF * (2 ** attempt) + _random.uniform(0, 0.5)
            _time.sleep(backoff)
            continue
        if r.status_code == 429:
            last_status = "rate_limited"
            if attempt >= _SERPAPI_MAX_RETRIES:
                print(f"  [serpapi] 429 rate-limited after {attempt+1} attempts; giving up "
                      f"— tripping circuit breaker for the rest of this process",
                      file=sys.stderr)
                _SERPAPI_QUOTA_EXHAUSTED = True
                return {}, last_status
            backoff = _SERPAPI_BASE_BACKOFF * (2 ** attempt) + _random.uniform(0, 0.5)
            _time.sleep(backoff)
            continue
        if 500 <= r.status_code < 600:
            last_status = "server_error"
            if attempt >= _SERPAPI_MAX_RETRIES:
                print(f"  [serpapi] HTTP {r.status_code} after {attempt+1} attempts; giving up", file=sys.stderr)
                return {}, last_status
            backoff = _SERPAPI_BASE_BACKOFF * (2 ** attempt) + _random.uniform(0, 0.5)
            _time.sleep(backoff)
            continue
        if not r.ok:
            print(f"  [serpapi] HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return {}, "client_error"
        try:
            return r.json(), "ok"
        except ValueError as e:
            print(f"  [serpapi] JSON parse error: {e}", file=sys.stderr)
            return {}, "client_error"
    return {}, last_status

# Matches masked-email patterns inside Google snippets.
# Examples we want to catch:
#   f***.n***@amundi.com
#   florian.n***@amundi.com
#   f****@firm.com
#   florian.neto@firm.com   (sometimes unmasked)
#   F***ian N***@firm.com  (rare; ignore, too noisy)
MASKED_EMAIL_RE = re.compile(
    r"\b([A-Za-z][\w*●·\-_]{1,30}(?:[\.\-_][\w*●·]{1,30})?)@([A-Za-z0-9][A-Za-z0-9.-]{1,60}\.[A-Za-z]{2,})",
    re.UNICODE,
)


GENERIC_LOCALS = {
    "info", "contact", "enquiry", "enquiries", "hello", "support", "hr",
    "compliance", "compliancehk", "regulatory", "legal", "admin", "office",
    "general", "recruitment", "careers", "jobs", "press", "media", "pr",
    "ir", "investorrelations", "investor", "sales", "marketing", "service",
    "services", "customerservice", "client", "clients", "clientservices",
    "feedback", "webmaster", "noreply", "no-reply", "donotreply",
    "team", "office", "operations", "ops",
}


@dataclass
class EmailFinding:
    person_name: str
    firm: str
    pattern: Optional[str] = None         # e.g. "{first}.{last}"
    domain: Optional[str] = None          # e.g. "amundi.com"
    constructed: Optional[str] = None     # e.g. "florian.neto@amundi.com"
    direct_hit: Optional[str] = None      # exact email if found unmasked + matches our person
    generic_inboxes: list[str] = field(default_factory=list)  # info@, compliance@ etc.
    confidence: str = "none"              # high / medium / low / none
    rationale: str = ""
    snippets: list[str] = field(default_factory=list)


def _classify_local_shape(local: str) -> Optional[str]:
    """Given a masked email local-part, return the inferred pattern shape."""
    # Normalize mask chars to *
    norm = local.replace("●", "*").replace("·", "*")
    # Look for {first}.{last} type: two segments separated by . - _
    m = re.fullmatch(r"([A-Za-z]?[a-z*]+)([.\-_])([A-Za-z]?[a-z*]+)", norm)
    if m:
        a, sep, b = m.groups()
        # If first segment is a single char (or letter+stars), it's {f}.{last}
        a_letters = sum(1 for c in a if c != "*")
        b_letters = sum(1 for c in b if c != "*")
        if a_letters == 1 and b_letters >= 1:
            return f"{{f}}{sep}{{last}}"
        if b_letters == 1 and a_letters >= 1:
            return f"{{first}}{sep}{{l}}"
        return f"{{first}}{sep}{{last}}"
    # Single segment (no separator): {first} or {last} alone — ambiguous
    m2 = re.fullmatch(r"[A-Za-z][a-z*]+", norm)
    if m2:
        # If it's just one letter + stars, it's {f} or {l} (ambiguous)
        letters = sum(1 for c in norm if c != "*")
        if letters == 1:
            return "{first}"   # most common single-char convention
        return "{first}"        # treat as {first} when no separator
    return None


def _normalize_local(s: str) -> str:
    """Strip whitespace + punctuation safe for an email local-part."""
    # Remove spaces, apostrophes, hyphens (some firms keep hyphens; keep simple)
    s = re.sub(r"[\s'\-]+", "", s.lower())
    return s


def _apply_pattern(pattern: str, first: str, last: str) -> Optional[str]:
    if not (first or last):
        return None
    f_norm = _normalize_local(first)
    l_norm = _normalize_local(last)
    rep = {
        "{first}": f_norm,
        "{last}": l_norm,
        "{f}": f_norm[:1] if f_norm else "",
        "{l}": l_norm[:1] if l_norm else "",
    }
    out = pattern
    for k, v in rep.items():
        out = out.replace(k, v)
    return out if out and "{" not in out else None


# Aggregator-snippet patterns: explicit format declarations from
# RocketReach / Prospeo / Hunter / SignalHire snippets. Examples:
#   "email format is [first].[last]"
#   "uses 1 email format. The most common is {first name}.{last name}"
#   "first.last (jane.doe@firm.com)"
#   "format: first.last@firm.com"
AGG_PATTERNS = [
    # Bracketed/braced explicit
    (re.compile(r"\[\s*first\s*\]\s*\.\s*\[\s*last\s*\]", re.I),     "{first}.{last}"),
    (re.compile(r"\{\s*first[^}]*\}\s*\.\s*\{\s*last[^}]*\}", re.I), "{first}.{last}"),
    (re.compile(r"\bfirst\s*\.\s*last\b", re.I),                     "{first}.{last}"),
    (re.compile(r"\[\s*f\s*\]\s*\.\s*\[\s*last\s*\]", re.I),         "{f}.{last}"),
    (re.compile(r"\bf\s*\.\s*last\b", re.I),                         "{f}.{last}"),
    (re.compile(r"\[\s*first\s*\]\s*\[\s*last\s*\]", re.I),          "{first}{last}"),
    (re.compile(r"\bfirstlast\b", re.I),                             "{first}{last}"),
    (re.compile(r"\b(?:flast|f_last)\b", re.I),                      "{f}{last}"),
    (re.compile(r"\[\s*first\s*\]_\[\s*last\s*\]", re.I),            "{first}_{last}"),
    # Placeholder-email patterns: "jane.doe@firm.com" / "john.smith@firm.com" / "first.last@firm.com"
    (re.compile(r"\b(?:jane\.doe|john\.doe|first\.last|john\.smith)@", re.I), "{first}.{last}"),
    (re.compile(r"\b(?:jdoe|jsmith)@", re.I),                        "{f}{last}"),
    (re.compile(r"\b(?:janedoe|johnsmith|johndoe)@", re.I),           "{first}{last}"),
]


def _scan_aggregator_pattern(snippets: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Look for explicit pattern declarations from aggregator sites.
    Returns (pattern, evidence_snippet)."""
    for snip in snippets:
        for regex, pattern in AGG_PATTERNS:
            m = regex.search(snip)
            if m:
                return pattern, snip[:300]
    return None, None


def query_firm_pattern(firm_domain: str, serpapi_key: Optional[str] = None) -> tuple[Optional[str], str, list[str]]:
    """One SerpAPI query per firm domain: pull aggregator pattern declarations.
    Returns (pattern, evidence_snippet, all_snippets)."""
    key = serpapi_key or SERPAPI_KEY
    if not key or not firm_domain:
        return None, "", []
    data, status = _serpapi_get({
        "q": f'"{firm_domain}" email format',
        "num": 8, "hl": "en", "gl": "us",
    })
    if status != "ok":
        return None, "", []
    snippets = []
    for item in data.get("organic_results", []):
        for k in ("snippet", "title"):
            v = item.get(k)
            if v: snippets.append(str(v))
    pattern, evidence = _scan_aggregator_pattern(snippets)
    return pattern, (evidence or ""), snippets


def find_email_pattern(
    person_name: str,
    firm: str,
    *,
    first_short: str = "",
    last: str = "",
    verified_domain: str = "",
    serpapi_key: Optional[str] = None,
) -> EmailFinding:
    """Run one SerpAPI query and infer the firm's email format.

    Inputs:
        person_name:     full name as listed (used in search query)
        firm:            natural firm name (used in search query)
        first_short:     parsed first name for constructing the final email
        last:            parsed last name
        verified_domain: the firm's actual domain (from SFC website_url_sfc); if
                         provided, we only trust snippet patterns where the
                         domain matches this — filters out wrong-firm collisions.
        serpapi_key:     override env var
    """
    key = serpapi_key or SERPAPI_KEY
    finding = EmailFinding(person_name=person_name, firm=firm)
    if not key:
        finding.rationale = "no SERPAPI_KEY"
        return finding

    # FAST PATH A: if we have a verified domain, query aggregators for that domain.
    if verified_domain:
        vd = re.sub(r"^www\.|^https?://", "", verified_domain.lower()).split("/")[0]
        if vd and "." in vd:  # guard against malformed input
            agg_pattern, agg_evidence, _ = query_firm_pattern(vd, key)
            if agg_pattern:
                constructed_local = _apply_pattern(agg_pattern, first_short, last)
                if constructed_local:
                    finding.pattern = agg_pattern
                    finding.domain = vd
                    finding.constructed = f"{constructed_local}@{vd}"
                    finding.confidence = "high"
                    finding.rationale = f"aggregator declared firm format: {agg_evidence[:120]}"
                    finding.snippets = [agg_evidence]
                    return finding

    # FAST PATH B: no verified domain — query firm name + "email format" on
    # aggregator-friendly terms. Aggregators surface "<firm> email format on
    # <domain>" pages even when we don't know the domain yet.
    if not verified_domain:
        data2, _status2 = _serpapi_get({
            "q": f'"{firm}" email format site:rocketreach.co OR site:hunter.io OR site:signalhire.com OR site:prospeo.io OR site:contactout.com',
            "num": 8, "hl": "en", "gl": "us",
        })
        snippets2: list[str] = []
        aggregator_domains: list[str] = []  # domains seen in aggregator URLs / snippets
        for item in data2.get("organic_results", []):
            for k in ("snippet", "title"):
                v = item.get(k)
                if v: snippets2.append(str(v))
            # Pull domain from the aggregator page URL itself (RocketReach URLs
            # often look like rocketreach.co/firmname-email-format_<hash>)
            link = (item.get("link") or "").lower()
            for m in re.finditer(r'@([A-Za-z0-9][A-Za-z0-9.-]+\.[A-Za-z]{2,})', (item.get("snippet","") or "")):
                cand = m.group(1)
                if cand not in aggregator_domains:
                    aggregator_domains.append(cand)
        agg_pattern, agg_evidence = _scan_aggregator_pattern(snippets2)
        if agg_pattern and aggregator_domains:
            # Cross-check the discovered domain against the firm name (rough slug match)
            firm_slug = re.sub(r"[^a-z0-9]", "", firm.lower())[:12]
            best_dom = next((d for d in aggregator_domains
                             if firm_slug and firm_slug[:6] in d.replace(".", "")), None)
            if best_dom:
                constructed_local = _apply_pattern(agg_pattern, first_short, last)
                if constructed_local:
                    finding.pattern = agg_pattern
                    finding.domain = best_dom
                    finding.constructed = f"{constructed_local}@{best_dom}"
                    finding.confidence = "high"
                    finding.rationale = f"aggregator pattern + discovered domain matches firm: {agg_evidence[:100]}"
                    finding.snippets = [agg_evidence] if agg_evidence else snippets2[:3]
                    return finding

    query = f'"{person_name}" "{firm}" email'
    try:
        r = requests.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": key, "num": 10, "hl": "en", "gl": "hk"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        finding.rationale = f"serpapi error: {e}"
        return finding

    # Pull snippets + titles
    snippets: list[str] = []
    for item in data.get("organic_results", []):
        for k in ("snippet", "title", "snippet_highlighted_words"):
            v = item.get(k)
            if isinstance(v, list):
                snippets.extend(str(x) for x in v)
            elif v:
                snippets.append(str(v))
    # Also check answer_box and knowledge_graph if present
    if data.get("answer_box"):
        ab = data["answer_box"]
        for k in ("snippet", "answer"):
            if ab.get(k):
                snippets.append(str(ab[k]))

    finding.snippets = snippets[:6]  # cap for sanity

    # Categorise every email-shaped match into:
    #   (a) masked   → use mask shape to infer pattern  [primary signal]
    #   (b) direct   → unmasked AND local matches our person's first/last  [direct hit]
    #   (c) generic  → unmasked, local is info/compliance/etc. for the firm
    #   (d) other    → ignore (someone else's email)
    vd = ""
    if verified_domain:
        vd = re.sub(r"^www\.|^https?://", "", verified_domain.lower()).split("/")[0]
    # Build a name-slug for loose domain matching when verified_domain is empty
    firm_slug = re.sub(r"[^a-z0-9]", "", firm.lower())[:8]
    def domain_matches(dom: str) -> bool:
        if vd:
            return dom == vd or dom.endswith("." + vd) or vd.endswith("." + dom)
        # No verified domain — accept only if domain contains the firm's name slug
        # (filters out hotmail.com, fangdalaw.com etc. when SFC has no website on file)
        if firm_slug and len(firm_slug) >= 4:
            dom_clean = dom.replace(".", "").replace("-", "")
            return firm_slug[:4] in dom_clean
        return False  # safer to abstain when we have nothing to verify against

    first_l = (first_short or "").lower()
    last_l = (last or "").lower()
    first_initial = first_l[:1] if first_l else ""
    last_initial = last_l[:1] if last_l else ""

    masked_candidates: list[tuple[str, str, str, int]] = []
    direct_hits: list[str] = []
    generics: list[str] = []
    seen = set()
    for snip in snippets:
        for m in MASKED_EMAIL_RE.finditer(snip):
            local, dom = m.group(1).lower(), m.group(2).lower()
            if (local, dom) in seen:
                continue
            seen.add((local, dom))
            has_mask = "*" in local or "●" in local
            local_alpha = local.replace("*", "").replace("●", "").replace(".", "").replace("-", "").replace("_", "")

            if not has_mask:
                # Unmasked — classify
                if local in GENERIC_LOCALS or any(g in local for g in ("compliance", "info", "contact", "enquiry")):
                    if domain_matches(dom):
                        generics.append(f"{local}@{dom}")
                    continue
                # Direct hit only if local matches the person's name parts AND domain matches
                # (e.g. local == "florian.neto" / "fneto" / "florian" + person is Florian Neto + domain is amundi.com)
                if not domain_matches(dom):
                    continue
                possible = {
                    f"{first_l}.{last_l}", f"{first_l}{last_l}",
                    f"{first_initial}{last_l}", f"{first_l}{last_initial}",
                    f"{last_l}.{first_l}", first_l, last_l,
                    f"{first_l}_{last_l}",
                }
                if local in possible:
                    direct_hits.append(f"{local}@{dom}")
                # else: probably someone else's email at the same firm — ignore
                continue

            # Masked email — primary pattern signal
            pat = _classify_local_shape(local)
            if not pat:
                continue
            score = 0
            if domain_matches(dom):
                score += 100
            else:
                score -= 30
            if "{first}" in pat and "{last}" in pat:
                score += 10
            masked_candidates.append((local, dom, pat, score))

    finding.generic_inboxes = sorted(set(generics))

    if direct_hits:
        # Highest possible confidence — we literally found the person's email
        finding.direct_hit = direct_hits[0]
        finding.constructed = direct_hits[0]
        finding.pattern = "direct"
        finding.domain = direct_hits[0].split("@", 1)[1]
        finding.confidence = "very_high"
        finding.rationale = "person's email found unmasked in snippets"
        return finding

    if masked_candidates:
        masked_candidates.sort(key=lambda x: -x[3])
        best_local, best_domain, best_pattern, best_score = masked_candidates[0]
        final_domain = vd or best_domain
        finding.pattern = best_pattern
        finding.domain = final_domain
        constructed_local = _apply_pattern(best_pattern, first_short, last)
        if constructed_local:
            finding.constructed = f"{constructed_local}@{final_domain}"
        if best_score >= 100:
            finding.confidence = "high"
            finding.rationale = "masked-email pattern in snippet matches verified firm domain"
        else:
            finding.confidence = "low"
            finding.rationale = "masked-email pattern found but domain doesn't match verified firm"
        return finding

    if generics:
        finding.rationale = f"no personal pattern found; {len(generics)} generic inbox(es) captured"
        return finding

    finding.rationale = "no email-shaped patterns found in snippets"
    return finding


# ---------------- CLI test harness ----------------

if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--person", required=True, help='e.g. "NETO Florian Andre Jean"')
    ap.add_argument("--firm", required=True, help='e.g. "Amundi"')
    ap.add_argument("--first-short", default="", help="parsed first name (for construction)")
    ap.add_argument("--last", default="", help="parsed last name")
    ap.add_argument("--domain", default="", help="verified domain (from SFC)")
    args = ap.parse_args()

    result = find_email_pattern(
        args.person, args.firm,
        first_short=args.first_short, last=args.last,
        verified_domain=args.domain,
    )
    out = {
        "person": result.person_name, "firm": result.firm,
        "pattern": result.pattern, "domain": result.domain,
        "constructed": result.constructed,
        "direct_hit": result.direct_hit,
        "generic_inboxes": result.generic_inboxes,
        "confidence": result.confidence,
        "rationale": result.rationale,
        "snippets_sampled": result.snippets,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))

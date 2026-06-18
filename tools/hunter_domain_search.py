"""Hunter.io domain-search bulk lookup for open-trigger firms.

Per firm: one /domain-search call → list of every email Hunter knows at
that domain (up to 25 by default). For each open R1 trigger, we then
match the RO's name against the returned list. Anything that matches by
first-name OR last-name gets added to the trigger's hunter_hits, then
AbstractAPI-verified.

Why this beats /email-finder on a per-RO basis:
  - /email-finder only succeeds when Hunter has the EXACT (name, domain)
    pair indexed. For HK boutique ROs (often Western nickname + Chinese
    given name) this misses a lot.
  - /domain-search returns ALL of Hunter's people at the firm; we apply
    our own name-matching, which is more forgiving (last name match,
    initial+lastname, etc.).

Cost: 1 Hunter search per unique domain. AbstractAPI verifications for
any matches found.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
import hunter_io
import email_verifier

DOMAIN_CACHE = PROJECT_ROOT / "data" / "hunter_domain_cache.json"
TIMEOUT = 30


def _load_cache() -> dict:
    if not DOMAIN_CACHE.exists():
        return {}
    return json.loads(DOMAIN_CACHE.read_text(encoding="utf-8"))


def _save_cache(c: dict) -> None:
    DOMAIN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    DOMAIN_CACHE.write_text(json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8")


def domain_search(domain: str) -> dict:
    """Call /v2/domain-search via the active Hunter key. Returns the data
    dict from Hunter, or {} on failure / quota exhaustion."""
    if not domain:
        return {}
    cache = _load_cache()
    if domain in cache:
        return cache[domain]
    key, _ = hunter_io.pick_active_key()
    if not key:
        return {"_err": "no Hunter key with quota available"}
    try:
        r = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": key},
            timeout=TIMEOUT,
        )
    except Exception as e:
        return {"_err": f"network: {e}"}
    if r.status_code == 402:
        return {"_err": "quota_exhausted"}
    if not r.ok:
        return {"_err": f"HTTP {r.status_code}"}
    data = (r.json() or {}).get("data") or {}
    cache[domain] = data
    _save_cache(cache)
    return data


def _name_match(ro_first_short: str, ro_first_full: str, ro_last: str,
                hunter_first: str, hunter_last: str, hunter_email: str) -> str:
    """Score how confidently a Hunter contact matches our RO.
    Returns: 'high' / 'medium' / 'low' / '' (no match)."""
    rfs = (ro_first_short or "").lower().strip()
    rff = (ro_first_full or "").lower().strip()
    rl = (ro_last or "").lower().strip()
    hf = (hunter_first or "").lower().strip()
    hl = (hunter_last or "").lower().strip()
    em = (hunter_email or "").lower().strip()
    em_local = em.split("@", 1)[0]

    # Strong: both first and last match
    if rl and hl and rl == hl:
        if rfs and hf and rfs == hf: return "high"
        if rff and hf and rff == hf: return "high"
        # Same surname but different first names — still suspicious-good
        if hf and rfs and (hf.startswith(rfs) or rfs.startswith(hf)):
            return "medium"
        # Last name match only → check local part for first-name signal
        if rfs and rfs in em_local: return "high"
        if rff and rff.split()[0] in em_local: return "medium"
        return "low"
    # First name match only — too noisy unless local part also contains last
    if rfs and hf and rfs == hf and rl and rl in em_local:
        return "medium"
    return ""


def main():
    out = subprocess.check_output(
        ["gh", "issue", "list", "--repo", "fengelh2/krollBD",
         "--state", "open", "--limit", "200", "--json", "body,title"],
        text=True, cwd=PROJECT_ROOT,
    )
    open_triggers = []
    for i in json.loads(out):
        m = re.search(r"TRIGGER_ID: ([A-Z]\d?-[A-Z0-9\-]+)", i["body"])
        if m:
            open_triggers.append(m.group(1))

    # Build per-trigger context
    ro_idx = {}
    with (PROJECT_ROOT / "data/snapshots/sfc_t9_corp_ros_latest.csv").open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ro_idx[r["ro_ceref"]] = r

    sfc_contacts = {}
    with (PROJECT_ROOT / "data/sfc_corp_contacts.csv").open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sfc_contacts[r["ceref"]] = r

    new_hits = []
    new_emails_seen = set()
    domains_searched = set()

    for tid in open_triggers:
        path = PROJECT_ROOT / "data" / "issue_meta" / f"{tid}.json"
        if not path.exists():
            continue
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("hunter_hits"):
            continue  # already have a Hunter result
        ce = meta.get("ceref", "")
        site = (sfc_contacts.get(ce, {}).get("website") or "").strip()
        if not site:
            continue
        host = urlparse(site if "://" in site else "https://" + site).netloc
        domain = re.sub(r"^www\.", "", host).lower()
        if not domain:
            continue

        if domain not in domains_searched:
            domains_searched.add(domain)
            print(f"[search] {tid} {ce}  domain={domain}", flush=True)
            data = domain_search(domain)
            if "_err" in data:
                print(f"  error: {data['_err']}", flush=True)
                continue
        else:
            cache = _load_cache()
            data = cache.get(domain, {})

        contacts = data.get("emails") or []
        if not contacts:
            continue

        ros = meta.get("ros") or meta.get("ros_current") or []
        for r in ros:
            row = ro_idx.get(r.get("ceref")) or {}
            rfs = row.get("ro_first_short", "")
            rff = row.get("ro_first_full", "")
            rl = row.get("ro_last", "")
            if not (rfs or rff) or not rl:
                continue
            for c in contacts:
                em = (c.get("value") or "").lower().strip()
                if not em or em in new_emails_seen:
                    continue
                conf = _name_match(rfs, rff, rl,
                                   c.get("first_name", ""), c.get("last_name", ""),
                                   em)
                # Skip surname-only "low" matches — different person, same family
                # name (Chen Wang vs Yingsi Wang). Need at least medium confidence.
                if conf not in ("high", "medium"):
                    continue
                # Got a match — record it
                hit = {
                    "email": em,
                    "score": c.get("confidence"),
                    "ro": r["name"],
                    "fetched_at_utc": dt.datetime.now(dt.UTC).isoformat(),
                    "match_confidence": conf,
                    "hunter_position": c.get("position", ""),
                    "hunter_seniority": c.get("seniority", ""),
                    "hunter_first_name": c.get("first_name", ""),
                    "hunter_last_name": c.get("last_name", ""),
                    "source": "domain_search",
                }
                # AbstractAPI verify
                v = email_verifier.verify(em)
                if v:
                    hit["abstract_verdict"] = v.get("status", "")
                    hit["abstract_detail"] = v.get("status_detail", "")
                # Persist into the trigger meta
                meta.setdefault("hunter_hits", []).append(hit)
                ec = meta.get("email_candidates") or []
                if not any((c2.get("email") or "").lower() == em for c2 in ec):
                    ec.insert(0, {
                        "email": em,
                        "kind": "hunter_io",
                        "confidence": "hunter_verified" if hit.get("abstract_verdict") == "deliverable"
                                      else ("medium" if conf in ("high", "medium") else "low"),
                        "ro": r["name"],
                        "score": c.get("confidence"),
                        "abstract_verdict": hit.get("abstract_verdict"),
                        "evidence": f"Hunter domain-search · {conf}-confidence name match · {hit.get('abstract_verdict','')}",
                    })
                meta["email_candidates"] = ec
                new_hits.append((tid, r["name"], em, conf, hit.get("abstract_verdict", "")))
                new_emails_seen.add(em)

        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n=== summary ===")
    print(f"domains searched: {len(domains_searched)}")
    print(f"new RO matches:   {len(new_hits)}")
    for tid, ro, em, conf, abs_v in new_hits:
        print(f"  {tid:25} {ro:35} {em:40} match={conf} abstract={abs_v}")


if __name__ == "__main__":
    main()

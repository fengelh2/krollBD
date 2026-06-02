"""On-demand enrichment helper.

Invoked by .github/workflows/enrich-on-demand.yml when the dashboard
fires a repository_dispatch event. Two modes:

  --mode firm
    Reads the firm's just-scraped on-site emails from
    strategy_classification.csv (emails_on_site + generic_emails_on_site)
    and merges them into data/issue_meta/<trigger_id>.json under
    `email_candidates` as kind=observed_on_site / generic_on_site
    (confidence=verified). The scrape itself is run by the workflow
    step above this one.

  --mode ros
    For each RO listed in the meta, runs hunter_io.find_email() against
    the firm's verified domain, then email_verifier.verify() on any
    hits. Persists results into the meta as hunter_hits + upgrades the
    matching email_candidates entry to kind=hunter_io,
    confidence=hunter_verified (or low if AbstractAPI says
    undeliverable). Quota-aware via the existing round-robin in
    hunter_io.py.

The dashboard hide-guesses-when-verified rule (in triggers.js) then
auto-suppresses the now-redundant pattern guesses for the same RO.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hunter_io  # noqa
import email_verifier  # noqa

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _meta_path(trigger_id: str) -> Path:
    return PROJECT_ROOT / "data" / "issue_meta" / f"{trigger_id}.json"


def _load_strategy_row(ceref: str) -> dict:
    p = PROJECT_ROOT / "data" / "strategy_classification.csv"
    with p.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["ceref"] == ceref:
                return r
    return {}


def _load_ro_index() -> dict:
    p = PROJECT_ROOT / "data" / "snapshots" / "sfc_t9_corp_ros_latest.csv"
    idx = {}
    if p.exists():
        with p.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                idx[r["ro_ceref"]] = r
    return idx


def _split_emails(s: str) -> list[str]:
    if not s:
        return []
    return [e.strip().lower() for e in re.split(r"[\s,;]+", s) if "@" in e]


def _now_utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def inject_firm_emails(trigger_id: str, ceref: str) -> dict:
    """Merge strategy_classification.csv's emails_on_site columns into meta."""
    p = _meta_path(trigger_id)
    if not p.exists():
        return {"ok": False, "err": f"meta file not found: {p}"}
    meta = json.load(p.open(encoding="utf-8"))
    strat = _load_strategy_row(ceref)
    named = _split_emails(strat.get("emails_on_site", ""))
    generics = _split_emails(strat.get("generic_emails_on_site", ""))
    ec = meta.get("email_candidates") or []
    existing = {(c.get("email") or "").lower() for c in ec}

    added = 0
    # Named go just below any verified entries
    for em in named:
        if em in existing:
            continue
        # find insertion point: after last "verified"/"hunter_verified" entry
        insert_at = 0
        for i, c in enumerate(ec):
            if (c.get("confidence") or "").lower() in ("hunter_verified", "verified", "very_high", "high"):
                insert_at = i + 1
            else:
                break
        ec.insert(insert_at, {
            "email": em, "kind": "observed_on_site", "confidence": "verified",
            "evidence": f"extracted from {strat.get('website_url','firm site')} contact pages",
        })
        existing.add(em); added += 1
    # Generic-on-site stay below named but above guesses
    for em in generics:
        if em in existing:
            continue
        ec.append({
            "email": em, "kind": "generic_on_site", "confidence": "verified",
            "evidence": f"generic inbox on {strat.get('website_url','firm site')}",
        })
        existing.add(em); added += 1

    meta["email_candidates"] = ec
    meta["deep_scrape_attempted_utc"] = _now_utc()
    if added == 0:
        meta["deep_scrape_note"] = "site scraped — no new emails found"
    json.dump(meta, p.open("w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return {"ok": True, "added": added, "named": named, "generic": generics}


def find_ros(trigger_id: str, ceref: str) -> dict:
    """Hunter find + AbstractAPI verify for every parsed RO on this trigger."""
    p = _meta_path(trigger_id)
    if not p.exists():
        return {"ok": False, "err": f"meta file not found: {p}"}
    meta = json.load(p.open(encoding="utf-8"))
    strat = _load_strategy_row(ceref)
    site = (strat.get("website_url") or "").strip()
    if not site:
        return {"ok": False, "err": "no verified website on file"}
    host = urlparse(site if "://" in site else "https://" + site).netloc
    domain = re.sub(r"^www\.", "", host or "")
    if not domain:
        return {"ok": False, "err": "could not extract domain from website_url"}

    ro_idx = _load_ro_index()
    ros = meta.get("ros") or meta.get("ros_current") or []
    if not ros:
        return {"ok": False, "err": "no ROs on this trigger"}

    ec = meta.get("email_candidates") or []
    hunter_hits = meta.get("hunter_hits") or []
    results = []

    for r in ros:
        row = ro_idx.get(r["ceref"]) or {}
        first = (row.get("ro_first_short") or row.get("ro_first_full") or "").strip()
        last = (row.get("ro_last") or "").strip()
        if not (first and last):
            results.append({"ro": r["name"], "status": "skipped_no_name"})
            continue
        hunter_resp = hunter_io.find_email(domain, first, last)
        st = (hunter_resp or {}).get("status")
        em = (hunter_resp or {}).get("email")
        sc = (hunter_resp or {}).get("score")
        if st != "ok" or not em:
            results.append({"ro": r["name"], "status": st, "email": None})
            continue
        verify = email_verifier.verify(em)
        verdict = (verify or {}).get("status")
        detail = (verify or {}).get("status_detail")
        results.append({"ro": r["name"], "status": "ok", "email": em,
                        "score": sc, "abstract": verdict, "detail": detail})

        if not any((h.get("email") or "").lower() == em.lower() for h in hunter_hits):
            hunter_hits.append({
                "email": em, "score": sc, "ro": r["name"],
                "fetched_at_utc": _now_utc(),
                "abstract_verdict": verdict, "abstract_detail": detail,
            })

        existing = next((c for c in ec if (c.get("email") or "").lower() == em.lower()), None)
        new_conf = "hunter_verified" if verdict != "undeliverable" else "low"
        evid = f"Hunter score {sc} + AbstractAPI: {verdict}"
        if existing:
            existing["kind"] = "hunter_io"
            existing["confidence"] = new_conf
            existing["ro"] = r["name"]
            existing["score"] = sc
            existing["abstract_verdict"] = verdict
            existing["abstract_detail"] = detail
            existing["evidence"] = evid
            if verdict == "undeliverable":
                existing["flag"] = "abstractapi_says_undeliverable"
            ec.remove(existing); ec.insert(0, existing)
        else:
            ec.insert(0, {
                "email": em, "kind": "hunter_io", "confidence": new_conf,
                "ro": r["name"], "score": sc,
                "abstract_verdict": verdict, "abstract_detail": detail,
                "evidence": evid,
            })

    meta["hunter_hits"] = hunter_hits
    meta["email_candidates"] = ec
    meta["hunter_attempted_utc"] = _now_utc()
    json.dump(meta, p.open("w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return {"ok": True, "domain": domain, "results": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger-id", required=True)
    ap.add_argument("--ceref", required=True)
    ap.add_argument("--mode", required=True, choices=["firm", "ros"])
    args = ap.parse_args()

    if args.mode == "firm":
        out = inject_firm_emails(args.trigger_id, args.ceref)
    else:
        out = find_ros(args.trigger_id, args.ceref)

    print(json.dumps(out, indent=2, ensure_ascii=False))
    if not out.get("ok"):
        sys.exit(2)


if __name__ == "__main__":
    main()

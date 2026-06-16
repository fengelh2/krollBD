"""Merge SFC-filed contacts (from data/sfc_corp_contacts.csv) into
trigger meta files + strategy_classification.csv, with cross-check
protection that NEVER overwrites verified Hunter / on-site data.

For each open trigger:
  - If meta has no email_candidates with kind=sfc_filed, insert SFC's
    filed email as top priority (confidence=verified, kind=sfc_filed)
  - Cross-check existing hunter_hits: if a hunter email's domain doesn't
    match SFC's filed website domain -> flag as 'domain_mismatch_with_sfc'
    and demote to low confidence (catches alex@intechopen.com style)
  - Cross-check existing observed_on_site emails: if domain doesn't match
    SFC's filed website -> flag as 'scraped_from_unrelated_site' (likely
    wrong website was scraped)

For strategy_classification.csv:
  - Fill website_url + accuracy='sfc_filed' for firms with empty website
  - When existing website_url disagrees with SFC's, prefer SFC + log a
    'sfc_conflict' note on the row for auditing
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SFC_CONTACTS = PROJECT_ROOT / "data" / "sfc_corp_contacts.csv"
STRAT_PATH = PROJECT_ROOT / "data" / "strategy_classification.csv"
META_DIR = PROJECT_ROOT / "data" / "issue_meta"


def _host(url: str) -> str:
    if not url: return ""
    if "://" not in url and not url.startswith("//"):
        url = "https://" + url
    try:
        h = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", h)
    except Exception:
        return ""


def _email_host(em: str) -> str:
    return (em or "").split("@", 1)[-1].lower().strip()


def _load_sfc() -> dict[str, dict]:
    out = {}
    if not SFC_CONTACTS.exists(): return out
    with SFC_CONTACTS.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[r["ceref"]] = r
    return out


def _open_trigger_ids() -> set[str]:
    out = subprocess.check_output(
        ["gh", "issue", "list", "--repo", "fengelh2/krollBD",
         "--state", "open", "--limit", "200", "--json", "body"],
        text=True, cwd=PROJECT_ROOT,
    )
    ids = set()
    for i in json.loads(out):
        m = re.search(r"TRIGGER_ID: ([A-Z]\d?-[A-Z0-9\-]+)", i["body"])
        if m: ids.add(m.group(1))
    return ids


def merge_into_meta(open_only: bool = True) -> dict:
    sfc = _load_sfc()
    open_ids = _open_trigger_ids() if open_only else None
    stats = {"meta_files_touched": 0, "sfc_emails_inserted": 0,
             "sfc_websites_recorded": 0, "hunter_demoted_domain_mismatch": 0,
             "onsite_flagged_domain_mismatch": 0, "skipped_no_sfc_data": 0}

    for path in sorted(META_DIR.glob("*.json")):
        tid = path.stem
        if open_only and tid not in open_ids: continue
        meta = json.load(path.open(encoding="utf-8"))
        ce = meta.get("ceref", "")
        s = sfc.get(ce)
        if not s or s.get("fetch_status") != "ok":
            stats["skipped_no_sfc_data"] += 1
            continue

        sfc_email = (s.get("email") or "").lower().strip()
        sfc_website = (s.get("website") or "").strip()
        sfc_web_host = _host(sfc_website)
        changed = False

        ec = meta.get("email_candidates") or []

        # 1) Insert SFC-filed email at top (highest priority)
        if sfc_email and not any(
            c.get("kind") == "sfc_filed" and (c.get("email") or "").lower() == sfc_email
            for c in ec
        ):
            ec.insert(0, {
                "email": sfc_email,
                "kind": "sfc_filed",
                "confidence": "verified",
                "evidence": "filed with SFC by the firm at licensing",
                "fetched_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            })
            stats["sfc_emails_inserted"] += 1
            changed = True

        # Domains are equivalent if either is a suffix of the other (handles
        # corporate setups like hk.allianzgi.com website + allianzgi.com email).
        def _hosts_match(a: str, b: str) -> bool:
            if not a or not b: return False
            if a == b: return True
            return a.endswith("." + b) or b.endswith("." + a)

        # 2) Cross-check hunter_hits: domain mismatch with SFC website?
        if sfc_web_host:
            for h in (meta.get("hunter_hits") or []):
                eh = _email_host(h.get("email", ""))
                if eh and not _hosts_match(eh, sfc_web_host):
                    h["flag"] = "domain_mismatch_with_sfc_filed_website"
                    h["sfc_filed_website"] = sfc_website
                    # Also demote matching email_candidates entry
                    for c in ec:
                        if (c.get("email") or "").lower() == (h.get("email") or "").lower():
                            c["confidence"] = "low"
                            c["flag"] = "hunter_domain_mismatch_with_sfc"
                            c["evidence"] = (c.get("evidence", "") +
                                f" · WARN: SFC has {sfc_website} on file, this email is at {eh}").strip()
                    stats["hunter_demoted_domain_mismatch"] += 1
                    changed = True

            # 3) Cross-check observed_on_site emails
            for c in ec:
                if c.get("kind") == "observed_on_site":
                    eh = _email_host(c.get("email", ""))
                    if eh and not _hosts_match(eh, sfc_web_host):
                        c["flag"] = "scraped_from_unrelated_site"
                        c["confidence"] = "low"
                        c["evidence"] = (c.get("evidence", "") +
                            f" · WARN: SFC has {sfc_website}, this was scraped from {eh}").strip()
                        stats["onsite_flagged_domain_mismatch"] += 1
                        changed = True

        # 4) Record SFC's website on the meta (informational; doesn't replace)
        if sfc_website and meta.get("sfc_filed_website") != sfc_website:
            meta["sfc_filed_website"] = sfc_website
            stats["sfc_websites_recorded"] += 1
            changed = True

        if changed:
            meta["email_candidates"] = ec
            json.dump(meta, path.open("w", encoding="utf-8"), indent=2, ensure_ascii=False)
            stats["meta_files_touched"] += 1

    return stats


def merge_into_strategy() -> dict:
    """Fill empty website_url in strategy_classification.csv from SFC.
    Log conflicts where existing URL differs from SFC's."""
    sfc = _load_sfc()
    with STRAT_PATH.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
        fns = list(rows[0].keys())

    stats = {"filled_from_sfc": 0, "agreed": 0, "conflicts": 0,
             "no_sfc_website": 0}
    conflicts = []

    for r in rows:
        ce = r["ceref"]
        s = sfc.get(ce)
        if not s or s.get("fetch_status") != "ok":
            continue
        sfc_website = (s.get("website") or "").strip()
        if not sfc_website:
            stats["no_sfc_website"] += 1
            continue
        sfc_host = _host(sfc_website)
        cur_url = (r.get("website_url") or "").strip()
        cur_host = _host(cur_url)

        if not cur_url:
            r["website_url"] = sfc_website if "://" in sfc_website else "https://" + sfc_website
            r["website_accuracy"] = "sfc_filed"
            r["classification_source"] = "sfc_filed"
            stats["filled_from_sfc"] += 1
        elif cur_host == sfc_host:
            stats["agreed"] += 1
        else:
            # Conflict: prefer SFC, log the discrepancy
            r["website_url"] = sfc_website if "://" in sfc_website else "https://" + sfc_website
            r["website_accuracy"] = "sfc_filed"
            r["classification_source"] = (r.get("classification_source") or "") + "+sfc_override"
            stats["conflicts"] += 1
            conflicts.append((ce, r.get("name_en", ""), cur_url, sfc_website))

    # Atomic write
    tmp = str(STRAT_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fns); w.writeheader()
        for r in rows: w.writerow({k: r.get(k, "") for k in fns})
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, STRAT_PATH)
    return stats, conflicts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="merge into all meta files (default: open triggers only)")
    ap.add_argument("--skip-strategy", action="store_true",
                    help="skip updating strategy_classification.csv")
    args = ap.parse_args()

    print("=== merging SFC contacts into trigger meta ===")
    s1 = merge_into_meta(open_only=not args.all)
    for k, v in s1.items():
        print(f"  {k:40} {v}")

    if not args.skip_strategy:
        print("\n=== merging SFC contacts into strategy_classification.csv ===")
        s2, conflicts = merge_into_strategy()
        for k, v in s2.items():
            print(f"  {k:40} {v}")
        if conflicts:
            print(f"\n  -- {len(conflicts)} website-URL conflicts (prefer SFC, log for review) --")
            for ce, n, old, new in conflicts[:30]:
                print(f"    {ce} {n[:35]:35}  was: {old[:35]:35} -> {new}")


if __name__ == "__main__":
    main()

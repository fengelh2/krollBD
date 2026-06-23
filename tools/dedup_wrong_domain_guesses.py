"""For every open-trigger meta file: determine the authoritative domain
from the highest-confidence verified email candidate (sfc_filed >
hunter_io > observed_on_site > generic_on_site, all at verified+
confidence). Drop any pattern guesses (ro_guess / inferred_pattern /
generic_guess) at a different host. Regenerate fresh per-RO + generic
guesses at the authoritative domain.

Fixes a class of bug where the publisher generates guesses at a
firm-name-derived domain (e.g. optimusprimeasset.com.hk) but the real
firm domain (from SFC's filed email) is different
(optimusprimefund.com).

Runs as a post-step in weekly.yml so newly published triggers are
clean from the start; can also be invoked ad-hoc.
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from publish_triggers_to_github import parse_name, email_patterns

VERIFIED_CONFS = ("hunter_verified", "verified", "very_high", "high")
VERIFIED_KINDS = ("sfc_filed", "hunter_io", "observed_on_site", "generic_on_site")
GUESS_KINDS = {"ro_guess", "inferred_pattern", "generic_guess"}


def _email_host(em: str) -> str:
    return (em or "").split("@", 1)[-1].lower().strip()


def _hosts_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return a.endswith("." + b) or b.endswith("." + a)


def _load_sfc_contacts() -> dict[str, dict]:
    p = PROJECT_ROOT / "data" / "sfc_corp_contacts.csv"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8-sig") as f:
        return {r["ceref"]: r for r in csv.DictReader(f)}


def _load_ro_idx() -> dict[str, dict]:
    p = PROJECT_ROOT / "data" / "snapshots" / "sfc_t9_corp_ros_latest.csv"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8-sig") as f:
        return {r["ro_ceref"]: r for r in csv.DictReader(f)}


def _open_trigger_ids() -> set[str]:
    out = subprocess.check_output(
        ["gh", "issue", "list", "--repo", "fengelh2/krollBD",
         "--state", "open", "--limit", "200", "--json", "body"],
        text=True, cwd=PROJECT_ROOT,
    )
    ids = set()
    for i in json.loads(out):
        m = re.search(r"TRIGGER_ID: ([A-Z]\d?-[A-Z0-9\-]+)", i["body"])
        if m:
            ids.add(m.group(1))
    return ids


def _authoritative_domain(meta: dict, sfc_row: dict) -> str | None:
    ec = meta.get("email_candidates") or []
    for kind in VERIFIED_KINDS:
        for c in ec:
            if c.get("kind") != kind:
                continue
            if (c.get("confidence") or "").lower() not in VERIFIED_CONFS:
                continue
            h = _email_host(c.get("email", ""))
            if h:
                return h
    # Fallback: SFC-filed website (host only) when no verified email exists
    site = (sfc_row.get("website") or "").strip()
    if site:
        host = urlparse(site if "://" in site else "https://" + site).netloc
        return re.sub(r"^www\.", "", host).lower() or None
    return None


def main():
    sfc = _load_sfc_contacts()
    ro_idx = _load_ro_idx()
    open_ids = _open_trigger_ids()

    n_meta = 0
    n_dropped = 0
    n_added = 0

    for path in sorted((PROJECT_ROOT / "data" / "issue_meta").glob("*.json")):
        tid = path.stem
        if tid not in open_ids:
            continue
        meta = json.loads(path.read_text(encoding="utf-8"))
        ce = meta.get("ceref", "")
        domain = _authoritative_domain(meta, sfc.get(ce, {}))
        if not domain:
            continue

        ec = meta.get("email_candidates") or []
        new_ec = []
        for c in ec:
            if c.get("kind") in GUESS_KINDS:
                if not _hosts_match(_email_host(c.get("email", "")), domain):
                    n_dropped += 1
                    continue
            new_ec.append(c)

        existing = {(c.get("email", "") or "").lower() for c in new_ec}

        # Regenerate per-RO pattern guesses at the authoritative domain
        ros = meta.get("ros") or meta.get("ros_current") or []
        for r in ros:
            row = ro_idx.get(r.get("ceref")) or {}
            first = (row.get("ro_first_short") or row.get("ro_first_full") or "").lower().strip()
            last = (row.get("ro_last") or "").lower().strip()
            if not (first and last):
                f2, l2 = parse_name(r.get("name", ""))
                first = first or f2
                last = last or l2
            if not (first and last):
                continue
            for pat in email_patterns(first, last)[:3]:
                em = f"{pat}@{domain}"
                if em.lower() in existing:
                    continue
                new_ec.append({
                    "email": em, "kind": "ro_guess", "confidence": "medium",
                    "ro": r.get("name", ""),
                })
                existing.add(em.lower())
                n_added += 1

        # Generic firm-level guesses at the authoritative domain
        for local in ("info", "contact", "enquiry"):
            em = f"{local}@{domain}"
            if em.lower() in existing:
                continue
            new_ec.append({
                "email": em, "kind": "generic_guess", "confidence": "low",
            })
            existing.add(em.lower())
            n_added += 1

        meta["email_candidates"] = new_ec
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        n_meta += 1

    print(f"dedup_wrong_domain_guesses: {n_meta} meta files updated; "
          f"dropped {n_dropped} wrong-domain candidates; "
          f"added {n_added} correct-domain guesses")


if __name__ == "__main__":
    main()

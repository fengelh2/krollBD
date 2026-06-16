"""Fetch the SFC 'Addresses' page for every corp and extract the
filed-with-regulator website / email / telephone / address.

These are the gold source: every firm in the register filed them with SFC
as part of licensing. Way more reliable than SerpAPI / Firecrawl / Hunter
discovery (which can land on the wrong company entirely — see the Vajra ->
intechopen.com saga).

The data lives in inline JS variables on /publicregWeb/corp/{ce}/addresses:
  var websiteData = [{"website":"www.firm.com"}];
  var emailData   = [{"email":"info@firm.com"}];
  var telephoneData = [{...}];
  var addressData = [{...}];

Output: data/sfc_corp_contacts.csv (ceref, website, email, telephone,
address, fetched_at_utc).

Usage:
  python tools/scrape_sfc_addresses.py --cerefs CE1,CE2,...   # targeted
  python tools/scrape_sfc_addresses.py --all                  # full register
  python tools/scrape_sfc_addresses.py --open-triggers        # just open issues
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = PROJECT_ROOT / "data" / "sfc_corp_contacts.csv"
SNAP_PATH = PROJECT_ROOT / "data" / "snapshots" / "sfc_t9_corps_latest.csv"
TIMEOUT = 15
SLEEP = 0.4   # be polite to apps.sfc.hk
UA = "Mozilla/5.0 (KrollBD dashboard, contact: fengelh@gmail.com)"

FIELDS = ["ceref", "name_en", "website", "email", "telephone", "address",
          "fetched_at_utc", "fetch_status"]


def _parse_js_data(html: str, var_name: str) -> list[dict]:
    m = re.search(rf'var\s+{var_name}\s*=\s*(\[[^;]*\])\s*;', html)
    if not m:
        return []
    raw = m.group(1)
    try:
        return json.loads(raw)
    except Exception:
        return []


def _first_or_empty(arr, key: str) -> str:
    if not arr: return ""
    first = arr[0]
    if not isinstance(first, dict): return ""
    v = first.get(key, "")
    return (v or "").strip()


def fetch_one(ceref: str) -> dict:
    out = {f: "" for f in FIELDS}
    out["ceref"] = ceref
    out["fetched_at_utc"] = dt.datetime.now(dt.UTC).isoformat()
    try:
        r = requests.get(
            f"https://apps.sfc.hk/publicregWeb/corp/{ceref}/addresses?locale=en",
            timeout=TIMEOUT, headers={"User-Agent": UA},
        )
    except Exception as e:
        out["fetch_status"] = f"error: {type(e).__name__}: {e}"
        return out
    if r.status_code != 200:
        out["fetch_status"] = f"http {r.status_code}"
        return out

    out["website"] = _first_or_empty(_parse_js_data(r.text, "websiteData"), "website")
    out["email"] = _first_or_empty(_parse_js_data(r.text, "emailData"), "email")
    out["telephone"] = _first_or_empty(_parse_js_data(r.text, "telephoneData"), "telephone")
    addresses = _parse_js_data(r.text, "addressData")
    out["address"] = _first_or_empty(addresses, "fullAddress")
    out["fetch_status"] = "ok"
    return out


def _load_existing() -> dict[str, dict]:
    if not OUT_PATH.exists():
        return {}
    with OUT_PATH.open(encoding="utf-8-sig") as f:
        return {r["ceref"]: r for r in csv.DictReader(f)}


def _write_all(rows: dict[str, dict]) -> None:
    tmp = OUT_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for ce in sorted(rows.keys()):
            w.writerow({k: rows[ce].get(k, "") for k in FIELDS})
        f.flush(); os.fsync(f.fileno())
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, OUT_PATH)


def _all_register_cerefs() -> list[tuple[str, str]]:
    rows = []
    with SNAP_PATH.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append((r["ceref"], r.get("name_en", "")))
    return rows


def _open_trigger_cerefs() -> list[tuple[str, str]]:
    out = subprocess.check_output(
        ["gh", "issue", "list", "--repo", "fengelh2/krollBD",
         "--state", "open", "--limit", "200", "--json", "body,title"],
        text=True, cwd=PROJECT_ROOT,
    )
    name_by_ce = {}
    for i in json.loads(out):
        m = re.search(r"`([A-Z]{3}\d{3})`", i["body"])
        if not m: continue
        ce = m.group(1)
        title = i.get("title", "")
        # Trigger-title patterns we need to parse the FIRM (not the RO) from:
        #   [C1] New Type 9 corp — {firm}
        #   [C2] Type 9 retired — {firm}
        #   [C5] Rebrand — {old} → {new}
        #   [R1] New RO at {firm} — {ro}
        nm = ""
        m2 = re.search(r"\bat\s+(.+?)\s+—\s+", title)
        if m2:
            nm = m2.group(1).strip()
        else:
            m3 = re.search(r"—\s+(.+)$", title)
            if m3:
                nm = m3.group(1).strip()
                # For C5 rebrand: take after the arrow if present
                if "→" in nm:
                    nm = nm.split("→")[-1].strip()
        name_by_ce[ce] = nm
    return list(name_by_ce.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cerefs", default="")
    ap.add_argument("--open-triggers", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even if already in cache")
    args = ap.parse_args()

    if args.cerefs:
        targets = [(c.strip(), "") for c in args.cerefs.split(",") if c.strip()]
    elif args.open_triggers:
        targets = _open_trigger_cerefs()
    elif args.all:
        targets = _all_register_cerefs()
    else:
        ap.error("pass --cerefs / --open-triggers / --all")

    existing = _load_existing()
    print(f"Fetching {len(targets)} corps (already cached: {len(existing)})", file=sys.stderr)

    n_new = n_skip = n_ok = n_err = 0
    n_with_website = n_with_email = 0
    for i, (ce, nm) in enumerate(targets, 1):
        if ce in existing and not args.force:
            n_skip += 1
            continue
        rec = fetch_one(ce)
        if nm: rec["name_en"] = nm
        existing[ce] = rec
        if rec["fetch_status"] == "ok":
            n_ok += 1
            if rec["website"]: n_with_website += 1
            if rec["email"]: n_with_email += 1
        else:
            n_err += 1
        n_new += 1
        # Show progress every 10 + at start
        if n_new <= 5 or n_new % 25 == 0:
            print(f"  [{i:>4}/{len(targets)}] {ce} {nm[:30]:30}  "
                  f"site={rec['website'][:30]:30} email={rec['email'][:30]}",
                  file=sys.stderr)
        # Persist every 100 to survive interruptions
        if n_new % 100 == 0:
            _write_all(existing)
        time.sleep(SLEEP)

    _write_all(existing)
    print(file=sys.stderr)
    print(f"== done ==", file=sys.stderr)
    print(f"  fetched:        {n_new}", file=sys.stderr)
    print(f"  cache-skipped:  {n_skip}", file=sys.stderr)
    print(f"  ok:             {n_ok}", file=sys.stderr)
    print(f"  errors:         {n_err}", file=sys.stderr)
    print(f"  with website:   {n_with_website} ({100*n_with_website//max(1,n_ok)}%)",
          file=sys.stderr)
    print(f"  with email:     {n_with_email} ({100*n_with_email//max(1,n_ok)}%)",
          file=sys.stderr)


if __name__ == "__main__":
    main()

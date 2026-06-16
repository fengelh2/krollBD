"""One-shot: regenerate email_subject + email_body + per_ro_drafts on
all open-trigger meta files using the lane-aware templates from
publish_triggers_to_github.py.

For each open trigger:
  - Pick template based on firm's illiq_likelihood:
      PV (high/medium) vs FSCR (everything else)
  - Re-render email_subject + email_body with the same natural + salutation
  - For C1: re-render per_ro_drafts using the same template per-RO
  - Stamp bd_lane + new variant_id (e.g. C1-v3-PV / R1-v2-FSCR)
  - Preserve existing email_candidates / hunter_hits / sfc_filed_website /
    everything else on the meta — we only touch the body.

Skips C2/C5 (no lane split discussed).
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from publish_triggers_to_github import (
    _pick_template, _ro_salutation, split_email, short_hash,
)


def _load_strategy_illiq() -> dict[str, str]:
    out = {}
    p = PROJECT_ROOT / "data" / "strategy_classification.csv"
    with p.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[r["ceref"]] = (r.get("illiquid_book_likelihood") or "").strip()
    return out


def _load_ro_index() -> dict:
    p = PROJECT_ROOT / "data" / "snapshots" / "sfc_t9_corp_ros_latest.csv"
    idx = {}
    with p.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            idx[r["ro_ceref"]] = r
    return idx


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


def main():
    illiq = _load_strategy_illiq()
    ro_idx = _load_ro_index()
    open_ids = _open_trigger_ids()

    stats = {"C1-PV": 0, "C1-FSCR": 0, "R1-PV": 0, "R1-FSCR": 0,
             "skipped_other_type": 0, "skipped_not_open": 0,
             "skipped_no_meta_change": 0}

    for path in sorted((PROJECT_ROOT / "data" / "issue_meta").glob("*.json")):
        tid = path.stem
        if tid not in open_ids:
            stats["skipped_not_open"] += 1
            continue
        meta = json.load(path.open(encoding="utf-8"))
        t = meta.get("type")
        if t not in ("C1", "R1"):
            stats["skipped_other_type"] += 1
            continue
        ce = meta.get("ceref", "")
        natural = meta.get("natural", "")
        primary_ce = (meta.get("ros") or [{}])[0].get("ceref")
        primary_row = ro_idx.get(primary_ce) if primary_ce else None
        salutation = _ro_salutation(primary_row, natural)

        firm_illiq = illiq.get(ce, "")
        template, lane = _pick_template(t, firm_illiq)
        body = template.format(natural=natural, salutation=salutation)
        subj, body_only = split_email(body)

        meta["email_subject"] = subj
        meta["email_body"] = body_only
        meta["email_body_hash"] = short_hash(subj + "\n" + body_only)
        meta["bd_lane"] = lane
        meta["variant_id"] = f"{t}-v3-{lane}" if t == "C1" else f"R1-v2-{lane}"

        # For C1: regenerate per_ro_drafts using same template per RO
        if t == "C1":
            drafts = []
            for r in (meta.get("ros") or []):
                row = ro_idx.get(r["ceref"])
                ro_sal = _ro_salutation(row, natural) if row else r.get("name", "")
                ro_body = template.format(natural=natural, salutation=ro_sal)
                ro_subj, ro_body_only = split_email(ro_body)
                drafts.append({
                    "ro_name": r.get("name", ""),
                    "ro_ceref": r.get("ceref", ""),
                    "salutation": ro_sal,
                    "email_subject": ro_subj,
                    "email_body": ro_body_only,
                })
            meta["per_ro_drafts"] = drafts

        json.dump(meta, path.open("w", encoding="utf-8"), indent=2, ensure_ascii=False)
        stats[f"{t}-{lane}"] += 1

    print("=== regen summary ===")
    for k, v in stats.items():
        print(f"  {k:25} {v}")


if __name__ == "__main__":
    main()

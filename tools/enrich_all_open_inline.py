"""Run Hunter.io + AbstractAPI enrichment across every open trigger,
inline (no git commit). Companion to _enrich_all_open.py which does
its own commits — that one is for the standalone on-demand workflow.

This version is intended to run as a step inside weekly.yml, where
the surrounding workflow handles the commit + push afterward. Writes
hunter_hits directly into each trigger's meta file and lets the next
step pick them up.

Skips logic mirrors find_ros() in _enrich_inject_into_meta.py:
  - Skip ROs already in hunter_hits
  - Skip firms with no verified domain
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from _enrich_inject_into_meta import find_ros  # noqa


def _open_trigger_ids() -> list[tuple[str, str]]:
    out = subprocess.check_output(
        ["gh", "issue", "list", "--repo", "fengelh2/krollBD",
         "--state", "open", "--limit", "200", "--json", "body,title"],
        text=True, cwd=PROJECT_ROOT,
    )
    out_list = []
    for i in json.loads(out):
        m = re.search(r"TRIGGER_ID: ([A-Z]\d?-[A-Z0-9\-]+)", i["body"])
        ce_m = re.search(r"`([A-Z]{3}\d{3})`", i["body"])
        if m and ce_m:
            out_list.append((m.group(1), ce_m.group(1)))
    return out_list


def main():
    triggers = _open_trigger_ids()
    print(f"Found {len(triggers)} open triggers", flush=True)
    counts = {"ok": 0, "no_domain": 0, "error": 0, "no_ros": 0}
    for i, (tid, ce) in enumerate(triggers, 1):
        try:
            r = find_ros(tid, ce)
            if not r.get("ok"):
                err = (r.get("err") or "").lower()
                if "no verified website" in err or "could not extract domain" in err:
                    counts["no_domain"] += 1
                elif "no ros" in err:
                    counts["no_ros"] += 1
                else:
                    counts["error"] += 1
                continue
            counts["ok"] += 1
            results = r.get("results") or []
            new_hits = sum(1 for x in results if x.get("status") == "ok")
            print(f"[{i:3}/{len(triggers)}] {tid} domain={r.get('domain')}  hits={new_hits}", flush=True)
        except Exception as e:
            counts["error"] += 1
            print(f"[{i:3}/{len(triggers)}] {tid} EXCEPTION {type(e).__name__}: {e}", flush=True)
    print()
    for k, v in counts.items():
        print(f"  {k:12} {v}")


if __name__ == "__main__":
    main()

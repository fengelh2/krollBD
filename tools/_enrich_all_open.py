"""Bulk-enrich every open trigger.

Invoked by the enrich-on-demand workflow when event_type=enrich_all. For
each open GitHub issue:

  1. Run deep-scrape on the firm's website (if it has one)
  2. Inject any newly-found on-site emails into the trigger meta
  3. Run Hunter+AbstractAPI find for each parsed RO on the trigger

Writes data/.enrich_status.json after every firm so the dashboard can
poll and show live progress. Commits the status file + meta updates in
batches (every 3 firms) to keep the repo write-rate reasonable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _enrich_inject_into_meta import inject_firm_emails, find_ros  # noqa

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = PROJECT_ROOT / "data" / ".enrich_status.json"
COMMIT_EVERY = 3


def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now(dt.UTC).isoformat()


def _write_status(payload: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATUS_FILE.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _git_commit_and_push(msg: str) -> None:
    subprocess.run(["git", "add", "data/issue_meta/", "data/strategy_classification.csv",
                    "data/hunter_io_cache.json", "data/email_verifier_cache.json",
                    str(STATUS_FILE.relative_to(PROJECT_ROOT))],
                   cwd=PROJECT_ROOT, check=False)
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PROJECT_ROOT)
    if r.returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", msg], cwd=PROJECT_ROOT, check=True)
    for attempt in range(3):
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=PROJECT_ROOT)
        r = subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=PROJECT_ROOT)
        if r.returncode == 0:
            return
        print(f"push retry {attempt+1}", file=sys.stderr)


def _list_open_triggers() -> list[dict]:
    """Use gh CLI to list every open issue, parse trigger_id + ceref out of bodies."""
    out = subprocess.check_output(
        ["gh", "issue", "list", "--state", "open", "--limit", "200",
         "--json", "number,title,body,labels"],
        cwd=PROJECT_ROOT, text=True,
    )
    issues = json.loads(out)
    triggers = []
    for i in issues:
        body = i.get("body", "")
        tid_m = re.search(r"TRIGGER_ID: ([A-Z]\d?-[A-Z0-9\-]+)", body)
        ce_m = re.search(r"\*\*SFC CE reference(?: \(firm\))?:\*\*\s*`([A-Z]{3}\d{3})`", body)
        if not tid_m or not ce_m:
            continue
        labels = [(l.get("name") if isinstance(l, dict) else l) for l in (i.get("labels") or [])]
        if any(lbl.startswith("dropped") for lbl in labels):
            continue
        triggers.append({
            "issue_number": i["number"],
            "title": i["title"],
            "trigger_id": tid_m.group(1),
            "ceref": ce_m.group(1),
        })
    return triggers


def main():
    triggers = _list_open_triggers()
    total = len(triggers)
    started_at = _now_iso()
    print(f"Found {total} open triggers to enrich.")
    _write_status({"started_at": started_at, "current_idx": 0, "total": total,
                   "phase": "starting", "done": False})
    _git_commit_and_push(f"enrich-all: starting ({total} triggers)")

    n_scrape_skipped = 0
    n_ros_skipped = 0
    for idx, t in enumerate(triggers, start=1):
        tid, ce, firm = t["trigger_id"], t["ceref"], t["title"][:60]
        print(f"\n[{idx}/{total}] {tid}  {firm}", flush=True)

        # Skip-checks: is this firm already scraped? are all ROs already
        # verified? If both yes, fast-path skip the entire trigger.
        meta_path = PROJECT_ROOT / "data" / "issue_meta" / f"{tid}.json"
        firm_already_scraped = False
        all_ros_already_verified = False
        if meta_path.exists():
            try:
                m = json.loads(meta_path.read_text(encoding="utf-8"))
                # Firm scraped already? (stamped by inject_firm_emails OR by
                # deep_scrape_contact_pages.py on the strategy_classification.csv side)
                firm_already_scraped = bool(m.get("deep_scrape_attempted_utc"))
                hh = m.get("hunter_hits") or []
                ros_in_meta = [(r.get("name") or "").lower().strip() for r in (m.get("ros") or m.get("ros_current") or [])]
                ros_hit_names = {(h.get("ro") or "").lower().strip() for h in hh if h.get("ro")}
                all_ros_already_verified = bool(ros_in_meta) and all(n in ros_hit_names for n in ros_in_meta)
            except Exception:
                pass
        if firm_already_scraped and all_ros_already_verified:
            print(f"  fully enriched already — skipping")
            n_scrape_skipped += 1; n_ros_skipped += 1
            _write_status({
                "started_at": started_at, "current_idx": idx, "total": total,
                "phase": "running", "current_firm": firm + " (skipped)",
                "current_trigger_id": tid, "done": False,
            })
            continue

        # 1. Deep-scrape (only if not previously attempted; no --force)
        if firm_already_scraped:
            print(f"  firm already scraped — skipping deep-scrape")
            n_scrape_skipped += 1
        else:
            try:
                subprocess.run(
                    ["python", "tools/deep_scrape_contact_pages.py", "--cerefs", ce],
                    cwd=PROJECT_ROOT, check=False,
                )
            except Exception as e:
                print(f"  scrape failed: {e}")

        # 2. Inject scraped emails into meta
        try:
            r1 = inject_firm_emails(tid, ce)
            print(f"  inject firm: {r1}")
        except Exception as e:
            print(f"  inject firm failed: {e}")

        # 3. Hunter + AbstractAPI for each RO
        try:
            r2 = find_ros(tid, ce)
            print(f"  find ros: ok={r2.get('ok')} domain={r2.get('domain')}")
        except Exception as e:
            print(f"  find ros failed: {e}")

        # Update status (every iteration)
        _write_status({
            "started_at": started_at, "current_idx": idx, "total": total,
            "phase": "running", "current_firm": firm, "current_trigger_id": tid,
            "done": False,
        })
        # Commit every COMMIT_EVERY firms to limit git churn
        if idx % COMMIT_EVERY == 0 or idx == total:
            _git_commit_and_push(f"enrich-all: progress {idx}/{total}")

    _write_status({
        "started_at": started_at, "current_idx": total, "total": total,
        "phase": "done", "done": True, "finished_at": _now_iso(),
    })
    _git_commit_and_push(f"enrich-all: complete ({total} processed)")
    print(f"\nDone. {total} triggers processed. "
          f"({n_scrape_skipped} firm-scrapes skipped, {n_ros_skipped} RO-finds skipped — already enriched.)")


if __name__ == "__main__":
    main()

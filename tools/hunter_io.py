"""Hunter.io helper — verified named-RO email finder.

Used by `publish_triggers_to_github.py` at trigger time. Cap is 50 lookups/mo
on the free tier, so this module:
  - Caches every lookup on disk (TTL-aware) so re-runs are free
  - Tracks remaining quota via /account
  - Refuses to call if remaining quota < QUOTA_FLOOR
  - FAILS CLOSED on quota probe failure (returns None) — never burns quota
    when we can't see what's left
  - Returns a typed status so the publisher can distinguish "no match",
    "quota exhausted", "rate limited", "error" and persist the right marker
  - Cache writes use a cross-process file lock; reads merge in fresh on-disk
    entries before write to avoid concurrent-clobber

Quota policy lives here, not in the publisher. Publisher just calls
`find_email(domain, first, last)` and trusts the return.
"""

from __future__ import annotations

import json
import os
import datetime as dt
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # repo root
load_dotenv(PROJECT_ROOT / ".env")

API_KEY = os.environ.get("HUNTER_API_KEY")
# Round-robin support: collect all HUNTER_API_KEY{,_2,_3,...} so we pool
# quota across multiple Free accounts. find_email() iterates these in order
# and uses the first key whose remaining quota is >= QUOTA_FLOOR.
API_KEYS: list[str] = [k for k in (
    os.environ.get("HUNTER_API_KEY"),
    os.environ.get("HUNTER_API_KEY_2"),
    os.environ.get("HUNTER_API_KEY_3"),
) if k]
CACHE_PATH = PROJECT_ROOT / "data" / "hunter_io_cache.json"
LOCK_PATH = PROJECT_ROOT / "data" / ".hunter_io_cache.lock"
QUOTA_FLOOR = 5     # never burn the last 5 lookups — reserved for ad-hoc + retries
MISS_TTL_DAYS = 60  # cached "no match" expires after this; hits are permanent
TIMEOUT = 15

# Typed status enum for find_email() return:
STATUS_OK = "ok"                       # email found
STATUS_NO_MATCH = "no_match"           # Hunter explicitly returned no email
STATUS_QUOTA_EXHAUSTED = "quota_exhausted"  # 402, or remaining < QUOTA_FLOOR
STATUS_QUOTA_UNKNOWN = "quota_unknown"      # /account probe failed → fail closed
STATUS_RATE_LIMITED = "rate_limited"   # 429
STATUS_ERROR = "error"                 # any other failure
STATUS_NOT_CONFIGURED = "not_configured"    # no API key

_thread_lock = threading.Lock()


# ---------------- cross-process file lock ----------------

class _FileLock:
    """Cross-process advisory lock. Uses msvcrt on Windows, fcntl on POSIX.
    Falls back to no-op if neither is available (best-effort)."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                # Block until lock acquired (we accept the small chance of starvation).
                while True:
                    try:
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        continue
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass  # best-effort
        return self

    def __exit__(self, *_):
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


# ---------------- cache I/O ----------------

def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache_merged(updates: dict) -> None:
    """Re-read disk, merge our updates on top, write back atomically under lock.
    This prevents concurrent processes from clobbering each other's new entries."""
    with _FileLock(LOCK_PATH):
        cur = _load_cache()
        cur.update(updates)
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            with open(tmp, "rb+") as f:
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
        tmp.replace(CACHE_PATH)


def _cache_key(domain: str, first: str, last: str) -> str:
    return f"{(domain or '').lower().strip()}|{(first or '').lower().strip()}|{(last or '').lower().strip()}"


def _miss_expired(rec: dict) -> bool:
    """A cached 'no_match' record is expired after MISS_TTL_DAYS so we re-query.
    Hits are permanent."""
    if (rec.get("status") or "") != STATUS_NO_MATCH:
        return False
    fetched = rec.get("fetched_at_utc") or ""
    if not fetched:
        return True
    try:
        when = dt.datetime.fromisoformat(fetched.rstrip("Z"))
        age = dt.datetime.now(dt.UTC).replace(tzinfo=None) - when.replace(tzinfo=None)
        return age.days >= MISS_TTL_DAYS
    except Exception:
        return True


# ---------------- public API ----------------

def _quota_for_key(key: str) -> int:
    """Remaining /email-finder searches for a single key. -1 if probe fails."""
    if not key:
        return -1
    try:
        r = requests.get(
            "https://api.hunter.io/v2/account",
            params={"api_key": key},
            timeout=TIMEOUT,
        )
        if not r.ok:
            return -1
        rq = (r.json().get("data") or {}).get("requests") or {}
        if isinstance(rq, dict):
            if "searches" in rq:
                s = rq["searches"] or {}
                return max(0, int(s.get("available", 0)) - int(s.get("used", 0)))
            if "available" in rq and "used" in rq:
                return max(0, int(rq["available"]) - int(rq["used"]))
        return -1
    except Exception:
        return -1


def remaining_quota() -> int:
    """Returns POOLED remaining searches across all configured keys.
    Returns -1 if every probe fails. Free tier is 50/mo per account."""
    if not API_KEYS:
        return -1
    totals = [_quota_for_key(k) for k in API_KEYS]
    valid = [t for t in totals if t >= 0]
    return sum(valid) if valid else -1


def pick_active_key() -> tuple[str | None, int]:
    """Choose the first key with remaining quota >= QUOTA_FLOOR.
    Returns (key, remaining) or (None, -1) if every key is dry or unreachable."""
    if not API_KEYS:
        return (None, -1)
    any_probed = False
    for k in API_KEYS:
        q = _quota_for_key(k)
        if q < 0:
            continue
        any_probed = True
        if q >= QUOTA_FLOOR:
            return (k, q)
    return (None, 0 if any_probed else -1)


def find_email(domain: str, first: str, last: str) -> dict | None:
    """Return a typed record:
      {
        "status": "ok" | "no_match" | "quota_exhausted" | "quota_unknown"
                  | "rate_limited" | "error" | "not_configured",
        "email": "kmcgann@hamiltonlane.com" | None,
        "score": int | None,
        "confidence": "high"|"medium"|"low"|"none",
        "verification_status": "valid" | "invalid" | "no_match" | "unknown",
        "source": "hunter.io",
        "fetched_at_utc": "...",
        "quota_remaining_when_fetched": int,
      }

    Callers should:
      - status == "ok"               → use email
      - status == "no_match"         → fall through to pattern guess
      - status == "quota_exhausted"  → mark for retry next month
      - status == "quota_unknown"    → mark for retry; could be transient outage
      - status == "rate_limited"     → mark for retry next run
      - status == "error"            → mark for retry next run
      - status == "not_configured"   → silently skip (no key set)
    Returns None only when domain/first/last is missing — never on API failure.
    """
    if not (domain and first and last):
        return None
    if not API_KEYS:
        return _record(STATUS_NOT_CONFIGURED, qr=-1)

    key = _cache_key(domain, first, last)
    # Thread-local fast-path: dedup same-process duplicate calls.
    with _thread_lock:
        cache = _load_cache()
        if key in cache and not _miss_expired(cache[key]):
            return cache[key]

    active_key, qr = pick_active_key()
    # FAIL CLOSED: if every probe failed, or all keys below floor.
    if qr == -1:
        return _record(STATUS_QUOTA_UNKNOWN, qr=qr)
    if not active_key:
        return _record(STATUS_QUOTA_EXHAUSTED, qr=qr)

    try:
        r = requests.get(
            "https://api.hunter.io/v2/email-finder",
            params={
                "domain": domain,
                "first_name": first,
                "last_name": last,
                "api_key": active_key,
            },
            timeout=TIMEOUT,
        )
    except Exception as e:
        return _record(STATUS_ERROR, qr=qr, err=str(e))

    if r.status_code == 402:
        return _record(STATUS_QUOTA_EXHAUSTED, qr=qr)
    if r.status_code == 429:
        return _record(STATUS_RATE_LIMITED, qr=qr)
    if not r.ok:
        return _record(STATUS_ERROR, qr=qr, err=f"HTTP {r.status_code}")

    try:
        data = (r.json() or {}).get("data") or {}
    except Exception as e:
        return _record(STATUS_ERROR, qr=qr, err=f"json: {e}")

    email = data.get("email")
    if not email:
        rec = _record(
            STATUS_NO_MATCH, qr=qr,
            email=None,
            verification_status=(data.get("verification") or {}).get("status") or "no_match",
        )
        _save_cache_merged({key: rec})
        return rec

    score = int(data.get("score") or 0)
    conf = "high" if score >= 85 else "medium" if score >= 60 else "low"
    rec = _record(
        STATUS_OK, qr=qr,
        email=email.lower().strip(),
        score=score,
        confidence=conf,
        verification_status=(data.get("verification") or {}).get("status") or "unknown",
    )
    _save_cache_merged({key: rec})
    return rec


def _record(status: str, *, qr: int = -1, email=None, score=None,
            confidence: str = "none", verification_status: str = "unknown",
            err: str | None = None) -> dict:
    rec = {
        "status": status,
        "email": email,
        "score": score,
        "confidence": confidence,
        "verification_status": verification_status,
        "source": "hunter.io",
        "fetched_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "quota_remaining_when_fetched": qr,
    }
    if err:
        rec["error"] = err
    return rec


def main():
    """Quick CLI for debugging: `python hunter_io.py <domain> <first> <last>`"""
    import sys
    if len(sys.argv) < 4:
        print(f"usage: {sys.argv[0]} <domain> <first> <last>")
        print(f"quota remaining: {remaining_quota()}")
        sys.exit(1)
    res = find_email(sys.argv[1], sys.argv[2], sys.argv[3])
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()

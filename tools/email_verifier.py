"""Generic email-verifier helper. Currently wired to AbstractAPI's free tier
(100 verifications/mo, no credit card on signup).

Why a separate module from hunter_io.py: Hunter's verifier is one of
*its* quotas; this lets us spot-check inferred-pattern emails when Hunter
is exhausted without burning the Hunter cap.

Sign up: https://www.abstractapi.com/api/email-verification-api
  → grab API key → add to .env as ABSTRACTAPI_KEY=...

Returns a typed status enum:
  - deliverable   → the SMTP server confirms the mailbox exists
  - undeliverable → confirmed bounce / no-such-user
  - risky         → catch-all domain, role address, etc. (can't be certain)
  - unknown       → verifier couldn't determine (greylist, timeout)
  - not_configured→ no API key
  - error         → network / parse failure

Caches every lookup to data/email_verifier_cache.json. Quota-aware: refuses
to call when remaining < QUOTA_FLOOR=5.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

ABSTRACT_KEY = os.environ.get("ABSTRACTAPI_KEY")
# Round-robin across multiple free accounts: try ABSTRACTAPI_KEY,
# ABSTRACTAPI_KEY_2, ... in order. If one returns quota_exhausted (HTTP 422
# "quota_reached") we fall through to the next.
ABSTRACT_KEYS: list[str] = [k for k in (
    os.environ.get("ABSTRACTAPI_KEY"),
    os.environ.get("ABSTRACTAPI_KEY_2"),
    os.environ.get("ABSTRACTAPI_KEY_3"),
) if k]
CACHE_PATH = PROJECT_ROOT / "data" / "email_verifier_cache.json"
LOCK_PATH = PROJECT_ROOT / "data" / ".email_verifier_cache.lock"
TIMEOUT = 15
QUOTA_FLOOR = 5

STATUS_DELIVERABLE = "deliverable"
STATUS_UNDELIVERABLE = "undeliverable"
STATUS_RISKY = "risky"
STATUS_UNKNOWN = "unknown"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_ERROR = "error"
STATUS_QUOTA_EXHAUSTED = "quota_exhausted"

_thread_lock = threading.Lock()


# ---------------- cross-process file lock (msvcrt / fcntl) ----------------

class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
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
            pass
        return self

    def __exit__(self, *_):
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                try:
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


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache_merged(updates: dict) -> None:
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


# ---------------- public API ----------------

def verify(email: str) -> dict | None:
    """Verify a single email. Returns dict like:
      {
        "email": "...",
        "status": "deliverable" | "undeliverable" | "risky" | "unknown" | ...,
        "is_valid_format": bool,
        "is_smtp_valid": bool,
        "is_catch_all": bool,
        "is_role": bool,
        "is_disposable": bool,
        "quality_score": float,
        "fetched_at_utc": "...",
        "source": "abstractapi",
      }
    """
    if not email:
        return None
    if not ABSTRACT_KEYS:
        return _record(email, STATUS_NOT_CONFIGURED)

    email_l = email.lower().strip()
    with _thread_lock:
        cache = _load_cache()
        if email_l in cache:
            return cache[email_l]

    # Endpoint: Email Reputation API (different product from Email Validation;
    # both report deliverability, response shapes differ).
    # Round-robin across configured keys: a 422 "quota_reached" from one key
    # falls through to the next so we get pooled coverage.
    r = None
    last_status = None
    for key in ABSTRACT_KEYS:
        try:
            r = requests.get(
                "https://emailreputation.abstractapi.com/v1/",
                params={"api_key": key, "email": email_l},
                timeout=TIMEOUT,
            )
        except Exception as e:
            return _record(email_l, STATUS_ERROR, err=str(e))
        last_status = r.status_code
        # AbstractAPI returns 422 with quota_reached body when free tier is dry.
        if r.status_code == 422 and "quota_reached" in r.text:
            continue
        if r.status_code in (401, 403):  # invalid key — try next
            continue
        if r.status_code == 429:  # rate limited
            continue
        break  # got an actionable response (200, or non-quota error)

    if r is None:
        return _record(email_l, STATUS_ERROR, err="no key returned a response")
    if r.status_code == 429:
        return _record(email_l, STATUS_QUOTA_EXHAUSTED)
    if r.status_code == 422 and "quota_reached" in r.text:
        return _record(email_l, STATUS_QUOTA_EXHAUSTED, err="all keys exhausted")
    if r.status_code in (401, 403):
        return _record(email_l, STATUS_NOT_CONFIGURED,
                       err=f"HTTP {r.status_code} — all keys rejected")
    if not r.ok:
        return _record(email_l, STATUS_ERROR, err=f"HTTP {r.status_code}")

    try:
        data = r.json() or {}
    except Exception as e:
        return _record(email_l, STATUS_ERROR, err=f"json: {e}")

    # Email Reputation response shape: nested under email_deliverability + email_quality
    deliv = ((data.get("email_deliverability") or {}).get("status") or "").lower()
    status_map = {
        "deliverable": STATUS_DELIVERABLE,
        "undeliverable": STATUS_UNDELIVERABLE,
        "risky": STATUS_RISKY,
        "unknown": STATUS_UNKNOWN,
    }
    status = status_map.get(deliv, STATUS_UNKNOWN)
    ed = data.get("email_deliverability") or {}
    eq = data.get("email_quality") or {}

    rec = _record(
        email_l, status,
        is_valid_format=_truthy(ed.get("is_format_valid")),
        is_smtp_valid=_truthy(ed.get("is_smtp_valid")),
        is_catch_all=_truthy(eq.get("is_catchall")),
        is_role=_truthy(eq.get("is_role_based")),
        is_disposable=_truthy(eq.get("is_disposable")),
        quality_score=_score(eq.get("score")),
        is_mx_valid=_truthy(ed.get("is_mx_valid")),
        status_detail=ed.get("status_detail") or "",
    )
    _save_cache_merged({email_l: rec})
    return rec


def _truthy(v) -> bool:
    if isinstance(v, dict):
        v = v.get("value")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return False


def _score(v) -> float:
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v)
    except Exception:
        return 0.0


def _record(email: str, status: str, **kw) -> dict:
    rec = {
        "email": email,
        "status": status,
        "source": "abstractapi",
        "fetched_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    rec.update(kw)
    return rec


def main():
    import sys
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <email>")
        print(f"  ABSTRACTAPI_KEY set: {bool(ABSTRACT_KEY)}")
        sys.exit(1)
    res = verify(sys.argv[1])
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()

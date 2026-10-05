"""Quiet-hours + weekend-aware outbound schedule calculator (<250 lines).

Rules: local_hour<07 OR >=22 → NOT sendable (shift to next 07:00 local).
Weekend: default Sat-Sun (5,6), Middle-East Fri-Sat (4,5). If target falls
on a weekend day → shift forward until non-weekend 07:00 local.
Env FOLLOWUPS_WEEKEND_SENDS_OK=true disables weekend shift entirely.
Unknown country prefix → fail-open (permit send immediately).
"""

from __future__ import annotations

import datetime as _dt
import os
import time
from typing import Dict, Optional, Tuple

COUNTRY_TZ_OFFSET: Dict[str, float] = {
    "65": 8.0, "60": 8.0, "62": 7.0, "66": 7.0, "84": 7.0, "63": 8.0,
    "61": 10.0, "86": 8.0, "91": 5.5, "971": 4.0, "966": 3.0, "20": 2.0,
    "44": 0.0, "49": 1.0, "33": 1.0, "41": 1.0, "34": 1.0, "39": 1.0,
    "1": -5.0, "1204": -5.0, "52": -6.0, "55": -3.0, "27": 2.0,
    "81": 9.0, "82": 9.0, "886": 8.0, "852": 8.0, "673": 8.0,
    "679": 12.0, "678": 11.0, "675": 10.0, "92": 5.0, "94": 5.5,
}

COUNTRY_WEEKEND_MAP: Dict[str, Tuple[int, int]] = {
    "971": (4, 5), "966": (4, 5), "965": (4, 5), "974": (4, 5),
    "968": (4, 5), "962": (4, 5), "20": (4, 5), "212": (5, 6),
    "213": (4, 5), "90": (4, 5), "98": (4, 5),
    "65": (5, 6), "60": (5, 6), "62": (5, 6), "66": (5, 6),
    "84": (5, 6), "63": (5, 6), "61": (5, 6), "86": (5, 6),
    "91": (5, 6), "44": (5, 6), "49": (5, 6), "1": (5, 6),
    "55": (5, 6), "27": (5, 6), "81": (5, 6), "82": (5, 6),
    "886": (5, 6), "852": (5, 6),
}

_FOLLOWUPS_WEEKEND_SENDS_OK: bool = (
    os.getenv("FOLLOWUPS_WEEKEND_SENDS_OK", "").strip().lower()
    in {"1", "true", "yes", "on"}
)


def _country_prefix_from_e164(e164: str) -> str:
    digits = e164.lstrip("+").strip()
    if not digits or not digits.isdigit():
        return ""
    for length in (4, 3, 2, 1):
        if len(digits) >= length:
            cand = digits[:length]
            if cand in COUNTRY_TZ_OFFSET:
                return cand
    return ""


def buyer_local_hour_utc_offset(e164: str) -> Optional[float]:
    prefix = _country_prefix_from_e164(e164)
    return COUNTRY_TZ_OFFSET.get(prefix) if prefix else None


def _weekend_for(prefix: str) -> Tuple[int, int]:
    return COUNTRY_WEEKEND_MAP.get(prefix, (5, 6)) if prefix else (5, 6)


def _utc_from_local_ymd_hm(y: int, m: int, d: int, h: int, mi: int, off: float) -> float:
    return (_dt.datetime(y, m, d, h, mi, 0, tzinfo=_dt.timezone.utc)
            - _dt.timedelta(hours=off)).timestamp()


def _next_non_weekend_0700_utc(start_local_date: _dt.date, off: float,
                                weekend: Tuple[int, int]) -> float:
    """From start_local_date (inclusive), walk forward 1 day at a time
    until the local weekday is NOT in weekend, then return 07:00 local
    that day as a UTC timestamp."""
    cur = start_local_date
    for _ in range(7):
        if cur.weekday() not in weekend:
            return _utc_from_local_ymd_hm(cur.year, cur.month, cur.day, 7, 0, off)
        cur += _dt.timedelta(days=1)
    return _utc_from_local_ymd_hm(cur.year, cur.month, cur.day, 7, 0, off)


def compute_next_sendable_utc_timestamp(
    now_utc_unix: float,
    offset_hours: Optional[float],
    weekend_override: Optional[str] = None,
) -> Tuple[bool, float]:
    if offset_hours is None:
        print("[SCHEDULER] unknown TZ prefix +PREFIX quiet window not enforced")
        return (True, 0.0)
    off = float(offset_hours)
    local_now = _dt.datetime.fromtimestamp(now_utc_unix + off * 3600, tz=_dt.timezone.utc)
    lh = local_now.hour + local_now.minute / 60.0 + local_now.second / 3600.0
    weekend = (5, 6)
    # Quiet window
    if lh < 7.0 or lh >= 22.0:
        tgt_date = local_now.date()
        if lh >= 22.0:
            tgt_date += _dt.timedelta(days=1)
        if _FOLLOWUPS_WEEKEND_SENDS_OK:
            return (False, _utc_from_local_ymd_hm(tgt_date.year, tgt_date.month,
                                                  tgt_date.day, 7, 0, off))
        return (False, _next_non_weekend_0700_utc(tgt_date, off, weekend))
    # Weekend check (only when not already in quiet window)
    if not _FOLLOWUPS_WEEKEND_SENDS_OK:
        wd = local_now.weekday()
        if wd in weekend:
            tgt_date = local_now.date()
            return (False, _next_non_weekend_0700_utc(tgt_date, off, weekend))
    return (True, 0.0)


def compute_next_sendable_for_e164(
    e164: str,
    now_utc_unix: Optional[float] = None,
) -> Tuple[bool, float]:
    if now_utc_unix is None:
        now_utc_unix = time.time()
    prefix = _country_prefix_from_e164(e164)
    if not prefix:
        print(f"[SCHEDULER] unknown TZ prefix {e164[:4] if len(e164)>=4 else e164} quiet window not enforced")
        return (True, 0.0)
    off = COUNTRY_TZ_OFFSET[prefix]
    weekend = _weekend_for(prefix)
    local_now = _dt.datetime.fromtimestamp(now_utc_unix + off * 3600, tz=_dt.timezone.utc)
    lh = local_now.hour + local_now.minute / 60.0 + local_now.second / 3600.0
    if lh < 7.0 or lh >= 22.0:
        tgt_date = local_now.date()
        if lh >= 22.0:
            tgt_date += _dt.timedelta(days=1)
        if _FOLLOWUPS_WEEKEND_SENDS_OK:
            return (False, _utc_from_local_ymd_hm(tgt_date.year, tgt_date.month,
                                                  tgt_date.day, 7, 0, off))
        return (False, _next_non_weekend_0700_utc(tgt_date, off, weekend))
    if not _FOLLOWUPS_WEEKEND_SENDS_OK:
        wd = local_now.weekday()
        if wd in weekend:
            tgt_date = local_now.date()
            return (False, _next_non_weekend_0700_utc(tgt_date, off, weekend))
    return (True, 0.0)


__all__ = [
    "COUNTRY_TZ_OFFSET", "COUNTRY_WEEKEND_MAP",
    "_country_prefix_from_e164", "buyer_local_hour_utc_offset",
    "compute_next_sendable_utc_timestamp", "compute_next_sendable_for_e164",
]

"""본 레포 supabase/functions/_shared/calc/time.ts의 서버판 (기준 b140f70) — 시각 계산, datetime 위에서 분 단위만"""

from datetime import datetime

from ..js import from_ms, js_round, ms

__all__ = ["MINUTE_MS", "add_minutes", "minutes_between", "same_time"]

MINUTE_MS = 60_000


def add_minutes(d: datetime, minutes: float) -> datetime:
    return from_ms(ms(d) + minutes * MINUTE_MS)


def minutes_between(a: datetime, b: datetime) -> int:
    return js_round((ms(b) - ms(a)) / MINUTE_MS)


def same_time(a: datetime | None, b: datetime | None) -> bool:
    return a is not None and b is not None and ms(a) == ms(b)

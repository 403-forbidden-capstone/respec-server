"""W2(plan · commit · morning-flow · 라우터) 시험이 같이 쓰는 도구 — 원본 .test.ts 파일 맨 위의 kst · iso · hm 도우미

시계는 2025년 5월 · 한국 시각 기준 (원본과 같음). 이 파일은 시험 도구라 배포 코드가 아님.
"""

import asyncio
import functools
from datetime import UTC, datetime, timedelta
from typing import Any


def sync(fn):
    """async 시험 함수를 pytest(동기)가 돌릴 수 있게 — pytest-asyncio 없이"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def kst(day: int, minutes: int) -> datetime:
    """2025년 5월 day일 한국 시각 minutes분 (원본의 kst)"""
    return datetime(2025, 5, day, tzinfo=UTC) + timedelta(minutes=minutes - 9 * 60)


def iso(day: int, minutes: int) -> str:
    return f"2025-05-{day}T{minutes // 60:02d}:{minutes % 60:02d}:00+09:00"


def to_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def hm(value: str | datetime) -> str:
    """한국 시각 'HH:MM' (원본의 hm)"""
    d = to_utc(value)
    m = (d.hour * 60 + d.minute + 9 * 60) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def iso_z(value: datetime) -> str:
    """Date.toISOString (밀리초 3자리 · Z)"""
    d = value.astimezone(UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def js(value: Any) -> str:
    """JS 템플릿 문자열에 값을 넣은 모양 — true · false · null"""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def get(obj: Any, *names: str) -> Any:
    """읽은 요청이 dict로 오든 객체로 오든 같은 칸을 읽음 (snake_case · camelCase 둘 다)"""
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    return None

"""본 레포 supabase/functions/_shared/tz.ts의 서버판 (기준 b140f70) — 사용자 시간대로 "그날"과 화면 시각 글자를 계산

하루 경계는 0시 (전체작업계획 8.1 F7 "0시 유지"). TS의 Intl 대신 zoneinfo를 씀 (tzdata 패키지가 시간대 자료를 줌).
시각은 UTC aware datetime. 날짜 글자 'YYYY-MM-DD'는 DB date 칸(appointments.occurrence_on)과 같은 글자.
"""

import re
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from .js import RangeError, from_ms, js_floor, js_round, ms, pad_start, utc_ms

__all__ = [
    "format_ko_duration",
    "format_ko_spoken_time",
    "format_ko_time",
    "local_date_key",
    "local_day_range",
    "local_minute_of_day",
    "offset_minutes",
    "safe_time_zone",
    "zoned_time",
]

DAY_MS = 86_400_000
_DATE_KEY = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})\Z")


@lru_cache(maxsize=256)
def _zone(name: str) -> ZoneInfo:
    """시간대 이름 → ZoneInfo. Intl처럼 대소문자는 가리지 않음 (asia/seoul도 받음). 모르는 이름이면 예외"""
    try:
        return ZoneInfo(name)
    except Exception:
        lowered = name.lower()
        for known in available_timezones():
            if known.lower() == lowered:
                return ZoneInfo(known)
        raise


def _parts_in(d: datetime, time_zone: str) -> tuple[int, int, int, int, int, int]:
    """(연, 월, 일, 시 0~23, 분, 초) — 그 시간대 벽시계"""
    z = _zone(time_zone)
    local = from_ms(ms(d)).astimezone(z)
    return local.year, local.month, local.day, local.hour, local.minute, local.second


def offset_minutes(d: datetime, time_zone: str) -> int:
    """그 시각에 시간대가 UTC보다 몇 분 앞서는지 (서울 = +540)"""
    y, mo, day, h, mi, s = _parts_in(d, time_zone)
    as_utc = utc_ms(y, mo - 1, day, h, mi, s)
    return js_round((as_utc - (ms(d) // 1000) * 1000) / 60_000)


def _zoned_midnight(year: int, month: int, day: int, time_zone: str) -> datetime:
    """그 시간대의 y년 m월 d일 0시 (일광절약시간이 있는 시간대도 두 번 보정해서 맞춤)"""
    naive = utc_ms(year, month - 1, day)
    first = naive - offset_minutes(from_ms(naive), time_zone) * 60_000
    return from_ms(naive - offset_minutes(from_ms(first), time_zone) * 60_000)


def local_day_range(d: datetime, time_zone: str) -> dict[str, datetime]:
    """d가 속한 날의 시작(0시)과 다음 날 시작 — [start, end)"""
    y, mo, day, *_ = _parts_in(d, time_zone)
    return {
        "start": _zoned_midnight(y, mo, day, time_zone),
        "end": _zoned_midnight(y, mo, day + 1, time_zone),
    }


def local_date_key(d: datetime, time_zone: str) -> str:
    """d가 그 시간대에서 몇 월 며칠인지 — 'YYYY-MM-DD'"""
    y, mo, day, *_ = _parts_in(d, time_zone)
    return f"{pad_start(y, 4)}-{pad_start(mo, 2)}-{pad_start(day, 2)}"


def local_minute_of_day(d: datetime, time_zone: str) -> int:
    """d가 그 시간대의 0시부터 몇 분째인지 (자정 0 · 오전 7:30은 450)"""
    _, _, _, h, mi, _ = _parts_in(d, time_zone)
    return h * 60 + mi


def zoned_time(date_key: str, minute_of_day: float, time_zone: str) -> datetime:
    """그 시간대의 날짜('YYYY-MM-DD') + 0시부터 분 → 시각. local_minute_of_day의 반대 (같은 벽시계 시:분을 다른 날짜에 놓을 때)
    분이 1440 이상이면 다음 날로 넘어감 (자정을 넘는 루틴 단계처럼)
    일광절약시간이 있는 시간대: 두 번 있는 시각(가을에 시계가 되돌아가는 때)은 먼저 오는 쪽, 없는 시각(봄에 시계가 건너뛰는 때)은 건너뛴 만큼 뒤로 밂
    (작성자 판단 — 한국은 일광절약시간이 없어 영향 없음)"""
    m = _DATE_KEY.match(date_key)
    if not m:
        raise RangeError(f"날짜는 YYYY-MM-DD 모양이어야 해요: {date_key}")
    # 벽시계 글자를 UTC로 읽은 값. 실제 시각은 여기서 그 시간대의 UTC 오프셋만큼 앞으로 당긴 값
    naive = utc_ms(int(m[1]), int(m[2]) - 1, int(m[3]), 0, minute_of_day)
    # 오프셋이 바뀌는 날(일광절약시간)에도 맞게 — 하루 전 · 하루 뒤의 오프셋을 둘 다 써 보고 벽시계가 맞아떨어지는 쪽을 고름
    offsets = list(
        dict.fromkeys(
            [
                offset_minutes(from_ms(naive - DAY_MS), time_zone),
                offset_minutes(from_ms(naive + DAY_MS), time_zone),
            ]
        )
    )
    fits = [
        t
        for t in (naive - o * 60_000 for o in offsets)
        if t + offset_minutes(from_ms(t), time_zone) * 60_000 == naive
    ]
    if fits:
        return from_ms(min(fits))
    return from_ms(naive - offsets[0] * 60_000)  # 건너뛴 시각 — 바뀌기 전 오프셋으로 읽어 건너뛴 만큼 뒤로 밂


def format_ko_time(d: datetime, time_zone: str) -> str:
    """ "오전 10:00" — 앱 lib/format의 시각 글자와 같은 모양 (RingPackage.subtitle에 씀)"""
    _, _, _, hour, minute, _ = _parts_in(d, time_zone)
    period = "오전" if hour < 12 else "오후"
    hour12 = 12 if hour % 12 == 0 else hour % 12
    return f"{period} {hour12}:{pad_start(minute, 2)}"


def format_ko_spoken_time(d: datetime, time_zone: str) -> str:
    """음성 안내용 "오전 10시" · "오후 2시 30분" — 앱 lib/format의 spokenTime과 같은 모양 (voiceText에 씀)
    숫자 시각(10:00)은 읽는 쪽이 오전 · 오후를 틀리기 쉬워서 말로 풀어 넘김 (회사 제안 2: 서버 변환 뒤 오류 0건)"""
    _, _, _, hour, minute, _ = _parts_in(d, time_zone)
    hour12 = 12 if hour % 12 == 0 else hour % 12
    return f"{'오전' if hour < 12 else '오후'} {hour12}시{f' {minute}분' if minute else ''}"


def format_ko_duration(minutes: float) -> str:
    """시간 길이 글자 "30분" · "1시간" · "1시간 10분" (분은 정수)"""
    total = max(0, js_round(minutes))
    h = js_floor(total / 60)
    m = total % 60
    return " ".join(p for p in (f"{h}시간" if h else "", f"{m}분" if m or not h else "") if p)


def safe_time_zone(time_zone: str | None) -> str:
    """모르는 시간대 이름이면 기본값 (profiles.timezone은 DB 트리거가 검사하지만 방어)"""
    tz = time_zone or "Asia/Seoul"
    try:
        _zone(tz)
        return tz
    except Exception:
        return "Asia/Seoul"

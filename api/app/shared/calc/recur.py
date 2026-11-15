"""본 레포 supabase/functions/_shared/calc/recur.ts의 서버판 (기준 b140f70) — 반복 규칙 읽기 · 다음 회차 날짜

서버(alarm-roll)가 쓰고, 앱의 일정 반복(mobile/src/features/alarm/repeat.ts) · 루틴 반복(features/routine/map.ts)과 같은 해석
  일정 반복 규칙은 두 모양만 받음: FREQ=DAILY · FREQ=WEEKLY;BYDAY=MO,… (앱이 만드는 모양, repeat.ts repeatToRrule).
  MONTHLY · INTERVAL · COUNT · UNTIL 같은 그 밖은 거절 — 앱 표시와 서버가 만드는 회차가 어긋나지 않게
  요일은 회차 날짜(내 시간대)의 요일임. UTC 요일이 아님 — 새벽 1시 약속은 UTC로는 전날
  하루 경계는 profiles.timezone 0시. 한국 · Android만, 앱 R02 기기 시간대와 같다고 봄(10/29 헤드)
날짜 글자 'YYYY-MM-DD'(시간대 없는 달력 날짜)와 datetime만 씀.
Recurrence = {"freq": "DAILY"} 또는 {"freq": "WEEKLY", "days": [월 0 … 일 6]}, Occurrence = {"date", "at"} dict
"""

import re
from datetime import datetime
from typing import Any, TypedDict

from ..js import (
    JS_WS,
    RangeError,
    from_ms,
    js_floor,
    js_max,
    js_trim,
    ms,
    pad_start,
    utc_ms,
    utc_weekday,
    utc_ymd,
)
from ..tz import local_date_key, local_minute_of_day, safe_time_zone, zoned_time

__all__ = [
    "ROLL_WINDOW_DAYS",
    "RRULE_PATTERN",
    "Occurrence",
    "WEEKDAY_CODES",
    "add_days_to_key",
    "next_occurrences",
    "next_routine_at",
    "occurs_on",
    "parse_recurrence",
    "parse_repeat_days",
    "weekday_of_date",
]


class Occurrence(TypedDict):
    """반복 회차 하나 — date = 내 시간대의 'YYYY-MM-DD', at = 약속 시각"""

    date: str
    at: datetime


_DAY_MS = 86_400_000

# 요일 번호: 월 0 · 화 1 · 수 2 · 목 3 · 금 4 · 토 5 · 일 6 (앱 lib/weekday의 WEEKDAYS 순서)
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

# 폰에 걸 알람이 앞으로 며칠치까지 있는지 — 약속 시각이 (지금, 지금 + 14일)인 회차를 미리 만듦 (폰 맞추기 창 reconcile.ts와 같은 시각 기준)
ROLL_WINDOW_DAYS = 14

# 쓸 때(draft.ts) 받는 글자 — 앱이 만드는 모양 그대로 (대문자 · RRULE: 접두사 없음). 읽을 때는 아래 parse_recurrence가 더 너그러움
# JS의 $는 글자 맨 끝만 뜻함 (Python $는 끝의 줄바꿈 앞도 맞아서 \Z 사용) — 시험은 RRULE_PATTERN.match(...)
_CODES = "|".join(WEEKDAY_CODES)
RRULE_PATTERN = re.compile(f"^(FREQ=DAILY|FREQ=WEEKLY;BYDAY=({_CODES})(,({_CODES}))*)\\Z")


def parse_recurrence(rule: str | None) -> dict[str, Any] | None:
    """일정 반복 규칙 → 두 모양. 읽는 해석은 앱 repeat.ts rruleToRepeat과 같음 (RRULE: 접두사 · 소문자 · 순서가 달라도 읽고 요일은 월요일부터 정렬).
    다른 점: 앱은 모르는 글자를 건너뛰어 읽지만 여기서는 두 모양 밖이면 통째로 거절(None) — 반복으로 치지 않음"""
    if not rule:
        return None
    parts: dict[str, str] = {}
    for part in re.sub("^RRULE:", "", js_trim(rule), count=1, flags=re.IGNORECASE).split(";"):
        pieces = part.split("=")
        key = pieces[0]
        value = pieces[1] if len(pieces) > 1 else None
        rest = pieces[2:]
        if not key or value is None or len(rest) > 0 or key.upper() in parts:
            return None
        parts[key.upper()] = value.upper()
    freq = parts.get("FREQ")
    if freq == "DAILY":
        return {"freq": "DAILY"} if len(parts) == 1 else None
    if freq != "WEEKLY" or len(parts) != 2:
        return None
    days = [
        WEEKDAY_CODES.index(code) if code in WEEKDAY_CODES else -1
        for code in parts.get("BYDAY", "").split(",")
    ]
    if any(d < 0 for d in days):
        return None  # BYDAY가 없거나 모르는 요일 코드
    return {"freq": "WEEKLY", "days": sorted(set(days))}


# ---------------------------------------------------------------------------
# 날짜 글자 'YYYY-MM-DD' (시간대 없는 달력 날짜 — DB date 칸 occurrence_on과 같은 글자)
# ---------------------------------------------------------------------------

_KEY = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})\Z")


def _parse_key(date_key: str) -> tuple[int, int, int]:
    m = _KEY.match(date_key)
    if not m:
        raise RangeError(f"날짜는 YYYY-MM-DD 모양이어야 해요: {date_key}")
    return int(m[1]), int(m[2]), int(m[3])


def weekday_of_date(date_key: str) -> int:
    """그 날짜의 요일 번호 (월 0 … 일 6)"""
    y, m, d = _parse_key(date_key)
    return (utc_weekday(utc_ms(y, m - 1, d)) + 6) % 7


def add_days_to_key(date_key: str, days: int) -> str:
    y, m, d = _parse_key(date_key)
    uy, um, ud = utc_ymd(utc_ms(y, m - 1, d + days))
    return f"{pad_start(uy, 4)}-{pad_start(um, 2)}-{pad_start(ud, 2)}"


def occurs_on(rec: dict[str, Any], date_key: str) -> bool:
    """그 날짜가 규칙에 드는지"""
    return rec["freq"] == "DAILY" or weekday_of_date(date_key) in rec["days"]


# ---------------------------------------------------------------------------
# 일정 · 일정 없는 알람의 다음 회차
# ---------------------------------------------------------------------------


def next_occurrences(args: dict[str, Any] | None = None, /, **kwargs: Any) -> list[Occurrence]:
    """다음 회차 목록 (가까운 순). 기준 회차(묶음에서 날짜가 가장 늦은 행)의 약속 시각 baseAt의 같은 벽시계 시:분을 쓰고, 날짜는 baseAt의 날짜 뒤부터 규칙대로 셈
    · 창: 지금 < 회차 시각 < 지금 + windowDays (둘 다 포함 안 함). 서버가 며칠 못 돌았어도 지난 회차는 안 메움
    · 첫 회차(사용자가 고른 날짜)는 요일이 규칙에 안 맞아도 그대로 두는 것 — 여기서는 baseAt 뒤 날짜만 규칙으로 셈
    · afterDate: 이 날짜 뒤부터 셈 (기본은 baseAt의 날짜). 이미 있는 날짜는 부르는 쪽이 뺌
    · 규칙이 두 모양 밖이면 빈 목록
    TS의 인자 객체 하나({rule, baseAt, now, timeZone, windowDays?, afterDate?})를 dict 하나로 주거나 같은 이름의 키워드 인자로 줌"""
    a = {**(args or {}), **kwargs}
    rule, baseAt, now, timeZone = a["rule"], a["baseAt"], a["now"], a["timeZone"]
    windowDays, afterDate = a.get("windowDays"), a.get("afterDate")
    rec = parse_recurrence(rule)
    if not rec:
        return []
    tz = safe_time_zone(timeZone)
    end_ms = ms(now) + (windowDays if windowDays is not None else ROLL_WINDOW_DAYS) * _DAY_MS
    end = from_ms(end_ms)
    minute = local_minute_of_day(baseAt, tz)
    today = local_date_key(now, tz)
    last_key = local_date_key(end, tz)
    key = add_days_to_key(afterDate if afterDate is not None else local_date_key(baseAt, tz), 1)
    if key < today:
        key = today  # 오늘보다 앞 날짜는 시각이 지금보다 뒤일 수 없음
    out: list[Occurrence] = []
    while key <= last_key:
        if occurs_on(rec, key):
            at = zoned_time(key, minute, tz)
            if ms(at) > ms(now) and ms(at) < ms(end):
                out.append({"date": key, "at": at})
        key = add_days_to_key(key, 1)
    return out


# ---------------------------------------------------------------------------
# 루틴 반복 요일 (routines.repeat_days)
# ---------------------------------------------------------------------------

_ROUTINE_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_RANGE = re.compile("^([A-Z]{3})[~-]([A-Z]{3})\\Z")
_SPLIT = re.compile(f"[,{JS_WS}]+")


def _routine_index(code: str) -> int:
    return _ROUTINE_CODES.index(code) if code in _ROUTINE_CODES else -1


def parse_repeat_days(text: str | None) -> list[int]:
    """'MON,WED' · 'MON~FRI' · 'SAT~MON'(일요일을 넘는 범위) → 요일 번호(월 0 … 일 6, 월요일부터). 모르는 글자는 건너뜀
    앱 features/routine/map.ts parseRepeatDays와 같은 해석"""
    picked: set[int] = set()
    for part in _SPLIT.split((text if text is not None else "").upper()):
        rng = _RANGE.match(part)
        if rng:
            start = _routine_index(rng[1])
            to = _routine_index(rng[2])
            if start < 0 or to < 0:
                continue
            i = start
            while True:
                picked.add(i)
                if i == to:
                    break
                i = (i + 1) % 7
        elif _routine_index(part) >= 0:
            picked.add(_routine_index(part))
    return [i for i in range(len(_ROUTINE_CODES)) if i in picked]


def next_routine_at(args: dict[str, Any] | None = None, /, **kwargs: Any) -> datetime | None:
    """루틴 단계 알람의 다음 시각 — 지금 뒤 첫 요일, 같은 단계 시각. 요일이 없으면 None
    stepOffsetMinutes = routines.steps[].offset (그 단계가 시작하는 시각, 0시부터 분. 자정을 넘으면 1440 이상)
    루틴이 도는 요일은 루틴을 시작하는 날 기준(앱 planRoutineAlarms와 같음) — 월요일 밤 11시에 시작해 화요일 00:30에 울리는 단계는 '월요일' 루틴의 알람
    루틴은 회차 행을 따로 만들지 않고 같은 알람 행의 시각만 이 값으로 옮김
    인자는 TS 인자 객체({repeatDays, stepOffsetMinutes, now, timeZone}) 하나를 dict로 주거나 같은 이름의 키워드 인자로 줌"""
    a = {**(args or {}), **kwargs}
    repeatDays, stepOffsetMinutes, now, timeZone = (
        a["repeatDays"],
        a["stepOffsetMinutes"],
        a["now"],
        a["timeZone"],
    )
    days = parse_repeat_days(repeatDays)
    if not days:
        return None
    tz = safe_time_zone(timeZone)
    offset = js_max(0, js_floor(stepOffsetMinutes))
    key = add_days_to_key(
        local_date_key(now, tz), -js_floor(offset / 1440)
    )  # 자정을 넘는 단계는 시작한 날로 거슬러 올라가 봄
    for _ in range(9):
        if weekday_of_date(key) in days:
            at = zoned_time(key, offset, tz)
            if ms(at) > ms(now):
                return at
        key = add_days_to_key(key, 1)
    return None

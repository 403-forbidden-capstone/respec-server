"""본 레포 supabase/functions/_shared/calc/wake.ts의 서버판 (기준 b140f70) — "그날 첫 일정" 판정

이 약속보다 약속 시각이 앞선, 같은 날의 다른 약속에 켜진 기상 알람이 이미 있으면 이 약속은 기상 없이 준비부터
서버(plan.ts)와 앱 목업(estimateAppointment)이 같은 함수를 씀. 알람이 울리는 시각이 아니라 약속 시각으로 앞뒤를 가림
"같은 날"(내 시간대 0시 기준)은 부르는 쪽이 정해서 그날의 참여만 넘김. 이 함수는 날짜를 보지 않음
SameDayWake = {appointmentId?, appointmentAt, isEnabled?} dict
"""

from datetime import datetime
from typing import Any, NotRequired, TypedDict

from ..js import ms
from ..tz import local_date_key, local_minute_of_day, safe_time_zone

__all__ = ["EVENING_WAKE_FROM_MINUTE", "SameDayWake", "has_earlier_wake", "is_evening_wake"]


class SameDayWake(TypedDict):
    """같은 날 다른 약속의 기상 알람 한 줄 (TS SameDayWake) — JSON 모양 dict, 키 그대로"""

    appointmentId: NotRequired[str]
    appointmentAt: datetime
    isEnabled: NotRequired[bool | None]


def has_earlier_wake(
    this_at: datetime, same_day_wakes: list[dict[str, Any]], own_appointment_id: str | None = None
) -> bool:
    """이 약속(this_at)보다 약속 시각이 엄밀히 앞인 다른 약속에 켜진 기상 알람이 있나
    · 같은 시각이면 앞선 게 아님 → 둘 다 기상을 가짐 (서로를 앞섰다고 보면 둘 다 기상이 없어지므로)
    · own_appointment_id = 지금 다시 계산하는 이 약속. 자기 자신의 기상은 안 셈"""
    return any(
        w.get("isEnabled") is not False
        and ms(w["appointmentAt"]) < ms(this_at)
        and not (own_appointment_id is not None and w.get("appointmentId") == own_appointment_id)
        for w in same_day_wakes
    )


# 계산된 기상 시각이 저녁(그 시간대 낮 12시 이후)인가 — 이러면 기상 알람 없이 준비 · 출발(+ 도착)만 (B1 7일 기능시험 버그 후보 4, 헤드 10/30)
#   저녁에만 일정이 있는 날 "그날 첫 일정"이라고 오후에 기상 알람이 울리던 것을 막음. 12:00 정각은 이후(= 기상 없음)
EVENING_WAKE_FROM_MINUTE = 12 * 60


def is_evening_wake(
    wake_at: datetime, time_zone: str | None = None, appointment_at: datetime | None = None
) -> bool:
    """appointment_at을 주면 기상이 약속 날짜가 아니라 전날 밤이면(새벽 1시 약속의 23:10 기상) 저녁 기상이 아님 — "그날"은 약속이 있는 날
    하루는 사용자 시간대(profiles.timezone, 없거나 틀리면 Asia/Seoul) 0시 기준"""
    zone = safe_time_zone(time_zone)
    if appointment_at is not None and local_date_key(wake_at, zone) != local_date_key(appointment_at, zone):
        return False
    return local_minute_of_day(wake_at, zone) >= EVENING_WAKE_FROM_MINUTE

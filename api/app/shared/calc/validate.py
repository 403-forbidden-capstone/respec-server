"""본 레포 supabase/functions/_shared/calc/validate.ts의 서버판 (기준 b140f70) — 저장 검증, 상세설계서 8장 8.2.1 · 7장 7.1.4

  · 알람 순서가 뒤집혔으면 막음 (F5 해석: 기상 ≤ 준비 ≤ 출발 ≤ 도착 목표)
  · 출발 시각이 지금보다 앞이면 저장 막음 (UNPROCESSABLE)
  · 기상 · 준비 시각이 지금 + 5분보다 앞이면 그 알림만 끄고(is_enabled = false) 나머지는 저장
  · 알람을 하나도 안 만든 일정(kinds = [])은 예약할 알람이 없어 시각 검사를 하지 않음
결과: {"ok": True, "alarms": [알람 + enabled (+ disabledReason)]} 또는 {"ok": False, "reason", "message", "detail"?}
"""

from datetime import datetime
from typing import Any

from ..js import ms
from .order import order_error
from .time import add_minutes
from .types import ChainAlarm

__all__ = ["PAST_GRACE_MINUTES", "check_alarms"]

PAST_GRACE_MINUTES = 5


def check_alarms(alarms: list[ChainAlarm], departure: datetime, now: datetime) -> dict[str, Any]:
    """alarms = alarm_chain 결과(만든 종류만), departure = 출발 시각(DEPARTURE 알람을 안 만들어도 있음)"""
    if len(alarms) == 0:
        return {"ok": True, "alarms": []}  # 알람 없는 일정
    order = order_error(alarms)
    if order:
        return {
            "ok": False,
            "reason": "ORDER",
            "message": order["message"],
            "detail": {"earlier": order["earlier"], "later": order["later"]},
        }
    if ms(departure) < ms(now):
        return {
            "ok": False,
            "reason": "DEPARTURE_PASSED",
            "message": "출발 시각이 이미 지나서 저장할 수 없어요",
        }
    limit = ms(add_minutes(now, PAST_GRACE_MINUTES))
    checked: list[dict[str, Any]] = []
    for x in alarms:
        passed = x["kind"] in ("WAKE", "PREPARE") and ms(x["at"]) < limit
        checked.append(
            {**x, "enabled": False, "disabledReason": "PASSED"} if passed else {**x, "enabled": True}
        )
    return {"ok": True, "alarms": checked}

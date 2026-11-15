"""본 레포 supabase/functions/_shared/calc/depart.ts의 서버판 (기준 b140f70) — 출발 여유(slack), 상세설계서 7장 7.5.2

기상 뒤 "지금 이 속도면 늦지 않게 나갈 수 있나"를 세 칸으로 나눔
  latest_departure = 약속 − 이동   (여유시간까지 다 쓴 출발 한계 — 약속 앞 여유(buffer)를 빼지 않음)
  slack = latest_departure − (지금 + 남은 준비분)
  slack ≥ 10 → 여유 / 0 ≤ slack < 10 → 빠듯 / slack < 0 → 지각 위험
분 단위는 minutes_between(반올림)을 따름. 출발 알람 문구(ring-package)와 기상 뒤 화면이 같은 함수를 씀
입력 · 결과는 JSON 모양 dict (DepartSlackInput = appointmentAt · travelMinutes · now · remainingMinutes)
"""

from typing import Any

from .time import add_minutes, minutes_between

__all__ = [
    "DEPART_COMFORT_MIN_SLACK",
    "DEPART_STATUS",
    "DEPART_TIGHT_MIN_SLACK",
    "depart_now",
    "depart_slack",
    "depart_status",
]

# 7장 7.5.2 — 이 분 이상 남으면 여유, 0 이상 이만큼 미만이면 빠듯, 0 미만이면 지각 위험
DEPART_COMFORT_MIN_SLACK = 10
DEPART_TIGHT_MIN_SLACK = 0

DEPART_STATUS = {"COMFORT": "COMFORT", "TIGHT": "TIGHT", "LATE_RISK": "LATE_RISK"}


def depart_status(slack_minutes: float) -> str:
    if slack_minutes >= DEPART_COMFORT_MIN_SLACK:
        return DEPART_STATUS["COMFORT"]
    if slack_minutes >= DEPART_TIGHT_MIN_SLACK:
        return DEPART_STATUS["TIGHT"]
    return DEPART_STATUS["LATE_RISK"]


def depart_slack(input: dict[str, Any]) -> dict[str, Any]:
    """지금 시작해서 남은 준비를 마치고 나가도 되는 만큼의 여유 (음수면 이미 늦음)
    input = {appointmentAt, travelMinutes, now, remainingMinutes(나가기 전까지 남은 준비 분. 지금 나가면 0)}"""
    latest_departure = add_minutes(input["appointmentAt"], -input["travelMinutes"])
    ready_at = add_minutes(input["now"], input["remainingMinutes"])
    slack_minutes = (
        minutes_between(ready_at, latest_departure) + 0
    )  # JS는 반올림이 낸 -0을 0으로 (+ 0). 정수라 -0이 없음
    return {"slackMinutes": slack_minutes, "status": depart_status(slack_minutes)}


def depart_now(input: dict[str, Any]) -> dict[str, Any]:
    """지금 바로 나가면 (남은 준비 0): 도착 시각과 약속까지의 여유 (slack = 약속 − 도착)"""
    r = depart_slack({**input, "remainingMinutes": 0})
    return {
        "arriveAt": add_minutes(input["now"], input["travelMinutes"]),
        "slackMinutes": r["slackMinutes"],
        "status": r["status"],
    }

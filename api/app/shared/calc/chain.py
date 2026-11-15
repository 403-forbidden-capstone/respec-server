"""본 레포 supabase/functions/_shared/calc/chain.ts의 서버판 (기준 b140f70) — ALG-01 알람 역산

출발 = 약속 − 여유 − 이동, 기상 = 출발 − 준비(기상~출발), 준비 알람 = 기상 + 준비 전 활동, 도착 목표 = 약속 − 여유
그날 첫 일정이 아니면(기상 알람 없음) 준비 알람만: 출발 − 준비
준비 알람 당김(prepareAdvanceMinutes, 안건 D111 1 · 2번): 준비 알람을 그만큼 앞당김 — 준비 전 활동에서 먼저 가져오고, 모자란 만큼만 기상도 앞당김
  준비 전 활동을 다 써도 준비 알람은 남김(기상과 같은 시각) — 준비 시작 기록(#490)과 다음 제안의 대상이 그 알람이라서
kinds가 있으면 H05에서 고른 종류만 — 도착 목표는 울리는 알람이 하나라도 있을 때만, 하나도 없으면 빈 목록(알람 없는 일정). 빈 목록도 "있음"임에 주의(JS의 [] 는 truthy)
manualTimes가 있으면 직접 고른 시각이 자동 계산보다 먼저. 직접 고른 알람은 auto에 자동 계산값을 남김
  준비 알람의 자동값은 (직접 고른 시각을 포함한) 기상 시각 뒤 — 기상을 옮기면 준비도 같은 간격으로 따라옴
문서(7장 7.1.1)와 다른 점은 목업 쪽을 따름: 도착 알람 = 약속 − 여유, 준비 시간 = 기상~출발 한 덩어리 (F1 → D56)
"""

from datetime import datetime

from .time import add_minutes, same_time
from .types import AlarmKind, ChainAlarm, ChainInput, RingKind

__all__ = ["alarm_chain", "departure_at", "departure_auto", "manual_times"]


def departure_auto(input: ChainInput) -> datetime:
    return add_minutes(input["at"], -(input["bufferMinutes"] + input["travelMinutes"]))


def alarm_chain(input: ChainInput) -> list[ChainAlarm]:
    depart = departure_auto(input)
    manual = input.get("manualTimes")

    def alarm(kind: AlarmKind, auto: datetime) -> ChainAlarm:
        picked = (
            None if kind == "ARRIVE" or manual is None else manual.get(kind)
        )  # 도착 목표는 약속에 묶여 직접 고르지 않음
        if picked is not None and not same_time(picked, auto):
            return {"kind": kind, "at": picked, "auto": auto}
        return {"kind": kind, "at": auto}

    result: list[ChainAlarm] = []
    advance = max(0, input.get("prepareAdvanceMinutes") or 0)
    if input["withWake"]:
        from_before = min(advance, input["beforePrepareMinutes"])
        wake = alarm("WAKE", add_minutes(depart, -(input["prepareMinutes"] + advance - from_before)))
        result.append(wake)
        if input["beforePrepareMinutes"] > 0:
            result.append(
                alarm("PREPARE", add_minutes(wake["at"], input["beforePrepareMinutes"] - from_before))
            )
    else:
        result.append(alarm("PREPARE", add_minutes(depart, -(input["prepareMinutes"] + advance))))
    result.append(alarm("DEPARTURE", depart))
    result.append(alarm("ARRIVE", add_minutes(input["at"], -input["bufferMinutes"])))
    kinds = input.get("kinds")
    if kinds is None:
        return result
    made = [x for x in result if x["kind"] == "ARRIVE" or x["kind"] in kinds]
    return made if any(x["kind"] != "ARRIVE" for x in made) else []


def departure_at(input: ChainInput) -> datetime:
    """출발 시각 (직접 고른 출발이 있으면 그것). DEPARTURE 알람을 만들지 않아도 단축 준비 안내(departAt) · 출발 지남 검사에 씀"""
    full = alarm_chain({k: v for k, v in input.items() if k != "kinds"})  # type: ignore[typeddict-item]
    found = next((x for x in full if x["kind"] == "DEPARTURE"), None)
    return found["at"] if found else departure_auto(input)


def manual_times(alarms: list[ChainAlarm]) -> dict[RingKind, datetime]:
    """직접 고른 시각만 모음 — 자동 계산값과 같으면 직접 고른 것이 아님"""
    times: dict[RingKind, datetime] = {}
    for x in alarms:
        if x["kind"] != "ARRIVE" and x.get("auto") is not None:
            times[x["kind"]] = x["at"]  # type: ignore[literal-required]
    return times

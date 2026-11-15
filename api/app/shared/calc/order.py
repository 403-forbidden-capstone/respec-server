"""본 레포 supabase/functions/_shared/calc/order.ts의 서버판 (기준 b140f70) — 알람 시각을 직접 고르기 + 순서 확인

앱의 features/alarm/adjust.ts(D42 C · D43, 전체작업계획 8.1 F5 해석)를 그대로 옮김
  · 직접 고른 시각은 "직접 고름"(auto에 자동값)으로 표시하고 자동 계산보다 먼저 씀. 자동값과 같아지면 다시 자동
  · 기상을 옮기면 준비도 같은 간격으로 따라옴. 출발 · 도착 목표는 약속에 묶여 그대로
  · 순서(기상 ≤ 준비 ≤ 출발 ≤ 도착 목표)가 뒤집히면 저장을 막음 (같은 시각은 허용, 만든 알람만 비교)
"""

from datetime import datetime
from typing import TypedDict

from ..js import from_ms, ms
from .time import same_time
from .types import KIND_LABEL, AlarmKind, ChainAlarm, RingKind

__all__ = ["OrderError", "auto_time", "is_manual_time", "move_alarm", "order_error", "reset_alarm"]

_ORDER: tuple[AlarmKind, ...] = ("WAKE", "PREPARE", "DEPARTURE", "ARRIVE")


def auto_time(x: ChainAlarm) -> datetime:
    """자동으로 계산했다면 이 시각 — 직접 고르지 않았으면 지금 시각"""
    auto = x.get("auto")
    return auto if auto is not None else x["at"]


def is_manual_time(x: ChainAlarm) -> bool:
    """직접 고른 시각인지"""
    return x.get("auto") is not None


def _with_time(x: ChainAlarm, at: datetime) -> ChainAlarm:
    """시각을 바꾼 알람 — 자동 계산값과 같으면 직접 고른 것이 아님"""
    base = auto_time(x)
    if same_time(at, base):
        return {"kind": x["kind"], "at": at}
    return {"kind": x["kind"], "at": at, "auto": base}


def _shifted(x: ChainAlarm, delta_ms: int) -> ChainAlarm:
    """자동 계산값까지 같이 옮김 — 기상을 옮기면 따라오는 준비 알람 (준비의 자동값도 기상 뒤로 계산하므로)"""

    def move(d: datetime) -> datetime:
        return from_ms(ms(d) + delta_ms)

    auto = x.get("auto")
    if auto is not None:
        return {"kind": x["kind"], "at": move(x["at"]), "auto": move(auto)}
    return {"kind": x["kind"], "at": move(x["at"])}


def move_alarm(alarms: list[ChainAlarm], kind: RingKind, to: datetime) -> list[ChainAlarm]:
    """kind 알람을 to로 옮김. 기상이면 준비 알람도 같은 간격만큼 같이 옮김"""
    target = next((x for x in alarms if x["kind"] == kind), None)
    if target is None:
        return list(alarms)
    delta = ms(to) - ms(target["at"])
    out: list[ChainAlarm] = []
    for x in alarms:
        if x["kind"] == kind:
            out.append(_with_time(x, to))
        elif kind == "WAKE" and x["kind"] == "PREPARE":
            out.append(_shifted(x, delta))
        else:
            out.append(x)
    return out


def reset_alarm(alarms: list[ChainAlarm], kind: RingKind) -> list[ChainAlarm]:
    """직접 고른 시각을 자동 계산값으로 되돌림 (기상이면 따라온 준비도 함께 되돌아감)"""
    target = next((x for x in alarms if x["kind"] == kind), None)
    return move_alarm(alarms, kind, auto_time(target)) if target is not None else list(alarms)


class OrderError(TypedDict):
    earlier: AlarmKind  # 앞이어야 하는 알람
    later: AlarmKind  # 뒤여야 하는데 앞서 버린 알람
    message: str  # 사용자에게 보일 짧은 이유


def order_error(alarms: list[ChainAlarm]) -> OrderError | None:
    """순서가 뒤집혔으면 어느 둘이 어긋났는지, 맞으면 None — 저장을 막을 때 씀"""
    sorted_alarms = [x for kind in _ORDER for x in alarms if x["kind"] == kind]
    for i in range(1, len(sorted_alarms)):
        prev = sorted_alarms[i - 1]
        nxt = sorted_alarms[i]
        if ms(nxt["at"]) >= ms(prev["at"]):
            continue
        if nxt["kind"] == "ARRIVE":
            message = f"{KIND_LABEL[prev['kind']]} 시각이 도착 목표보다 늦을 수 없어요"
        else:
            message = f"{KIND_LABEL[nxt['kind']]} 시각이 {KIND_LABEL[prev['kind']]}보다 빠를 수 없어요"
        return {"earlier": prev["kind"], "later": nxt["kind"], "message": message}
    return None

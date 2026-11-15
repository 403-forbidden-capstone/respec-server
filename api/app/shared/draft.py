"""본 레포 supabase/functions/_shared/draft.ts의 서버판 (기준 b140f70) — 알람 초안 읽기(출발 · 이동 · 여유 칸, 알람 칸, 반복 규칙)

alarm-suggest(5.4.1)와 alarm-commit(5.4.2)이 같은 칸을 같은 이름으로 읽음.
시각은 ISO 8601 + 오프셋, 값 이름은 DB와 같음. 읽은 값 모양은 dataclass (없음 = None — TS의 undefined와 같게 다룸).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.read import Reader
from app.shared.calc.recur import RRULE_PATTERN
from app.shared.calc.types import INTENSITY_AXES, RING_KINDS

TRANSPORTS = ("WALK", "TRANSIT", "BUS", "SUBWAY", "CAR")
IMPORTANCES = ("LOW", "NORMAL", "HIGH")

# 일정 반복 규칙 (D44) — appointments.recurrence_rule · alarms.recurrence_rule. 두 모양만 받음: FREQ=DAILY · FREQ=WEEKLY;BYDAY=MO,… (정의는 calc/recur.py)
__all__ = [
    "IMPORTANCES",
    "RRULE_PATTERN",
    "TRANSPORTS",
    "AlarmFields",
    "ParticipantFields",
    "PlaceInput",
    "read_alarm_fields",
    "read_participant_fields",
    "read_place",
    "read_recurrence",
]


@dataclass
class PlaceInput:
    """name · lat · lng · radius_m(TS의 radiusM) — 목적지 감지 반경(m, 100~500)은 새 약속의 destination에만 읽음 (appointments.dest_radius_m, D68)"""

    name: str | None = None
    lat: float | None = None
    lng: float | None = None
    radius_m: int | None = None


@dataclass
class ParticipantFields:
    origin: PlaceInput | None = None
    transport: str | None = None
    buffer_minutes: int | None = None
    before_prepare_minutes: int | None = None
    with_wake: bool | None = None


@dataclass
class AlarmFields:
    kinds: list[str] | None = None  # 없음 = 지금 있는 종류 그대로(없으면 계산된 전부), [] = 알람 없는 일정
    enabled: dict[str, bool] | None = None  # 종류별 켜기 · 끄기 (없으면 지금 상태 유지)
    # 직접 고른 알람 시각. 이 칸 전체가 없으면 저장된 직접 값을 그대로 둠, 있으면 이 칸이 전부 (없는 종류 · None = 자동으로 되돌림)
    manual_times: dict[str, datetime | None] | None = None
    # 직접 고른 강도 단계(0 약 · 1 중 · 2 강). 이 칸 전체가 없으면 저장된 직접 값을 그대로 둠, None = 자동
    levels: dict[str, int | None] | None = None


class _Unset:
    """TS `undefined` — 칸이 없거나 읽기에 실패함 (None = 명시적 null과 구분)"""

    _instance: "_Unset | None" = None

    def __new__(cls) -> "_Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


def read_place(
    parent: Reader,
    key: str,
    *,
    required: bool = False,
    name_required: bool = False,
    radius: bool = False,
) -> PlaceInput | None:
    r = parent.object(key, required=required)
    if r is None:
        return None
    name = r.string("name", max=200, required=name_required)
    lat = r.number("lat", min=-90, max=90)
    lng = r.number("lng", min=-180, max=180)
    if (lat is None) != (lng is None):
        r.problem_at("lat" if lat is None else "lng", "lat · lng는 같이 있어야 해요")
    radius_m = r.int("radius_m", min=100, max=500) if radius else None
    return PlaceInput(
        name=name if name else None,
        lat=lat if lat is not None and lng is not None else None,
        lng=lng if lat is not None and lng is not None else None,
        radius_m=radius_m,
    )


def read_participant_fields(r: Reader) -> ParticipantFields:
    origin = read_place(r, "origin")
    transport = r.one_of("transport_type", TRANSPORTS)
    buffer_minutes = r.int("buffer_minutes", min=0, max=240)
    before_prepare_minutes = r.int("before_prepare_minutes", min=0, max=240)
    with_wake = r.bool("with_wake")
    return ParticipantFields(
        origin=origin,
        transport=transport,
        buffer_minutes=buffer_minutes,
        before_prepare_minutes=before_prepare_minutes,
        with_wake=with_wake,
    )


def read_alarm_fields(r: Reader) -> AlarmFields:
    out = AlarmFields()

    kinds = r.string_list("kinds", RING_KINDS, max=3)
    if kinds is not None:
        out.kinds = kinds

    enabled = r.object("enabled")
    if enabled is not None:
        out.enabled = {}
        for kind in RING_KINDS:
            v = enabled.bool(kind)
            if v is not None:
                out.enabled[kind] = v

    times = r.object("manual_times")
    if times is not None:
        out.manual_times = {}
        for kind in RING_KINDS:
            if times.is_null(kind):
                out.manual_times[kind] = None
            else:
                at = times.date(kind)
                if at:
                    out.manual_times[kind] = at

    levels = r.object("levels")
    if levels is not None:
        out.levels = {}
        for axis in INTENSITY_AXES:
            if levels.is_null(axis):
                out.levels[axis] = None
            else:
                v = levels.int(axis, min=0, max=2)
                if v is not None:
                    out.levels[axis] = v
    return out


def read_recurrence(r: Reader, key: str = "recurrence_rule") -> Any:
    """None(null) · 문자열 · 없음 — 없음은 EMPTY(TS undefined)로 구분: null = 반복 끔, 없음 = 안 건드림"""
    if r.is_null(key):
        return None
    value = r.string(key, max=100, pattern=RRULE_PATTERN)
    return value if value is not None else UNSET

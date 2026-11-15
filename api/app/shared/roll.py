"""본 레포 supabase/functions/_shared/roll.ts의 서버판 (기준 b140f70) — 반복 다음 회차 만들기 (alarm-roll의 본문)

반복 약속 · 일정 없는 반복 알람은 앞으로 14일치 회차(약속 시각 ∈ (지금, 지금 + 14일))를 회차마다 새 행으로 미리 만들고, 지난 회차 행은 기록으로 남김 (D4 A).
루틴은 회차 행을 따로 만들지 않고 같은 알람 행의 시각만 다음 발생으로 옮김.
두 길 — 같은 코드를 쓰고 읽는 클라이언트와 한도만 다름
  본인 길(own)  앱이 syncNow · 반복 일정 저장 직후 부름. 읽기는 부른 사람의 RLS 클라이언트, 쓰기는 service_role. 길찾기 최대 4회차, 응답에 폰에 걸 꾸러미
  배치 길(batch)  5분마다 (pg_cron 또는 서버 스케줄러). service_role이라 RLS를 안 타므로 모든 읽기 · 쓰기를 사용자(creator_id · user_id)로 직접 좁힘
회차 하나를 쓰는 건 DB 함수 roll_insert_occurrence(이미 있으면 null) → 같은 요청을 두 번 보내도 같은 결과 (같은 (series_id, occurrence_on) 중복 없음).
하루 경계는 profiles.timezone(없으면 Asia/Seoul) 0시.

다른 모듈에서 가져다 쓰는 이름
  .calc: RING_KINDS · Occurrence({date, at} dict) · add_days_to_key · next_occurrences(rule=, baseAt=, now=, timeZone=, afterDate=) ·
         next_routine_at(repeatDays=, stepOffsetMinutes=, now=, timeZone=) · parse_recurrence
  .plan_core: PlanDraft · PlanInputs(dataclass) · compute_plan · resolve_basics(draft=, profile=, appointment=, participant=) · without_friend_times
  .route: ResolvedTravel(chosen=, auto=, warnings=) · TravelChoice(minutes=, source=, needs_confirmation=, note=) · resolve_auto_travel(args dict, TravelDeps)
"""

import asyncio
import functools
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict

from ..core.errors import AppError
from ..core.read import Reader
from ..kakao import KakaoError
from .calc import (
    RING_KINDS,
    Occurrence,
    add_days_to_key,
    next_occurrences,
    next_routine_at,
    parse_recurrence,
)
from .commit_core import diff_alarms, resolve_enabled
from .db import db_fail, must
from .draft import PlaceInput
from .edit import pick_base_row
from .js import from_ms, js_round, ms, parse_iso, to_iso
from .plan import (
    ALARM_COLUMNS,
    SERIES_APPOINTMENT_COLUMNS,
    load_alarms,
    load_late_count,
    load_participant,
    load_prepare_stats,
    load_profile,
    load_same_day_wake,
    load_series_appointment,
    load_series_appointments,
    load_travel_samples,
)
from .plan_core import PlanDraft, PlanInputs, compute_plan, resolve_basics, without_friend_times
from .ring_package import build_ring_packages, subtitle_for
from .route import ResolvedTravel, TravelChoice, TravelDeps, resolve_auto_travel
from .supabase import Db
from .tz import local_date_key, local_day_range, local_minute_of_day, safe_time_zone, zoned_time

log = logging.getLogger("respec.roll")

# ---------------------------------------------------------------------------
# 한도 · 상수
# ---------------------------------------------------------------------------

PendingReason = Literal["ROUTE_RETRY", "DAILY_BUDGET", "ALARM_CAP", "RUN_LIMIT"]

# 길찾기 실패 뒤 다시 해 보는 간격 (실패 1번째 → 1시간 뒤, 2번째 → 3시간, 3번째부터 → 6시간)
RETRY_HOURS = (1, 3, 6)
# 첫 알람이 이 시간 안으로 다가왔는데도 길찾기가 안 되면 내 실측 중앙값(CACHED) → 직선 근사(ESTIMATE)로 만듦 (7장 7.1.2 폴백 순서)
FALLBACK_BEFORE_FIRST_ALARM_HOURS = 12
# 사용자 한 명이 하루(내 시간대)에 roll로 쓰는 길찾기 수 — 작성자 판단. 전체 합계 한도는 kakao_calls 로그(L8 하루 집계)로 보고 정함
DAILY_LOOKUP_BUDGET = 300
# 앞으로 켜진 알람이 이만큼이면 더 안 만듦 (폰 예약 앱당 500개 · reconcile 읽기 상한 500줄 아래) — 작성자 판단
UPCOMING_ALARM_CAP = 400


class RollLimits(TypedDict):
    lookups: int  # 이번 실행에서 부를 수 있는 길찾기 수
    series: float  # 이번 실행에서 다룰 "일이 있는" 묶음 수 (할 일 없는 묶음은 안 셈). 본인 길은 무제한(inf)
    fetch: int  # roll_due_series에서 읽어 올 후보 수 (일이 있는 묶음 수보다 넉넉히)
    ms: int | None  # 이 시간(ms)이 지나면 남은 회차는 다음 실행으로 (None = 제한 없음)
    dailyLookups: int  # 사용자 한 명의 하루 길찾기 몫 (DAILY_LOOKUP_BUDGET)
    alarmCap: int  # 앞으로 켜진 알람 상한 (UPCOMING_ALARM_CAP)


ROLL_LIMITS: dict[str, RollLimits] = {
    "own": {
        "lookups": 4,
        "series": math.inf,
        "fetch": 0,
        "ms": None,
        "dailyLookups": DAILY_LOOKUP_BUDGET,
        "alarmCap": UPCOMING_ALARM_CAP,
    },
    # 후보를 50개보다 많이 읽는 이유: roll_due_series는 "마지막 회차가 +14일 안"인 묶음을 모두 돌려줘서, 이번에 만들 회차가 없는 주간 묶음도 5분마다 후보에 듦.
    # 그런 빈 묶음이 앞줄을 채워도 일이 있는 묶음이 밀리지 않게 넉넉히 읽고, 빈 묶음은 기준 회차 한 줄만 읽고 넘김 (50 한도에 안 셈)
    "batch": {
        "lookups": 20,
        "series": 50,
        "fetch": 200,
        "ms": 40_000,
        "dailyLookups": DAILY_LOOKUP_BUDGET,
        "alarmCap": UPCOMING_ALARM_CAP,
    },
}

# (origin=, destination=, transport=) → {"minutes": int, "partial": bool}  (TS: 인자 하나짜리 객체 → 키워드 인자)
TravelLookup = Callable[..., Awaitable[dict[str, Any]]]


@dataclass
class RollDeps:
    path: Literal["own", "batch"]
    now: datetime
    read: Db  # 읽기 클라이언트 — own: 부른 사람의 RLS, batch: service_role
    write: Db  # service_role (쓰기 · roll_attempts · DB 함수)
    lookup: TravelLookup | None = (
        None  # 카카오 길찾기 1번. 키가 없으면 안 줌 → 길찾기가 늘 안 되는 것으로 침(AUTH와 같음)
    )
    limits: dict[str, Any] | None = None  # RollLimits 일부 (Partial)
    clock: Callable[[], float] | None = None  # 실행 시간 재기 (ms) — 시험이 바꿔 끼움
    log: Callable[[str], None] | None = None  # 실행마다 한 줄 (JSON) — L8이 하루 집계에 씀
    warn: Callable[[str], None] | None = None  # 묶음 하나를 건너뛴 일 (JSON, 이유 글자만)


class RollCreated(TypedDict, total=False):
    series_id: str
    occurrence_on: str
    appointment_id: str | None
    alarm_id: str  # 일정 없는 알람 회차에만 (키가 없을 수 있음)


class RollPending(TypedDict):
    series_id: str
    occurrence_on: str
    reason: str


# 응답 (설계 메모 4절) — 앱 applyCommit이 그대로 받는 alarm-commit 모양을 따름
class RollResult(TypedDict):
    created: list[RollCreated]
    moved_alarm_ids: list[str]
    pending: list[RollPending]
    ring_packages: list[dict[str, Any]]  # 본인 길만 (이번에 만들거나 옮긴 켜진 알람). 배치 길은 빈 목록
    canceled_alarm_ids: list[str]  # 늘 빈 목록 (지우는 일 없음)
    kakao_calls: int


def read_roll_request(r: Reader) -> dict[str, str]:
    series_id = r.uuid("series_id")
    return {"seriesId": series_id} if series_id else {}


# ---------------------------------------------------------------------------
# 순수 계산
# ---------------------------------------------------------------------------

HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def _js_number(v: Any) -> float | int:
    """JS `Number(v)` — null은 0, 글자는 숫자로 읽고 못 읽으면 NaN, bool은 0 · 1"""
    if v is None:
        return 0
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int | float):
        return v
    if isinstance(v, str):
        text = v.strip()
        if not text:
            return 0
        try:
            return float(text)
        except ValueError:
            return math.nan
    return math.nan


def _finite(v: Any) -> bool:
    """JS `typeof v === 'number' && Number.isFinite(v)` — bool은 숫자가 아님"""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:  # 아주 큰 정수
        return False


# 길찾기가 실패한 횟수 → 다음 시도 시각
def retry_at(now: datetime, failures: int) -> datetime:
    return from_ms(ms(now) + RETRY_HOURS[min(max(failures, 0), len(RETRY_HOURS) - 1)] * HOUR_MS)


# 루틴 steps(JSON) → 단계마다 시작 시각 (0시부터 분, 자정을 넘으면 1440 이상). 순서는 단계 순서(= alarms.step_order − 1)
#   D29 임시 모양: routines에 시작 시각 칸이 없고 steps 모양이 정해지지 않아(D29 열림) 앱 mobile/src/features/routine/map.ts parseSteps · planRoutineAlarms와 같은 규칙으로 읽음.
#   order가 있으면 그 순서(없으면 들어 있는 순서), 첫 단계의 offset이 루틴 시작 시각(없으면 21:30), 단계가 걸리는 시간은 minutes → 다음 단계 offset과의 간격 → 5분,
#   다음 단계는 앞 단계 시각 + 걸리는 시간. D29가 정해져 모양이 바뀌면 여기와 map.ts를 같이 고침 (루틴 단계 시각을 읽는 곳은 이 함수 하나)
ROUTINE_DEFAULT_START_MINUTE = 21 * 60 + 30
ROUTINE_DEFAULT_STEP_MINUTES = 5


def routine_step_starts(steps: Any) -> list[int] | None:
    if not isinstance(steps, list):
        return None
    indexed = [(raw, index) for index, raw in enumerate(steps) if isinstance(raw, dict)]

    def compare(a: tuple[dict, int], b: tuple[dict, int]) -> float:
        # JS 정렬 비교 함수 그대로 (order가 둘 다 숫자일 때만 order 차이, 아니면 들어 있는 순서)
        if _finite(a[0].get("order")) and _finite(b[0].get("order")):
            return a[0]["order"] - b[0]["order"]
        return a[1] - b[1]

    items = [raw for raw, _ in sorted(indexed, key=functools.cmp_to_key(compare))]
    if not items:
        return None
    first_offset = items[0].get("offset")
    at = js_round(first_offset) if _finite(first_offset) else ROUTINE_DEFAULT_START_MINUTE
    out: list[int] = []
    for i, raw in enumerate(items):
        start = at
        nxt = items[i + 1].get("offset") if i + 1 < len(items) else None
        gap = js_round(nxt - raw["offset"]) if _finite(raw.get("offset")) and _finite(nxt) else 0
        minutes = raw.get("minutes")
        if _finite(minutes) and minutes > 0:
            at += js_round(minutes)
        elif gap > 0:
            at += gap
        else:
            at += ROUTINE_DEFAULT_STEP_MINUTES
        out.append(start)
    return out


# 기준 회차의 직접 고른 시각(MANUAL)을 새 회차 날짜의 같은 시:분으로 — 약속 날짜와 알람 날짜가 다른 알람(새벽 1시 약속의 전날 밤 기상)은 그 날 수 차이를 그대로 둠
def shift_manual_times(
    alarms: list[dict[str, Any]], base_at: datetime, occurrence_date: str, time_zone: str
) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    base_key = local_date_key(base_at, time_zone)
    for a in alarms:
        if a.get("time_source") != "MANUAL" or a.get("alarm_type") not in RING_KINDS:
            continue
        at = parse_iso(a["scheduled_at"])
        day_key = local_date_key(at, time_zone)
        offset_days = js_round(
            (ms(parse_iso(f"{day_key}T00:00:00Z")) - ms(parse_iso(f"{base_key}T00:00:00Z"))) / DAY_MS
        )
        out[a["alarm_type"]] = zoned_time(
            add_days_to_key(occurrence_date, offset_days), local_minute_of_day(at, time_zone), time_zone
        )
    return out


# 기준 회차의 만든 알람 종류. 기준 회차가 그날 첫 일정이 아니어서 기상이 없던 것이지 사용자가 기상을 안 만든 게 아니라면, 새 회차가 그날 첫 일정일 때 기상을 더함
#   L6 안건후보: 기준 회차에 기상이 없을 때 기본안 = 기준 회차가 "그날 다른 기상에 밀려" 없던 거면 새 회차가 첫 일정일 때 기상을 만들고, 그 밖(사용자가 안 고름)이면 없는 채로 이어받음
def inherited_kinds(
    alarms: list[dict[str, Any]], *, with_wake: bool, base_had_earlier_wake: bool
) -> list[str]:
    kinds = [k for k in RING_KINDS if any(a.get("alarm_type") == k for a in alarms)]
    if with_wake and base_had_earlier_wake and kinds and "WAKE" not in kinds:
        return ["WAKE", *kinds]
    return kinds


# 기준 회차에서 끈 종류는 끈 채로 (H02에서 끈 것). 시각이 이미 지나서 꺼진 것은 사용자가 끈 게 아니라고 보고 켬 — resolve_enabled(commit_core.py)와 같은 규칙
def inherited_enabled(alarms: list[dict[str, Any]], now: datetime) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for a in alarms:
        if a.get("alarm_type") in RING_KINDS:
            out[a["alarm_type"]] = resolve_enabled(checked_enabled=True, old=a, now=now)
    return out


# 반복 다음 회차의 자동 준비시간이 직전 회차에서 움직일 수 있는 폭 (분) — D62 처음 2주 몫
PREPARE_DAILY_STEP_MAX = 10


@dataclass
class RollPlanCtx:
    user_id: str  # 만든 사람 — 본인 길은 검증한 로그인 사용자, 배치는 roll_due_series가 준 사용자. 요청 본문에서 받지 않음
    now: datetime
    profile: dict[str, Any]
    time_zone: str
    stats: Any  # PlanInputs.stats (prepare_stats_of 결과)
    late_count4w: int


# 기준 회차 = 묶음에서 occurrence_on이 가장 늦은 행 (약속 + 내 참여 조건 + 그 알람 + 준비물 제목)
@dataclass
class RollBase:
    appointment: dict[str, Any]
    participant: dict[str, Any]
    alarms: list[dict[str, Any]]
    items: list[str]


@dataclass
class PlannedOccurrence:
    """ok=True면 plan · inputs · alarms(roll_insert_occurrence의 p_alarms) · payload, ok=False면 reason(DEPARTURE_PASSED | ORDER) · plan"""

    ok: bool
    plan: Any
    inputs: Any = None
    alarms: list[dict[str, Any]] | None = None
    payload: dict[str, Any] | None = None  # {appointment, participant, items: [{title, due_date}]}
    reason: str | None = None


# 이동시간 → 계산에 넘기는 모양 (buildPlan이 길찾기 값을 쓸 때와 같음)
def resolved_travel_of(chosen: Any) -> Any:
    return ResolvedTravel(chosen=chosen, auto=chosen, warnings=[])


# 새 회차 하나의 계획 — 설계 메모 2절 이어받기 표대로. 계산은 alarm-commit · alarm-suggest가 쓰는 compute_plan · diff_alarms 그대로 (따로 규칙을 만들지 않음)
#   이어받음: 만들 종류 · 끈 종류 · 직접 강도(단계 그대로) · 직접 시각(같은 시:분) · 출발지 · 이동 수단 · 여유 · 직접 이동시간(travel에 MANUAL로 넘어옴) · 준비물 · 제목 · 장소 · 중요도 · 메모 · 반경 · 규칙
#   새로 구함: 이동시간(API) · 준비시간(첫 값 + 개인 보정, D56-3 A — 단 기준 회차 준비값 ±10분 안에서만, D62 처음 2주 몫) · 그날 첫 일정(회차 날짜 기준 hasEarlierWake)
#   travel = 이 회차에 쓸 이동시간, same_day_wake = 이 회차 날짜에 약속 시각이 더 앞선 켜진 기상이 있나, base_had_earlier_wake = 기준 회차가 그랬나(inherited_kinds)
def plan_occurrence(
    ctx: RollPlanCtx,
    base: RollBase,
    occ: Occurrence,
    *,
    travel: Any,
    same_day_wake: bool,
    base_had_earlier_wake: bool | None = None,
) -> PlannedOccurrence:
    now, profile, time_zone = ctx.now, ctx.profile, ctx.time_zone
    appointment, participant = base.appointment, base.participant
    # 기준 회차에 친구가 제안해 주인이 적용한 시각이 있어도 새 회차에는 이어받지 않음 — 친구가 바꾸기 전 모습(시각 · time_source)에서 이어받음 (한 번의 제안이 반복 전체에 퍼지지 않게)
    alarms = without_friend_times(base.alarms)

    def num(v: Any) -> float | int | None:
        return None if v is None else _js_number(v)

    dest_lat = num(appointment.get("dest_lat"))
    dest_lng = num(appointment.get("dest_lng"))

    def draft_of(manual_times: dict[str, datetime]) -> Any:
        destination = PlaceInput(name=appointment.get("dest_name"))
        if dest_lat is not None and dest_lng is not None:
            destination = PlaceInput(name=appointment.get("dest_name"), lat=dest_lat, lng=dest_lng)
        return PlanDraft(
            at=occ["at"],
            destination=destination,
            importance=appointment.get("importance"),
            kinds=inherited_kinds(
                alarms, with_wake=not same_day_wake, base_had_earlier_wake=base_had_earlier_wake is True
            ),
            manual_times=manual_times,
        )

    # D62 처음 2주 몫: 하루에 움직이는 폭 상한 — 새 회차의 자동 준비시간은 직전(기준) 회차의 준비값 ±10분 안에서만 (학습 루프 기초 1절 ④)
    #   기준 회차에 기상 알람이 있어서 준비값이 "기상~출발"일 때만 비교함 — 기상 없는 회차(공통 15분)와 섞어 비교하면 15 ↔ 60처럼 엉뚱하게 당겨지므로.
    #   L6 안건후보: 기준 회차에 기상 행이 없는 묶음의 상한 기본안 = 상한을 걸지 않음 (기준이 기상 없는 회차인지 알 수 없음)
    prepare_bound = (
        {"around": _js_number(participant.get("prepare_minutes")), "within": PREPARE_DAILY_STEP_MAX}
        if any(a.get("alarm_type") == "WAKE" for a in alarms)
        else None
    )

    def plan_with(manual_times: dict[str, datetime]) -> tuple[Any, Any]:
        extra: dict[str, Any] = {}
        if prepare_bound is not None and _finite(prepare_bound["around"]):
            extra["prepare_bound"] = prepare_bound
        inputs = PlanInputs(
            now=now,
            draft=draft_of(manual_times),
            profile=profile,
            participant=participant,  # 출발지 · 이동 수단 · 여유를 이 참여 조건에서 이어받음 (resolve_basics)
            existing_alarms=alarms,  # 직접 강도 · 준비 전 활동 간격을 기준 회차 알람에서 읽음
            stats=ctx.stats,
            late_count4w=ctx.late_count4w,
            same_day_wake=same_day_wake,
            **extra,
        )
        return inputs, compute_plan(inputs, resolved_travel_of(travel), "save")

    manual = shift_manual_times(alarms, parse_iso(appointment["appointment_at"]), occ["date"], time_zone)
    inputs, plan = plan_with(manual)
    # L6 안건후보: 직접 시각이 새 회차에서 순서가 어긋날 때 기본안 = 이동시간이 달라져 직접 고른 기상이 출발보다 늦어지는 식이면, 직접 시각을 버리고 자동 시각으로 만듦
    if not plan.check["ok"] and plan.check["reason"] == "ORDER" and len(manual):
        inputs, plan = plan_with({})
    if not plan.check["ok"]:
        return PlannedOccurrence(ok=False, reason=plan.check["reason"], plan=plan)

    writes = diff_alarms(
        plan, [], now=now, enabled=inherited_enabled(alarms, now), replace_existing=False
    ).insert
    basics = plan.basics
    origin = basics.origin
    return PlannedOccurrence(
        ok=True,
        plan=plan,
        inputs=inputs,
        alarms=writes,
        payload={
            "appointment": {
                "creator_id": ctx.user_id,  # roll_insert_occurrence는 security definer라 이 값을 그대로 믿음 — 읽어 온 행이 아니라 검증된 사용자에서
                "group_id": None,  # 공동 약속은 반복이어도 첫 회차만 — 개인 약속만 회차를 만듦
                "series_id": appointment.get("series_id"),
                "occurrence_on": occ["date"],
                "title": appointment.get("title"),
                "appointment_at": to_iso(occ["at"]),
                "dest_name": appointment.get("dest_name"),
                "dest_lat": appointment.get("dest_lat"),
                "dest_lng": appointment.get("dest_lng"),
                "dest_radius_m": appointment.get("dest_radius_m"),
                "importance": basics.importance,
                "recurrence_rule": appointment.get("recurrence_rule"),
                "memo": appointment.get("memo"),
            },
            "participant": {
                "origin_name": origin.name,
                "origin_lat": origin.lat,
                "origin_lng": origin.lng,
                "transport_type": basics.transport,
                "est_travel_minutes": plan.travel_minutes,
                "travel_source": travel.source,
                "prepare_minutes": plan.prepare["minutes"],
                "buffer_minutes": basics.buffer_minutes,
            },
            "items": [{"title": title, "due_date": occ["date"]} for title in base.items],
        },
    )


# 이 회차의 첫 알람 시각 — 알람이 없는 회차(kinds = [])는 출발 시각
def first_alarm_at(plan: Any) -> datetime:
    return from_ms(min(ms(a["at"]) for a in plan.chain)) if plan.chain else plan.departure


# ---------------------------------------------------------------------------
# 읽기 도우미 (모두 사용자로 좁힘)
# ---------------------------------------------------------------------------

STANDALONE_COLUMNS = f"{ALARM_COLUMNS}, series_id, occurrence_on"
ROUTINE_COLUMNS = "routine_id, title, repeat_days, steps, is_active"


class RollTask(TypedDict):
    user_id: str
    series_id: str
    kind: Literal["APPOINTMENT", "STANDALONE", "ROUTINE"]
    template_id: str


@dataclass
class Task:
    user_id: str
    series_id: str
    kind: str
    template_id: str
    appointment: dict[str, Any] | None = None
    standalone: dict[str, Any] | None = None
    routine: dict[str, Any] | None = None


# 묶음마다 가장 늦은 행 (occurrence_on이 같으면 먼저 읽은 것) — 목록은 occurrence_on 내림차순으로 읽어 옴
def _newest_per_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["series_id"] in seen:
            continue
        seen.add(r["series_id"])
        out.append(r)
    return out


# 묶음마다 기준 회차 (edit.py pick_base_row) — 예외 아닌 행이 우선, 예외뿐이면 이번만 고친 행, 지운 행은 기준이 아님
def _base_rows_per_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_series: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_series[r["series_id"]] = [*by_series.get(r["series_id"], []), r]
    out: list[dict[str, Any]] = []
    for group in by_series.values():
        base = pick_base_row(group)
        if base:
            out.append(base)
    return out


def _attempt_key(series_id: str, date: str) -> str:
    return f"{series_id}|{date}"


def _day_range(now: datetime, time_zone: str) -> tuple[datetime, datetime]:
    """local_day_range의 (start, end)"""
    r = local_day_range(now, time_zone)
    return r["start"], r["end"]


# 사람 하나에 대해 이번 실행 동안 들고 있는 값 — 처음 쓸 때 읽음
class UserState:
    def __init__(self, run: "RollRun", user_id: str) -> None:
        self._cached: dict[str, asyncio.Future[Any]] = {}
        self._upcoming: int | None = None
        self._lookups_today: int | None = None
        self._run = run
        self.user_id = user_id

    async def _memo(self, key: str, load: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._cached.get(key)
        if hit is None:
            hit = asyncio.ensure_future(load())
            self._cached[key] = hit

            def forget(task: asyncio.Future[Any], key: str = key) -> None:
                # 실패는 기억하지 않음
                if (task.cancelled() or task.exception() is not None) and self._cached.get(key) is task:
                    del self._cached[key]

            hit.add_done_callback(forget)
        return await hit

    async def profile(self) -> dict[str, Any]:
        return await self._memo("profile", lambda: load_profile(self._run.deps.read, self.user_id))

    async def time_zone(self) -> str:
        return safe_time_zone((await self.profile()).get("timezone"))

    async def plan_ctx(self) -> RollPlanCtx:
        read, now = self._run.deps.read, self._run.deps.now
        profile, stats, late_count4w = await asyncio.gather(
            self.profile(),
            self._memo("stats", lambda: load_prepare_stats(read, self.user_id)),
            self._memo("late", lambda: load_late_count(read, self.user_id, now)),
        )
        return RollPlanCtx(
            user_id=self.user_id,
            now=now,
            profile=profile,
            time_zone=safe_time_zone(profile.get("timezone")),
            stats=stats,
            late_count4w=late_count4w,
        )

    async def samples(self) -> Any:
        return await self._memo("samples", lambda: load_travel_samples(self._run.deps.read, self.user_id))

    # 재시도 기록 (roll_attempts, service 전용이라 write로 읽음) — 어제 이후 날짜만
    async def attempts(self) -> dict[str, dict[str, Any]]:
        async def load() -> dict[str, dict[str, Any]]:
            since = add_days_to_key(local_date_key(self._run.deps.now, await self.time_zone()), -1)
            rows = must(
                "roll_attempts",
                await self._run.deps.write.from_("roll_attempts")
                .select("series_id, occurrence_on, tries, next_try_at, skipped")
                .eq("user_id", self.user_id)
                .gte("occurrence_on", since),
            )
            return {_attempt_key(r["series_id"], r["occurrence_on"]): r for r in rows}

        return await self._memo("attempts", load)

    # 이미 있는 회차 날짜(occurrence_on) — 어제 이후, 묶음별로. 사람 하나에 한 번 읽음 (묶음마다 읽지 않게 — 만들 게 없는 묶음이 쿼리를 더 쓰지 않음)
    async def series_dates(self, kind: str) -> dict[str, set[str]]:
        async def load() -> dict[str, set[str]]:
            read, now = self._run.deps.read, self._run.deps.now
            since = add_days_to_key(local_date_key(now, await self.time_zone()), -1)
            if kind == "APPOINTMENT":
                result = await (
                    read.from_("appointments")
                    .select("series_id, occurrence_on")
                    .eq("creator_id", self.user_id)
                    .gte("occurrence_on", since)
                )
            else:
                result = await (
                    read.from_("alarms")
                    .select("series_id, occurrence_on")
                    .eq("user_id", self.user_id)
                    .not_("series_id", "is", None)
                    .gte("occurrence_on", since)
                )
            rows = must("series dates", result)
            by_series: dict[str, set[str]] = {}
            for r in rows:
                by_series.setdefault(r["series_id"], set()).add(r["occurrence_on"])
            return by_series

        return await self._memo(f"dates:{kind}", load)

    # 앞으로 켜진 알람 수 (상한 확인용). 이번 실행에서 만든 만큼 늘려 감
    async def upcoming_alarms(self) -> int:
        if self._upcoming is None:
            result = await (
                self._run.deps.read.from_("alarms")
                .select("alarm_id", count="exact", head=True)
                .eq("user_id", self.user_id)
                .eq("is_enabled", True)
                .gte("scheduled_at", to_iso(self._run.deps.now))
            )
            if result.error:
                db_fail("upcoming alarms", result.error)
            self._upcoming = result.count or 0
        return self._upcoming

    def add_upcoming(self, n: int) -> None:
        if self._upcoming is not None:
            self._upcoming += n

    # 오늘(내 시간대 하루) roll이 쓴 길찾기 수. 셈 방법: 오늘 roll이 만든 회차(만든 때가 오늘이고 묶음 번호가 자기 id가 아닌 약속) + 길찾기를 쓰고도 못 만든 날짜(오늘 고쳐진 재시도 기록 중 tries ≥ 1).
    #   · 한 줄(재시도 기록)이 하루에 여러 번 실패해도 한 번으로 셈 — 한도는 어림 상한이라 이 정도 오차는 둠
    #   · 지난 실행들의 호출 수를 따로 저장하는 곳이 없어서(마이그레이션 없이) DB에 남은 흔적으로 셈. 폴백으로 만든 회차도 만든 회차로 셈(그 회차도 길찾기를 한 번 썼음)
    #   L6 안건후보: 하루 몫 범위 기본안 = 사용자별 (배치가 사용자로 좁혀 읽는 규칙을 지키려고). 전체 합계 한도는 kakao_calls 로그로 L8이 보고 정함
    async def lookups_used(self) -> int:
        if self._lookups_today is None:
            read, write, now = self._run.deps.read, self._run.deps.write, self._run.deps.now
            start, end = _day_range(now, await self.time_zone())
            rows = must(
                "roll created today",
                await read.from_("appointments")
                .select("appointment_id, series_id")
                .eq("creator_id", self.user_id)
                .gte("created_at", to_iso(start))
                .lt("created_at", to_iso(end))
                .not_("recurrence_rule", "is", None),
            )
            made = len([a for a in rows if a["series_id"] != a["appointment_id"]])
            result = await (
                write.from_("roll_attempts")
                .select("series_id", count="exact", head=True)
                .eq("user_id", self.user_id)
                .gte("updated_at", to_iso(start))
                .gt("tries", 0)
            )
            if result.error:
                db_fail("roll attempts today", result.error)
            self._lookups_today = made + (result.count or 0)
        return self._lookups_today

    def add_lookup(self) -> None:
        if self._lookups_today is not None:
            self._lookups_today += 1


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


@dataclass
class Item:
    task: Task
    occ: Occurrence
    requested: bool  # 요청의 series_id인 묶음 (본인 길)
    appointment: dict[str, Any] | None = None
    standalone: dict[str, Any] | None = None
    looked_up: bool = False  # 이 회차에 길찾기를 이미 부름 (처리 중 던지면 재시도 기록을 남기려고)
    settled: bool = (
        False  # 결과가 DB에 남음 (재시도 기록을 올렸거나 회차를 만듦) — 이후에 던져도 다시 기록하지 않음
    )


# 길찾기 값을 못 정했을 때 — 다음에 어떻게 할지
#   RETRY 재시도 기록을 올려 1 · 3 · 6시간 뒤, NEXT_DAY 내 시간대 다음 날 0시 뒤, NONE 기록 없이 다음 실행에서
@dataclass
class Failure:
    name: str
    mode: Literal["RETRY", "NEXT_DAY", "NONE"]
    reason: str
    spent: bool


def _json_line(value: dict[str, Any]) -> str:
    """JSON.stringify — 공백 없이, 한글은 그대로"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _coords_of(lat: Any, lng: Any) -> dict[str, Any]:
    return {"lat": _js_number(lat), "lng": _js_number(lng)} if lat is not None and lng is not None else {}


def _lat_lng(place: Any) -> dict[str, Any] | None:
    lat, lng = place.lat, place.lng
    return {"lat": lat, "lng": lng} if lat is not None and lng is not None else None


def _lookup_value(r: dict[str, Any]) -> tuple[Any, bool]:
    """길찾기 결과 {minutes, partial} → (minutes, partial)"""
    return r["minutes"], bool(r["partial"])


class RollRun:
    def __init__(self, deps: RollDeps, user_id: str | None = None, series_id: str | None = None) -> None:
        self.result: RollResult = {
            "created": [],
            "moved_alarm_ids": [],
            "pending": [],
            "ring_packages": [],
            "canceled_alarm_ids": [],
            "kakao_calls": 0,
        }
        self.deps = deps
        self.scope_user_id = user_id
        self.scope_series_id = series_id
        self.limits: RollLimits = {**ROLL_LIMITS[deps.path], **(deps.limits or {})}  # type: ignore[typeddict-item]
        self._users: dict[str, UserState] = {}
        self._bases: dict[str, asyncio.Future[RollBase | None]] = {}
        self._stopped_by: str | None = None  # 이번 실행의 길찾기를 멈춘 이유 (RATE_LIMIT | AUTH)
        self._started_at = self._clock()
        self._seen_pending: set[str] = set()

    def _clock(self) -> float:
        return (self.deps.clock or (lambda: time.time() * 1000))()

    def user(self, user_id: str) -> UserState:
        u = self._users.get(user_id)
        if u is None:
            u = self._users[user_id] = UserState(self, user_id)
        return u

    def _time_up(self) -> bool:
        return self.limits["ms"] is not None and self._clock() - self._started_at > self.limits["ms"]

    # 묶음 하나가 실패해도 다음 묶음으로 — 이유 글자만 남김 (사용자 · 일정 내용 없이)
    #   JS RangeError(Invalid Date · 범위 밖 시각) = 읽을 수 없는 시각 · 숫자 → 파이썬에선 ValueError · OverflowError
    def _reason_of(self, e: BaseException) -> str:
        if isinstance(e, ValueError | OverflowError):
            return "INVALID_VALUE"
        if isinstance(e, AppError):
            return e.code
        return "ERROR"

    def _skipped(self, where: str, e: BaseException) -> None:
        reason = self._reason_of(e)
        line = _json_line({"fn": "alarm-roll", "event": "series_skipped", "where": where, "reason": reason})
        (self.deps.warn or log.warning)(line)

    def _pend(self, series_id: str, date: str, reason: str) -> None:
        key = f"{series_id}|{date}"
        if key in self._seen_pending:
            return
        self._seen_pending.add(key)
        self.result["pending"].append({"series_id": series_id, "occurrence_on": date, "reason": reason})

    # ---- 할 일 목록 ----

    async def _due_tasks(self) -> list[Task]:
        read, write, now = self.deps.read, self.deps.write, self.deps.now
        if self.deps.path == "batch":
            rows = must(
                "roll_due_series",
                await write.rpc("roll_due_series", {"p_now": to_iso(now), "p_limit": self.limits["fetch"]}),
            )
            # 사람별로 기준 회차를 한 번에 읽음 (묶음마다 한 번씩 읽지 않게)
            tasks = [
                Task(
                    user_id=r["user_id"],
                    series_id=r["series_id"],
                    kind=r["kind"],
                    template_id=r["template_id"],
                )
                for r in rows
            ]
            by_user: dict[str, list[Task]] = {}
            for t in tasks:
                if t.kind == "APPOINTMENT":
                    by_user[t.user_id] = [*by_user.get(t.user_id, []), t]
            for user_id, group in by_user.items():
                try:
                    found = {
                        a["appointment_id"]: a
                        for a in await load_series_appointments(read, user_id, [t.template_id for t in group])
                    }
                    for t in group:
                        t.appointment = found.get(t.template_id)
                except Exception as e:
                    self._skipped(
                        "templates", e
                    )  # 이 사람 것만 건너뜀 (appointment가 비면 collect가 하나씩 다시 읽어 봄)
            return tasks

        # 본인 길 — 내 묶음을 RLS 클라이언트로 직접 읽음 (roll_due_series는 service_role 전용이고 모든 사람의 줄을 줌)
        user_id = self.scope_user_id
        assert user_id is not None
        # 기준 회차는 예외(이번만 고친 · 지운 회차, exception_kind)가 아닌 행 중 가장 늦은 것 — 이번만 고친 값이 다음 회차들로 번지지 않게 (edit.py pick_base_row = DB 함수 roll_due_series와 같은 규칙)
        appointments = [
            Task(
                user_id=user_id,
                series_id=a["series_id"],
                kind="APPOINTMENT",
                template_id=a["appointment_id"],
                appointment=a,
            )
            for a in _base_rows_per_series(
                must(
                    "appointments",
                    await read.from_("appointments")
                    .select(f"{SERIES_APPOINTMENT_COLUMNS}, exception_kind")
                    .eq("creator_id", user_id)
                    .not_("recurrence_rule", "is", None)
                    .order("occurrence_on", ascending=False)
                    .limit(500),
                )
            )
        ]
        standalones = [
            Task(
                user_id=user_id,
                series_id=a["series_id"],
                kind="STANDALONE",
                template_id=a["alarm_id"],
                standalone=a,
            )
            for a in _newest_per_series(
                must(
                    "alarms",
                    await read.from_("alarms")
                    .select(STANDALONE_COLUMNS)
                    .eq("user_id", user_id)
                    .not_("series_id", "is", None)
                    .not_("recurrence_rule", "is", None)
                    .order("occurrence_on", ascending=False)
                    .limit(500),
                )
            )
        ]
        routines = [
            Task(
                user_id=user_id,
                series_id=r["routine_id"],
                kind="ROUTINE",
                template_id=r["routine_id"],
                routine=r,
            )
            for r in must(
                "routines",
                await read.from_("routines")
                .select(ROUTINE_COLUMNS)
                .eq("user_id", user_id)
                .eq("is_active", True),
            )
        ]
        return [*appointments, *standalones, *routines]

    # ---- 한 묶음에서 만들 회차 모으기 (루틴은 바로 옮김) ----

    async def _collect(self, task: Task) -> list[Item]:
        if task.kind == "ROUTINE":
            await self._move_routine(task)
            return []
        read, now = self.deps.read, self.deps.now
        user = self.user(task.user_id)
        requested = task.series_id == self.scope_series_id

        if task.kind == "APPOINTMENT":
            if task.appointment is None:
                task.appointment = await load_series_appointment(read, task.user_id, task.template_id)
            a = task.appointment
            if not a or a.get("group_id") is not None:
                return []  # 공동 약속은 반복이어도 첫 회차만
            rule, base_at = a.get("recurrence_rule"), a.get("appointment_at")
        else:
            if task.standalone is None:
                task.standalone = must(
                    "alarms",
                    await read.from_("alarms")
                    .select(STANDALONE_COLUMNS)
                    .eq("alarm_id", task.template_id)
                    .eq("user_id", task.user_id)
                    .maybe_single(),
                )
            s = task.standalone
            if not s:
                return []
            rule, base_at = s.get("recurrence_rule"), s.get("scheduled_at")
        # 규칙이 두 모양(FREQ=DAILY · FREQ=WEEKLY;BYDAY=…) 밖이면(옛 MONTHLY · INTERVAL …) 오류가 아니라 반복이 아닌 것으로 보고 조용히 건너뜀
        if not parse_recurrence(rule):
            return []

        tz = await user.time_zone()
        today = local_date_key(now, tz)
        # 오늘부터 다시 구함 — 길찾기 실패로 중간 날짜가 비어 있어도 채우고, 이미 있는 날짜는 아래에서 뺌 (지난 날은 recur가 안 줌)
        #   날짜의 기준은 기준 회차 약속 시각(appointment_at)의 날짜가 아니라 회차 날짜 칸(occurrence_on): 시각을 옮긴 행은 둘이 어긋나므로, 시각의 날짜에서 이어 세지 않고
        #   규칙 날짜를 오늘부터 구한 뒤 이미 있는 occurrence_on을 빼서 같은 날짜를 두 번 만들지(길찾기를 헛쓰지) 않음. 기준 회차에서는 시:분만 가져옴
        found = next_occurrences(
            rule=rule,
            baseAt=parse_iso(base_at),
            now=now,
            timeZone=tz,
            afterDate=add_days_to_key(today, -1),
        )
        if not found:
            return []  # 할 일 없는 묶음 — 여기서 끝 (기준 회차 한 줄만 읽었음)

        have = (await user.series_dates(task.kind)).get(task.series_id) or set()
        fresh = [o for o in found if o["date"] not in have]
        if not fresh:
            return []  # 만들 회차가 이미 다 있음 (재시도 기록은 볼 것도 없음)
        attempts = await user.attempts()

        def blocked(date: str) -> bool:
            a = attempts.get(_attempt_key(task.series_id, date))
            # 건너뛴 날짜 · 재시도 기다리는 날짜
            return a is not None and bool(a.get("skipped") or ms(parse_iso(a["next_try_at"])) > ms(now))

        return [
            Item(
                task=task,
                occ=occ,
                requested=requested,
                appointment=task.appointment if task.kind == "APPOINTMENT" else None,
                standalone=task.standalone if task.kind != "APPOINTMENT" else None,
            )
            for occ in fresh
            if not blocked(occ["date"])
        ]

    # ---- 루틴: 같은 행의 시각만 다음 발생으로 ----

    async def _move_routine(self, task: Task) -> None:
        read, write, now = self.deps.read, self.deps.write, self.deps.now
        routine = task.routine
        if routine is None:
            routine = must(
                "routines",
                await read.from_("routines")
                .select(ROUTINE_COLUMNS)
                .eq("routine_id", task.template_id)
                .eq("user_id", task.user_id)
                .maybe_single(),
            )
        if not routine or not routine.get("is_active"):
            return
        tz = await self.user(task.user_id).time_zone()
        starts = routine_step_starts(routine.get("steps"))
        if not starts:
            return
        past = must(
            "routine alarms",
            await read.from_("alarms")
            .select(
                "alarm_id, step_order, alarm_type, scheduled_at, intensity, voice_tone, sound_key, vibration, snooze_limit, is_enabled"
            )
            .eq("user_id", task.user_id)
            .eq("routine_id", routine["routine_id"])
            .eq("is_enabled", True)
            .lt("scheduled_at", to_iso(now)),
        )
        moved: list[dict[str, Any]] = []
        for alarm in past:
            step_order = alarm.get("step_order")
            # JS starts[step_order - 1]: 범위 밖(0 · 음수 · 너무 큼)이면 undefined
            start = (
                starts[step_order - 1]
                if step_order is not None and 0 <= step_order - 1 < len(starts)
                else None
            )
            if start is None:
                continue
            at = next_routine_at(
                repeatDays=routine.get("repeat_days"), stepOffsetMinutes=start, now=now, timeZone=tz
            )
            if not at:
                continue
            # 옛 시각이 아직 그대로일 때만 옮김 — 본인 길과 배치가 동시에 와도 먼저 옮긴 쪽만 (두 번째는 할 일 없음)
            updated = must(
                "routine alarm move",
                await write.from_("alarms")
                .update({"scheduled_at": to_iso(at), "last_calculated_at": to_iso(now)})
                .eq("alarm_id", alarm["alarm_id"])
                .eq("user_id", task.user_id)
                .eq("scheduled_at", alarm["scheduled_at"])
                .select("alarm_id"),
            )
            if not updated:
                continue
            self.result["moved_alarm_ids"].append(alarm["alarm_id"])
            moved.append({**alarm, "scheduled_at": to_iso(at)})
        if self.deps.path == "own" and moved:
            self.result["ring_packages"].extend(build_ring_packages(alarms=moved, title=routine["title"]))

    # ---- 기준 회차 읽기 (묶음마다 한 번) ----

    async def _base_of(self, task: Task) -> RollBase | None:
        key = f"{task.user_id}|{task.series_id}"
        hit = self._bases.get(key)
        if hit is None:
            hit = asyncio.ensure_future(self._load_base(task))
            self._bases[key] = hit
        return await hit

    async def _load_base(self, task: Task) -> RollBase | None:
        read = self.deps.read
        appointment = task.appointment
        assert appointment is not None
        participant = await load_participant(read, appointment["appointment_id"], task.user_id)
        if not participant:
            return None  # 참여 조건이 없는 약속은 이어받을 게 없음

        async def load_items() -> list[str]:
            rows = must(
                "todos",
                await read.from_("todos")
                .select("title")
                .eq("user_id", task.user_id)
                .eq("appointment_id", appointment["appointment_id"])
                .eq("todo_type", "ITEM")
                .order("created_at", ascending=True),
            )
            return [t["title"] for t in rows]

        alarms, items = await asyncio.gather(
            load_alarms(read, participant["participant_id"], task.user_id), load_items()
        )
        return RollBase(appointment=appointment, participant=participant, alarms=alarms, items=items)

    # ---- 재시도 기록 ----

    async def _record(
        self,
        user_id: str,
        series_id: str,
        date: str,
        *,
        failure: Failure | None = None,
        skipped: str | None = None,
        spent: bool,
    ) -> None:
        write, now = self.deps.write, self.deps.now
        user = self.user(user_id)
        attempts = await user.attempts()
        key = _attempt_key(series_id, date)
        prev = attempts.get(key)
        prev_tries = (prev.get("tries") if prev else None) or 0
        retrying = failure is not None and failure.mode == "RETRY"
        tries = prev_tries + (1 if (retrying or spent) else 0)  # 길찾기를 쓴(또는 재시도를 올린) 횟수
        if failure is not None and failure.mode == "NEXT_DAY":
            next_try_at = _day_range(now, await user.time_zone())[1]  # 내 시간대 다음 날 0시
        elif retrying:
            next_try_at = retry_at(now, prev_tries)
        else:
            next_try_at = now
        last_error = failure.name if failure is not None else skipped
        row = {
            "user_id": user_id,
            "series_id": series_id,
            "occurrence_on": date,
            "tries": tries,
            "next_try_at": to_iso(next_try_at),
            "last_error": last_error[:40] if last_error is not None else None,
            "skipped": skipped is not None,
            "updated_at": to_iso(now),
        }
        must(
            "roll_attempts",
            await write.from_("roll_attempts").upsert(row, on_conflict="user_id,series_id,occurrence_on"),
        )
        attempts[key] = {
            "series_id": series_id,
            "occurrence_on": date,
            "tries": tries,
            "next_try_at": row["next_try_at"],
            "skipped": row["skipped"],
        }

    # 길찾기를 이미 썼는데 계산 · 저장이 던졌으면 재시도 기록을 남김 — 안 남기면 다음 5분 실행이 같은 회차로 길찾기를 또 부름.
    #   기존 1 · 3 · 6시간 규칙 그대로, 하루 몫에도 한 번 센 것으로 남음. last_error는 이름 글자만 (좌표 · 장소명 없음). 길찾기를 안 썼으면(순수 계산 오류) 아무것도 안 함
    async def _retry_after_throw(self, item: Item, e: BaseException) -> None:
        if not item.looked_up or item.settled:
            return
        try:
            await self._record(
                item.task.user_id,
                item.task.series_id,
                item.occ["date"],
                failure=Failure(name=self._reason_of(e), mode="RETRY", reason="ROUTE_RETRY", spent=True),
                spent=True,
            )
        except Exception as e2:
            self._skipped("retry_record", e2)

    async def _clear_attempt(self, user_id: str, series_id: str, date: str) -> None:
        attempts = await self.user(user_id).attempts()
        key = _attempt_key(series_id, date)
        if key not in attempts:
            return
        del attempts[key]
        must(
            "roll_attempts",
            await self.deps.write.from_("roll_attempts")
            .delete()
            .eq("user_id", user_id)
            .eq("series_id", series_id)
            .eq("occurrence_on", date),
        )

    # ---- 회차 하나 ----

    async def _process(self, item: Item) -> None:
        if item.task.kind == "APPOINTMENT":
            await self._process_appointment(item)
        else:
            await self._process_standalone(item)

    async def _process_standalone(self, item: Item) -> None:
        task, occ = item.task, item.occ
        s = item.standalone
        assert s is not None
        user = self.user(task.user_id)
        if (await user.upcoming_alarms()) >= self.limits["alarmCap"]:
            return self._pend(task.series_id, occ["date"], "ALARM_CAP")
        row = {
            "user_id": task.user_id,
            "participant_id": None,
            "routine_id": None,
            "series_id": task.series_id,
            "occurrence_on": occ["date"],
            "alarm_type": "WAKE",
            "scheduled_at": to_iso(occ["at"]),
            "intensity": s.get("intensity"),
            "voice_tone": s.get("voice_tone"),
            "sound_key": s.get("sound_key"),
            "vibration": s.get("vibration"),  # 직접 고른 진동도 이어받음 (NULL이면 강도를 따름)
            "is_enabled": s.get(
                "is_enabled"
            ),  # 꺼 둔 반복 알람은 새 회차도 꺼진 채 (켜는 순간 다음 회차가 이미 있게)
            "snooze_limit": s.get("snooze_limit"),
            "time_source": s.get("time_source"),
            "intensity_source": s.get("intensity_source"),
            "recurrence_rule": s.get("recurrence_rule"),
            "last_calculated_at": to_iso(self.deps.now),
        }
        # 같은 날짜가 이미 있으면 아무것도 안 하고 돌려주는 행도 없음 (ON CONFLICT DO NOTHING … RETURNING)
        inserted = must(
            "alarms upsert",
            await self.deps.write.from_("alarms")
            .upsert(row, on_conflict="user_id,series_id,occurrence_on", ignore_duplicates=True)
            .select(ALARM_COLUMNS),
        )
        if not inserted:
            return
        self.result["created"].append(
            {
                "series_id": task.series_id,
                "occurrence_on": occ["date"],
                "appointment_id": None,
                "alarm_id": inserted[0]["alarm_id"],
            }
        )
        if inserted[0].get("is_enabled"):
            user.add_upcoming(1)
        if self.deps.path == "own":
            tz = await user.time_zone()
            self.result["ring_packages"].extend(
                build_ring_packages(alarms=inserted, title="알람", subtitle=subtitle_for(occ["at"], None, tz))
            )

    # 길찾기를 불러도 되는지 — 못 부르면 그 이유
    async def _lookup_gate(self, user: UserState) -> Failure | None:
        if self.deps.lookup is None:
            return Failure("AUTH", "NEXT_DAY", "ROUTE_RETRY", False)  # 키가 없음
        if self._stopped_by:
            return Failure(self._stopped_by, "NEXT_DAY", "ROUTE_RETRY", False)
        if self.result["kakao_calls"] >= self.limits["lookups"]:
            return Failure("RUN_LIMIT", "NONE", "RUN_LIMIT", False)
        if (await user.lookups_used()) >= self.limits["dailyLookups"]:
            return Failure("DAILY_BUDGET", "NEXT_DAY", "DAILY_BUDGET", False)
        return None

    async def _process_appointment(self, item: Item) -> None:
        task, occ = item.task, item.occ
        user = self.user(task.user_id)
        read, write, now = self.deps.read, self.deps.write, self.deps.now
        if (await user.upcoming_alarms()) >= self.limits["alarmCap"]:
            return self._pend(task.series_id, occ["date"], "ALARM_CAP")
        base = await self._base_of(task)
        if not base:
            return
        ctx = await user.plan_ctx()
        same_day_wake = await load_same_day_wake(
            read, task.user_id, occ["at"], ctx.time_zone
        )  # 회차 날짜 기준 그날 첫 일정 판정
        if (
            not same_day_wake
            and len(base.alarms)
            and not any(a.get("alarm_type") == "WAKE" for a in base.alarms)
        ):
            base_had_earlier_wake = await load_same_day_wake(
                read,
                task.user_id,
                parse_iso(base.appointment["appointment_at"]),
                ctx.time_zone,
                base.appointment["appointment_id"],
            )
        else:
            base_had_earlier_wake = False

        def plan_for(travel: Any) -> PlannedOccurrence:
            return plan_occurrence(
                ctx,
                base,
                occ,
                travel=travel,
                same_day_wake=same_day_wake,
                base_had_earlier_wake=base_had_earlier_wake,
            )

        # 이동시간 — 직접 입력(MANUAL)은 그 분 그대로(길찾기 없음), 아니면 회차마다 길찾기 1번
        travel: Any = None
        failure: Failure | None = None
        looked_up = False
        p = base.participant
        if p.get("travel_source") == "MANUAL" and p.get("est_travel_minutes") is not None:
            travel = TravelChoice(
                minutes=p["est_travel_minutes"], source="MANUAL", needs_confirmation=False, note="직접 입력"
            )
        else:
            basics = resolve_basics(
                draft=PlanDraft(
                    at=occ["at"],
                    destination=PlaceInput(
                        name=base.appointment.get("dest_name"),
                        **_coords_of(base.appointment.get("dest_lat"), base.appointment.get("dest_lng")),
                    ),
                    importance=base.appointment.get("importance"),
                    kinds=[],
                    manual_times={},
                ),
                profile=ctx.profile,
                participant=p,
            )
            origin = _lat_lng(basics.origin)
            destination = _lat_lng(basics.destination)
            if origin is None or destination is None:
                failure = Failure("NO_COORDS", "RETRY", "ROUTE_RETRY", False)
            else:
                failure = await self._lookup_gate(user)
            if failure is None and origin is not None and destination is not None:
                looked_up = True
                item.looked_up = True
                self.result["kakao_calls"] += (
                    1  # 실패한 호출도 한도를 씀 (회차마다 새로 부름 — 같은 실행 안 같은 경로라도 재사용 안 함, D5 A)
                )
                user.add_lookup()
                try:
                    assert self.deps.lookup is not None
                    minutes, partial = _lookup_value(
                        await self.deps.lookup(
                            origin=origin, destination=destination, transport=basics.transport
                        )
                    )
                    # 앞뒤 도보 구간이 빠진 결과(partial)는 API로 저장하지 않고 출처를 대략(ESTIMATE)으로 둠 — 7장 7.1.2 대체 순서와 같은 취급, 화면의 "대략적인 값이에요" 줄을 그대로 씀 (새 확인 단계 없음, 헤드 10/30)
                    if partial:
                        travel = TravelChoice(
                            minutes=minutes,
                            source="ESTIMATE",
                            needs_confirmation=True,
                            note="대략적인 값이에요 · 확인이 필요해요",
                        )
                    else:
                        travel = TravelChoice(
                            minutes=minutes, source="API", needs_confirmation=False, note="길찾기 기준"
                        )
                except Exception as e:
                    kind = e.kind if isinstance(e, KakaoError) else "HTTP"
                    if kind in ("RATE_LIMIT", "AUTH"):
                        self._stopped_by = kind  # 이번 실행의 길찾기를 멈춤 — 다음 날 0시 뒤 다시
                        failure = Failure(kind, "NEXT_DAY", "ROUTE_RETRY", True)
                    else:
                        failure = Failure(
                            kind, "RETRY", "ROUTE_RETRY", True
                        )  # TIMEOUT · HTTP · NO_ROUTE · BAD_RESPONSE

            # 길찾기를 못 정함: 첫 알람이 12시간 안이면 폴백(내 실측 중앙값 CACHED → 직선 근사 ESTIMATE)으로 지금 만들고, 아니면 미룸.
            #   묻는 알림 · 질문은 만들지 않음 (화면 "대략" 표시는 L5). 나중에 길찾기가 되어도 이 회차의 값을 API로 바꾸는 일은 여기서 안 함 —
            #   출발 전 다시 보기(route-watch, PR 10)의 몫
            if travel is None and failure is not None and failure.mode != "NONE" and origin and destination:
                fallback = (
                    await resolve_auto_travel(
                        {"origin": origin, "destination": destination, "transport": basics.transport},
                        TravelDeps(samples=lambda: user.samples()),
                    )
                ).choice
                if fallback:
                    provisional = plan_for(fallback)
                    if (
                        ms(first_alarm_at(provisional.plan)) - ms(now)
                        <= FALLBACK_BEFORE_FIRST_ALARM_HOURS * HOUR_MS
                    ):
                        travel = fallback

        if travel is None:
            why = failure
            assert why is not None
            if why.mode != "NONE":
                await self._record(task.user_id, task.series_id, occ["date"], failure=why, spent=why.spent)
            item.settled = True
            return self._pend(task.series_id, occ["date"], why.reason)

        planned = plan_for(travel)
        if not planned.ok:
            # 출발 시각까지 이미 지남(또는 순서가 어긋남) → 그 날짜는 건너뜀 (다시 안 만듦)
            await self._record(
                task.user_id, task.series_id, occ["date"], skipped=planned.reason, spent=looked_up
            )
            item.settled = True
            return None

        assert planned.payload is not None and planned.alarms is not None
        appointment_id = must(
            "roll_insert_occurrence",
            await write.rpc(
                "roll_insert_occurrence",
                {
                    "p_appointment": planned.payload["appointment"],
                    "p_participant": planned.payload["participant"],
                    "p_alarms": planned.alarms,
                    "p_items": planned.payload["items"],
                },
            ),
        )
        item.settled = True  # 만들기 끝 (이후 던져도 회차는 이미 있음)
        await self._clear_attempt(task.user_id, task.series_id, occ["date"])
        if not appointment_id:
            return None  # 다른 실행이 먼저 만듦 — 같은 결과
        self.result["created"].append(
            {"series_id": task.series_id, "occurrence_on": occ["date"], "appointment_id": appointment_id}
        )
        user.add_upcoming(len([a for a in planned.alarms if a.get("is_enabled")]))

        if self.deps.path == "own":
            # 폰에 걸 꾸러미 — alarm-commit(commit.py persist_appointment)과 같은 조립 함수(ring_package.py build_ring_packages)로. 알림 글자가 더해져도 그 함수를 따라감
            part = await load_participant(read, appointment_id, task.user_id)
            alarms = await load_alarms(read, part["participant_id"], task.user_id) if part else []
            self.result["ring_packages"].extend(
                build_ring_packages(
                    alarms=alarms,
                    title=base.appointment["title"],
                    subtitle=subtitle_for(occ["at"], planned.plan.basics.destination.name, ctx.time_zone),
                    depart_at=planned.plan.departure,
                )
            )
        return None

    # ---- 전체 ----

    async def execute(self) -> RollResult:
        tasks = await self._due_tasks()
        items: list[Item] = []
        worked = 0
        for task in tasks:
            if worked >= self.limits["series"] or self._time_up():
                break
            try:
                before = len(self.result["moved_alarm_ids"])
                found = await self._collect(task)
                items.extend(found)
                if found or len(self.result["moved_alarm_ids"]) > before:
                    worked += 1
            except Exception as e:
                self._skipped("collect", e)

        # 가까운 회차 먼저 (본인 길은 요청한 묶음 먼저). 48시간 안 회차는 가까워서 늘 앞에 옴
        items.sort(key=lambda it: (-int(it.requested), ms(it.occ["at"])))
        for item in items:
            if self._time_up():
                self._pend(item.task.series_id, item.occ["date"], "RUN_LIMIT")
                continue
            try:
                await self._process(item)
            except Exception as e:
                self._skipped("process", e)
                await self._retry_after_throw(item, e)

        self.result["ring_packages"].sort(key=lambda p: p["atMillis"])
        (self.deps.log or log.info)(
            _json_line(
                {
                    "fn": "alarm-roll",
                    "path": self.deps.path,
                    "kakao_calls": self.result["kakao_calls"],
                    "created": len(self.result["created"]),
                    "pending": len(self.result["pending"]),
                }
            )
        )
        return self.result


# user_id: 본인 길에서 부른 사람. series_id: 그 묶음 먼저 (본인 길)
async def run_roll(deps: RollDeps, *, user_id: str | None = None, series_id: str | None = None) -> RollResult:
    if deps.path == "own" and not user_id:
        raise RuntimeError("본인 길은 userId가 필요해요")
    return await RollRun(deps, user_id, series_id).execute()

"""본 레포 supabase/functions/_shared/plan-core.ts의 서버판 (기준 b140f70) — 알람 계획 만들기

DB에서 읽어 온 값(PlanInputs)과 요청 초안(PlanDraft)을 합쳐 calc/로 계산 (순수). alarm-suggest(미리 보기)와 alarm-commit(저장)이 같은 계산을 씀.
합치는 규칙:
  · 약속 시각 · 장소 · 중요도는 기존 약속(appointment_id)이 있으면 DB 값 (D41: 이 함수는 일정 제목 · 시각 · 장소를 고치지 않음)
  · 출발지 · 이동 수단 · 여유는 요청 → 내 참여 조건(저장된 값) → 프로필 기본값(집 · 기본 이동 수단 · 기본 여유) 순
  · 직접 고른 알람 시각 · 강도: 요청에 칸이 있으면 그 칸이 전부(없는 종류 · None = 자동), 칸이 없으면 저장된 직접 값(MANUAL)을 그대로
  · 만들 알람 종류(kinds): 요청에 있으면 그것, 없으면 지금 있는 종류 그대로(없으면 계산된 전부)

타입 대응: DB 행(ProfileRow · AppointmentRow · ParticipantRow · AlarmRow)은 DB 칸 이름 그대로의 dict, 안쪽 값(PlanDraft · PlanInputs · Basics · Plan)은 dataclass(snake_case).
TS의 undefined는 None으로 다룸 (draft의 `with_wake`처럼 "없음"이 뜻이 있는 칸도 None = 없음).
"""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.read import JS_WHITESPACE
from app.shared.calc.chain import alarm_chain, departure_at
from app.shared.calc.intensity import (
    AXES_PICKER_ENABLED,
    auto_level,
    level_of_sound_key,
    level_of_vibration,
    level_of_voice_tone,
    resolve_intensity,
    wake_sensitivity_of,
)
from app.shared.calc.prepare import (
    DEFAULT_BEFORE_PREPARE_MINUTES,
    DEFAULT_COMMON_P80,
    PrepareBasis,
    PrepareStats,
    personal_prepare_of,
    predict_prepare,
    recent_prepare_of,
    wake_margin_of,
)
from app.shared.calc.types import RING_KINDS, ChainInput
from app.shared.calc.validate import check_alarms
from app.shared.calc.wake import SameDayWake, is_evening_wake
from app.shared.commit_core import resolve_enabled
from app.shared.draft import AlarmFields, ParticipantFields, PlaceInput
from app.shared.js import from_ms, js_round, ms, parse_iso, to_iso
from app.shared.tz import safe_time_zone

# ---- DB에서 읽은 행 (필요한 칸만) — 모양만 적어 둠, 값은 dict ----
#   ProfileRow: timezone · default_buffer_minutes · default_transport · home_name · home_lat · home_lng
#               · default_prepare_minutes? (온보딩 "일어나서 나가기까지") · wake_sensitivity? ('LIGHT'|'NORMAL'|'HEAVY', D69) · wake_margin? ('TIGHT'|'NORMAL'|'LOOSE', D63)
#   AppointmentRow: appointment_id · creator_id · group_id · title · appointment_at · dest_name · dest_lat · dest_lng · importance · recurrence_rule
#   ParticipantRow: participant_id · appointment_id · user_id · origin_name · origin_lat · origin_lng · transport_type · est_travel_minutes · travel_source
#                   · prepare_minutes · buffer_minutes · prepare_advance_minutes? (받아들인 준비 알람 당김 분, #484 · 안건 D111)
#   AlarmRow: alarm_id · participant_id · user_id · alarm_type · scheduled_at · intensity · voice_tone · sound_key · vibration · is_enabled · snooze_limit
#             · time_source · intensity_source · recurrence_rule · original_scheduled_at? · original_time_source? (친구 제안 적용 알람 — 친구 10장)
ProfileRow = Mapping[str, Any]
AppointmentRow = Mapping[str, Any]
ParticipantRow = Mapping[str, Any]
AlarmRow = dict[str, Any]


_NUMBER_TEXT = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", re.ASCII)


def _js_number(v: Any) -> float | int:
    """JS `Number(v)` — 숫자 · 숫자 글자 · 불리언. 못 읽으면 NaN"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int | float):
        return v
    if isinstance(v, str):
        s = v.strip(JS_WHITESPACE)
        if not s:
            return 0
        if s in ("Infinity", "+Infinity"):
            return math.inf
        if s == "-Infinity":
            return -math.inf
        return float(s) if _NUMBER_TEXT.fullmatch(s) else math.nan
    return math.nan


def _or_zero(n: float | int) -> float | int:
    """JS `n || 0` — NaN · 0은 0"""
    return 0 if n != n or n == 0 else n


def _num(v: Any) -> float | int | None:
    return None if v is None else _js_number(v)


def _first(*values: Any) -> Any:
    """JS `a ?? b ?? c` — None(undefined · null)이 아닌 첫 값"""
    for v in values:
        if v is not None:
            return v
    return None


def _ms(s: str) -> int:
    """JS `Date.parse(s)`"""
    return ms(parse_iso(s))


def without_friend_times(alarms: Sequence[AlarmRow]) -> list[AlarmRow]:
    """친구가 제안해 주인이 적용한 알람(original_scheduled_at 있음)을 친구가 바꾸기 전 모습(시각 · time_source)으로 되돌린 목록.

    약속의 시각 · 장소가 바뀌거나 반복 새 회차를 만들 때 새 계산 · 직접 고른 값 옮기기의 입력으로 씀 — D67: 친구 값은 새 계산이 이기고
    (그 알람은 원래 자동이었으면 자동으로 계산되고), 주인이 직접 고른 값이었으면 그 값을 이어받음. DB에 쓰는 비교(diff_alarms)에는 실제 행을 그대로 넘김
    """
    out: list[AlarmRow] = []
    for a in alarms:
        if a.get("original_scheduled_at") is None:
            out.append(a)
            continue
        out.append(
            {
                **a,
                "scheduled_at": a["original_scheduled_at"],
                "time_source": _first(a.get("original_time_source"), "AUTO"),
                "original_scheduled_at": None,
                "original_time_source": None,
            }
        )
    return out


@dataclass
class PlanDraft(ParticipantFields, AlarmFields):
    appointment_id: str | None = None
    at: datetime | None = None  # 새 약속의 시각 (기존 약속이면 DB 값)
    destination: PlaceInput | None = None  # 새 약속의 장소
    importance: str | None = None
    travel_minutes: float | None = None  # 정해진 이동시간 — 직접 입력하거나 앱이 확인 시트에서 보여 준 값
    travel_source: str | None = None  # 'API' | 'MANUAL'
    prepare_minutes: float | None = None  # 정해진 준비 시간(기상 ~ 출발)


@dataclass
class PlanInputs:
    now: datetime
    draft: PlanDraft
    profile: ProfileRow
    stats: PrepareStats  # 내 기상~출발 기록 최근 30건 중 10~240분 (values가 있어야 깨우는 여유(0.7 · 0.9)를 반영)
    existing_alarms: list[AlarmRow] = field(default_factory=list)
    appointment: AppointmentRow | None = None
    participant: ParticipantRow | None = None
    late_count4w: int = 0  # lateCount4w
    same_day_wake: bool = False
    ml_prediction: Any = (
        None  # MlPrediction(model, minutes) — 검증된 프로필일 때만 조회. 없으면 기존 P80 계산 유지
    )
    # 반복 다음 회차용 — 자동 준비시간이 이 값에서 `within`분 넘게 벗어나면 그 범위로 (D62 처음 2주 몫). {"around", "within"}
    prepare_bound: Mapping[str, float] | None = None
    # 저장된 알람(existing_alarms)을 계산할 때의 당김 분 — 준비 전 활동을 되찾을 때 씀. 없으면 participant의 지금 값
    existing_advance_minutes: float | None = None


# 기상~출발 첫 값 — 온보딩 값이 있으면 그것, 없으면(null · 프로필 없음 · 범위 밖) 목업의 50
#   L6 안건후보: 첫 값 범위 기본안 = 5~300분만 믿음 (온보딩 · 설정 입력 화면 범위와 같음)
FIRST_PREPARE_MIN = 5
FIRST_PREPARE_MAX = 300


def first_prepare_minutes(profile: Mapping[str, Any]) -> int:
    v = profile.get("default_prepare_minutes")
    if v is not None:
        n = _js_number(v)
        if math.isfinite(n) and FIRST_PREPARE_MIN <= n <= FIRST_PREPARE_MAX:
            return js_round(n)
    return DEFAULT_COMMON_P80["withWake"]


def clamp_prepare_to_bound(prepare: Any, bound: Mapping[str, float]) -> Any:
    """자동 준비시간을 기준 값 ±within분 안으로 — 직접 고른 값(MANUAL)은 그대로. 설명용 칸(개인 보정)은 합이 맞게 같이 바꿈"""
    if prepare["source"] == "MANUAL":
        return prepare
    auto = min(
        max(prepare["autoMinutes"], bound["around"] - bound["within"]), bound["around"] + bound["within"]
    )
    if auto == prepare["autoMinutes"]:
        return prepare
    return {**prepare, "autoMinutes": auto, "minutes": auto, "correction": auto - prepare["commonP80"]}


# ---- 그날 첫 일정 판정에 쓰는 행 ----
# 내 참여 한 줄 = 약속 시각 + 그 참여의 기상 알람 (PostgREST 임베드: appointments!inner · alarms!inner). 일대다 · 다대일 임베드는 객체 또는 배열로 옴
#   SameDayWakeRow: appointment_id · appointments({appointment_at} 또는 그 배열 또는 None) · alarms({is_enabled} 또는 그 배열 또는 None)


def _as_list(v: Any) -> list[Any]:
    return [] if v is None else list(v) if isinstance(v, list) else [v]


def same_day_wakes_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[SameDayWake]:
    """판정 함수(calc/wake.py has_earlier_wake)에 넘길 모양으로. 약속 시각을 못 읽는 행은 건너뜀"""
    out: list[SameDayWake] = []
    for r in rows:
        appointments = _as_list(r.get("appointments"))
        at = appointments[0].get("appointment_at") if appointments else None
        appointment_at = None
        if at:
            try:
                appointment_at = parse_iso(at)
            except (ValueError, OverflowError):
                appointment_at = None
        if appointment_at is None:
            continue
        for a in _as_list(r.get("alarms")):
            out.append(
                {
                    "appointmentId": r["appointment_id"],
                    "appointmentAt": appointment_at,
                    "isEnabled": a["is_enabled"],
                }
            )
    return out


# ---- 기본 값 합치기 ----


@dataclass
class BasicsPlace:
    name: str
    lat: float | None = None
    lng: float | None = None


@dataclass
class Basics:
    at: datetime
    destination: BasicsPlace
    origin: BasicsPlace
    transport: str
    buffer_minutes: float
    importance: str


def resolve_basics(
    *,
    draft: PlanDraft,
    profile: ProfileRow,
    appointment: AppointmentRow | None = None,
    participant: ParticipantRow | None = None,
) -> Basics:
    at = parse_iso(appointment["appointment_at"]) if appointment else draft.at
    dest_name = (
        appointment["dest_name"] if appointment else (draft.destination.name if draft.destination else None)
    )
    if not at or not dest_name:
        raise ValueError("appointment_at · destination.name은 요청을 읽을 때 걸러져야 해요")
    if appointment:
        dest_lat, dest_lng = _num(appointment.get("dest_lat")), _num(appointment.get("dest_lng"))
    else:
        dest_lat = draft.destination.lat if draft.destination else None
        dest_lng = draft.destination.lng if draft.destination else None

    home_name = profile.get("home_name")
    home = (
        home_name if home_name is not None else "집",
        _num(profile.get("home_lat")),
        _num(profile.get("home_lng")),
        True,
    )
    stored = None
    if participant and (participant.get("origin_name") or participant.get("origin_lat") is not None):
        stored = (
            _first(participant.get("origin_name"), ""),
            _num(participant.get("origin_lat")),
            _num(participant.get("origin_lng")),
            False,
        )

    # 요청에 출발지 이름만 있고 좌표가 없으면 (목업의 '집' 같은 글자), 이름이 같은 저장된 출발지 · 집의 좌표를 씀
    def resolve_origin() -> tuple[str | None, float | None, float | None]:
        asked = draft.origin
        if not asked:
            c = stored or home
            return c[0], c[1], c[2]
        if asked.lat is not None and asked.lng is not None:
            return asked.name, asked.lat, asked.lng
        same = next(
            (
                c
                for c in (stored, home)
                if c and c[1] is not None and (c[0] == asked.name or (c[3] and asked.name == "집"))
            ),
            None,
        )
        return (asked.name, same[1], same[2]) if same else (asked.name, asked.lat, asked.lng)

    o_name, o_lat, o_lng = resolve_origin()
    return Basics(
        at=at,
        destination=BasicsPlace(
            name=dest_name,
            lat=dest_lat if dest_lat is not None and dest_lng is not None else None,
            lng=dest_lng if dest_lat is not None and dest_lng is not None else None,
        ),
        origin=BasicsPlace(
            name=o_name or "집",
            lat=o_lat if o_lat is not None and o_lng is not None else None,
            lng=o_lng if o_lat is not None and o_lng is not None else None,
        ),
        transport=_first(
            draft.transport,
            participant.get("transport_type") if participant else None,
            profile.get("default_transport"),
        ),
        buffer_minutes=_first(
            draft.buffer_minutes,
            participant.get("buffer_minutes") if participant else None,
            profile.get("default_buffer_minutes"),
        ),
        # 새 일정 기본은 약(LOW) — 요청에 없으면 requests.py가 제목으로 자동
        importance=_first(appointment.get("importance") if appointment else None, draft.importance, "LOW"),
    )


# ---- 지금 있는 알람에서 읽는 값 ----


def _ring_rows(rows: Sequence[AlarmRow]) -> list[AlarmRow]:
    return [a for a in rows if a["alarm_type"] in RING_KINDS]


def resolve_kinds(draft_kinds: list[str] | None, existing: Sequence[AlarmRow]) -> list[str] | None:
    if draft_kinds is not None:
        return draft_kinds
    kinds = [a["alarm_type"] for a in _ring_rows(existing)]
    return kinds if kinds else None


def resolve_manual_times(
    draft: Mapping[str, datetime | None] | None, existing: Sequence[AlarmRow]
) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    if draft is not None:
        for kind in RING_KINDS:
            picked = draft.get(kind)
            if picked:
                out[kind] = picked
        return out
    for a in _ring_rows(existing):
        if a["time_source"] == "MANUAL":
            out[a["alarm_type"]] = parse_iso(a["scheduled_at"])
    return out


def resolve_overrides(
    draft: Mapping[str, int | None] | None,
    existing: Sequence[AlarmRow],
    auto: int,
    axes_enabled: bool = AXES_PICKER_ENABLED,
) -> dict[str, int | None]:
    if not axes_enabled:
        return {}  # 축별 선택을 끄면 요청 · 저장된 축 값 모두 계산에서 무시(값은 지우지 않음) — calc/intensity.py
    if draft is not None:
        return dict(draft)
    stored = next(
        (a for a in existing if a["alarm_type"] == "WAKE" and a["intensity_source"] == "MANUAL"), None
    ) or next((a for a in existing if a["intensity_source"] == "MANUAL"), None)
    if not stored:
        return {}

    # 저장돼 있는 값이 자동 값과 같으면 직접 고른 칸으로 보지 않음 (자동 값이 바뀌면 같이 바뀌도록)
    def pick(level: int | None) -> int | None:
        return level if level is not None and level != auto else None

    return {
        "sound": pick(level_of_sound_key(stored.get("sound_key"))),
        "voice": pick(level_of_voice_tone(stored.get("voice_tone"))),
        "vibration": pick(level_of_vibration(stored.get("vibration"))),
    }


def infer_before_prepare(
    existing: Sequence[AlarmRow], advance: float = 0, prepare_minutes: float | None = None
) -> int | None:
    """저장된 알람에서 준비 전 활동을 되찾음 (따로 저장하지 않음). advance · prepare_minutes = 그 알람을 계산할 때의 당김 분 · 준비 시간(참여 조건 칸)

    준비 알람이 자동으로 잡힌 약속이면 준비 − 기상 간격을 "준비 전 활동"으로 읽음. 기상을 직접 옮겼어도 간격은 그대로 준비 전 활동.
    준비를 직접 옮겼으면 간격이 준비 전 활동이 아니므로 읽지 않음.
    당김이 없으면 간격 그대로. 당김이 있으면(안건 D111 · B안) 간격 = 준비 전 활동 − min(당김, 준비 전 활동)
      간격 > 0: 간격 + 당김 / 간격 = 0: 기상이 자동값(출발 − 준비 시간)에서 당겨진 분을 빼서 되찾음
      출발 알람이 없거나 기상 · 출발을 직접 골랐으면 당겨진 분을 알 수 없어 간격 + 당김 (몇 분 틀어질 수 있음 — PR 한계)
    """
    wake = next((a for a in existing if a["alarm_type"] == "WAKE"), None)
    prepare = next((a for a in existing if a["alarm_type"] == "PREPARE"), None)
    if not wake or not prepare or prepare["time_source"] == "MANUAL":
        return None
    gap = js_round((_ms(prepare["scheduled_at"]) - _ms(wake["scheduled_at"])) / 60_000)
    if not advance > 0:
        return gap if gap > 0 and gap <= 240 else None
    wake_shift = 0
    depart = next((a for a in existing if a["alarm_type"] == "DEPARTURE"), None)
    if (
        gap == 0
        and depart
        and depart["time_source"] != "MANUAL"
        and wake["time_source"] != "MANUAL"
        and prepare_minutes is not None
    ):
        wake_shift = max(
            0,
            js_round(
                (
                    _ms(depart["scheduled_at"])
                    - _js_number(prepare_minutes) * 60_000
                    - _ms(wake["scheduled_at"])
                )
                / 60_000
            ),
        )
    base = gap + advance - wake_shift
    return base if base > 0 and base <= 240 else None


# ---- 계획 ----


@dataclass
class Plan:
    basics: Basics
    with_wake: bool
    same_day_wake: bool
    before_prepare_minutes: float
    travel: Any  # ResolvedTravel
    travel_minutes: float
    prepare: PrepareBasis
    recent_prepare: list[
        int
    ]  # H03 "최근 5번" — 최신순 최대 5개(분). 그날 두 번째 일정(기록을 안 씀)은 빈 목록
    intensity: Any  # ResolvedIntensity
    kinds: list[str] | None  # 만든 종류 (None = 계산된 전부)
    manual_times: dict[str, datetime]
    full_chain: list[Any]  # 종류를 거르기 전 (미리 보기 · 단축 준비 안내용)
    chain: list[Any]  # 만들 알람
    departure: datetime
    check: Any  # CheckResult
    warnings: list[dict[str, Any]]


# 어느 쪽에서 부르는지 — preview = alarm-suggest(저장하지 않음), save = alarm-commit(저장). 계산은 같고 경고 문구만 다름
PAST_ALARM_MESSAGE = {
    "preview": "이미 지난 알림은 저장할 때 꺼져요",
    "save": "이미 지난 알림은 끄고 저장했어요",
}


def compute_plan(i: PlanInputs, travel: Any, mode: str = "save") -> Plan:
    """travel.chosen이 있는 경우만 부름 (없으면 부르는 쪽이 ROUTE_UNAVAILABLE)"""
    basics = resolve_basics(
        draft=i.draft, profile=i.profile, appointment=i.appointment, participant=i.participant
    )
    chosen = travel.chosen
    if not chosen:
        raise ValueError("이동시간 없이 계획을 만들 수 없어요")
    chosen_minutes = chosen.minutes

    # 받아들인 준비 알람 당김 (#484 · 안건 D111) — 계획의 준비 시간 · 준비 전 활동은 원래 값 그대로 두고 알람 사슬만 당김(calc/chain.py)
    advance = max(
        0,
        _or_zero(
            _js_number(_first(i.participant.get("prepare_advance_minutes") if i.participant else None, 0))
        ),
    )
    existing_advance = max(0, _or_zero(_js_number(_first(i.existing_advance_minutes, advance))))

    def derive(with_wake: bool) -> tuple[Any, Any]:
        if i.draft.before_prepare_minutes is not None:
            before_prepare_minutes: Any = i.draft.before_prepare_minutes
        elif with_wake:
            inferred = infer_before_prepare(
                i.existing_alarms,
                existing_advance,
                i.participant.get("prepare_minutes") if i.participant else None,
            )
            before_prepare_minutes = inferred if inferred is not None else DEFAULT_BEFORE_PREPARE_MINUTES
        else:
            before_prepare_minutes = 0

        # 기록(stats.p80)은 이미 "기상 ~ 출발"이라 그대로 씀 — 준비 전 활동을 또 더하지 않음 (학습 루프 기초 2절 1)
        # 개인 보정은 그날 첫 일정(기상 알람 있음)에만 — 두 번째 일정(기상 없음, 공통 15분)은 기록 수도 0으로 넘김 → 출처 DEFAULT · 보정 0
        # 깨우는 여유(wake_margin)는 내 기록의 0.7 · 0.8 · 0.9 분위수. 직접 고른 준비 시간이 있으면 그 값이 먼저
        # 첫 값(공통)은 온보딩 "일어나서 나가기까지" (2절 3). 반복 다음 회차는 prepare_bound로 직전 회차 ±10분 안에서만 움직임 (1절 ④)
        fallback = predict_prepare(
            {
                "commonP80": first_prepare_minutes(i.profile) if with_wake else DEFAULT_COMMON_P80["sameDay"],
                "personalP80": personal_prepare_of(
                    i.stats, wake_margin_of(i.profile.get("wake_margin")), basics.importance
                )
                if with_wake
                else None,
                "sampleCount": i.stats["sampleCount"] if with_wake else 0,
                "override": i.draft.prepare_minutes,
            }
        )
        # C1/W4가 적용돼도 직접 지정한 준비시간과 그날 두 번째 일정(기상 없음)은 기존 규칙이 우선.
        # 기존 DTO 필드(commonP80/correction)는 화면 호환을 위해 유지. MODEL일 때 correction은
        # 기존 공통 기준 대비 모델값 차이이며 개인 P80 보정으로 해석하면 안 됨.
        ml = i.ml_prediction if with_wake and i.draft.prepare_minutes is None else None
        if ml:
            predicted: PrepareBasis = {
                **fallback,
                "minutes": ml["minutes"],
                "autoMinutes": ml["minutes"],
                "correction": ml["minutes"] - fallback["commonP80"],
                "residualP80": 0,
                "weight": 0,
                "source": "MODEL",
                "confidence": "LOW",  # 실제 서비스 대상에서 검증/보정 전: 높은 신뢰도를 주장하지 않음
                "mlModel": ml["model"],
            }
        else:
            predicted = fallback
        prepare = (
            clamp_prepare_to_bound(predicted, i.prepare_bound) if i.prepare_bound and with_wake else predicted
        )
        return before_prepare_minutes, prepare

    manual_times = resolve_manual_times(i.draft.manual_times, i.existing_alarms)
    time_zone = safe_time_zone(i.profile.get("timezone"))
    # 그날 첫 일정이면 기상 알람 — 단 계산된 기상이 그날 낮 12시 이후(저녁에만 일정이 있는 날)면 기상 없이 준비 · 출발(+ 도착)만 (B1 버그 후보 4, 헤드 10/30)
    #   먼저 기상 있는 계산으로 기상 시각을 구해 보고, 12:00 이후면 기상 없는 계산(준비 15분)으로 다시 함. 둘 다 순수 계산
    #   직접 정한 값은 존중: 요청의 with_wake · 직접 고른 기상 시각(manual_times.WAKE)이 있으면 자동 규칙을 건너뜀
    #   L6 안건후보: kinds에 WAKE가 들어 있는 것만으로는 존중하지 않음 — kinds는 원래 거르기만 할 뿐 기상을 만들지 못함
    with_wake = i.draft.with_wake if i.draft.with_wake is not None else not i.same_day_wake
    derived = derive(with_wake)
    if i.draft.with_wake is None and with_wake and manual_times.get("WAKE") is None:
        depart = departure_at(
            {
                "at": basics.at,
                "travelMinutes": chosen_minutes,
                "bufferMinutes": basics.buffer_minutes,
                "prepareMinutes": derived[1]["minutes"],
                "beforePrepareMinutes": derived[0],
                "withWake": True,
                "manualTimes": {},
            }
        )
        if is_evening_wake(from_ms(ms(depart) - int(derived[1]["minutes"] * 60_000)), time_zone, basics.at):
            with_wake = False
            derived = derive(False)
    before_prepare_minutes, prepare = derived

    # 잠귀(profiles.wake_sensitivity): 무거움(HEAVY)이면 강, 가벼움 · 보통은 기본 보통 (D69)
    wake_sensitivity = wake_sensitivity_of(i.profile.get("wake_sensitivity"))
    auto = auto_level(basics.importance, i.late_count4w, wake_sensitivity)
    intensity = resolve_intensity(
        basics.importance,
        i.late_count4w,
        resolve_overrides(i.draft.levels, i.existing_alarms, auto),
        wake_sensitivity,
    )

    kinds = resolve_kinds(i.draft.kinds, i.existing_alarms)
    chain_input: ChainInput = {
        "at": basics.at,
        "travelMinutes": chosen_minutes,
        "bufferMinutes": basics.buffer_minutes,
        "prepareMinutes": prepare["minutes"],
        "beforePrepareMinutes": before_prepare_minutes,
        "withWake": with_wake,
        "manualTimes": manual_times,
        # 준비 알람을 직접 고른 일정은 당기지 않음 (그 시각이 먼저, D42 C)
        "prepareAdvanceMinutes": 0 if manual_times.get("PREPARE") else advance,
    }
    full_chain = alarm_chain(chain_input)
    chain = alarm_chain({**chain_input, "kinds": kinds})
    departure = departure_at(chain_input)
    check = check_alarms(chain, departure, i.now)

    warnings: list[dict[str, Any]] = list(travel.warnings)
    if check["ok"]:
        for a in check["alarms"]:
            if a.get("disabledReason") == "PASSED":
                warnings.append(
                    {"code": "PAST_ALARM_DISABLED", "message": PAST_ALARM_MESSAGE[mode], "detail": a["kind"]}
                )

    return Plan(
        basics=basics,
        with_wake=with_wake,
        same_day_wake=i.same_day_wake,
        before_prepare_minutes=before_prepare_minutes,
        travel=travel,
        travel_minutes=chosen_minutes,
        prepare=prepare,
        recent_prepare=recent_prepare_of(i.stats) if with_wake else [],
        intensity=intensity,
        kinds=kinds,
        manual_times=manual_times,
        full_chain=full_chain,
        chain=chain,
        departure=departure,
        check=check,
        warnings=warnings,
    )


# ---- 응답 모양 (alarm-suggest, 5.4.1 AlarmSuggestionDTO) ----


def _iso(d: datetime | None) -> str | None:
    return to_iso(d) if d else None


def _kind_at(chain: Sequence[Any], kind: str) -> datetime | None:
    found = next((x for x in chain if x["kind"] == kind), None)
    return found["at"] if found else None


LEVEL_NAME = ("약", "중", "강")
IMPORTANCE_NAME = {"LOW": "평소", "NORMAL": "중요", "HIGH": "매우 중요"}


def to_travel_required_suggestion(i: PlanInputs, travel: Any) -> dict[str, Any]:
    """이동시간을 구할 길이 없을 때의 미리 보기 — 오류 대신 "이동시간만 직접 입력해 주세요"를 알리는 200 응답 (7.1.2 마지막 단계)

    이동시간에 안 걸리는 칸(준비 · 강도 · 그날 첫 일정 여부)은 그대로 채우고, 이동시간이 있어야 나오는 시각은 비움
    check.reason = TRAVEL_REQUIRED. 앱은 travel.manual_required가 true면 입력 칸을 보이고 participant.est_travel_minutes(MANUAL)로 다시 부름
    """
    from app.shared.route import ResolvedTravel, TravelChoice

    # 이동시간 없이는 시각을 못 구하므로 자리표시 값(0분)으로 계산해서 준비 · 강도 칸만 가져옴 — 시각 · 경고는 아래에서 비움
    placeholder = compute_plan(
        i,
        ResolvedTravel(
            chosen=TravelChoice(minutes=0, source="MANUAL", needs_confirmation=True, note=""),
            auto=None,
            warnings=[],
        ),
        "preview",
    )
    return {
        **to_suggestion(placeholder),
        "wake_at": None,
        "prepare_at": None,
        "departure_at": None,
        "arrive_at": None,
        "alarms": [],
        "check": {
            "ok": False,
            "reason": "TRAVEL_REQUIRED",
            "message": "이동시간을 구하지 못했어요. 직접 입력해 주세요",
        },
        "travel": {
            "minutes": None,
            "source": None,
            "manual_required": True,
            "needs_confirmation": True,
            "sample_count": None,
            "note": "이동시간을 구하지 못했어요. 직접 입력해 주세요",
            "auto_minutes": None,
            "auto_source": None,
            "corrected_by": None,
        },
        "warnings": travel.warnings,
    }


def intensity_reason(basics: Any, it: Any) -> str:
    """기본 강도는 보통(D69) — 매우 중요 · 늦잠 지각 · 잠귀가 있을 때만 "올림"이 문구에 붙음. 중요도 낮음이어도 약이라고 쓰지 않음"""
    up_text = " (한 단계 올림)" if it["importancePoint"] else ""
    late_text = f" + 최근 4주 늦잠 지각 {_js_str(it['lateCount'])}회" if it["latePoint"] else ""
    heavy_text = " + 잠귀가 무거움" if it["heavySleeper"] else ""
    base = f"{IMPORTANCE_NAME[basics.importance]} 일정{up_text}{late_text}{heavy_text} → {LEVEL_NAME[it['auto']]}"
    return f"{base} (직접 고른 칸이 있어요)" if it["manual"] else base


def _js_str(n: Any) -> str:
    """JS `${n}` — 3.0은 3"""
    return str(int(n)) if isinstance(n, float) and n.is_integer() else str(n)


def to_suggestion(plan: Plan, inputs: PlanInputs | None = None) -> dict[str, Any]:
    """inputs를 주면 켜짐 여부도 저장과 같은 규칙으로 보여 줌 (요청의 enabled · 사용자가 끈 알람) — 안 주면 지난 알림 검사 결과만"""
    basics, travel, prepare, intensity = plan.basics, plan.travel, plan.prepare, plan.intensity
    chosen = travel.chosen
    auto_choice = travel.auto
    if plan.check["ok"]:
        checked: list[dict[str, Any]] = [
            {
                "kind": a["kind"],
                "at": a["at"],
                "auto": a.get("auto"),
                "enabled": a["enabled"],
                "disabled_reason": a.get("disabledReason"),
            }
            for a in plan.check["alarms"]
        ]
    else:
        checked = [
            {
                "kind": a["kind"],
                "at": a["at"],
                "auto": a.get("auto"),
                "enabled": True,
                "disabled_reason": None,
            }
            for a in plan.chain
        ]
    rows = []
    for a in checked:
        if inputs:
            enabled = resolve_enabled(
                checked_enabled=a["enabled"],
                old=next((x for x in inputs.existing_alarms if x["alarm_type"] == a["kind"]), None),
                requested=None
                if a["kind"] == "ARRIVE" or inputs.draft.enabled is None
                else inputs.draft.enabled.get(a["kind"]),
                now=inputs.now,
            )
        else:
            enabled = a["enabled"]
        rows.append({**a, "enabled": enabled})
    return {
        "appointment_at": _iso(basics.at),
        "wake_at": _iso(_kind_at(plan.full_chain, "WAKE")),
        "prepare_at": _iso(_kind_at(plan.full_chain, "PREPARE")),
        "departure_at": _iso(plan.departure),
        "arrive_at": _iso(_kind_at(plan.full_chain, "ARRIVE")),
        # 만들 알람(고른 종류) — 지난 알림은 is_enabled false로 (8.2.1). 저장은 이 목록과 같게 됨
        "alarms": [
            {
                "alarm_type": a["kind"],
                "scheduled_at": _iso(a["at"]),
                "auto_scheduled_at": _iso(a["auto"]),
                "time_source": "MANUAL" if a["auto"] is not None else "AUTO",
                "is_enabled": a["enabled"],
                "disabled_reason": a["disabled_reason"],
            }
            for a in rows
        ],
        # 저장하면 UNPROCESSABLE이 될 입력 (미리 보기는 막지 않고 알려 줌)
        "check": {"ok": True}
        if plan.check["ok"]
        else {"ok": False, "reason": plan.check["reason"], "message": plan.check["message"]},
        "travel": {
            "minutes": plan.travel_minutes,
            "source": chosen.source if chosen else None,
            "manual_required": False,
            "needs_confirmation": _first(chosen.needs_confirmation if chosen else None, False),
            "sample_count": _first(chosen.sample_count if chosen else None),
            "note": _first(chosen.note if chosen else None),
            "auto_minutes": _first(auto_choice.minutes if auto_choice else None),
            "auto_source": _first(auto_choice.source if auto_choice else None),
            "corrected_by": None,  # 7.3.1 개인 이동 보정은 아직 없음
        },
        "prepare": {
            "minutes": prepare["minutes"],
            "auto_minutes": prepare["autoMinutes"],
            "source": prepare["source"],
            **({"model": prepare["mlModel"]} if prepare.get("mlModel") else {}),
            "common_p80": prepare["commonP80"],
            "correction": prepare["correction"],
            "sample_count": prepare["sampleCount"],
            "prior": prepare["prior"],
            "weight": prepare["weight"],
            "residual_p80": prepare["residualP80"],
            "before_prepare_minutes": plan.before_prepare_minutes,
            "recent": plan.recent_prepare,  # 내 기상~출발 기록 최근 5개, 최신순 (0건이면 빈 배열)
        },
        "intensity": {
            "level": intensity["fields"]["intensity"],
            "voice_tone": intensity["fields"]["voiceTone"],
            "sound_key": intensity["fields"]["soundKey"],
            "vibration": intensity["fields"]["vibration"],  # 직접 고른 진동만 값, 자동이면 None (강도를 따름)
            "source": "MANUAL" if intensity["manual"] else "AUTO",
            "reason": intensity_reason(basics, intensity),
            "auto_level": intensity["auto"],
            "importance_point": intensity["importancePoint"],
            "late_count": intensity["lateCount"],
            "late_point": intensity["latePoint"],
            "axes": {
                a: {"level": intensity["axes"][a]["level"], "manual": intensity["axes"][a]["manual"]}
                for a in ("sound", "voice", "vibration")
            },
        },
        "with_wake": plan.with_wake,
        "same_day_wake": plan.same_day_wake,
        "confidence": prepare["confidence"],
        "warnings": plan.warnings,
    }

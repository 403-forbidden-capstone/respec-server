"""본 레포 supabase/functions/_shared/plan.ts의 서버판 (기준 b140f70) — 알람 계획 만들기의 DB 쪽

읽기는 부른 사람의 RLS 클라이언트로 (남의 일정 · 기록은 애초에 안 보임). 읽어 온 값을 plan_core의 순수 계산에 넘김.
alarm-suggest(미리 보기)와 alarm-commit(저장)이 같이 씀.
DB 행(ProfileRow · AppointmentRow · ParticipantRow · AlarmRow)은 PostgREST가 돌려준 dict 그대로.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from ..core.config import Settings
from ..core.errors import AppError
from ..kakao import kakao_travel_minutes
from ..kakao_parse import to_number
from .calc.prepare import PREPARE_RECORD_MAX, PREPARE_RECORD_MIN, PREPARE_WINDOW, prepare_stats_of
from .calc.travel import TravelSample
from .calc.wake import has_earlier_wake
from .db import db_fail, must
from .js import from_ms, ms, parse_iso, to_iso
from .plan_core import (
    Plan,
    PlanDraft,
    PlanInputs,
    compute_plan,
    resolve_basics,
    same_day_wakes_from_rows,
)
from .prepare_ml import (
    approved_ml_profile,
    build_ml_request,
    may_use_ml,
    previous_date_key,
    request_ml_prediction,
)
from .route import ResolvedTravel, TravelChoice, TravelDeps, resolve_travel
from .server_settings import predict_settings
from .supabase import Db, create_service_client
from .survey_pure_prep import pure_prep_candidate
from .tz import local_day_range, safe_time_zone

log = logging.getLogger("respec.plan")


@dataclass
class PlanContext:
    user_id: str
    db: Db  # 부른 사람의 RLS 클라이언트
    now: datetime
    kakao_key: str | None = None  # KAKAO_REST_API_KEY. 없으면 길찾기 단계를 건너뜀 (F10)
    http: httpx.AsyncClient | None = (
        None  # 원본의 fetchImpl 자리 — 카카오 · 예측 서버 호출. 없으면 호출마다 임시 클라이언트
    )
    settings: Settings | None = (
        None  # 예측 서버 주소 · 토큰(PREPARE_ML_URL · _API_TOKEN)은 Settings(env). 켜기 조건 3개는 서버 설정 표 (없으면 예측 꺼짐)
    )


DEFAULT_PROFILE: dict[str, Any] = {
    "timezone": "Asia/Seoul",
    "default_buffer_minutes": 10,
    "default_transport": "TRANSIT",
    "home_name": None,
    "home_lat": None,
    "home_lng": None,
    "default_prepare_minutes": None,
    "default_outing_prep_minutes": None,
    "default_outing_prep_updated_at": None,
    "ml_profile_consented_at": None,
    "wake_sensitivity": "NORMAL",
    "wake_margin": "NORMAL",
}

ALARM_COLUMNS = (
    "alarm_id, participant_id, user_id, alarm_type, scheduled_at, intensity, voice_tone, sound_key, vibration, "
    "is_enabled, snooze_limit, time_source, intensity_source, recurrence_rule, original_scheduled_at, "
    "original_time_source"
)


def _js_number(value: Any) -> float | int:
    """JS Number(value) — 읽을 수 없으면 NaN"""
    number = to_number(value)
    return float("nan") if number is None else number


async def load_profile(db: Db, user_id: str) -> dict[str, Any]:
    row = must(
        "profiles",
        await db.from_("profiles")
        .select(
            "timezone, default_buffer_minutes, default_transport, home_name, home_lat, home_lng, "
            "default_prepare_minutes, default_outing_prep_minutes, "
            "default_outing_prep_updated_at, ml_profile_consented_at, "
            "wake_sensitivity, wake_margin"
        )
        .eq("user_id", user_id)
        .maybe_single(),
    )
    return row or DEFAULT_PROFILE


async def load_appointment(db: Db, appointment_id: str) -> dict[str, Any] | None:
    return must(
        "appointments",
        await db.from_("appointments")
        .select(
            "appointment_id, creator_id, group_id, title, appointment_at, dest_name, dest_lat, dest_lng, "
            "importance, recurrence_rule"
        )
        .eq("appointment_id", appointment_id)
        .maybe_single(),
    )


# 반복 회차의 기준이 되는 약속 (alarm-roll) — 묶음 · 날짜 · 메모 · 장소 반경까지 읽고, 만든 사람(creator_id)으로 좁힘
#   load_appointment는 id만으로 읽어서 RLS가 거르는 부른 사람 클라이언트 전용. alarm-roll 배치는 service_role이라 RLS를 안 타므로 여기서 직접 좁힘
# SeriesAppointmentRow = AppointmentRow + series_id · occurrence_on · memo · dest_radius_m (dict)
SERIES_APPOINTMENT_COLUMNS = (
    "appointment_id, creator_id, group_id, title, appointment_at, dest_name, dest_lat, dest_lng, importance, "
    "recurrence_rule, series_id, occurrence_on, memo, dest_radius_m"
)


async def load_series_appointment(db: Db, creator_id: str, appointment_id: str) -> dict[str, Any] | None:
    return must(
        "appointments",
        await db.from_("appointments")
        .select(SERIES_APPOINTMENT_COLUMNS)
        .eq("appointment_id", appointment_id)
        .eq("creator_id", creator_id)
        .maybe_single(),
    )


# 한 사람의 약속 여러 개를 한 번에 (배치가 이번에 볼 묶음의 기준 회차를 사람별로 묶어 읽음)
async def load_series_appointments(
    db: Db, creator_id: str, appointment_ids: list[str]
) -> list[dict[str, Any]]:
    if not appointment_ids:
        return []
    return must(
        "appointments",
        await db.from_("appointments")
        .select(SERIES_APPOINTMENT_COLUMNS)
        .eq("creator_id", creator_id)
        .in_("appointment_id", list(appointment_ids)),
    )


async def load_participant(db: Db, appointment_id: str, user_id: str) -> dict[str, Any] | None:
    return must(
        "appointment_participants",
        await db.from_("appointment_participants")
        .select(
            "participant_id, appointment_id, user_id, origin_name, origin_lat, origin_lng, transport_type, "
            "est_travel_minutes, travel_source, prepare_minutes, prepare_value_source, "
            "outing_prep_minutes, outing_prep_source, outing_prep_model, "
            "buffer_minutes, prepare_advance_minutes"
        )
        .eq("appointment_id", appointment_id)
        .eq("user_id", user_id)
        .maybe_single(),
    )


# user_id를 주면 그 사람의 알람으로 한 번 더 좁힘 (service_role로 읽는 alarm-roll 배치가 남의 행에 닿지 않게)
async def load_alarms(db: Db, participant_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
    query = db.from_("alarms").select(ALARM_COLUMNS).eq("participant_id", participant_id)
    return must("alarms", await (query.eq("user_id", user_id) if user_id else query))


# 예상 준비시간 통계 — 내 기상~출발 기록(daily_records.actual_prepare_minutes) 중 10~240분인 최근 30건 (학습 루프 기초 ③)
#   예전에는 뷰 v_prepare_time_stats(전체 기간 · 거르기 없음)를 읽었음. 뷰와 마이그레이션은 그대로 두고 쓰지 않음
#   기록은 하루 한 줄(record_date)이라 날짜 최신순 = 출발 최신순. 부른 사람의 RLS 클라이언트라 내 기록만 보이지만, 친구 기록을 읽는 정책이 생겨도 내 줄만 오게 user_id로 좁힘
#   n(sampleCount)도 같은 30건 기준 — P80은 calc의 percentile (선형 보간, 뷰의 percentile_cont(0.8)과 같은 방식)
async def load_prepare_stats(db: Db, user_id: str) -> Any:
    rows = must(
        "daily_records",
        await db.from_("daily_records")
        .select("actual_prepare_minutes")
        .eq("user_id", user_id)
        .gte("actual_prepare_minutes", PREPARE_RECORD_MIN)
        .lte("actual_prepare_minutes", PREPARE_RECORD_MAX)
        .order("record_date", ascending=False)
        .limit(PREPARE_WINDOW),
    )
    return prepare_stats_of([r["actual_prepare_minutes"] for r in rows])


# 최근 4주 지각 횟수 (is_late). 늦잠이 원인인 지각만 세는 late_cause 칸은 DB에 없어서 지각 전체로 셈 — 문서 보완 목록
async def load_late_count(db: Db, user_id: str, now: datetime) -> int:
    since = to_iso(now - timedelta(days=28))
    result = (
        await db.from_("appointment_participants")
        .select("participant_id", count="exact", head=True)
        .eq("user_id", user_id)
        .eq("is_late", True)
        .gte("arrived_at", since)
    )
    if result.error:
        db_fail("late count", result.error)
    return result.count or 0


# 그날(내 시간대 0시 기준) 이 약속보다 약속 시각이 앞선 내 참여에 켜진 기상 알람이 있는지 — 있으면 이 약속은 기상 없이 준비부터
#   내 참여 → 그 약속의 시각 + 기상 알람을 한 번에 읽고(임베드 appointments!inner · alarms!inner), 그날 전체를 가져와서 앞뒤 판정은 calc/wake.py에 맡김
#   (알람이 울리는 시각이 아니라 약속 시각으로 가림 — 10시 약속의 8시 기상이 있는 날 8:30 약속은 그날 첫 일정)
async def load_same_day_wake(
    db: Db, user_id: str, at: datetime, time_zone: str, own_appointment_id: str | None = None
) -> bool:
    day = local_day_range(at, time_zone)
    start, end = day["start"], day["end"]
    rows = must(
        "same-day wake",
        await db.from_("appointment_participants")
        .select("appointment_id, appointments!inner(appointment_at), alarms!inner(is_enabled)")
        .eq("user_id", user_id)
        .eq("alarms.alarm_type", "WAKE")
        .gte("appointments.appointment_at", to_iso(start))
        .lt("appointments.appointment_at", to_iso(end)),
    )
    return has_earlier_wake(at, same_day_wakes_from_rows(rows), own_appointment_id)


# 내 지난 이동 기록 (출발 · 도착이 모두 찍힌 것) — 같은 경로 실측 중앙값용. alarm-suggest · alarm-commit은 읽기만 하고(CACHED로 쓰지 않음, D5),
# alarm-roll은 회차 길찾기가 12시간 전까지 안 될 때 이 중앙값을 CACHED로 저장함
#   이동 분은 appointment_participants.actual_travel_minutes(L2가 채움)가 있으면 그 값, 없으면 도착 − 출발 (calc/travel는 두 시각의 차를 쓰므로
#   실측 분이 있는 줄은 도착을 출발 + 그 분으로 바꿔 넘김). 출발 · 도착이 모두 찍힌 줄만 읽는 조건은 그대로
async def load_travel_samples(db: Db, user_id: str) -> list[Any]:
    rows = must(
        "travel samples",
        await db.from_("appointment_participants")
        .select(
            "origin_lat, origin_lng, transport_type, departed_at, arrived_at, actual_travel_minutes, "
            "appointments!inner(dest_lat, dest_lng)"
        )
        .eq("user_id", user_id)
        .not_("departed_at", "is", None)
        .not_("arrived_at", "is", None)
        .not_("origin_lat", "is", None)
        .order("arrived_at", ascending=False)
        .limit(100),
    )
    out: list[Any] = []
    for r in rows:
        embedded = r.get("appointments")
        a = (embedded[0] if embedded else None) if isinstance(embedded, list) else embedded
        if (
            not a
            or r.get("origin_lat") is None
            or r.get("origin_lng") is None
            or a.get("dest_lat") is None
            or a.get("dest_lng") is None
        ):
            continue
        departed_at = parse_iso(r["departed_at"])
        actual_raw = r.get("actual_travel_minutes")
        actual = float("nan") if actual_raw is None else _js_number(actual_raw)
        finite = actual == actual and abs(actual) != float("inf")
        out.append(
            TravelSample(
                origin={"lat": _js_number(r["origin_lat"]), "lng": _js_number(r["origin_lng"])},
                destination={"lat": _js_number(a["dest_lat"]), "lng": _js_number(a["dest_lng"])},
                transport=r["transport_type"],
                departedAt=departed_at,
                arrivedAt=from_ms(int(ms(departed_at) + actual * 60_000))
                if finite
                else parse_iso(r["arrived_at"]),
            )
        )
    return out


@dataclass
class BuiltPlan:
    plan: Plan
    inputs: PlanInputs
    time_zone: str


# 이동시간을 구할 길이 없을 때(좌표가 없고 직접 입력도 없음) — 7.1.2의 마지막 단계(직접 입력)로 가야 하는 상태
#   alarm-commit은 이 오류(502 ROUTE_UNAVAILABLE, details.manual_required)를 그대로 돌려주고,
#   alarm-suggest(미리 보기)는 잡아서 오류 대신 "이동시간만 직접 입력해 주세요"인 200 응답으로 바꿈 (to_travel_required_suggestion)
class TravelRequiredError(AppError):
    def __init__(self, inputs: PlanInputs, travel: ResolvedTravel, time_zone: str) -> None:
        super().__init__(
            "ROUTE_UNAVAILABLE",
            "이동시간을 구하지 못했어요. 직접 입력해 주세요",
            {"manual_required": True, "warnings": travel.warnings},
        )
        self.inputs = inputs
        self.travel = travel
        self.time_zone = time_zone


async def _optional_ml_prediction(
    ctx: PlanContext, at: datetime, time_zone: str, allow: bool
) -> dict[str, Any] | None:
    """CatBoost is experimental and off by default. Nothing is sent until both the
    feature flag and verified, persisted survey codes exist. All failures fall
    back to the original P80-based preparation-time calculation."""
    settings = ctx.settings
    if settings is None or not allow:
        return None
    url = (settings.prepare_ml_url or "").strip()
    token = settings.prepare_ml_api_token.get_secret_value().strip() if settings.prepare_ml_api_token else ""
    if not url or not token:
        return None  # 예측 서버가 안 붙어 있으면 표를 읽을 일도 없음
    # 켜기 조건 3개(켜짐 · 허용 계정 · 코드북 판)는 서버 설정 표에서 (캐시 30초 — 요청마다 DB를 안 부름).
    # 읽기에 실패하면 직전 값, 처음부터 실패면 꺼짐 (server_settings.py)
    ml = (await predict_settings()).settings
    if not may_use_ml(
        enabled=ml.enabled,
        allow_for_plan=allow,
        user_id=ctx.user_id,
        allowed_user_ids=ml.allowed_user_ids,
    ):
        return None

    try:
        # Never trust client-writable profiles.ml_* fields. The separate table
        # is readable only by service_role; this is reached only after auth +
        # exact user UUID allowlist. Match an explicitly approved codebook version.
        profile_result = (
            await create_service_client()
            .from_("prepare_ml_verified_profiles")
            .select("ml_age_code, ml_holiday_code, codebook_version, consent_at, verified_at")
            .eq("user_id", ctx.user_id)
            .maybe_single()
        )
        row = profile_result.data
        if profile_result.error or not row:
            return None
        verified = approved_ml_profile(row, ml.codebook_version)
        if not verified:
            return None

        # Crucial: use the record from EXACTLY the preceding local calendar day.
        # Unlike load_prepare_stats(), it is NOT the latest arbitrary history item.
        previous_result = (
            await ctx.db.from_("daily_records")
            .select("actual_prepare_minutes")
            .eq("user_id", ctx.user_id)
            .eq("record_date", previous_date_key(at, time_zone))
            .maybe_single()
        )
        previous = previous_result.data
        # Do not turn a query failure into a fabricated zero-minute warm-start input.
        minutes = (
            None
            if previous_result.error or previous is None or previous.get("actual_prepare_minutes") is None
            else _js_number(previous["actual_prepare_minutes"])
        )
        request = build_ml_request(
            appointment_at=at, timezone=time_zone, profile=verified, previous_day_minutes=minutes
        )
        if not request:
            return None
        if ctx.http is not None:
            return await request_ml_prediction(
                ctx.http, request, {"enabled": True, "url": url, "token": token}
            )
        async with httpx.AsyncClient() as client:
            return await request_ml_prediction(client, request, {"enabled": True, "url": url, "token": token})
    except Exception:
        return None  # Missing migration, API offline, timeout etc.


# want_auto_travel: 직접 입력한 이동시간이 있어도 자동 값을 같이 구함 (H03 "자동으로 되돌리기" 미리 보기). 호출이 한 번 늘어남
# mode: 어느 쪽에서 부르는지. preview(alarm-suggest)는 경고를 "저장할 때 …" 말투로, save(alarm-commit, 기본)는 저장한 뒤 말투로
async def build_plan(
    ctx: PlanContext, draft: PlanDraft, *, want_auto_travel: bool = False, mode: str | None = None
) -> BuiltPlan:
    profile = await load_profile(ctx.db, ctx.user_id)
    time_zone = safe_time_zone(profile["timezone"])

    appointment: dict[str, Any] | None = None
    participant: dict[str, Any] | None = None
    existing_alarms: list[dict[str, Any]] = []
    if draft.appointment_id:
        # RLS가 거르므로 내 일정이거나 내가 참여 중인 그룹 일정만 보임
        appointment = await load_appointment(ctx.db, draft.appointment_id)
        if not appointment:
            raise AppError("NOT_FOUND", "일정을 찾을 수 없어요")
        participant = await load_participant(ctx.db, appointment["appointment_id"], ctx.user_id)
        if participant:
            existing_alarms = await load_alarms(ctx.db, participant["participant_id"])

    basics = resolve_basics(draft=draft, profile=profile, appointment=appointment, participant=participant)

    async def same_day_wake_of() -> bool:
        if draft.with_wake is not None:
            return not draft.with_wake
        return await load_same_day_wake(
            ctx.db, ctx.user_id, basics.at, time_zone, appointment["appointment_id"] if appointment else None
        )

    stats, late_count_4w, same_day_wake = await asyncio.gather(
        load_prepare_stats(ctx.db, ctx.user_id),
        load_late_count(ctx.db, ctx.user_id, ctx.now),
        same_day_wake_of(),
    )

    inputs = PlanInputs(
        now=ctx.now,
        draft=draft,
        profile=profile,
        appointment=appointment,
        participant=participant,
        existing_alarms=existing_alarms,
        stats=stats,
        late_count4w=late_count_4w,
        same_day_wake=same_day_wake,
    )

    # 요청에 이동시간이 없고 저장된 게 직접 입력(MANUAL)이면 그대로 이어받음 — 시각 · 강도의 직접 값과 같은 규칙(D42 C). 길찾기를 다시 안 부름
    #   API로 저장된 값은 D5(카카오 응답 저장 · 재사용 불허) 때문에 이어받지 않고 다시 구함
    kept_manual_minutes = (
        participant.get("est_travel_minutes")
        if draft.travel_minutes is None and participant and participant.get("travel_source") == "MANUAL"
        else None
    )

    if draft.travel_minutes is not None and draft.travel_source == "API":
        # 앱이 확인 시트에서 보여 준 길찾기 값을 그대로 저장 — 다시 부르지 않음
        chosen = TravelChoice(draft.travel_minutes, "API", False, "길찾기 기준")
        travel = ResolvedTravel(chosen, chosen, [])
    else:
        kakao_dep = None
        if ctx.kakao_key:
            key = ctx.kakao_key

            async def kakao_call(a: dict[str, Any]) -> dict[str, Any]:
                if ctx.http is not None:
                    return await kakao_travel_minutes(
                        ctx.http,
                        key,
                        origin=a["origin"],
                        destination=a["destination"],
                        transport=a["transport"],
                    )
                async with httpx.AsyncClient() as client:
                    return await kakao_travel_minutes(
                        client,
                        key,
                        origin=a["origin"],
                        destination=a["destination"],
                        transport=a["transport"],
                    )

            kakao_dep = kakao_call

        async def samples() -> list[Any]:
            return await load_travel_samples(ctx.db, ctx.user_id)

        deps = TravelDeps(kakao=kakao_dep, samples=samples)

        def coords(p: Any) -> dict[str, float] | None:
            lat, lng = p.lat, p.lng
            return {"lat": lat, "lng": lng} if lat is not None and lng is not None else None

        travel = await resolve_travel(
            {
                "origin": coords(basics.origin),
                "destination": coords(basics.destination),
                "transport": basics.transport,
                "override": draft.travel_minutes if draft.travel_minutes is not None else kept_manual_minutes,
            },
            deps,
            want_auto=bool(want_auto_travel),
        )

    # 길찾기도 같은 경로 기록도 근사값도 없음 → 직접 입력하는 흐름으로 (8장 8.2.2)
    if not travel.chosen:
        raise TravelRequiredError(inputs, travel, time_zone)
    # Keep manual prepare settings and already-awake schedules on the existing path.
    allow_ml = (
        draft.prepare_minutes is None
        and draft.with_wake is not False
        and not (draft.with_wake is None and same_day_wake)
    )
    # Personal AUTO pure preparation uses a different target than the
    # older C1/W4 whole morning window. Prefer the user's explicitly saved
    # pure-preparation expectation; a survey proxy is optional and gated.
    personal = draft.new_group_id is None and (appointment is None or appointment.get("group_id") is None)
    # Rollout does NOT start merely because a user has saved a new profile
    # value. Keep current production alarms unchanged until a single explicitly
    # authorized account enables pure-prep scheduling.
    pure_enabled = bool(
        personal
        and ctx.settings
        and ctx.settings.pure_prep_apply_enabled
        and ctx.settings.pure_prep_allowed_user_id == ctx.user_id
    )
    if pure_enabled:
        inputs.pure_prep_candidate = await pure_prep_candidate(
            db=ctx.db,
            user_id=ctx.user_id,
            profile=profile,
            now=ctx.now,
            appointment_at=basics.at,
            timezone=time_zone,
            settings=ctx.settings,
            http=ctx.http,
            allow_model=allow_ml
            and not (participant and participant.get("prepare_value_source") == "MANUAL"),
        )
    # Never silently substitute legacy wake->departure model when a pure
    # preparation input is present. Historical fallback stays in plan_core.
    if inputs.pure_prep_candidate is None:
        inputs.ml_prediction = await _optional_ml_prediction(ctx, basics.at, time_zone, allow_ml)
    return BuiltPlan(
        plan=compute_plan(inputs, travel, "save" if mode is None else mode),
        inputs=inputs,
        time_zone=time_zone,
    )

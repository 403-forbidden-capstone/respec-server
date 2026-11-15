"""본 레포 supabase/functions/_shared/commit.ts의 서버판 (기준 b140f70) — alarm-commit이 DB에 쓰는 부분

읽기는 부른 사람의 RLS 클라이언트로, 쓰기는 service_role로 DB 함수 하나(public.alarm_commit_apply)를 한 번 불러서 — 약속 → 참여 조건 → 알람(고치기 · 만들기 · 지우기)
→ 고친 값 기록이 한 트랜잭션이라 중간에 실패하면 전부 취소됨 (TS commit.ts는 PostgREST로 따로 써서 손으로 되돌렸지만 서버판은 이 방식 — 정할 일 0-10).
어떤 행을 쓸지는 commit_core(순수)가 정하고, 새 약속 · 참여 조건 · 알람의 id는 서버가 미리 만들어(uuid4) 고친 값 기록까지 함수 부르기 전에 완성함.
요청 키(Idempotency-Key)가 있으면 같은 요청을 다시 보내도 첫 결과를 그대로 돌려줌 (DB 함수가 private.request_keys로 막음). 키가 없으면 중복 방지 없음.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..core.errors import AppError
from .calc.intensity import auto_level, resolve_intensity, wake_sensitivity_of
from .commit_core import build_edit_log_rows, diff_alarms
from .db import db_fail, must
from .draft import UNSET
from .js import ms, parse_iso, to_iso
from .plan import load_profile
from .plan_core import Plan, PlanDraft, PlanInputs, resolve_overrides
from .ring_package import AppointmentNotice, build_ring_packages, canceled_alarm_ids, subtitle_for
from .supabase import Db
from .tz import local_day_range


@dataclass
class CommitCtx:
    user_id: str
    user_db: Db  # 부른 사람의 RLS 클라이언트 (읽기)
    service: Db  # service_role (쓰기)
    now: datetime


ALARM_COLUMNS = (
    "alarm_id, participant_id, user_id, alarm_type, scheduled_at, intensity, voice_tone, sound_key, vibration, "
    "is_enabled, snooze_limit, time_source, intensity_source, recurrence_rule"
)
APPOINTMENT_COLUMNS = (
    "appointment_id, creator_id, group_id, title, appointment_at, dest_name, dest_lat, dest_lng, importance, "
    "recurrence_rule"
)


def _first(obj: Any, *names: str, default: Any = None) -> Any:
    """요청 · 계산 값이 dict(TypedDict)로 오든 객체로 오든 읽음 — 이름은 snake_case · camelCase 둘 다 받음 (없으면 default)"""
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    return default


def _has(obj: Any, *names: str) -> bool:
    """원본의 `x !== undefined` — dict는 키가 있는지, 객체는 속성이 있는지. 요청 객체의 UNSET(draft.UNSET = undefined)은 없는 것으로 봄"""
    return any(
        ((name in obj) if isinstance(obj, dict) else hasattr(obj, name)) and _first(obj, name) is not UNSET
        for name in names
    )


def _recurrence_of(created: Any) -> Any:
    """원본의 `created.recurrenceRule ?? null` — 요청에 없으면(UNSET) null"""
    value = _first(created, "recurrence_rule", "recurrenceRule")
    return None if value is UNSET else value


def _date_ms(value: str) -> float:
    """JS Date.parse — 읽을 수 없으면 NaN"""
    try:
        return float(ms(parse_iso(value)))
    except (ValueError, TypeError, OverflowError):
        return float("nan")


# 응답에 싣는 알람 요약 — 읽어 온 행에 없는 칸은 키를 뺌 (JS의 undefined는 JSON에서 빠짐)
SUMMARY_KEYS = (
    "alarm_id",
    "alarm_type",
    "scheduled_at",
    "is_enabled",
    "intensity",
    "voice_tone",
    "sound_key",
    "vibration",
    "time_source",
    "intensity_source",
    "recurrence_rule",
)


def summarize(a: dict[str, Any]) -> dict[str, Any]:
    return {key: a[key] for key in SUMMARY_KEYS if key in a}


def _new_id() -> str:
    return str(uuid.uuid4())


def _rpc_args(
    ctx: CommitCtx,
    *,
    key: str | None,
    request_hash: str | None,
    mode: str,
    appointment: dict[str, Any] | None,
    appointment_ref: dict[str, Any] | None,
    participant: dict[str, Any] | None,
    alarm_updates: list[dict[str, Any]],
    alarm_inserts: list[dict[str, Any]],
    alarm_removes: list[str],
    edit_logs: list[dict[str, Any]],
    before: list[dict[str, Any]],
) -> dict[str, Any]:
    """DB 함수 alarm_commit_apply의 인자 (키 이름은 SQL 시그니처 그대로)"""
    return {
        "p_user_id": ctx.user_id,
        "p_key": key,
        "p_request_hash": request_hash if key else None,
        "p_mode": mode,
        "p_appointment": appointment,
        "p_appointment_ref": appointment_ref,
        "p_participant": participant,
        "p_alarm_updates": alarm_updates,
        "p_alarm_inserts": alarm_inserts,
        "p_alarm_removes": alarm_removes,
        "p_edit_logs": edit_logs,
        "p_before": before,
    }


async def _apply(ctx: CommitCtx, args: dict[str, Any], not_found_message: str) -> dict[str, Any]:
    """DB 함수를 한 번 부름 — 오류는 서버 함수 오류로 (같은 요청 키로 다른 내용 = CONFLICT, 못 찾음(P0002) = NOT_FOUND)"""
    result = await ctx.service.rpc("alarm_commit_apply", args)
    error = result.error
    if error:
        if "IDEMPOTENCY_KEY_REUSED" in str(error.get("message") or ""):
            raise AppError(
                "CONFLICT", "같은 요청 키로 다른 내용을 보냈어요. 새로고침한 뒤 다시 시도해 주세요"
            )
        if error.get("code") == "P0002":
            raise AppError("NOT_FOUND", not_found_message)
        db_fail("alarm_commit_apply", error)
    return must("alarm_commit_apply", result)


# 저장해도 되는 계획인지 — 아니면 사용자에게 보일 오류로
#   · 이동시간은 길찾기 값(API)이나 사용자가 정한 값(MANUAL)만 저장. 서버가 대신 구한 같은 경로 중앙값(CACHED)은 D5가 열려 있어 쓰지 않고,
#     직선거리 근사값(ESTIMATE)은 사용자가 확인해야 하므로 확인한 값을 MANUAL로 다시 보내게 함 (7장 7.1.2 단계 4)
#   · 출발 시각까지 지났거나 알람 순서가 어긋나면 UNPROCESSABLE (8장 8.2.1)
def assert_persistable(plan: Plan) -> None:
    travel = plan.travel.chosen
    if not travel or (travel.source != "API" and travel.source != "MANUAL"):
        raise AppError(
            "ROUTE_UNAVAILABLE",
            "길찾기 값을 못 구해서 저장하지 못했어요. 이동시간을 확인하고 직접 입력해 주세요",
            {
                "manual_required": True,
                "fallback": {
                    "minutes": travel.minutes,
                    "source": travel.source,
                    "needs_confirmation": travel.needs_confirmation,
                    "sample_count": travel.sample_count,
                }
                if travel
                else None,
                "warnings": plan.travel.warnings,
            },
        )
    if not plan.check["ok"]:
        detail = plan.check.get("detail") or {}
        raise AppError("UNPROCESSABLE", plan.check["message"], {"reason": plan.check["reason"], **detail})


# ---------------------------------------------------------------------------
# 약속 알람
# ---------------------------------------------------------------------------


async def persist_appointment(
    ctx: CommitCtx,
    *,
    plan: Plan,
    inputs: PlanInputs,
    draft: PlanDraft,
    replace_existing: bool,
    edits: list[Any],
    time_zone: str,
    created: Any = None,  # 새 약속이면 (기존 약속이면 없음 — 기존 약속은 안 고침, D41)
    key: str | None = None,  # 요청 키 (Idempotency-Key 헤더). 없으면 중복 방지 없음
    request_hash: str | None = None,  # 요청 본문 해시 — 같은 키로 다른 내용을 가려냄
) -> dict[str, Any]:
    user_id, now = ctx.user_id, ctx.now
    # 지난 공동 약속에는 참여 조건을 새로 만들 수 없음 (9-32 · DB 정책 appointment_participants_insert_own와 같은 규칙). 이미 참여 중인 사람의 고치기는 그대로.
    #   '지난'은 하루 경계 — 내 폰 시간대의 오늘 0시보다 앞의 약속 (오늘 이미 지난 약속은 오늘 안에는 참여할 수 있음, 9-러)
    if inputs.appointment and not inputs.participant and inputs.appointment.get("group_id") is not None:
        day_start = local_day_range(now, time_zone)["start"]
        if _date_ms(inputs.appointment["appointment_at"]) < ms(day_start):
            raise AppError("UNPROCESSABLE", "지난 약속이에요", {"reason": "PAST_APPOINTMENT"})

    assert_persistable(plan)
    travel = plan.travel.chosen
    place = plan.basics.destination
    place_name = place.name

    # 1. 약속 (새 약속일 때만 만듦) — id는 서버가 미리 만듦
    appointment: dict[str, Any] | None = inputs.appointment
    new_appointment: dict[str, Any] | None = None
    if not appointment:
        if not created:
            raise AppError("VALIDATION_FAILED", "appointment_id 또는 appointment가 필요해요")
        destination = draft.destination
        new_appointment = {
            "appointment_id": _new_id(),
            "creator_id": user_id,
            "group_id": _first(created, "group_id", "groupId"),
            "title": _first(created, "title"),
            "appointment_at": to_iso(plan.basics.at),
            "dest_name": place_name,
            "dest_lat": place.lat,
            "dest_lng": place.lng,
            # 목적지 감지 반경 (D68). 새 약속일 때만 — 기존 약속은 안 고침
            "dest_radius_m": _first(destination, "radius_m", "radiusM") if destination else None,
            "importance": plan.basics.importance,
            # D44. 이 호출은 첫 회차만 만듦 — 다음 회차는 alarm-roll이 14일치를 미리 만듦 (D4 A, roll.ts)
            "recurrence_rule": _recurrence_of(created),
        }
        appointment_id = new_appointment["appointment_id"]
    else:
        appointment_id = appointment["appointment_id"]
    appointment_ref = (
        None
        if new_appointment
        else {
            "appointment_id": appointment_id,
            "title": appointment["title"],
            "appointment_at": appointment["appointment_at"],
        }
    )

    # 2. 내 참여 조건 (이동 · 준비 · 여유는 약속 시점 값을 그대로 보관, 4.4)
    participant_fields = {
        "origin_name": plan.basics.origin.name,
        "origin_lat": plan.basics.origin.lat,
        "origin_lng": plan.basics.origin.lng,
        "transport_type": plan.basics.transport,
        "est_travel_minutes": plan.travel_minutes,
        "travel_source": travel.source,
        "prepare_minutes": plan.prepare["minutes"],
        "buffer_minutes": plan.basics.buffer_minutes,
    }
    if inputs.participant:
        participant_id = inputs.participant["participant_id"]
        participant = {"participant_id": participant_id, "is_new": False, "fields": participant_fields}
    else:
        participant_id = _new_id()
        participant = {"participant_id": participant_id, "is_new": True, "fields": participant_fields}

    # 3. 알람 — 고치기 → 만들기 → 지우기 (순서는 DB 함수가 지킴)
    diff = diff_alarms(
        plan,
        inputs.existing_alarms,
        now=now,
        enabled=_first(draft, "enabled"),
        replace_existing=replace_existing,
    )
    diff_update = _first(diff, "update") or []
    diff_insert = _first(diff, "insert") or []
    diff_remove = _first(diff, "remove") or []
    alarm_updates = [{"alarm_id": _first(u, "alarm_id"), "patch": _first(u, "patch")} for u in diff_update]
    alarm_inserts = [
        {**w, "alarm_id": _new_id(), "user_id": user_id, "participant_id": participant_id}
        for w in diff_insert
    ]
    updated_ids = {u["alarm_id"] for u in alarm_updates}
    by_id = {a["alarm_id"]: a for a in inputs.existing_alarms}
    # 저장 뒤 알람 목록 (고친 것 → 만든 것 → 그대로인 것) — 고친 값 기록이 알람 id를 찾는 데 씀. DB 함수의 after와 같은 순서
    saved_alarms = [
        *[{**by_id.get(u["alarm_id"], {}), "alarm_id": u["alarm_id"], **u["patch"]} for u in alarm_updates],
        *alarm_inserts,
        *[
            a
            for a in inputs.existing_alarms
            if a["alarm_id"] not in diff_remove and a["alarm_id"] not in updated_ids
        ],
    ]

    # 4. 고친 값 기록 (user_edit_logs) — 요청에 담긴 것만
    edit_logs = build_edit_log_rows(user_id, appointment_id, saved_alarms, edits) if edits else []

    # 5. 한 번에 저장 → 응답
    saved = await _apply(
        ctx,
        _rpc_args(
            ctx,
            key=key,
            request_hash=request_hash,
            mode="APPOINTMENT",
            appointment=new_appointment,
            appointment_ref=appointment_ref,
            participant=participant,
            alarm_updates=alarm_updates,
            alarm_inserts=alarm_inserts,
            alarm_removes=list(diff_remove),
            edit_logs=edit_logs,
            before=inputs.existing_alarms,
        ),
        "고칠 약속 알람을 찾을 수 없어요",
    )
    after = saved["after"]
    saved_appointment = saved["appointment"]
    return {
        "appointment_id": saved_appointment["appointment_id"],
        "participant_id": saved["participant_id"],
        "alarms": [summarize(a) for a in after],
        "ring_packages": build_ring_packages(
            alarms=after,
            title=saved_appointment["title"],
            subtitle=subtitle_for(parse_iso(saved_appointment["appointment_at"]), place_name, time_zone),
            depart_at=plan.departure,
            # 준비 · 출발 알림 문구와 기상 음성 문장 (앱 reconcile이 같은 값으로 같은 문구를 만듦 — 이동은 저장한 est_travel_minutes)
            appointment=AppointmentNotice(
                appointment_at=parse_iso(saved_appointment["appointment_at"]),
                travel_minutes=plan.travel_minutes,
                time_zone=time_zone,
            ),
        ),
        "canceled_alarm_ids": canceled_alarm_ids(before=saved["before"], after=after),
        "warnings": plan.warnings,
    }


# ---------------------------------------------------------------------------
# 일정 없는 알람 (D46 · Q10 B)
# ---------------------------------------------------------------------------


async def commit_standalone(
    ctx: CommitCtx,
    *,
    alarm: Any,
    edits: list[Any],
    time_zone: str,
    key: str | None = None,  # 요청 키 (Idempotency-Key 헤더). 없으면 중복 방지 없음
    request_hash: str | None = None,
) -> dict[str, Any]:
    user_db, user_id, now = ctx.user_db, ctx.user_id, ctx.now
    alarm_id = _first(alarm, "alarm_id", "alarmId")
    alarm_scheduled_at = _first(alarm, "scheduled_at", "scheduledAt")
    alarm_enabled = _first(alarm, "is_enabled", "isEnabled")

    existing: dict[str, Any] | None = None
    if alarm_id:
        # 일정 없는 알람(약속 · 루틴과 연결 안 됨)만 고칠 수 있음
        existing = must(
            "alarm lookup",
            await user_db.from_("alarms")
            .select(ALARM_COLUMNS)
            .eq("alarm_id", alarm_id)
            .eq("user_id", user_id)
            .is_("participant_id", None)
            .is_("routine_id", None)
            .maybe_single(),
        )
        if not existing:
            raise AppError("NOT_FOUND", "고칠 알람을 찾을 수 없어요")

    scheduled_at = alarm_scheduled_at or (parse_iso(existing["scheduled_at"]) if existing else None)
    if not scheduled_at:
        raise AppError("VALIDATION_FAILED", "scheduled_at이 필요해요")
    is_enabled = (
        alarm_enabled
        if alarm_enabled is not None
        else (existing["is_enabled"] if existing and existing.get("is_enabled") is not None else True)
    )
    # 요청이 새로 보낸 시각이 이미 지났으면 울릴 수 없음. 끄는 요청은 시각과 상관없이 받음
    # 시각 없이 강도 · 켜기만 고치는 요청은 저장돼 있던 시각을 검사하지 않음 — 반복 알람은 첫 회차가 지나면 옛 시각이 남는데(D4 전),
    # 그 알람을 켜도 알람 코어가 지난 시각은 건너뛰므로(skipped_past) 안전함. 회차 넘기기는 D4 뒤
    if alarm_scheduled_at is not None and is_enabled and ms(alarm_scheduled_at) < ms(now):
        raise AppError("UNPROCESSABLE", "이미 지난 시각이라 알람을 만들 수 없어요", {"reason": "PAST_TIME"})

    # 일정이 없으니 중요도 점수도 늦잠 보정도 없음 → 중(NORMAL)이 자동 값 (작성자 판단 — 화면 입력이 정해지면 바뀔 수 있음). 고른 칸만 직접 값
    #   잠귀(profiles.wake_sensitivity)는 일정이 아니라 사람 기준이라 여기도 적용 — 깊음이면 자동 강 (헤드 확정 10/30, 학습 기초 b)
    wake_sensitivity = wake_sensitivity_of((await load_profile(user_db, user_id)).get("wake_sensitivity"))
    intensity = resolve_intensity(
        "NORMAL",
        0,
        resolve_overrides(
            _first(alarm, "levels"), [existing] if existing else [], auto_level("NORMAL", 0, wake_sensitivity)
        ),
        wake_sensitivity,
    )
    fields = intensity["fields"]
    write = {
        "alarm_type": "WAKE",
        "scheduled_at": to_iso(scheduled_at),
        "intensity": fields["intensity"],
        "voice_tone": fields["voiceTone"],
        "sound_key": fields["soundKey"],
        "vibration": fields["vibration"],
        "is_enabled": is_enabled,
        "time_source": "MANUAL",  # 시각 전부를 사용자가 정함
        "intensity_source": "MANUAL" if intensity["manual"] else "AUTO",
        "last_calculated_at": to_iso(now),
    }
    # D46 · Q10: 반복은 RRULE, 다음 회차는 alarm-roll이 새 행으로 (일정 없는 알람은 series_id · occurrence_on, D4 A)
    if _has(alarm, "recurrence_rule", "recurrenceRule"):
        recurrence = _first(alarm, "recurrence_rule", "recurrenceRule")
    else:
        recurrence = existing.get("recurrence_rule") if existing else None

    alarm_updates: list[dict[str, Any]] = []
    alarm_inserts: list[dict[str, Any]] = []
    if existing:
        patch = {**write, "recurrence_rule": recurrence}
        alarm_updates = [{"alarm_id": existing["alarm_id"], "patch": patch}]
        saved_alarm = {**existing, **patch}
    else:
        saved_alarm = {
            **write,
            "recurrence_rule": recurrence,
            "alarm_id": _new_id(),
            "user_id": user_id,
            "participant_id": None,
            "routine_id": None,
        }
        alarm_inserts = [saved_alarm]

    result = await _apply(
        ctx,
        _rpc_args(
            ctx,
            key=key,
            request_hash=request_hash,
            mode="STANDALONE",
            appointment=None,
            appointment_ref=None,
            participant=None,
            alarm_updates=alarm_updates,
            alarm_inserts=alarm_inserts,
            alarm_removes=[],
            edit_logs=build_edit_log_rows(user_id, None, [saved_alarm], edits) if edits else [],
            before=[existing] if existing else [],
        ),
        "고칠 알람을 찾을 수 없어요",
    )
    (saved,) = result["after"]

    return {
        "alarm_id": saved["alarm_id"],
        "alarms": [summarize(saved)],
        "ring_packages": build_ring_packages(
            alarms=[saved], title="알람", subtitle=subtitle_for(scheduled_at, None, time_zone)
        ),
        "canceled_alarm_ids": canceled_alarm_ids(before=result["before"], after=[saved]),
        "warnings": [],
    }

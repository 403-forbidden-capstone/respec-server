"""본 레포 supabase/functions/_shared/commit.ts의 서버판 (기준 b140f70) — alarm-commit이 DB에 쓰는 부분

쓰기는 service_role 클라이언트로, 읽기는 부른 사람의 RLS 클라이언트로. 어떤 행을 쓸지는 commit_core(순수)가 정함.
PostgREST에는 트랜잭션이 없어 순서(약속 → 참여 조건 → 알람 → 고친 값 기록)대로 쓰고, 이번 호출이 새로 만든 약속 · 참여 조건은
중간에 실패하면 지워서(CASCADE) 되돌림 (새 트랜잭션은 안 씀 — 원본과 같은 방식). 같은 요청을 다시 보냈을 때 같은 상태가 되는 건 기존 약속(appointment_id)이 있을 때만 —
새 약속(appointment)은 응답만 잃고 다시 보내면 약속 · 알람이 두 벌 생기고, 일정 없는 알람도 alarm_id 없이 다시 보내면 하나 더 생김 (요청 키는 아직 안 받음).
근본 해결은 DB 함수(RPC)로 묶는 것.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..core.errors import AppError
from .calc.intensity import auto_level, resolve_intensity, wake_sensitivity_of
from .commit_core import build_edit_log_rows, diff_alarms
from .db import must
from .draft import UNSET
from .js import ms, parse_iso, to_iso
from .plan import load_profile
from .plan_core import Plan, PlanDraft, PlanInputs, resolve_overrides
from .ring_package import AppointmentNotice, build_ring_packages, canceled_alarm_ids, subtitle_for
from .supabase import Db
from .tz import local_day_range

log = logging.getLogger("respec.commit")


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


async def _save_edit_logs(
    ctx: CommitCtx, appointment_id: str | None, alarms: list[dict[str, Any]], edits: list[Any]
) -> None:
    if not edits:
        return
    must(
        "user_edit_logs",
        await ctx.service.from_("user_edit_logs").insert(
            build_edit_log_rows(ctx.user_id, appointment_id, alarms, edits)
        ),
    )


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
) -> dict[str, Any]:
    service, user_id, now = ctx.service, ctx.user_id, ctx.now
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
    created_appointment_id: str | None = None
    created_participant_id: str | None = None

    try:
        # 1. 약속 (새 약속일 때만 만듦)
        appointment: dict[str, Any] | None = inputs.appointment
        if not appointment:
            if not created:
                raise AppError("VALIDATION_FAILED", "appointment_id 또는 appointment가 필요해요")
            destination = draft.destination
            row = must(
                "appointments insert",
                await service.from_("appointments")
                .insert(
                    {
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
                )
                .select(APPOINTMENT_COLUMNS)
                .single(),
            )
            created_appointment_id = row["appointment_id"]
            appointment = row

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
            must(
                "appointment_participants update",
                await service.from_("appointment_participants")
                .update(participant_fields)
                .eq("participant_id", participant_id)
                .eq("user_id", user_id),
            )
        else:
            row = must(
                "appointment_participants insert",
                await service.from_("appointment_participants")
                .insert(
                    {
                        "appointment_id": appointment["appointment_id"],
                        "user_id": user_id,
                        **participant_fields,
                    }
                )
                .select("participant_id")
                .single(),
            )
            participant_id = row["participant_id"]
            created_participant_id = participant_id

        # 3. 알람 — 고치기 → 만들기 → 지우기 순 (중간에 실패해도 있던 알람이 먼저 사라지지 않게)
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
        touched: list[dict[str, Any]] = []
        for u in diff_update:
            touched.append(
                must(
                    "alarms update",
                    await service.from_("alarms")
                    .update(_first(u, "patch"))
                    .eq("alarm_id", _first(u, "alarm_id"))
                    .eq("user_id", user_id)
                    .select(ALARM_COLUMNS)
                    .single(),
                )
            )
        if diff_insert:
            rows = [{**w, "user_id": user_id, "participant_id": participant_id} for w in diff_insert]
            touched.extend(
                must("alarms insert", await service.from_("alarms").insert(rows).select(ALARM_COLUMNS))
            )
        if diff_remove:
            must(
                "alarms delete",
                await service.from_("alarms").delete().in_("alarm_id", diff_remove).eq("user_id", user_id),
            )
        updated_ids = {_first(u, "alarm_id") for u in diff_update}
        untouched = [
            a
            for a in inputs.existing_alarms
            if a["alarm_id"] not in diff_remove and a["alarm_id"] not in updated_ids
        ]
        after = [*touched, *untouched]

        # 4. 고친 값 기록 (user_edit_logs) — 요청에 담긴 것만. 같은 요청을 다시 보내면 같은 줄이 또 들어감 (멱등 키는 아직 없음)
        await _save_edit_logs(ctx, appointment["appointment_id"], after, edits)

        # 5. 응답
        return {
            "appointment_id": appointment["appointment_id"],
            "participant_id": participant_id,
            "alarms": [summarize(a) for a in after],
            "ring_packages": build_ring_packages(
                alarms=after,
                title=appointment["title"],
                subtitle=subtitle_for(parse_iso(appointment["appointment_at"]), place_name, time_zone),
                depart_at=plan.departure,
                # 준비 · 출발 알림 문구와 기상 음성 문장 (앱 reconcile이 같은 값으로 같은 문구를 만듦 — 이동은 저장한 est_travel_minutes)
                appointment=AppointmentNotice(
                    appointment_at=parse_iso(appointment["appointment_at"]),
                    travel_minutes=plan.travel_minutes,
                    time_zone=time_zone,
                ),
            ),
            "canceled_alarm_ids": canceled_alarm_ids(before=inputs.existing_alarms, after=after),
            "warnings": plan.warnings,
        }
    except Exception:
        # 이번 호출이 새로 만든 것만 되돌림 (기존 약속 · 참여 조건 · 알람은 그대로 — 같은 요청을 다시 보내면 이어서 맞춰짐)
        try:
            if created_appointment_id:
                await service.from_("appointments").delete().eq("appointment_id", created_appointment_id)
            elif created_participant_id:
                await (
                    service.from_("appointment_participants")
                    .delete()
                    .eq("participant_id", created_participant_id)
                )
        except Exception:
            log.exception("[alarm-commit] 되돌리기 실패")
        raise


# ---------------------------------------------------------------------------
# 일정 없는 알람 (D46 · Q10 B)
# ---------------------------------------------------------------------------


async def commit_standalone(
    ctx: CommitCtx, *, alarm: Any, edits: list[Any], time_zone: str
) -> dict[str, Any]:
    user_db, service, user_id, now = ctx.user_db, ctx.service, ctx.user_id, ctx.now
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

    if existing:
        saved = must(
            "alarms update",
            await service.from_("alarms")
            .update({**write, "recurrence_rule": recurrence})
            .eq("alarm_id", existing["alarm_id"])
            .eq("user_id", user_id)
            .select(ALARM_COLUMNS)
            .single(),
        )
    else:
        saved = must(
            "alarms insert",
            await service.from_("alarms")
            .insert(
                {
                    **write,
                    "recurrence_rule": recurrence,
                    "user_id": user_id,
                    "participant_id": None,
                    "routine_id": None,
                }
            )
            .select(ALARM_COLUMNS)
            .single(),
        )

    await _save_edit_logs(ctx, None, [saved], edits)

    return {
        "alarm_id": saved["alarm_id"],
        "alarms": [summarize(saved)],
        "ring_packages": build_ring_packages(
            alarms=[saved], title="알람", subtitle=subtitle_for(scheduled_at, None, time_zone)
        ),
        "canceled_alarm_ids": canceled_alarm_ids(before=[existing] if existing else [], after=[saved]),
        "warnings": [],
    }

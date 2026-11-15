"""본 레포 supabase/functions/_shared/edit-run.ts의 서버판 (기준 b140f70) — appointment-edit이 DB를 읽고 쓰는 부분: 일정 고치기 · 지우기 · 되돌리기 · 공동 약속에서 빠지기

계산(어떤 범위가 무엇을 하는가 · 바뀌는 알람 · 확인 · 7.1.3 재판정)은 edit.py(순수), 알람 시각 계산은 plan_core.compute_plan · commit_core.diff_alarms 그대로 (alarm-commit과 같은 규칙).
라우터 없이 모듈로 빼서 가짜 DB로 시험함 (tests/shared/test_edit_run.py).
읽기 · 쓰기 모두 부른 사람의 RLS 클라이언트(user_db)로 함 — 남의 약속은 애초에 안 보이고 못 고침. service 클라이언트는 셋뿐:
  (1) roll_attempts 정리 (service 전용 표) (2) 공동 약속을 지울 때 다른 참여자의 출발 · 도착 기록 확인 (RLS로는 내 참여만 보임)
  (3) 공동 약속의 시각 · 장소를 만든 사람이 고칠 때 다른 참여자의 참여 조건 · 알람을 읽고 다시 계산해 씀(참여자별 user_id로 좁혀서) + 푸시 (9장)
supabase-js에는 트랜잭션이 없어 같은 요청을 다시 보내면 이어서 맞춰지는 순서로 씀:
  고치기 = (이후 모두면) 뒤 회차 지우기 → 참여 조건 → 알람 → 약속 행 마지막 / 지우기(이후 모두) = 뒤 회차 → 규칙 비우기 → 이 행 마지막
  약속 행을 마지막에 쓰는 이유: 앞에서 실패해도 약속 행이 안 바뀌어 있어 다시 보내면 고칠 것이 그대로 보임
하루 경계는 profiles.timezone(없으면 Asia/Seoul) 0시 (tz.py)

JS와 달라서 맞춘 곳: Promise.all → asyncio.gather · Promise.allSettled → gather(return_exceptions=True) 뒤 처음 실패를 올림(요청이 끝난 뒤 남은 쓰기가 안 돌게) ·
Env의 읽기 캐시는 Task로 한 번만 돌려 여러 곳이 같이 기다림 · Date → UTC aware datetime(ms 정수는 js.ms) · `opts.held ?`(Set은 비어도 참)는 `is not None` ·
응답은 dict(키는 TS와 같음, 없던 키는 안 넣음 — participants · notified) · 다른 묶음 객체(Plan · 경고 · 검사 결과)는 dict든 객체든 읽도록 _f로 꺼냄.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cmp_to_key
from typing import Any

from ..core.errors import AppError
from .calc import SameDayWake, has_earlier_wake, is_evening_wake
from .commit import summarize
from .commit_core import AlarmDiff, diff_alarms
from .db import must
from .edit import (
    CONFIRM_MOVE_MINUTES,
    UNSET,
    DayWakeRow,
    SeriesRow,
    alarm_changes,
    apply_patch,
    by_time_then_id,
    delete_mode,
    editor_verdict,
    later_rows_to_delete,
    needs_confirm,
    scope_effect,
    wake_action,
    wakes_of_rows,
)
from .edit_request import EditRequest
from .js import from_ms, ms, parse_iso, to_iso
from .notify import NotifyOptions, plan_group_push, send_group_push
from .plan import (
    ALARM_COLUMNS,
    SERIES_APPOINTMENT_COLUMNS,
    load_alarms,
    load_late_count,
    load_participant,
    load_prepare_stats,
    load_profile,
    load_travel_samples,
)
from .plan_core import (
    PlanDraft,
    PlanInputs,
    compute_plan,
    first_prepare_minutes,
    resolve_basics,
    without_friend_times,
)
from .ring_package import AppointmentNotice, build_ring_packages, canceled_alarm_ids, subtitle_for
from .roll import inherited_kinds, resolved_travel_of, shift_manual_times
from .route import TravelChoice, TravelDeps, resolve_travel
from .supabase import Db
from .tz import local_date_key, local_day_range, safe_time_zone

log = logging.getLogger("respec.edit")


@dataclass
class EditCtx:
    user_id: str  # 검증한 로그인 사용자 (요청 본문에서 받지 않음)
    user_db: Db  # 부른 사람의 RLS 클라이언트 — 읽기 · 쓰기 모두
    service: Db  # service_role — roll_attempts 정리 · 공동 약속 기록 확인 · 공동 약속 고치기의 참여자 동기화 · 푸시만
    now: datetime
    kakao: Callable[..., Awaitable[Any]] | None = None  # 장소를 바꿀 때만 씀. 키가 없으면 안 줌
    push: NotifyOptions | None = None  # 공동 약속 푸시를 보낼 때(Expo 액세스 토큰 등). 시험은 가짜 fetch


# EditWarning = dict: { code, message, detail? }
EditWarning = dict[str, Any]

# 모든 응답이 공통으로 가짐 — 앱 applyCommit({ ring_packages, canceled_alarm_ids })이 그대로 받음 (alarm-commit과 같은 이름 · 모양)
#   appointment_id · ring_packages(폰에 걸 예약: 고친 일정 + 7.1.3으로 다시 판정한 같은 날 다른 일정) · canceled_alarm_ids(폰에서 지울 예약) · warnings
# 동작별 칸 (모두 dict):
#   UPDATE  { saved, needs_confirm, changes[{kind, from, to}], alarms, deleted_appointment_ids, participants?{updated, held, skipped}, notified? }
#   DELETE  { deleted, mode(ROW|CANCEL_THIS|FROM_HERE|NONE), deleted_appointment_ids, notified? }
#   RESTORE { restored, reason?(NOT_FOUND|NOT_DELETED) } / LEAVE { left }
EditResponse = dict[str, Any]

# ---------------------------------------------------------------------------
# 읽기 도우미
# ---------------------------------------------------------------------------

APPOINTMENT_COLUMNS = f"{SERIES_APPOINTMENT_COLUMNS}, exception_kind"
PARTICIPANT_COLUMNS = "participant_id, appointment_id, user_id, origin_name, origin_lat, origin_lng, transport_type, est_travel_minutes, travel_source, prepare_minutes, buffer_minutes, prepare_advance_minutes"

# Target = { appointment, participant, alarms } · DayRow = { appointment, participant, alarms } (모두 dict)
Target = dict[str, Any]
DayRow = dict[str, Any]


def _range(value: dict[str, datetime]) -> tuple[datetime, datetime]:
    """local_day_range의 {start, end}"""
    return value["start"], value["end"]


def _warning(w: dict[str, Any]) -> EditWarning:
    """경고 하나를 응답용 dict로 — detail은 값이 있을 때만 (TS `...(w.detail ? { detail } : {})`)"""
    out: EditWarning = {"code": w["code"], "message": w["message"]}
    if w.get("detail"):
        out["detail"] = w["detail"]
    return out


def _common(appointment_id: str) -> EditResponse:
    return {"appointment_id": appointment_id, "ring_packages": [], "canceled_alarm_ids": [], "warnings": []}


def _wake_of(alarms: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    return next((a for a in alarms if a["alarm_type"] == "WAKE"), None)


def _ids(rows: Sequence[dict[str, Any]]) -> list[str]:
    return [a["alarm_id"] for a in rows]


def _unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _at(value: str) -> datetime:
    return parse_iso(value)


def _ms_of(value: str) -> int:
    return ms(parse_iso(value))


async def _load_target(ctx: EditCtx, appointment_id: str) -> Target | None:
    appointment = must(
        "appointments",
        await ctx.user_db.from_("appointments")
        .select(APPOINTMENT_COLUMNS)
        .eq("appointment_id", appointment_id)
        .maybe_single(),
    )
    if not appointment:
        return None  # 없거나 내게 안 보임 (RLS) — 구분하지 않음
    participant = await load_participant(ctx.user_db, appointment_id, ctx.user_id)
    alarms = await load_alarms(ctx.user_db, participant["participant_id"], ctx.user_id) if participant else []
    return {"appointment": appointment, "participant": participant, "alarms": alarms}


class Env:
    """계산에 쓰는 사용자 값 (프로필 · 준비시간 통계 · 지각 횟수) — 한 요청에서 처음 쓸 때 한 번 읽음"""

    def __init__(self, ctx: EditCtx) -> None:
        self.ctx = ctx
        self._cache: dict[str, asyncio.Future[Any]] = {}

    def _memo(self, key: str, load: Callable[[], Awaitable[Any]]) -> asyncio.Future[Any]:
        hit = self._cache.get(key)
        if hit is None:
            hit = asyncio.ensure_future(load())
            self._cache[key] = hit
        return hit

    def profile(self) -> asyncio.Future[Any]:
        return self._memo("profile", lambda: load_profile(self.ctx.user_db, self.ctx.user_id))

    async def tz(self) -> str:
        return safe_time_zone((await self.profile())["timezone"])

    def stats(self) -> asyncio.Future[Any]:
        return self._memo("stats", lambda: load_prepare_stats(self.ctx.user_db, self.ctx.user_id))

    def late(self) -> asyncio.Future[Any]:
        return self._memo("late", lambda: load_late_count(self.ctx.user_db, self.ctx.user_id, self.ctx.now))


async def _read_day(ctx: EditCtx, tz: str, instant: datetime) -> list[DayRow]:
    """그날(내 시간대 0시 기준) 내 참여 약속 — 약속 + 내 참여 조건 + 내 알람. 이번만 지운(CANCELED) 회차는 알람이 없고 숨김이라 뺌"""
    start, end = _range(local_day_range(instant, tz))
    joined = must(
        "day participants",
        await ctx.user_db.from_("appointment_participants")
        .select(f"{PARTICIPANT_COLUMNS}, appointments!inner({APPOINTMENT_COLUMNS})")
        .eq("user_id", ctx.user_id)
        .gte("appointments.appointment_at", to_iso(start))
        .lt("appointments.appointment_at", to_iso(end)),
    )
    rows: list[dict[str, Any]] = []
    for j in joined:
        embedded = j.get("appointments")
        appointment = embedded[0] if isinstance(embedded, list) and embedded else embedded
        if (
            not appointment
            or isinstance(appointment, list)
            or appointment.get("exception_kind") == "CANCELED"
        ):
            continue
        participant = {k: v for k, v in j.items() if k != "appointments"}
        rows.append({"appointment": appointment, "participant": participant})
    if not rows:
        return []
    alarms = must(
        "day alarms",
        await ctx.user_db.from_("alarms")
        .select(ALARM_COLUMNS)
        .eq("user_id", ctx.user_id)
        .in_("participant_id", [r["participant"]["participant_id"] for r in rows]),
    )
    return [
        {**r, "alarms": [a for a in alarms if a["participant_id"] == r["participant"]["participant_id"]]}
        for r in rows
    ]


def _day_wake_row(r: DayRow) -> DayWakeRow:
    wake = _wake_of(r["alarms"])
    return DayWakeRow(
        id=r["appointment"]["appointment_id"],
        at=_at(r["appointment"]["appointment_at"]),
        wake_enabled=wake["is_enabled"] if wake is not None else None,
    )


@dataclass
class Snapshot:
    days: dict[str, list[DayRow]]
    instants: dict[str, datetime]


async def _snapshot_days(ctx: EditCtx, env: Env, instants: Sequence[datetime]) -> Snapshot:
    """바꾸기 전의 날들을 읽어 둠 — 7.1.3 재판정이 "전에는 앞 기상에 밀려 있었나"를 이것으로 가림. 키 = 내 시간대 날짜"""
    tz = await env.tz()
    by_key: dict[str, datetime] = {}
    for at in instants:
        by_key[local_date_key(at, tz)] = at
    days: dict[str, list[DayRow]] = {}
    for key, at in by_key.items():
        days[key] = await _read_day(ctx, tz, at)
    return Snapshot(days=days, instants=by_key)


async def _record_ids_of(ctx: EditCtx, appointment_ids: Sequence[str]) -> set[str]:
    """이 회차들 중 내 출발 · 도착 기록이 있는 것 (지난 회차는 기록으로 남김)"""
    if not appointment_ids:
        return set()
    out: set[str] = set()
    for column in ("departed_at", "arrived_at"):
        rows = must(
            "records",
            await ctx.user_db.from_("appointment_participants")
            .select("appointment_id")
            .eq("user_id", ctx.user_id)
            .in_("appointment_id", list(appointment_ids))
            .not_(column, "is", None),
        )
        for r in rows:
            out.add(r["appointment_id"])
    return out


async def _shared_record_exists(ctx: EditCtx, appointment_id: str) -> bool:
    """공동 약속은 다른 참여자의 기록도 확인 (RLS로는 내 참여만 보여서 service로)"""
    for column in ("departed_at", "arrived_at"):
        rows = must(
            "shared records",
            await ctx.service.from_("appointment_participants")
            .select("participant_id")
            .eq("appointment_id", appointment_id)
            .not_(column, "is", None),
        )
        if rows:
            return True
    return False


async def _series_rows(ctx: EditCtx, row: dict[str, Any]) -> list[SeriesRow]:
    return must(
        "series rows",
        await ctx.user_db.from_("appointments")
        .select("appointment_id, occurrence_on, appointment_at, exception_kind")
        .eq("creator_id", ctx.user_id)
        .eq("series_id", row["series_id"]),
    )


# ---------------------------------------------------------------------------
# 쓰기 도우미
# ---------------------------------------------------------------------------


async def _apply_alarm_diff(
    ctx: EditCtx, participant_id: str, diff: Any, existing: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """알람 고치기 → 만들기 → 지우기 순 (commit.persistAppointment와 같음 — 중간에 실패해도 있던 알람이 먼저 사라지지 않게). 돌려주는 값 = 고친 뒤 알람 전체"""
    update = diff.update
    insert = diff.insert
    remove = diff.remove
    touched: list[dict[str, Any]] = []
    for u in update:
        touched.append(
            must(
                "alarms update",
                await ctx.user_db.from_("alarms")
                .update(u["patch"])
                .eq("alarm_id", u["alarm_id"])
                .eq("user_id", ctx.user_id)
                .select(ALARM_COLUMNS)
                .single(),
            )
        )
    if insert:
        rows = [{**w, "user_id": ctx.user_id, "participant_id": participant_id} for w in insert]
        touched.extend(
            must("alarms insert", await ctx.user_db.from_("alarms").insert(rows).select(ALARM_COLUMNS))
        )
    if remove:
        must(
            "alarms delete",
            await ctx.user_db.from_("alarms").delete().in_("alarm_id", remove).eq("user_id", ctx.user_id),
        )
    updated_ids = {u["alarm_id"] for u in update}
    return [
        *touched,
        *[a for a in existing if a["alarm_id"] not in remove and a["alarm_id"] not in updated_ids],
    ]


_ME: Any = object()  # "내 id가 기본값" 표시 (None은 만든 사람이 빈 약속을 뜻함)


async def _update_row(
    ctx: EditCtx, appointment_id: str, patch: dict[str, Any], creator_id: Any = _ME
) -> None:
    """약속 행 고치기 — 내가 만든 행 하나가 정확히 고쳐져야 함"""
    if creator_id is _ME:
        creator_id = ctx.user_id
    query = ctx.user_db.from_("appointments").update(patch).eq("appointment_id", appointment_id)
    query = query.is_("creator_id", None) if creator_id is None else query.eq("creator_id", creator_id)
    done = must("appointments update", await query.select("appointment_id"))
    if len(done) != 1:
        raise AppError("NOT_FOUND", "일정을 찾을 수 없어요")


async def _assert_group_member(ctx: EditCtx, row: dict[str, Any]) -> None:
    """공동 약속을 고치거나 지우려면 지금 그 그룹에 참여 중이어야 함. 나갔거나 내보내진 만든 사람은 만든 사람 경로로 약속이 아직 보이고 editor_verdict도 통과하는데,
    약속 행 쓰기는 RLS가 막으므로(is_group_member) 그 전에 service로 다른 참여자의 알람을 먼저 바꾸는 일이 없게 쓰기 전에 확인함 (9장 PR 리뷰)"""
    if row.get("group_id") is None:
        return
    mine = must(
        "group member",
        await ctx.user_db.from_("group_members")
        .select("user_id")
        .eq("group_id", row["group_id"])
        .eq("user_id", ctx.user_id)
        .eq("invite_status", "JOINED")
        .limit(1),
    )
    if not mine:
        raise AppError("NOT_FOUND", "일정을 찾을 수 없어요")


async def _delete_rows(ctx: EditCtx, appointment_ids: Sequence[str], creator_id: Any = _ME) -> list[str]:
    """약속 행들을 지움 (참여 조건 · 알람은 CASCADE). 돌려주는 값 = 지워지는 내 알람 id (폰에서 지울 예약)

    creator_id = 지울 행의 만든 사람 — 내가 만든 약속은 내 id, 만든 사람이 비어 있는(탈퇴한) 공동 약속은 None (참여자가 지움, DB 정책 appointments_delete_creator가 참여 중인 그룹원에게만 열어 둠)
    """
    if creator_id is _ME:
        creator_id = ctx.user_id
    if not appointment_ids:
        return []
    id_list = list(appointment_ids)
    participants = must(
        "participants",
        await ctx.user_db.from_("appointment_participants")
        .select("participant_id")
        .eq("user_id", ctx.user_id)
        .in_("appointment_id", id_list),
    )
    alarm_ids = (
        _ids(
            must(
                "alarms",
                await ctx.user_db.from_("alarms")
                .select("alarm_id")
                .eq("user_id", ctx.user_id)
                .in_("participant_id", [p["participant_id"] for p in participants]),
            )
        )
        if participants
        else []
    )
    query = ctx.user_db.from_("appointments").delete().in_("appointment_id", id_list)
    if creator_id is None:
        # 만든 사람이 빈 약속은 RLS 정책(참여자 · 안 지난 약속)만이 조건을 걸어 막혀도 오류 없이 0행 — 지운 줄 알고 성공으로 돌려주지 않게 지운 행 수를 확인
        gone = must("appointments delete", await query.is_("creator_id", None).select("appointment_id"))
        if len(gone) != len(id_list):
            raise AppError("FORBIDDEN", "이 약속은 지울 수 없어요")
    else:
        must("appointments delete", await query.eq("creator_id", creator_id))
    return alarm_ids


def _settle(result: EditResponse, canceled: Sequence[str]) -> EditResponse:
    """응답 마무리 — 폰에 걸 예약(ring_packages)과 지울 예약(canceled_alarm_ids)이 겹치지 않게: 같은 알람이 여러 번 실렸으면(고친 일정이 7.1.3 재판정으로 한 번 더 계산됨) 마지막 것만,
    지워진 알람은 걸 예약에서 뺌. 시각 순으로 정렬"""
    gone = set(canceled)
    last: dict[str, dict[str, Any]] = {}
    for p in result["ring_packages"]:
        last[p["alarmId"]] = p
    result["ring_packages"] = sorted(
        (p for p in last.values() if p["alarmId"] not in gone), key=lambda p: p["atMillis"]
    )
    result["canceled_alarm_ids"] = list(dict.fromkeys(canceled))  # JS `[...new Set(canceled)]`
    return result


def _stored_travel(p: dict[str, Any]) -> Any:
    if p.get("est_travel_minutes") is None:
        return None
    return TravelChoice(
        minutes=p["est_travel_minutes"],
        source=p["travel_source"],
        needs_confirmation=False,
        note="저장된 이동시간",
    )


def _packages_of(
    *,
    alarms: Sequence[dict[str, Any]],
    appointment: dict[str, Any],
    at: datetime,
    tz: str,
    travel_minutes: Any,
    depart_at: datetime | None = None,
) -> list[dict[str, Any]]:
    return build_ring_packages(
        alarms=alarms,
        title=appointment["title"],
        subtitle=subtitle_for(at, appointment["dest_name"], tz),
        depart_at=depart_at,
        appointment=AppointmentNotice(appointment_at=at, travel_minutes=travel_minutes, time_zone=tz),
    )


# ---------------------------------------------------------------------------
# 7.1.3 재판정 — 앞 일정이 바뀌거나 지워지면 같은 날 다음 일정의 기상 알람을 다시 정함
# ---------------------------------------------------------------------------


async def _reevaluate_wake(ctx: EditCtx, env: Env, snapshot: Snapshot) -> dict[str, Any]:
    """고친 뒤(또는 지운 뒤) 영향받은 날들을 다시 읽어, 그날 약속을 시각 순으로 보며 기상이 붙고 떨어져야 하는 일정만 다시 계산해 씀.

    고친 일정 자신도 같이 봄 — 그날 다른 일정의 기상이 바뀌면 고친 일정의 기상도 달라질 수 있어서(예: 뒤로 옮겼더니 옛 첫 일정이 다시 첫 일정). 판정은 edit.wake_action → calc/wake has_earlier_wake (alarm-commit · 앱 목업과 같은 판정)
    이동시간은 저장된 값을 그대로 (길찾기를 새로 안 부름). 다시 계산이 안 되면(출발 시각 지남 · 이동시간 없음) 그 일정은 그대로 두고 경고만
    """
    out: dict[str, Any] = {"ring_packages": [], "canceled_alarm_ids": [], "warnings": []}
    tz = await env.tz()
    for key, instant in snapshot.instants.items():
        before_wakes = wakes_of_rows([_day_wake_row(r) for r in snapshot.days.get(key, [])])
        after = sorted(
            await _read_day(ctx, tz, instant),
            key=cmp_to_key(lambda a, b: by_time_then_id(_day_wake_row(a), _day_wake_row(b))),
        )
        sim: list[SameDayWake] = []
        for row in after:
            row_id = row["appointment"]["appointment_id"]
            at = _at(row["appointment"]["appointment_at"])
            wake_row = _wake_of(row["alarms"])
            final_wake: bool | None = wake_row["is_enabled"] if wake_row is not None else None
            has_ring = any(a["alarm_type"] != "ROUTINE" for a in row["alarms"])
            if has_ring:
                # 울릴 시각이 이미 지난 기상 알람은 REMOVE 대상에서 뺌 — 울린 기록이 학습 · 미루기 근거라 알람 행을 지우지 않음 (edit.wake_action)
                wake = _wake_of(row["alarms"])
                wake_rung = wake is not None and _ms_of(wake["scheduled_at"]) <= ms(ctx.now)
                verdict = wake_action(
                    id=row_id,
                    at=at,
                    has_wake=wake is not None,
                    wake_rung=wake_rung,
                    before_wakes=before_wakes,
                    sim=sim,
                )
                if verdict["action"] != "NONE":
                    done = await _replan_for_wake(ctx, env, row, verdict["earlierAfter"])
                    if done["ok"]:
                        out["ring_packages"].extend(done["ring_packages"])
                        out["canceled_alarm_ids"].extend(done["canceled_alarm_ids"])
                        after_wake = _wake_of(done["after"])
                        final_wake = after_wake["is_enabled"] if after_wake is not None else None
                    else:
                        out["warnings"].append(
                            {
                                "code": "WAKE_REEVAL_SKIPPED",
                                "message": "같은 날 다른 일정의 기상 알람을 다시 정하지 못했어요",
                                "detail": done["reason"],
                            }
                        )
            if final_wake is not None:
                sim.append(SameDayWake(appointmentId=row_id, appointmentAt=at, isEnabled=final_wake))
    out["canceled_alarm_ids"] = _unique(out["canceled_alarm_ids"])
    return out


async def _replan_for_wake(ctx: EditCtx, env: Env, row: DayRow, earlier_after: bool) -> dict[str, Any]:
    travel = _stored_travel(row["participant"])
    if not travel:
        return {"ok": False, "reason": "NO_TRAVEL"}
    profile, stats, late_count_4w = await asyncio.gather(env.profile(), env.stats(), env.late())
    tz = safe_time_zone(profile["timezone"])
    inputs = PlanInputs(
        now=ctx.now,
        # 기상이 붙는 쪽(ADD)은 "밀려서 없던" 일정이므로 기상을 더해 줌, 떨어지는 쪽(REMOVE)은 기존 종류 그대로 두고 기상만 빠짐 (inherited_kinds)
        draft=PlanDraft(
            kinds=inherited_kinds(row["alarms"], with_wake=not earlier_after, base_had_earlier_wake=True)
        ),
        profile=profile,
        appointment=row["appointment"],
        participant=row["participant"],
        existing_alarms=row["alarms"],
        stats=stats,
        late_count4w=late_count_4w,
        same_day_wake=earlier_after,
    )
    plan = compute_plan(inputs, resolved_travel_of(travel), "save")
    check = plan.check
    if not check["ok"]:
        return {"ok": False, "reason": check["reason"]}
    diff = diff_alarms(plan, row["alarms"], now=ctx.now, replace_existing=True)
    must(
        "appointment_participants update",
        await ctx.user_db.from_("appointment_participants")
        .update({"prepare_minutes": plan.prepare["minutes"]})
        .eq("participant_id", row["participant"]["participant_id"])
        .eq("user_id", ctx.user_id),
    )
    after = await _apply_alarm_diff(ctx, row["participant"]["participant_id"], diff, row["alarms"])
    return {
        "ok": True,
        "after": after,
        "ring_packages": _packages_of(
            alarms=after,
            appointment=row["appointment"],
            at=_at(row["appointment"]["appointment_at"]),
            tz=tz,
            travel_minutes=plan.travel_minutes,
            depart_at=plan.departure,
        ),
        "canceled_alarm_ids": canceled_alarm_ids(before=row["alarms"], after=after),
    }


# ---------------------------------------------------------------------------
# 고치기
# ---------------------------------------------------------------------------


def _assert_editor(target: Target | None, ctx: EditCtx, verb: str) -> None:
    verdict = editor_verdict(
        row=target["appointment"] if target else None,
        user_id=ctx.user_id,
        participating=target is not None and target["participant"] is not None,
    )
    if verdict == "NOT_VISIBLE":
        raise AppError("NOT_FOUND", "일정을 찾을 수 없어요")
    if verdict == "NOT_CREATOR":
        # L6 안건후보: 공동 약속을 참여만 한 사람이 고치기 · 지우기를 하면 기본안 = 거절 (만든 사람만). 참여자는 "나는 빠지기"(LEAVE)
        raise AppError("FORBIDDEN", f"공동 약속은 만든 사람만 {verb} 수 있어요. 나는 빠지기를 써 주세요")


async def _replan_for(
    ctx: EditCtx,
    env: Env,
    *,
    row: dict[str, Any],
    patched: dict[str, Any],
    participant: dict[str, Any],
    alarms: list[dict[str, Any]],
    snapshot: Snapshot,
    excluded: set[str],
    old_at: datetime,
    new_at: datetime,
    tz: str,
) -> dict[str, Any]:
    """고친 약속으로 한 참여자의 알람을 다시 계산함 (쓰지는 않음). 부른 사람(만든 사람)과 공동 약속의 다른 참여자가 같은 계산을 씀 — 다른 참여자는 ctx를 그 사람 몫(user_id · user_db)으로 바꿔서 부름
    장소가 바뀌면 길찾기를 새로 부르고 안 되면 ROUTE_UNAVAILABLE을 던짐(호출한 쪽이 처리)"""
    warnings: list[EditWarning] = []
    patched_row = {**row, **patched["next"], "recurrence_rule": row["recurrence_rule"]}
    new_day = snapshot.days.get(local_date_key(new_at, tz), [])
    old_day = snapshot.days.get(local_date_key(old_at, tz), [])
    same_day_wake = has_earlier_wake(
        new_at,
        wakes_of_rows(
            [_day_wake_row(r) for r in new_day if r["appointment"]["appointment_id"] not in excluded]
        ),
        row["appointment_id"],
    )
    # 기상이 없던 이유가 "앞 기상에 밀려서"만이 아님 — 저녁 일정은 계산된 기상이 낮 12시 이후라 기상을 안 만듦(plan_core, #320). 저녁 일정을 아침으로 옮기면 기상이 붙어야 하므로 같이 "밀려서 없던 것"으로 봄
    #   L6 안건후보: 기상 없는 일정을 고칠 때 기본안 = 옛 시각의 **계산된** 기상(옛 이동 · 여유 + 기상 있는 첫 준비값)이 낮 12시 이후였으면 규칙 때문에 없던 것이라 다시 계산하고,
    #   12시 전이었는데 기상이 없으면 사용자가 일부러 끈 것이라 그대로 둠 (헤드 확정 10/30 — 13:00 일정에서 끈 기상이 장소만 고쳐도 되살아나던 것, 리뷰 F1)
    old_first_prepare = first_prepare_minutes(await env.profile())
    buffer = participant.get("buffer_minutes")
    travel_min = participant.get("est_travel_minutes")
    old_wake_at = from_ms(
        ms(old_at)
        - (
            (buffer if buffer is not None else 0)
            + (travel_min if travel_min is not None else 0)
            + old_first_prepare
        )
        * 60_000
    )
    had_no_wake_in_evening = (
        any(x["alarm_type"] in ("PREPARE", "DEPARTURE") for x in alarms)
        and not any(x["alarm_type"] == "WAKE" for x in alarms)
        and is_evening_wake(old_wake_at, tz, old_at)
    )
    had_earlier_wake = (
        has_earlier_wake(old_at, wakes_of_rows([_day_wake_row(r) for r in old_day]), row["appointment_id"])
        or had_no_wake_in_evening
    )

    travelled = await _travel_for(
        ctx, env, row=patched_row, participant=participant, place_changed=patched["changed"]["place"]
    )
    warnings.extend(travelled["warnings"])

    profile, stats, late_count_4w = await asyncio.gather(env.profile(), env.stats(), env.late())
    # 친구가 제안해 주인이 적용한 시각(original_scheduled_at 있음)은 약속의 시각 · 장소를 바꾸면 새 계산이 이김 — 친구가 바꾸기 전 모습으로 되돌린 알람을 새 계산의 입력(직접 고른 시각 · 기존 알람 간격)으로 씀.
    #   시각 · 장소가 그대로면(제목 · 중요도만) 이 되돌림을 안 해서 친구 값이 그대로 남음. 변경 비교(diff_alarms)에는 실제 행을 넘김
    plan_alarms = (
        without_friend_times(alarms) if patched["changed"]["at"] or patched["changed"]["place"] else alarms
    )
    inputs = PlanInputs(
        now=ctx.now,
        draft=PlanDraft(
            # 그날 첫 일정이 바뀌면 기상이 붙고 떨어짐 (7.1.3). 직접 고른 알람 시각은 같은 날이면 그대로 · 날짜가 바뀌면 날짜만 따라감
            #   L6 안건후보: 일정 시각을 옮길 때 직접 고른 알람 시각 기본안 = 같은 날이면 유지 · 날짜가 바뀌면 날짜만 따라감 (앱 가짜 모드와 같음)
            kinds=inherited_kinds(
                alarms, with_wake=not same_day_wake, base_had_earlier_wake=had_earlier_wake
            ),
            #   친구가 제안해서 주인이 적용한 시각(original_scheduled_at 있음)은 약속의 시각 · 장소를 바꾸면 새 계산이 이김 — 시각 · 장소가 그대로면(제목 · 중요도만) 이 길이 아예 안 돎 (친구 10-22, D67)
            manual_times=shift_manual_times(plan_alarms, old_at, local_date_key(new_at, tz), tz),
        ),
        profile=profile,
        appointment=patched_row,
        participant=participant,
        existing_alarms=plan_alarms,
        stats=stats,
        late_count4w=late_count_4w,
        same_day_wake=same_day_wake,
    )
    plan = compute_plan(inputs, resolved_travel_of(travelled["travel"]), "save")
    check = plan.check
    if not check["ok"]:
        detail = check.get("detail") if isinstance(check, Mapping) else getattr(check, "detail", None)
        raise AppError(
            "UNPROCESSABLE",
            check["message"],
            {"reason": check["reason"], **(detail or {})},
        )
    diff = diff_alarms(plan, alarms, now=ctx.now, replace_existing=True)
    changes = alarm_changes(alarms, [*[u["patch"] for u in diff.update], *diff.insert])
    warnings.extend(_warning(w) for w in plan.warnings if w["code"] == "PAST_ALARM_DISABLED")
    return {"plan": plan, "diff": diff, "changes": changes, "warnings": warnings}


# 공동 약속의 시각 · 장소를 만든 사람이 고쳤을 때 다른 참여자들의 알람을 다시 계산해 씀 — 각자의 출발지 · 수단 · 준비 시간으로 (9-14).
#   앞당김과 5분 미만 늦춤은 바로 쓰고, 5분 이상 늦춰지면 그 사람이 확인할 때까지 보류(D67, 9-11 · 9-12 — 그 사람 앱이 푸시의 needsConfirm을 보고 alarm-commit 미리 보기로 다시 계산해 묻고 저장)
#   이미 울린 알람은 안 건드림(9-16). 계산을 못 한 사람(길찾기 안 됨 · 계산 불가)은 약속 시각이 안 바뀌었거나 5분 이상 늦춰지면 기존 알람을 그대로 두고 경고 · 확인 알림만(9-17), 앞당김 · 5분 미만 늦춤이면 알람을 약속이 옮겨진 만큼 같이 옮김(약속 행을 쓴 뒤, X-APPT-03). 사람 이름은 응답에 넣지 않음
#   service 클라이언트로 읽고 쓰되 모든 조회를 그 참여자의 user_id로 좁힘(ctx를 그 사람 몫으로 바꿔서 같은 함수를 부름). DB 오류는 올리고(같은 요청을 다시 보내면 이어서 맞춰짐), 계산 못 하는 사람만 건너뜀
# 한 참여자를 맞춘 결과 SyncOutcome (dict: userId · kind · warning? · confirm? · shift?). kind: UPDATED = 바로 반영 · HELD = 5분 이상 늦춰져 확인 전 보류 · SKIPPED = 맞출 알람이 없음(다 울림) ·
#   NOT_UPDATED = 계산을 못 해 알람을 그대로 둠 · SHIFT = 계산을 못 해 약속이 옮겨진 만큼 알람을 같이 옮길 예정(약속 행을 쓴 뒤 shift()로 씀 — 못 쓰면 NOT_UPDATED처럼 알람 그대로 + 경고)

ROUTE_FAILED: EditWarning = {
    "code": "PARTICIPANT_ROUTE_FAILED",
    "message": "참여자 한 명의 이동시간을 구하지 못해 그 사람의 알람을 그대로 뒀어요. 이동 시간 확인이 필요해요",
}

ROUTE_FAILED_SHIFTED: EditWarning = {
    "code": "PARTICIPANT_ROUTE_FAILED",
    "message": "참여자 한 명의 이동시간을 새로 구하지 못해 지난번 이동 · 준비 간격 그대로 알람을 약속이 옮겨진 만큼 같이 옮겼어요. 그 사람의 확인이 필요해요",
    "detail": "SHIFTED",
}
SHIFT_KINDS: tuple[str, ...] = ("WAKE", "PREPARE", "DEPARTURE", "ARRIVE")


def _plan_shift(
    ctx: EditCtx,
    p: dict[str, Any],
    loaded: dict[str, Any],
    *,
    old_at: datetime,
    new_at: datetime,
    at_changed: bool,
) -> Callable[[], Awaitable[None]] | None:
    """알람을 새로 계산하지 못한 참여자(길찾기 실패 · 계산 불가)의 안 울린 알람을 약속이 옮겨진 만큼 그대로 같이 옮길 계획을 세움 — 지난번 이동 · 준비 간격을 쓰는 셈.

    약속을 앞당겼는데 알람이 새 약속 뒤에 그대로 남아 지각하는 일을 막음 (탐색 시험 X-APPT-03). 옮긴 시각이 이미 지난 알람은 끄고, 꺼 둔 알람은 꺼 둔 채로 같이 옮겨 순서가 안 뒤집히게 함
    약속 시각이 안 바뀌었거나(장소만 바뀜), 5분 이상 늦춰져 확인이 먼저인 경우는 옮기지 않음(None) — 기존대로 알람을 두고 경고 · 확인 알림만 (종류와 상관없이 약속이 늦춰진 만큼으로 정함)
    옮기는 쓰기는 돌려주는 함수를 부를 때 함 — 약속 행을 쓴 뒤에 부름. 시각을 상대로 옮기는 일이라 약속 행 쓰기 앞에서 하면 요청이 실패해 다시 보낼 때 한 번 더 옮겨지기 때문 (약속 행이 새 시각이 되면 다시 보내도 시각이 안 바뀐 요청이라 옮기지 않음)
    """
    delta_ms = ms(new_at) - ms(old_at)
    if not at_changed or delta_ms == 0 or delta_ms >= CONFIRM_MOVE_MINUTES * 60_000:
        return None
    movable = [
        x for x in loaded["alarms"] if x["alarm_id"] not in loaded["rung"] and x["alarm_type"] in SHIFT_KINDS
    ]
    if not any(x["is_enabled"] for x in movable):
        return None
    now_iso = to_iso(ctx.now)
    update = []
    for x in movable:
        at = from_ms(_ms_of(x["scheduled_at"]) + delta_ms)
        patch = {
            "alarm_type": x["alarm_type"],
            "scheduled_at": to_iso(at),
            "intensity": x["intensity"],
            "voice_tone": x["voice_tone"],
            "sound_key": x[
                "sound_key"
            ],  # DB 칸은 null 허용 — 쓰기 모양이 string이라 좁혀 놓았을 뿐 값은 그대로
            "vibration": x["vibration"],
            "is_enabled": x["is_enabled"] and ms(at) > ms(ctx.now),
            "time_source": x["time_source"],
            "intensity_source": x["intensity_source"],
            "last_calculated_at": now_iso,
        }
        update.append({"alarm_id": x["alarm_id"], "patch": patch})

    async def shift() -> None:
        await _apply_alarm_diff(
            ctx, p["participant_id"], AlarmDiff(insert=[], update=update, remove=[]), loaded["alarms"]
        )

    return shift


# 참여자를 몇 명씩 한꺼번에 맞출지 — 참여자 한 명마다 DB 약 12번 + 길찾기 1번이라 20명을 하나씩 하면 길찾기가 느린 순간 앱 제한(30초)을 넘음
SYNC_CONCURRENCY = 4


async def _sync_one(
    ctx: EditCtx,
    a: dict[str, Any],
    p: dict[str, Any],
) -> dict[str, Any]:
    row, patched, old_at, new_at = a["row"], a["patched"], a["old_at"], a["new_at"]
    pctx = replace(ctx, user_id=p["user_id"], user_db=ctx.service)
    loaded: dict[str, Any] | None = None

    def shifted() -> dict[str, Any] | None:
        shift = (
            _plan_shift(pctx, p, loaded, old_at=old_at, new_at=new_at, at_changed=patched["changed"]["at"])
            if loaded
            else None
        )
        if shift:
            return {
                "userId": p["user_id"],
                "kind": "SHIFT",
                "warning": ROUTE_FAILED_SHIFTED,
                "confirm": True,
                "shift": shift,
            }
        return None

    try:
        alarms = await load_alarms(ctx.service, p["participant_id"], p["user_id"])
        rung = {
            x["alarm_id"] for x in alarms if _ms_of(x["scheduled_at"]) <= ms(ctx.now)
        }  # 이미 울린 알람은 안 건드림
        loaded = {"alarms": alarms, "rung": rung}
        if all(x["alarm_id"] in rung for x in alarms):
            return {"userId": p["user_id"], "kind": "SKIPPED"}
        penv = Env(pctx)
        tz = await penv.tz()
        snapshot = await _snapshot_days(pctx, penv, [old_at, new_at])
        made = await _replan_for(
            pctx,
            penv,
            row=row,
            patched=patched,
            participant=p,
            alarms=alarms,
            snapshot=snapshot,
            excluded={row["appointment_id"]},
            old_at=old_at,
            new_at=new_at,
            tz=tz,
        )
        # 장소가 바뀌었는데 길찾기가 안 돼 근사값 · 내 이동 기록뿐이면 그 사람의 기존 알람을 그대로 두고 '이동 시간 확인 필요' 경고만 (9-17)
        chosen = made["plan"].travel.chosen
        source = chosen.source if chosen is not None else None
        #   약속 시각도 바뀌었으면 지난번 이동 간격 그대로 알람을 같이 옮김 (X-APPT-03, 쓰기는 약속 행을 쓴 뒤)
        if patched["changed"]["place"] and source in ("ESTIMATE", "CACHED"):
            return shifted() or {"userId": p["user_id"], "kind": "NOT_UPDATED", "warning": ROUTE_FAILED}
        mdiff = made["diff"]
        diff = AlarmDiff(
            insert=mdiff.insert,
            update=[u for u in mdiff.update if u["alarm_id"] not in rung],
            remove=[i for i in mdiff.remove if i not in rung],
        )
        changes = alarm_changes(
            [x for x in alarms if x["alarm_id"] not in rung],
            [*[u["patch"] for u in diff.update], *diff.insert],
        )
        if needs_confirm(changes):
            return {"userId": p["user_id"], "kind": "HELD"}
        must(
            "appointment_participants update",
            await ctx.service.from_("appointment_participants")
            .update(
                {
                    "est_travel_minutes": made["plan"].travel_minutes,
                    "travel_source": source,
                    "prepare_minutes": made["plan"].prepare["minutes"],
                }
            )
            .eq("participant_id", p["participant_id"])
            .eq("user_id", p["user_id"]),
        )
        await _apply_alarm_diff(pctx, p["participant_id"], diff, alarms)
        return {"userId": p["user_id"], "kind": "UPDATED"}
    except Exception as e:
        # 계산을 못 하는 사람(길찾기 장애 · 계산 결과가 맞지 않음)은 알람을 같이 옮기거나(shifted) 그대로 두고 경고. DB 오류 같은 나머지는 요청 전체를 실패로 올림 —
        #   이 시점에 약속 행은 아직 안 바뀌어 있어 같은 요청을 다시 보내면 이어서 맞춰짐 (이미 맞춘 사람은 바뀔 것이 없음 — 같이 옮기는 쓰기는 약속 행 뒤에 해서 이 약속을 깨지 않음)
        if not isinstance(e, AppError) or e.code not in ("ROUTE_UNAVAILABLE", "UNPROCESSABLE"):
            raise
        moved = shifted()  # 약속 시각이 바뀌었으면 알람을 같이 옮김 — 약속을 앞당겼는데 알람이 새 약속 뒤에 남지 않게 (X-APPT-03)
        if moved:
            return moved
        return {
            "userId": p["user_id"],
            "kind": "NOT_UPDATED",
            "warning": ROUTE_FAILED
            if e.code == "ROUTE_UNAVAILABLE"
            else {
                "code": "PARTICIPANT_NOT_UPDATED",
                "message": "참여자 한 명의 알람을 다시 계산하지 못해 그대로 뒀어요",
                "detail": e.code,
            },
        }


async def _sync_other_participants(ctx: EditCtx, a: dict[str, Any]) -> dict[str, Any]:
    """held = 푸시에 needsConfirm: true를 실을 사람 — 서버가 아직 맞추지 못했거나 정확히 계산하지 못한 사람(보류 · 계산 못 함 · 지난번 간격으로 옮김). 그 사람 폰이 미리 보기로 다시 계산해 묻고 저장함
    finish() = 약속 행을 쓴 뒤 부르는 마무리 — 계산을 못 해 같이 옮기기로 한 사람의 알람을 쓰고, 쓰기가 안 되면(DB 오류) 그 사람은 알람 그대로 + 경고 · 확인 알림으로 내림 (이미 약속이 바뀐 뒤라 요청을 실패로 돌리지 않음)"""
    row = a["row"]
    summary = {"updated": 0, "held": 0, "skipped": 0}
    held: set[str] = set()
    warnings: list[EditWarning] = []
    members = must(
        "group members",
        await ctx.service.from_("group_members")
        .select("user_id")
        .eq("group_id", row["group_id"])
        .eq("invite_status", "JOINED"),
    )
    joined = {m["user_id"] for m in members}
    others = [
        p
        for p in must(
            "participants",
            await ctx.service.from_("appointment_participants")
            .select(PARTICIPANT_COLUMNS)
            .eq("appointment_id", row["appointment_id"])
            .neq("user_id", ctx.user_id),
        )
        if p["user_id"] in joined
    ]

    outcomes: list[dict[str, Any]] = []
    for i in range(0, len(others), SYNC_CONCURRENCY):
        # return_exceptions: 한 명이 실패해도 같은 묶음의 쓰기가 끝난 뒤에 오류를 올림 (요청이 끝난 뒤에 남은 쓰기가 돌지 않게)
        settled = await asyncio.gather(
            *[_sync_one(ctx, a, p) for p in others[i : i + SYNC_CONCURRENCY]], return_exceptions=True
        )
        failed = next((r for r in settled if isinstance(r, BaseException)), None)
        if failed is not None:
            raise failed
        outcomes.extend(settled)  # type: ignore[arg-type]
    pending: list[dict[str, Any]] = []
    for o in outcomes:
        if o["kind"] == "UPDATED":
            summary["updated"] += 1
            if o.get("confirm"):
                held.add(o["userId"])
            if o.get("warning"):
                warnings.append(o["warning"])
        elif o["kind"] == "SHIFT":
            summary["updated"] += 1  # 같이 옮길 사람 — 쓰기는 finish()에서, 못 쓰면 거기서 skipped로 내림
            held.add(o["userId"])  # 지난번 간격으로 옮긴 사람 폰은 확인 화면을 띄움
            if o.get("warning"):
                warnings.append(o["warning"])
            pending.append(o)
        elif o["kind"] == "HELD":
            summary["held"] += 1
            held.add(o["userId"])
        else:
            summary["skipped"] += 1
            if o["kind"] == "NOT_UPDATED":
                held.add(o["userId"])
            if o.get("warning"):
                warnings.append(o["warning"])

    async def finish() -> None:
        for o in pending:
            try:
                await o["shift"]()
            except Exception:
                summary["updated"] -= 1
                summary["skipped"] += 1
                idx = next((k for k, w in enumerate(warnings) if w is o["warning"]), -1)
                if idx >= 0:
                    warnings[idx] = ROUTE_FAILED

    return {"summary": summary, "held": held, "warnings": warnings, "finish": finish}


async def _plan_group_push_safe(ctx: EditCtx, row: dict[str, Any]) -> dict[str, Any]:
    """공동 약속 푸시(GROUP_APPT_CHANGED) — 받는 사람은 DB 함수 plan_group_push가 정함(보낸 사람이 만든 사람이고 아직 안 지난 약속일 때만). 푸시가 안 가도 고치기 · 지우기는 이미 끝난 일이라 오류로 만들지 않고 경고만"""
    try:
        plan = await plan_group_push(
            ctx.service,
            ctx.user_id,
            {
                "kind": "GROUP_APPT_CHANGED",
                "groupId": row["group_id"],
                "appointmentId": row["appointment_id"],
            },
        )
        return {"plan": plan, "warnings": []}
    except Exception as e:
        log.error("[appointment-edit] 공동 약속 푸시 받는 사람 정하기 실패 %s", e)
        return {"warnings": [{"code": "PUSH_NOT_SENT", "message": "참여자에게 알림을 보내지 못했어요"}]}


async def _push_group_change(
    ctx: EditCtx,
    row: dict[str, Any],
    change: str,
    *,
    plan: Any = None,
    held: set[str] | None = None,
) -> dict[str, Any]:
    planned = {"plan": plan, "warnings": []} if plan else await _plan_group_push_safe(ctx, row)
    if not planned.get("plan"):
        return {"warnings": planned["warnings"]}
    req = {"kind": "GROUP_APPT_CHANGED", "groupId": row["group_id"], "appointmentId": row["appointment_id"]}
    try:
        per_recipient = (
            {
                rid: {"needsConfirm": rid in held}
                for rid in [*planned["plan"]["recipients"], *planned["plan"]["silentRecipients"]]
            }
            if held is not None
            else None
        )
        out = await send_group_push(
            ctx.service,
            req,
            planned["plan"],
            ctx.push if ctx.push is not None else NotifyOptions(),
            {"change": change, **({"perRecipient": per_recipient} if per_recipient is not None else {})},
        )
        reason = out.get("reason")
        sent = out["sent"]
        failed = reason and reason != "NO_TOKENS"
        return {
            "notified": sent,
            "warnings": [
                {"code": "PUSH_NOT_SENT", "message": "참여자에게 알림을 보내지 못했어요", "detail": reason}
            ]
            if failed
            else [],
        }
    except Exception as e:
        log.error("[appointment-edit] 공동 약속 푸시 실패 %s", e)
        return {"warnings": [{"code": "PUSH_NOT_SENT", "message": "참여자에게 알림을 보내지 못했어요"}]}


async def update_appointment(ctx: EditCtx, req: EditRequest) -> EditResponse:
    target = await _load_target(ctx, req["appointment_id"])
    _assert_editor(target, ctx, "고칠")
    assert target is not None
    await _assert_group_member(ctx, target["appointment"])
    row, participant, alarms = target["appointment"], target["participant"], target["alarms"]
    if row.get("exception_kind") == "CANCELED":
        raise AppError(
            "NOT_FOUND", "일정을 찾을 수 없어요"
        )  # 이번만 지운 회차 — 고치려면 되돌리기(RESTORE)가 먼저

    env = Env(ctx)
    group = row.get("group_id") is not None
    recurring = not group and row.get("recurrence_rule") is not None
    patch = req["patch"]
    patched = apply_patch(row, patch)
    effect = scope_effect(
        recurring=recurring,
        group=group,
        scope=req["scope"],
        patch_rule=patch.get("recurrence_rule", UNSET),
        current_rule=row.get("recurrence_rule"),
    )

    # 지난 공동 약속은 고칠 수 없고, 지난 날로 옮길 수도 없음 (9-32 · DB 정책 appointments_update와 같은 규칙). '지난'은 하루 경계 — 내 폰 시간대의 오늘 0시보다 앞 (오늘 이미 지난 약속은 오늘 안에는 고칠 수 있음, 9-러)
    today_start = ms(_range(local_day_range(ctx.now, await env.tz()))[0]) if group else 0
    if group and (
        _ms_of(row["appointment_at"]) < today_start
        or (patched["changed"]["at"] and _ms_of(patched["next"]["appointment_at"]) < today_start)
    ):
        raise AppError("UNPROCESSABLE", "지난 약속이에요", {"reason": "PAST_APPOINTMENT"})
    # 공동 약속의 반복은 회차마다 참여자 알람을 만드는 일이 따로 필요해 아직 못 고침 (9-머 · 9-버, 반복 공동 약속은 회차 만들기부터 이어서)
    if group and "recurrence_rule" in patch and patch["recurrence_rule"] != row.get("recurrence_rule"):
        raise AppError(
            "UNPROCESSABLE", "공동 약속의 반복은 아직 고칠 수 없어요", {"reason": "GROUP_RECURRENCE_LATER"}
        )

    result: EditResponse = {
        **_common(row["appointment_id"]),
        "saved": True,
        "needs_confirm": False,
        "changes": [],
        "alarms": [summarize(a) for a in alarms],
        "deleted_appointment_ids": [],
    }
    if not patched["any"] and effect["rule"]["kind"] == "KEEP":
        return result  # 바뀐 것이 없음 — 같은 요청을 두 번 보내면 두 번째가 여기

    tz = await env.tz()
    old_at = _at(row["appointment_at"])
    new_at = _at(patched["next"]["appointment_at"])

    # 뒤 회차 (이후 모두 고치기) — 지운 뒤 alarm-roll이 새 기준으로 다시 만듦
    later_ids: list[str] = []
    if effect["deleteLater"]:
        rows = await _series_rows(ctx, row)
        later_ids = later_rows_to_delete(
            rows=rows,
            target=row,
            now=ctx.now,
            record_ids=await _record_ids_of(ctx, [r["appointment_id"] for r in rows]),
            mode="EDIT",
        )

    # 바꾸기 전의 날들을 읽어 둠 — 알람을 다시 계산하거나(시각 · 장소 · 중요도) 7.1.3 재판정을 할 때만 필요
    need_snapshot = (
        (participant is not None and patched["affects_alarms"])
        or patched["changed"]["at"]
        or len(later_ids) > 0
    )
    snapshot = (
        await _snapshot_days(ctx, env, [old_at, new_at, *(await _later_ats(ctx, later_ids))])
        if need_snapshot
        else Snapshot(days={}, instants={})
    )
    excluded = {row["appointment_id"], *later_ids}

    # ---- 알람 다시 계산 (시각 · 장소 · 중요도가 바뀔 때만) ----
    plan: Any = None
    diff: Any = None
    if participant and patched["affects_alarms"]:
        made = await _replan_for(
            ctx,
            env,
            row=row,
            patched=patched,
            participant=participant,
            alarms=alarms,
            snapshot=snapshot,
            excluded=excluded,
            old_at=old_at,
            new_at=new_at,
            tz=tz,
        )
        plan = made["plan"]
        diff = made["diff"]
        result["changes"] = made["changes"]
        result["warnings"].extend(made["warnings"])

        # L6 안건후보: 확인 시트 기본안 = 고치는 일정의 알람이 5분 이상 늦춰지면 저장하지 않고 needs_confirm. 앞당김은 바로 저장 (D67)
        if needs_confirm(result["changes"]) and not req["confirmed"]:
            return {
                **result,
                "saved": False,
                "needs_confirm": True,
                "alarms": [summarize(a) for a in alarms],
                "warnings": result["warnings"],
            }

    # ---- 쓰기 ----
    canceled: list[str] = []
    if later_ids:
        canceled.extend(await _delete_rows(ctx, later_ids))
        result["deleted_appointment_ids"] = later_ids

    after = alarms
    if participant and plan is not None and diff is not None:
        chosen = plan.travel.chosen
        must(
            "appointment_participants update",
            await ctx.user_db.from_("appointment_participants")
            .update(
                {
                    "est_travel_minutes": plan.travel_minutes,
                    "travel_source": chosen.source if chosen is not None else None,
                    "prepare_minutes": plan.prepare["minutes"],
                }
            )
            .eq("participant_id", participant["participant_id"])
            .eq("user_id", ctx.user_id),
        )
        after = await _apply_alarm_diff(ctx, participant["participant_id"], diff, alarms)
        canceled.extend(canceled_alarm_ids(before=alarms, after=after))

    # 공동 약속: 만든 사람이 시각 · 장소를 고치면 다른 참여자의 알람도 다시 계산함. 약속 행을 쓰기 전에 해서 실패해도 같은 요청을 다시 보내면 이어서 맞춰지게 함 (9-10 ~ 9-17 · 9-43)
    sync = (
        await _sync_other_participants(
            ctx, {"row": row, "patched": patched, "old_at": old_at, "new_at": new_at}
        )
        if group and (patched["changed"]["at"] or patched["changed"]["place"])
        else None
    )

    row_patch: dict[str, Any] = {**patched["next"]}
    if effect["mark"] == "EDITED":
        row_patch["exception_kind"] = "EDITED"
    if effect["mark"] == "CLEAR":
        row_patch["exception_kind"] = None
    if effect["rule"]["kind"] == "SET":
        row_patch["recurrence_rule"] = effect["rule"]["value"]
    if effect["rule"]["kind"] == "CLEAR_ALL":
        # 반복 끔: 앞 회차가 기준이 되어 roll이 다시 만들지 않도록 묶음 모든 행의 규칙을 비움 (지난 행은 기록으로 남음)
        must(
            "appointments rule clear",
            await ctx.user_db.from_("appointments")
            .update({"recurrence_rule": None})
            .eq("creator_id", ctx.user_id)
            .eq("series_id", row["series_id"])
            .not_("recurrence_rule", "is", None),
        )
        row_patch["recurrence_rule"] = None
    await _update_row(ctx, row["appointment_id"], row_patch, row.get("creator_id"))
    if sync:
        await sync[
            "finish"
        ]()  # 계산을 못 한 참여자의 알람을 약속이 옮겨진 만큼 같이 옮김 — 약속 행 뒤에 해야 요청을 다시 보내도 두 번 안 옮겨짐 (X-APPT-03)

    if effect["deleteLater"]:
        await _clear_attempts(ctx, row, {"after": row["occurrence_on"]})

    # 시각 · 장소가 바뀐 공동 약속만 참여자에게 알림(제목 · 중요도만 고치면 없음, 9-39). 푸시는 약속 행이 써진 뒤에, 실패해도 고친 것은 그대로
    if sync:
        result["participants"] = sync["summary"]
        result["warnings"].extend(sync["warnings"])
        pushed = await _push_group_change(ctx, row, "CHANGED", held=sync["held"])
        if pushed.get("notified") is not None:
            result["notified"] = pushed["notified"]
        result["warnings"].extend(pushed["warnings"])

    # 제목만 바뀐 경우도 폰의 알람 제목이 바뀌어야 하므로 꾸러미를 다시 줌
    if participant and (plan is not None or patched["changed"]["title"]):
        departure = next((a for a in after if a["alarm_type"] == "DEPARTURE"), None)
        result["ring_packages"].extend(
            _packages_of(
                alarms=after,
                appointment={"title": patched["next"]["title"], "dest_name": patched["next"]["dest_name"]},
                at=new_at,
                tz=tz,
                travel_minutes=plan.travel_minutes
                if plan is not None
                else participant.get("est_travel_minutes"),
                depart_at=plan.departure
                if plan is not None
                else (_at(departure["scheduled_at"]) if departure else None),
            )
        )
    result["alarms"] = [summarize(a) for a in after]

    # ---- 7.1.3: 같은 날 다른 일정의 기상 다시 판정 ----
    if patched["changed"]["at"] or later_ids:
        again = await _reevaluate_wake(ctx, env, snapshot)
        result["ring_packages"].extend(again["ring_packages"])
        canceled.extend(again["canceled_alarm_ids"])
        result["warnings"].extend(again["warnings"])
    return _settle(result, canceled)


async def _later_ats(ctx: EditCtx, appointment_ids: Sequence[str]) -> list[datetime]:
    """지울 회차들의 약속 시각 (7.1.3으로 다시 볼 날을 정하려고)"""
    if not appointment_ids:
        return []
    rows = must(
        "later rows",
        await ctx.user_db.from_("appointments")
        .select("appointment_at")
        .eq("creator_id", ctx.user_id)
        .in_("appointment_id", list(appointment_ids)),
    )
    return [_at(r["appointment_at"]) for r in rows]


async def _travel_for(
    ctx: EditCtx, env: Env, *, row: dict[str, Any], participant: dict[str, Any], place_changed: bool
) -> dict[str, Any]:
    """이동시간 정하기 — 장소가 안 바뀌면 저장된 값 그대로, 직접 입력(MANUAL)도 그대로 (D42 C). 장소가 바뀌면 길찾기 → 내 실측 중앙값 → 직선 근사 (7.1.2)

    L6 안건후보: 시각만 바꿀 때 이동시간 기본안 = 저장된 값 그대로 (길찾기를 새로 안 부름 — 앱 가짜 모드와 같음). 장소를 바꾸면 길찾기를 새로 구함
    """
    stored = _stored_travel(participant)
    if stored and (not place_changed or participant.get("travel_source") == "MANUAL"):
        return {"travel": stored, "warnings": []}

    profile = await env.profile()
    basics = resolve_basics(draft=PlanDraft(), profile=profile, appointment=row, participant=participant)

    def coords(p: Any) -> dict[str, Any] | None:
        return {"lat": p.lat, "lng": p.lng} if p.lat is not None and p.lng is not None else None

    async def samples() -> Any:
        return await load_travel_samples(ctx.user_db, ctx.user_id)

    resolved = await resolve_travel(
        {
            "origin": coords(basics.origin),
            "destination": coords(basics.destination),
            "transport": basics.transport,
        },
        TravelDeps(kakao=ctx.kakao, samples=samples),
    )
    chosen = resolved.chosen
    if not chosen:
        raise AppError(
            "ROUTE_UNAVAILABLE",
            "새 장소까지 이동시간을 구하지 못했어요. 장소를 검색해서 골라 주세요",
            {"manual_required": True, "warnings": [_warning(w) for w in resolved.warnings]},
        )
    warnings = [_warning(w) for w in resolved.warnings]
    # 길찾기가 안 돼 근사값(ESTIMATE) · 내 이동 기록(CACHED)으로 저장함 — H03가 "대략"으로 보임 (roll의 폴백과 같음)
    #   L6 안건후보: 장소를 바꿨는데 길찾기가 안 될 때 기본안 = 근사값(ESTIMATE)으로 저장하고 경고를 같이 줌 (저장을 막지 않음)
    source = chosen.source
    if source in ("ESTIMATE", "CACHED"):
        warnings.append(
            {
                "code": "TRAVEL_NOT_EXACT",
                "message": "이동시간이 길찾기 값이 아니에요. 대략적인 값이니 확인해 주세요",
                "detail": source,
            }
        )
    return {"travel": chosen, "warnings": warnings}


async def _clear_attempts(ctx: EditCtx, row: dict[str, Any], scope: Any) -> None:
    """이후 모두 고치기 · 지우기로 날짜가 바뀐 묶음의 재시도 기록 정리 (service 전용 표). after = 이 날짜 뒤의 기록만 (이 날짜까지는 그대로)"""
    q = (
        ctx.service.from_("roll_attempts")
        .delete()
        .eq("user_id", ctx.user_id)
        .eq("series_id", row["series_id"])
    )
    must("roll_attempts delete", await (q if scope == "all" else q.gt("occurrence_on", scope["after"])))


# ---------------------------------------------------------------------------
# 지우기
# ---------------------------------------------------------------------------


async def delete_appointment(ctx: EditCtx, req: EditRequest) -> EditResponse:
    target = await _load_target(ctx, req["appointment_id"])
    # 없는 행(이미 지움 · 남의 개인 약속)은 성공 — 같은 요청을 두 번 보내면 같은 결과. 남의 것은 RLS로 안 보여서 건드릴 것이 애초에 없음
    if not target:
        return {
            **_common(req["appointment_id"]),
            "deleted": False,
            "mode": "NONE",
            "deleted_appointment_ids": [],
        }
    _assert_editor(target, ctx, "지울")
    await _assert_group_member(ctx, target["appointment"])
    row, alarms = target["appointment"], target["alarms"]

    group = row.get("group_id") is not None
    recurring = not group and row.get("recurrence_rule") is not None
    mode = delete_mode(recurring=recurring, group=group, scope=req["scope"])
    result: EditResponse = {
        **_common(row["appointment_id"]),
        "deleted": True,
        "mode": mode,
        "deleted_appointment_ids": [],
    }
    if mode == "CANCEL_THIS" and row.get("exception_kind") == "CANCELED":
        return result  # 이미 이번만 지움 — 같은 결과

    # 출발 · 도착 기록이 있는 회차는 지우지 않음 (기록이 사라짐). L6 안건후보: 기록 있는 일정 지우기 기본안 = 거절
    with_records = row["appointment_id"] in await _record_ids_of(ctx, [row["appointment_id"]]) or (
        group and await _shared_record_exists(ctx, row["appointment_id"])
    )
    if with_records:
        raise AppError(
            "UNPROCESSABLE", "출발 · 도착 기록이 있는 일정은 지울 수 없어요", {"reason": "HAS_RECORDS"}
        )

    env = Env(ctx)
    at = _at(row["appointment_at"])
    # 만든 사람이 비어 있는 공동 약속은 안 지난 약속만 지움 (DB 정책 appointments_delete_creator · 고치기와 같은 하루 경계, X-APPT-01). 정책이 막으면 0행이라 오류 없이 성공처럼 보이므로 먼저 거절
    if (
        group
        and row.get("creator_id") is None
        and ms(at) < ms(_range(local_day_range(ctx.now, await env.tz()))[0])
    ):
        raise AppError("UNPROCESSABLE", "지난 약속이에요", {"reason": "PAST_APPOINTMENT"})
    canceled: list[str] = []
    later_ids: list[str] = []
    later_at_list: list[datetime] = []
    if mode == "FROM_HERE":
        rows = await _series_rows(ctx, row)
        later_ids = later_rows_to_delete(
            rows=rows,
            target=row,
            now=ctx.now,
            record_ids=await _record_ids_of(ctx, [r["appointment_id"] for r in rows]),
            mode="DELETE",
        )
        later_at_list = await _later_ats(ctx, later_ids)
    snapshot = await _snapshot_days(ctx, env, [at, *later_at_list])

    if mode == "CANCEL_THIS":
        # 행은 두고 알람만 지움 + 지운 회차 표시 — 행을 지우면 alarm-roll이 이 날짜를 다시 만듦. 표시는 마지막에 (알람이 먼저 지워지면 다시 보냈을 때 이어서 끝남)
        canceled.extend(_ids(alarms))
        if alarms:
            must(
                "alarms delete",
                await ctx.user_db.from_("alarms")
                .delete()
                .in_("alarm_id", _ids(alarms))
                .eq("user_id", ctx.user_id),
            )
        await _update_row(ctx, row["appointment_id"], {"exception_kind": "CANCELED"})
    elif mode == "FROM_HERE":
        canceled.extend(await _delete_rows(ctx, later_ids))
        result["deleted_appointment_ids"] = later_ids
        # 남은 행의 규칙을 비움 (alarm-roll 대상에서 빠짐, 지난 행은 기록으로) — 이 행을 지우기 전에 해서, 중간에 실패해도 다시 보내면 이어서 끝남
        must(
            "appointments rule clear",
            await ctx.user_db.from_("appointments")
            .update({"recurrence_rule": None})
            .eq("creator_id", ctx.user_id)
            .eq("series_id", row["series_id"])
            .not_("recurrence_rule", "is", None),
        )
        canceled.extend(await _delete_rows(ctx, [row["appointment_id"]]))
        result["deleted_appointment_ids"] = [*later_ids, row["appointment_id"]]
        await _clear_attempts(ctx, row, "all")
    else:
        # 한 번짜리 · 공동 약속(만든 사람) — 약속 + 참여 + 알람. 공동 약속은 다른 참여자의 참여 · 알람도 같이 사라짐(CASCADE)
        #   L6 안건후보: 공동 약속 지우기 기본안 = 만든 사람이 지우면 약속 전체가 지워짐. 다른 참여자의 폰 예약은 그 사람의 다음 맞추기(reconcile)에서 정리 (변경 알림은 L3 S2 뒤)
        #   공동 약속의 받는 사람(참여자)은 지우기 전에 정해 두고 지운 뒤에 '취소됐어요' 푸시를 보냄 — 받은 폰이 그 약속의 알람을 지움 (9-20). 지난 약속은 알릴 일이 없음
        planned = (
            await _plan_group_push_safe(ctx, row)
            if group
            and _ms_of(row["appointment_at"]) >= ms(_range(local_day_range(ctx.now, await env.tz()))[0])
            else None
        )
        canceled.extend(await _delete_rows(ctx, [row["appointment_id"]], row.get("creator_id")))
        result["deleted_appointment_ids"] = [row["appointment_id"]]
        if planned:
            result["warnings"].extend(planned["warnings"])
            if planned.get("plan"):
                pushed = await _push_group_change(ctx, row, "CANCELED", plan=planned["plan"])
                if pushed.get("notified") is not None:
                    result["notified"] = pushed["notified"]
                result["warnings"].extend(pushed["warnings"])

    again = await _reevaluate_wake(ctx, env, snapshot)
    result["ring_packages"] = again["ring_packages"]
    result["warnings"] = [*result["warnings"], *again["warnings"]]
    return _settle(result, [*canceled, *again["canceled_alarm_ids"]])


# ---------------------------------------------------------------------------
# 되돌리기 — 이미 지운 뒤의 되돌리기 (이번만 지운 회차만)
# ---------------------------------------------------------------------------


async def restore_appointment(ctx: EditCtx, req: EditRequest) -> EditResponse:
    """앱은 5초 안에 되돌리면 서버에 아무것도 안 보내므로 이 함수는 "이미 지운 뒤"에만 불림.

    되살릴 수 있는 것은 CANCELED 표시로 행이 남아 있는 회차뿐 — 한 번짜리 · 이후 모두 지우기는 행 자체를 지워서 되살릴 수 없음(restored false).
      L6 안건후보: 되돌리기 기본안 = 이번만 지운 회차만 서버에서 되살림. 행을 지우는 경우까지 되살리려면 모든 읽는 곳이 거를 소프트 삭제 칸이 필요해 범위 밖 (앱은 지우기 전 값을 들고 있다가 alarm-commit으로 다시 만들 수 있음)
    """
    target = await _load_target(ctx, req["appointment_id"])
    if not target:
        return {**_common(req["appointment_id"]), "restored": False, "reason": "NOT_FOUND"}
    _assert_editor(target, ctx, "고칠")
    row, participant, existing_alarms = target["appointment"], target["participant"], target["alarms"]
    if row.get("exception_kind") != "CANCELED":
        return {
            **_common(row["appointment_id"]),
            "restored": False,
            "reason": "NOT_DELETED",
        }  # 지운 적 없음 — 아무것도 안 함 (같은 요청을 두 번 보내면 두 번째)

    env = Env(ctx)
    tz = await env.tz()
    at = _at(row["appointment_at"])
    result: EditResponse = {**_common(row["appointment_id"]), "restored": True}
    # 되살린 회차는 이번만 고침(EDITED)으로 둠 — 지우기 전에 이번만 고쳤는지 알 수 없어서, 다음 회차의 기준이 되어 값이 번지는 쪽보다 안전한 쪽
    #   L6 안건후보: 되돌린 회차의 표시 기본안 = EDITED (안전쪽)
    if not participant:
        await _update_row(ctx, row["appointment_id"], {"exception_kind": "EDITED"})
        return result
    travel = _stored_travel(participant)
    if not travel:
        raise AppError(
            "ROUTE_UNAVAILABLE",
            "이동시간을 알 수 없어서 알람을 다시 만들지 못했어요",
            {"manual_required": True},
        )

    snapshot = await _snapshot_days(ctx, env, [at])
    day = snapshot.days.get(local_date_key(at, tz), [])
    same_day_wake = has_earlier_wake(
        at, wakes_of_rows([_day_wake_row(r) for r in day]), row["appointment_id"]
    )
    profile, stats, late_count_4w = await asyncio.gather(env.profile(), env.stats(), env.late())
    # 지운 알람의 종류 · 직접 고른 시각은 남아 있지 않음 → 계산된 알람 전부를 자동 값으로 다시 만듦
    # 이미 만들다 만 알람이 있으면(알람은 만들었는데 표시 쓰기가 실패한 뒤 다시 보낸 경우) 그 알람을 기준으로 이어서 맞춤 — 없는 것처럼 다시 넣으면 유일 제약에 걸려 영영 CONFLICT
    plan = compute_plan(
        PlanInputs(
            now=ctx.now,
            draft=PlanDraft(),
            profile=profile,
            appointment=row,
            participant=participant,
            existing_alarms=existing_alarms,
            stats=stats,
            late_count4w=late_count_4w,
            same_day_wake=same_day_wake,
        ),
        resolved_travel_of(travel),
        "save",
    )
    check = plan.check
    if not check["ok"]:
        detail = check.get("detail") if isinstance(check, Mapping) else getattr(check, "detail", None)
        raise AppError("UNPROCESSABLE", check["message"], {"reason": check["reason"], **(detail or {})})
    diff = diff_alarms(plan, existing_alarms, now=ctx.now, replace_existing=False)
    after = await _apply_alarm_diff(ctx, participant["participant_id"], diff, existing_alarms)
    await _update_row(ctx, row["appointment_id"], {"exception_kind": "EDITED"})

    result["ring_packages"] = _packages_of(
        alarms=after,
        appointment=row,
        at=at,
        tz=tz,
        travel_minutes=plan.travel_minutes,
        depart_at=plan.departure,
    )
    again = await _reevaluate_wake(ctx, env, snapshot)
    result["ring_packages"].extend(again["ring_packages"])
    result["warnings"] = again["warnings"]
    return _settle(result, again["canceled_alarm_ids"])


# ---------------------------------------------------------------------------
# 공동 약속에서 나만 빠지기
# ---------------------------------------------------------------------------


async def leave_shared_appointment(ctx: EditCtx, req: EditRequest) -> EditResponse:
    """내 참여 조건과 내 알람만 지움 — 약속은 다른 참여자에게 그대로. 만든 사람은 못 빠짐(지워야 함). 되살리기 없음"""
    target = await _load_target(ctx, req["appointment_id"])
    if not target:
        return {**_common(req["appointment_id"]), "left": False}
    row, participant, alarms = target["appointment"], target["participant"], target["alarms"]
    if row.get("group_id") is None:
        raise AppError("UNPROCESSABLE", "공동 약속이 아니에요", {"reason": "NOT_SHARED"})
    # L6 안건후보: 만든 사람이 빠지려 할 때 기본안 = 막고 "지워 주세요" 안내 (앱 가짜 모드와 같음)
    if row.get("creator_id") == ctx.user_id:
        raise AppError(
            "UNPROCESSABLE",
            "내가 만든 공동 약속은 빠질 수 없어요. 지워 주세요",
            {"reason": "CREATOR_CANNOT_LEAVE"},
        )
    if not participant:
        return {
            **_common(row["appointment_id"]),
            "left": True,
        }  # 이미 빠짐 — 같은 요청을 두 번 보내면 두 번째
    if row["appointment_id"] in await _record_ids_of(ctx, [row["appointment_id"]]):
        raise AppError(
            "UNPROCESSABLE", "출발 · 도착 기록이 있는 일정에서는 빠질 수 없어요", {"reason": "HAS_RECORDS"}
        )

    env = Env(ctx)
    snapshot = await _snapshot_days(ctx, env, [_at(row["appointment_at"])])
    must(
        "appointment_participants delete",
        await ctx.user_db.from_("appointment_participants")
        .delete()
        .eq("participant_id", participant["participant_id"])
        .eq("user_id", ctx.user_id),
    )  # 내 알람은 CASCADE
    again = await _reevaluate_wake(ctx, env, snapshot)
    return _settle(
        {
            **_common(row["appointment_id"]),
            "left": True,
            "ring_packages": again["ring_packages"],
            "warnings": again["warnings"],
        },
        [*_ids(alarms), *again["canceled_alarm_ids"]],
    )


# ---------------------------------------------------------------------------


async def run_edit(ctx: EditCtx, req: EditRequest) -> EditResponse:
    action = req["action"]
    if action == "UPDATE":
        return await update_appointment(ctx, req)
    if action == "DELETE":
        return await delete_appointment(ctx, req)
    if action == "RESTORE":
        return await restore_appointment(ctx, req)
    return await leave_shared_appointment(ctx, req)

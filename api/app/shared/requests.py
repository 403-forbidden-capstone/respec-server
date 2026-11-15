"""본 레포 supabase/functions/_shared/requests.ts의 서버판 (기준 b140f70) — alarm-suggest · alarm-commit 요청 읽기

둘이 같은 초안 모양을 받음 — 필드 이름은 상세설계서 5장 5.4.2를 따름. 문제는 Reader에 모아 두고 부르는 쪽이 finish()로 던짐.

초안 모양:
  appointment_id(기존 약속) 또는 appointment{ title, appointment_at, destination{name,lat,lng,radius_m(선택, 100~500)}, importance, group_id, recurrence_rule }(새 약속)
  participant{ origin, transport_type, est_travel_minutes + travel_source(API · MANUAL), prepare_minutes, buffer_minutes, before_prepare_minutes, with_wake }
  + 루트의 kinds · enabled · manual_times · levels
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.read import Reader
from app.shared.calc.importance import auto_importance
from app.shared.commit_core import EditInput
from app.shared.draft import (
    IMPORTANCES,
    UNSET,
    ParticipantFields,
    read_alarm_fields,
    read_participant_fields,
    read_place,
    read_recurrence,
)
from app.shared.plan_core import PlanDraft


@dataclass
class _Appointment:
    draft: PlanDraft
    appointment: "NewAppointment | None"


def _read_appointment_draft(r: Reader, *, title_required: bool) -> _Appointment:
    """titleRequired: 저장할 때(alarm-commit)만 새 약속의 제목이 필수"""
    appointment_id = r.uuid("appointment_id")
    raw_new = r.object("appointment")
    if appointment_id and raw_new is not None:
        r.problem_at(
            "appointment",
            "appointment_id와 같이 쓸 수 없어요 (일정 제목 · 시각 · 장소는 여기서 고치지 않아요)",
        )
    if not appointment_id and raw_new is None:
        r.problem_at("appointment_id", "appointment_id 또는 appointment가 필요해요")

    appointment: NewAppointment | None = None
    at: datetime | None = None
    destination = None
    importance: str | None = None
    if raw_new is not None and not appointment_id:
        title = raw_new.string("title", required=title_required, min=1, max=100)
        at = raw_new.date("appointment_at", required=True)
        destination = read_place(raw_new, "destination", required=True, name_required=True, radius=True)
        # 고르지 않았으면 제목으로 자동 (시험 · 면접만 HIGH, 나머지 LOW) — 직접 고른 값이 이김
        importance = raw_new.one_of("importance", IMPORTANCES)
        if importance is None:
            importance = auto_importance(title) if title else None
        group_id = raw_new.uuid("group_id")
        recurrence_rule = read_recurrence(raw_new)
        if title:
            appointment = NewAppointment(
                title=title, group_id=group_id or None, recurrence_rule=recurrence_rule
            )

    # participant{origin, transport_type, est_travel_minutes, travel_source, prepare_minutes, buffer_minutes} (+ before_prepare_minutes · with_wake)
    p = r.object("participant")
    participant_fields = ParticipantFields()
    travel_minutes = None
    travel_source = None
    prepare_minutes = None
    if p is not None:
        participant_fields = read_participant_fields(p)
        travel_minutes = p.int("est_travel_minutes", min=0, max=600)
        # CACHED(같은 경로 실측 중앙값)는 D5가 열려 있어 저장하지 않음 — 보여 준 값을 저장하려면 사용자가 확인한 값으로 MANUAL
        # ESTIMATE(직선거리 근사값)도 확인한 값으로 MANUAL (7장 7.1.2 단계 4)
        travel_source = p.one_of("travel_source", ("API", "MANUAL"))
        prepare_minutes = p.int("prepare_minutes", min=0, max=600)
        if travel_minutes is not None and not travel_source:
            p.problem_at("travel_source", "est_travel_minutes와 같이 API · MANUAL 중 하나를 보내 주세요")
        if travel_source and travel_minutes is None:
            p.problem_at("est_travel_minutes", "travel_source와 같이 보내 주세요")

    alarm_fields = read_alarm_fields(r)
    draft = PlanDraft(
        appointment_id=appointment_id or None,
        at=at or None,
        destination=destination or None,
        importance=importance or None,
        origin=participant_fields.origin,
        transport=participant_fields.transport,
        buffer_minutes=participant_fields.buffer_minutes,
        before_prepare_minutes=participant_fields.before_prepare_minutes,
        with_wake=participant_fields.with_wake,
        travel_minutes=travel_minutes,
        travel_source=travel_source if travel_minutes is not None else None,
        prepare_minutes=prepare_minutes,
        kinds=alarm_fields.kinds,
        enabled=alarm_fields.enabled,
        manual_times=alarm_fields.manual_times,
        levels=alarm_fields.levels,
    )
    return _Appointment(draft=draft, appointment=appointment)


# ---- alarm-suggest (5.4.1) — 미리 보기 ----
def read_suggest_request(r: Reader) -> PlanDraft:
    """미리 보기에는 title · group_id · recurrence_rule · replace_existing · edits가 필요 없어 읽기만 하고 안 씀"""
    return _read_appointment_draft(r, title_required=False).draft


# ---- alarm-commit (5.4.2) ----


@dataclass
class NewAppointment:
    title: str
    group_id: str | None = None
    recurrence_rule: Any = UNSET  # D44 — 첫 회차만 알람을 만듦 (D4). UNSET = 안 보냄 · None = 반복 끔(null)


@dataclass
class StandaloneInput:
    alarm_id: str | None = None  # 있으면 고치기
    scheduled_at: datetime | None = None  # 만들 때 필수
    recurrence_rule: Any = UNSET  # UNSET = 안 보냄 · None = null
    is_enabled: bool | None = None
    levels: dict[str, int | None] | None = None


@dataclass
class CommitRequest:
    """type 'APPOINTMENT': draft · appointment(새 약속이면, 없으면 draft.appointment_id의 기존 약속) · replace_existing · edits
    type 'STANDALONE': alarm · edits"""

    type: str
    edits: list[EditInput] = field(default_factory=list)
    draft: PlanDraft | None = None
    appointment: NewAppointment | None = None
    replace_existing: bool = True
    alarm: StandaloneInput | None = None


_SCREENS = ("H03", "H05", "S01")
_ALARM_TYPES = ("WAKE", "PREPARE", "DEPARTURE", "ARRIVE", "ROUTINE")
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*\Z")


def _read_edits(r: Reader) -> list[EditInput]:
    items = r.objects("edits", max=50) or []
    out: list[EditInput] = []
    for e in items:
        screen = e.one_of("screen", _SCREENS, required=True)
        field_name = e.string("field", required=True, max=40, pattern=_FIELD_NAME)
        alarm_type = e.one_of("alarm_type", _ALARM_TYPES)
        auto_value = e.json("auto_value")
        edited_value = e.json("edited_value")
        if not screen or not field_name:
            continue
        out.append(
            EditInput(
                screen=screen,
                field=field_name,
                alarm_type=alarm_type or None,
                auto_value=auto_value,
                edited_value=edited_value,
            )
        )
    return out


def read_commit_request(r: Reader) -> CommitRequest:
    type_ = r.one_of("type", ("APPOINTMENT", "STANDALONE")) or "APPOINTMENT"
    edits = _read_edits(r)

    if type_ == "STANDALONE":
        # 일정 없는 알람 (D46 · Q10 B): 시각 + 반복만. 알람 종류는 기상(소리) 고정, 이름은 '알람' 고정 (F4 ②)
        a = r.object("alarm", required=True)
        if a is None:
            return CommitRequest(type=type_, alarm=StandaloneInput(), edits=edits)
        alarm_id = a.uuid("alarm_id")
        scheduled_at = a.date("scheduled_at", required=not alarm_id)
        recurrence_rule = read_recurrence(a)
        is_enabled = a.bool("is_enabled")
        levels = read_alarm_fields(a).levels
        return CommitRequest(
            type=type_,
            alarm=StandaloneInput(
                alarm_id=alarm_id or None,
                scheduled_at=scheduled_at or None,
                recurrence_rule=recurrence_rule,
                is_enabled=is_enabled,
                levels=levels,
            ),
            edits=edits,
        )

    # 약속 알람: 새 약속(appointment) 또는 기존 약속(appointment_id) 중 하나 — alarm-suggest와 같은 초안 모양
    read = _read_appointment_draft(r, title_required=True)
    replace = r.bool("replace_existing")
    return CommitRequest(
        type="APPOINTMENT",
        draft=read.draft,
        appointment=read.appointment,
        replace_existing=True if replace is None else replace,
        edits=edits,
    )

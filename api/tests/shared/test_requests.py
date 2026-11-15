"""requests.ts → requests.py 시험 (원본 requests.test.ts의 모든 경우) — alarm-suggest · alarm-commit 요청 읽기"""

import copy
from datetime import UTC, datetime

import pytest

from app.core.errors import AppError
from app.core.read import Reader
from app.shared.commit_core import EditInput
from app.shared.draft import PlaceInput
from app.shared.plan_core import PlanDraft
from app.shared.requests import NewAppointment, StandaloneInput, read_commit_request, read_suggest_request

ID = "11111111-1111-4111-8111-111111111111"

NEW_BODY = {
    "appointment": {
        "title": "팀 주간회의",
        "appointment_at": "2026-10-20T10:00:00+09:00",
        "destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07},
        "importance": "NORMAL",
        "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
    },
    "participant": {
        "origin": {"name": "집", "lat": 37.5, "lng": 127},
        "transport_type": "TRANSIT",
        "est_travel_minutes": 50,
        "travel_source": "API",
        "prepare_minutes": 60,
        "buffer_minutes": 10,
    },
    "kinds": ["WAKE", "PREPARE", "DEPARTURE"],
    "edits": [
        {
            "screen": "H05",
            "field": "scheduled_at",
            "alarm_type": "WAKE",
            "auto_value": "2026-10-20T08:00:00+09:00",
            "edited_value": "2026-10-20T07:30:00+09:00",
        }
    ],
}


def body(**over):
    return {**copy.deepcopy(NEW_BODY), **over}


def issue_paths(err: AppError) -> list[str]:
    assert err.code == "VALIDATION_FAILED"
    return [i["path"] for i in err.details["issues"]]


def test_suggest_new_schedule_requires_time_and_place_manual_for_typed_travel_prepare():
    """suggest: 새 일정은 시각 · 장소가 필수, 이동 · 준비 직접 입력은 MANUAL (commit과 같은 초안 모양)"""
    r = Reader.root(
        {
            "appointment": {
                "appointment_at": "2026-10-20T10:00:00+09:00",
                "destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07},
                "importance": "HIGH",
            },
            "participant": {
                "origin": {"name": "집"},
                "transport_type": "TRANSIT",
                "buffer_minutes": 10,
                "est_travel_minutes": 70,
                "travel_source": "MANUAL",
                "prepare_minutes": 40,
            },
            "kinds": ["WAKE"],
            "manual_times": {"WAKE": "2026-10-20T07:30:00+09:00"},
        }
    )
    d = read_suggest_request(r)
    r.finish()
    assert d.at == datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
    assert d.destination == PlaceInput(name="세종대학교", lat=37.55, lng=127.07)
    assert d.importance == "HIGH"
    assert [d.travel_minutes, d.travel_source, d.prepare_minutes] == [70, "MANUAL", 40]
    assert d.kinds == ["WAKE"]
    assert d.transport == "TRANSIT"


def test_commit_destination_radius_m_optional():
    """commit: 새 약속 destination의 radius_m(선택)을 초안에 실음 — 없으면 칸 없음, 범위 밖 · 정수 아님 · 글자는 문제"""
    with_radius = Reader.root(
        body(
            appointment={
                **NEW_BODY["appointment"],
                "destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07, "radius_m": 300},
            }
        )
    )
    d = read_commit_request(with_radius)
    with_radius.finish()
    assert d.draft.destination == PlaceInput(name="세종대학교", lat=37.55, lng=127.07, radius_m=300)

    without = Reader.root(body())
    d0 = read_commit_request(without)
    without.finish()
    assert d0.draft.destination == PlaceInput(name="세종대학교", lat=37.55, lng=127.07)

    for bad in [99, 501, 1.5, "abc"]:
        r = Reader.root(
            body(
                appointment={
                    **NEW_BODY["appointment"],
                    "destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07, "radius_m": bad},
                }
            )
        )
        read_commit_request(r)
        with pytest.raises(AppError) as exc:
            r.finish()
        assert ",".join(issue_paths(exc.value)) == "appointment.destination.radius_m", str(bad)

    # 출발지의 radius_m은 저장 칸이 없어 와도 무시 (오류도 안 냄)
    origin_radius = Reader.root(
        body(
            participant={
                **NEW_BODY["participant"],
                "origin": {"name": "집", "lat": 37.5, "lng": 127, "radius_m": 9999},
            }
        )
    )
    d1 = read_commit_request(origin_radius)
    origin_radius.finish()
    assert d1.draft.origin == PlaceInput(name="집", lat=37.5, lng=127)


def test_suggest_missing_required_tells_which_field():
    """suggest: 필수 값이 없으면 어느 칸인지 알려 줌 (제목은 미리 보기에 필요 없음)"""
    none = Reader.root({})
    read_suggest_request(none)
    with pytest.raises(AppError) as exc:
        none.finish()
    assert ",".join(issue_paths(exc.value)) == "appointment_id"
    empty = Reader.root({"appointment": {}})
    read_suggest_request(empty)
    with pytest.raises(AppError) as exc:
        empty.finish()
    assert ",".join(issue_paths(exc.value)) == "appointment.appointment_at,appointment.destination"


def test_suggest_existing_appointment_uses_db_values_and_rejects_both():
    """suggest: 기존 약속(appointment_id)이면 시각 · 장소 · 중요도는 DB 값 — appointment를 같이 보내면 거절 (D41, commit과 같음)"""
    r = Reader.root({"appointment_id": ID, "participant": {"buffer_minutes": 20}})
    d = read_suggest_request(r)
    r.finish()
    assert d == PlanDraft(appointment_id=ID, buffer_minutes=20)

    both = Reader.root(
        {
            "appointment_id": ID,
            "appointment": {"appointment_at": "2026-10-25T10:00:00+09:00", "destination": {"name": "딴 곳"}},
        }
    )
    read_suggest_request(both)
    with pytest.raises(AppError):
        both.finish()


def test_suggest_old_root_shape_is_not_read():
    """suggest: 예전 모양(루트의 appointment_at · destination · travel_minutes_override)은 읽지 않음"""
    r = Reader.root(
        {
            "appointment_at": "2026-10-20T10:00:00+09:00",
            "destination": {"name": "세종대학교"},
            "travel_minutes_override": 70,
        }
    )
    read_suggest_request(r)
    with pytest.raises(AppError):  # appointment_id 또는 appointment가 필요
        r.finish()


def test_commit_new_appointment_participant_kinds_edits_and_recurrence():
    """commit: 새 약속 + 참여 조건 + 알람 종류 + 고친 값 기록 (D44 반복 규칙)"""
    r = Reader.root(body())
    c = read_commit_request(r)
    r.finish()
    assert c.type == "APPOINTMENT"
    assert c.appointment == NewAppointment(title="팀 주간회의", recurrence_rule="FREQ=WEEKLY;BYDAY=TU")
    assert c.draft.at == datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
    assert [
        c.draft.travel_minutes,
        c.draft.travel_source,
        c.draft.prepare_minutes,
        c.draft.buffer_minutes,
    ] == [50, "API", 60, 10]
    assert c.draft.origin == PlaceInput(name="집", lat=37.5, lng=127)
    assert c.replace_existing is True
    assert c.edits == [
        EditInput(
            screen="H05",
            field="scheduled_at",
            alarm_type="WAKE",
            auto_value="2026-10-20T08:00:00+09:00",
            edited_value="2026-10-20T07:30:00+09:00",
        )
    ]


def test_commit_existing_appointment_adds_my_participant_and_rejects_both_or_neither():
    """commit: 기존 약속에 내 참여 조건을 더함 (공동 약속 각자 계산) — 약속 정보를 같이 보내면 거절"""
    ok = Reader.root({"appointment_id": ID, "participant": {"transport_type": "CAR"}})
    c = read_commit_request(ok)
    ok.finish()
    assert c.type == "APPOINTMENT" and c.draft.appointment_id == ID
    assert c.type == "APPOINTMENT" and c.appointment is None

    both = Reader.root({"appointment_id": ID, "appointment": NEW_BODY["appointment"]})
    read_commit_request(both)
    with pytest.raises(AppError):
        both.finish()
    neither = Reader.root({})
    read_commit_request(neither)
    with pytest.raises(AppError):
        neither.finish()


def test_commit_group_id_uuid_only():
    """commit: 새 약속의 group_id(H05 "함께할 그룹")는 uuid만 받아 appointment.groupId로 — 안 보내면 칸 없음(개인 일정), uuid가 아니면 문제 (9-01)"""
    r = Reader.root(body(appointment={**NEW_BODY["appointment"], "group_id": ID}))
    c = read_commit_request(r)
    r.finish()
    assert c.type == "APPOINTMENT"
    assert c.appointment == NewAppointment(
        title="팀 주간회의", group_id=ID, recurrence_rule="FREQ=WEEKLY;BYDAY=TU"
    )

    solo = read_commit_request(Reader.root(body()))
    assert solo.type == "APPOINTMENT" and solo.appointment is not None and solo.appointment.group_id is None

    bad = Reader.root(body(appointment={**NEW_BODY["appointment"], "group_id": "not-a-uuid"}))
    read_commit_request(bad)
    with pytest.raises(AppError):
        bad.finish()


def test_commit_travel_with_source_and_no_cached_or_estimate():
    """commit: 이동시간은 출처와 같이, CACHED · ESTIMATE는 받지 않음 (D5)"""
    for source in ["CACHED", "ESTIMATE"]:
        r = Reader.root(body(participant={"est_travel_minutes": 40, "travel_source": source}))
        read_commit_request(r)
        with pytest.raises(AppError):
            r.finish()
    lonely = Reader.root(body(participant={"est_travel_minutes": 40}))
    read_commit_request(lonely)
    with pytest.raises(AppError):
        lonely.finish()


def test_commit_standalone_alarm():
    """commit: 일정 없는 알람(D46) — 시각 필수, 반복 규칙, 고치기는 alarm_id"""
    create = Reader.root(
        {
            "type": "STANDALONE",
            "alarm": {
                "scheduled_at": "2026-10-20T07:00:00+09:00",
                "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
                "levels": {"vibration": 2},
            },
        }
    )
    c = read_commit_request(create)
    create.finish()
    assert c.type == "STANDALONE"
    assert c.alarm.levels == {"vibration": 2}
    assert c.alarm.recurrence_rule == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"

    no_time = Reader.root({"type": "STANDALONE", "alarm": {"recurrence_rule": "FREQ=DAILY"}})
    read_commit_request(no_time)
    with pytest.raises(AppError):
        no_time.finish()

    edit = Reader.root(
        {"type": "STANDALONE", "alarm": {"alarm_id": ID, "is_enabled": False, "recurrence_rule": None}}
    )
    e = read_commit_request(edit)
    edit.finish()
    assert e.type == "STANDALONE" and e.alarm == StandaloneInput(
        alarm_id=ID, recurrence_rule=None, is_enabled=False
    )


def test_commit_edit_log_field_name_and_screen_rules():
    """commit: 고친 값 기록의 값 이름은 소문자 · 숫자 · _만, 화면은 H03 · H05 · S01만"""
    r = Reader.root(
        body(
            edits=[
                {"screen": "H09", "field": "Scheduled At"},
                {"screen": "S01", "field": "intensity", "edited_value": None},
            ]
        )
    )
    c = read_commit_request(r)
    with pytest.raises(AppError):
        r.finish()
    assert len(r.issues) == 2
    assert c.type == "APPOINTMENT" and len(c.edits) == 1  # 올바른 한 줄만 남음


def test_suggest_and_commit_same_draft_same_calc_input():
    """suggest · commit: 같은 초안을 보내면 같은 계산 입력이 됨 (title · edits · replace_existing은 commit만 씀)"""
    suggest = read_suggest_request(Reader.root(body()))
    c = read_commit_request(Reader.root(body()))
    assert c.type == "APPOINTMENT"
    assert suggest == c.draft

    existing = {
        "appointment_id": ID,
        "participant": {"transport_type": "CAR", "buffer_minutes": 5},
        "levels": {"vibration": 2},
        "manual_times": {"WAKE": "2026-10-20T07:30:00+09:00"},
    }
    c2 = read_commit_request(Reader.root(existing))
    assert read_suggest_request(Reader.root(existing)) == (c2.draft if c2.type == "APPOINTMENT" else None)

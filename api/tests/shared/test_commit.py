"""본 레포 supabase/functions/_shared/commit.test.ts의 서버판 — alarm-commit의 쓰기 순서 · 응답 · 되돌리기를 가짜 DB(FakeDb)로 시험

요청 쪽 값(created · edits · 일정 없는 알람)은 JSON 모양이라 dict로 넘김 (키는 snake_case).
"""

from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.shared.calc.prepare import prepare_stats_of
from app.shared.commit import CommitCtx, assert_persistable, commit_standalone, persist_appointment
from app.shared.commit_core import EditInput
from app.shared.draft import PlaceInput
from app.shared.js import from_ms, ms
from app.shared.plan_core import PlanDraft, PlanInputs, compute_plan, to_suggestion
from app.shared.route import ResolvedTravel, TravelChoice
from tests.fake_db import FakeDb

from .w2_helpers import hm, iso_z, kst, sync

USER = "u-me"
NOW = kst(22, 400)  # 6:40
PROFILE = {
    "timezone": "Asia/Seoul",
    "default_buffer_minutes": 10,
    "default_transport": "TRANSIT",
    "home_name": "집",
    "home_lat": 37.5,
    "home_lng": 127.0,
}
API = ResolvedTravel(
    chosen=TravelChoice(50, "API", False, ""),
    auto=TravelChoice(50, "API", False, ""),
    warnings=[],
)
TZ = "Asia/Seoul"


def setup():
    db = FakeDb()
    ctx = CommitCtx(user_id=USER, user_db=db, service=db, now=NOW)
    return SimpleNamespace(db=db, ctx=ctx)


def new_draft(**over):
    fields = {
        "at": kst(22, 600),
        "destination": PlaceInput(name="세종대학교", lat=37.55, lng=127.07),
        "travel_minutes": 50,
        "travel_source": "API",
        "prepare_minutes": 60,
        "before_prepare_minutes": 30,
    }
    return PlanDraft(**{**fields, **over})


def inputs_of(draft, **over):
    fields = {
        "now": NOW,
        "draft": draft,
        "profile": PROFILE,
        "existing_alarms": [],
        "stats": prepare_stats_of([]),
        "late_count4w": 1,
        "same_day_wake": False,
    }
    return PlanInputs(**{**fields, **over})


def commit_args(draft, over=None, input_over=None):
    inputs = inputs_of(draft, **(input_over or {}))
    plan = compute_plan(inputs, API)
    args = {
        "plan": plan,
        "inputs": inputs,
        "created": {"title": "팀 주간회의", "recurrence_rule": "FREQ=WEEKLY;BYDAY=TH"},
        "draft": draft,
        "replace_existing": True,
        "edits": [],
        "time_zone": TZ,
    }
    return {**args, **(over or {})}


def at_hm(millis):
    return hm(iso_z(from_ms(millis)))


@sync
async def test_new_appointment_creates_appointment_participant_and_four_alarms_and_returns_ring_packages():
    """새 약속: 약속 · 참여 조건 · 알람 4개를 만들고 예약 꾸러미를 돌려줌"""
    env = setup()
    body = await persist_appointment(env.ctx, **commit_args(new_draft()))

    (appointment,) = env.db.rows("appointments")
    assert [
        appointment["creator_id"],
        appointment["title"],
        appointment["dest_name"],
        appointment["importance"],
        appointment["recurrence_rule"],
        appointment["group_id"],
    ] == [
        USER,
        "팀 주간회의",
        "세종대학교",
        "LOW",
        "FREQ=WEEKLY;BYDAY=TH",
        None,
    ]  # 중요도를 안 고르면 기본 약(LOW)
    (participant,) = env.db.rows("appointment_participants")
    assert [
        participant["appointment_id"],
        participant["user_id"],
        participant["est_travel_minutes"],
        participant["travel_source"],
        participant["prepare_minutes"],
        participant["buffer_minutes"],
        participant["transport_type"],
    ] == [appointment["appointment_id"], USER, 50, "API", 60, 10, "TRANSIT"]
    assert [
        f"{a['alarm_type']} {hm(a['scheduled_at'])} {str(a['is_enabled']).lower()} {a['time_source']}/{a['intensity_source']}"
        for a in env.db.rows("alarms")
    ] == [
        "WAKE 08:00 true AUTO/AUTO",
        "PREPARE 08:30 true AUTO/AUTO",
        "DEPARTURE 09:00 true AUTO/AUTO",
        "ARRIVE 09:50 true AUTO/AUTO",
    ]
    assert all(
        a["user_id"] == USER and a["participant_id"] == participant["participant_id"]
        for a in env.db.rows("alarms")
    )

    assert body["appointment_id"] == appointment["appointment_id"]
    assert body["participant_id"] == participant["participant_id"]
    assert [[p["type"], at_hm(p["atMillis"])] for p in body["ring_packages"]] == [
        ["WAKE", "08:00"],
        ["PREPARE", "08:30"],
        ["DEPARTURE", "09:00"],
        ["ARRIVE", "09:50"],
    ]
    assert body["ring_packages"][0]["subtitle"] == "오전 10:00 · 세종대학교"
    assert body["ring_packages"][0]["title"] == "팀 주간회의"
    assert body["ring_packages"][0]["departAt"] == ms(kst(22, 540))
    assert body["canceled_alarm_ids"] == []


@sync
async def test_group_id_chosen_in_h05_is_saved_and_no_group_means_a_personal_event():
    """9-01: H05에서 고른 그룹(created.groupId)이 새 약속의 group_id로 저장되고 내 참여 조건 · 알람이 같이 생김 — 그룹을 안 골랐으면 null(개인 일정)"""
    group = setup()
    body = await persist_appointment(
        group.ctx, **commit_args(new_draft(), {"created": {"title": "팀 회의", "group_id": "g-1"}})
    )
    (appointment,) = group.db.rows("appointments")
    assert [appointment["creator_id"], appointment["group_id"], appointment["title"]] == [
        USER,
        "g-1",
        "팀 회의",
    ]
    (participant,) = group.db.rows("appointment_participants")
    assert [participant["appointment_id"], participant["user_id"]] == [appointment["appointment_id"], USER]
    assert len(group.db.rows("alarms")) == 4
    assert all(
        a["user_id"] == USER and a["participant_id"] == participant["participant_id"]
        for a in group.db.rows("alarms")
    )
    assert body["appointment_id"] == appointment["appointment_id"]

    solo = setup()
    await persist_appointment(solo.ctx, **commit_args(new_draft(), {"created": {"title": "개인 약속"}}))
    assert solo.db.rows("appointments")[0]["group_id"] is None


@sync
async def test_destination_radius_is_saved_as_dest_radius_m_or_null():
    """새 약속: destination의 radiusM이 appointments.dest_radius_m으로 저장됨, 없으면 null"""
    with_radius = setup()
    await persist_appointment(
        with_radius.ctx,
        **commit_args(
            new_draft(destination=PlaceInput(name="세종대학교", lat=37.55, lng=127.07, radius_m=250))
        ),
    )
    assert with_radius.db.rows("appointments")[0]["dest_radius_m"] == 250

    none = setup()
    await persist_appointment(none.ctx, **commit_args(new_draft()))
    assert none.db.rows("appointments")[0]["dest_radius_m"] is None


@sync
async def test_manual_time_and_level_are_saved_as_manual_and_edit_logs_get_the_alarm_id():
    """직접 고른 시각 · 강도는 MANUAL로, 고친 값 기록에 알람 id가 붙음"""
    env = setup()
    draft = new_draft(manual_times={"WAKE": kst(22, 450)}, levels={"vibration": 2})
    await persist_appointment(
        env.ctx,
        **commit_args(
            draft,
            {
                "edits": [
                    EditInput(
                        screen="H05",
                        field="scheduled_at",
                        alarm_type="WAKE",
                        auto_value="a",
                        edited_value="b",
                    ),
                    EditInput(screen="H05", field="buffer_minutes", auto_value=10, edited_value=15),
                ]
            },
        ),
    )

    wake = next(a for a in env.db.rows("alarms") if a["alarm_type"] == "WAKE")
    # 진동을 직접 고르면 vibration 칸에, intensity(전체 세기)는 자동(보통) 그대로
    assert [
        hm(wake["scheduled_at"]),
        wake["time_source"],
        wake["intensity_source"],
        wake["intensity"],
        wake["vibration"],
    ] == [
        "07:30",
        "MANUAL",
        "MANUAL",
        "NORMAL",
        "STRONG_CONTINUOUS",
    ]
    prepare = next(a for a in env.db.rows("alarms") if a["alarm_type"] == "PREPARE")
    assert prepare["vibration"] == "STRONG_CONTINUOUS"  # 한 일정의 강도라 모든 종류에 같은 값
    assert prepare["time_source"] == "AUTO"

    logs = env.db.rows("user_edit_logs")
    assert len(logs) == 2
    assert [logs[0]["alarm_id"], logs[0]["alarm_type"], logs[0]["user_id"], logs[0]["appointment_id"]] == [
        wake["alarm_id"],
        "WAKE",
        USER,
        env.db.rows("appointments")[0]["appointment_id"],
    ]
    assert logs[1]["alarm_id"] is None


def saved_state(env):
    appointment = env.db.rows("appointments")[0]
    participant = env.db.rows("appointment_participants")[0]
    existing = [dict(a) for a in env.db.rows("alarms")]
    return appointment, participant, existing


@sync
async def test_resaving_the_same_appointment_updates_removes_missing_kinds_and_reports_canceled_ids():
    """같은 약속을 다시 저장: 있던 알람은 고치고, 빠진 종류는 지우고, 지운 알람은 canceled_alarm_ids로"""
    env = setup()
    first = await persist_appointment(env.ctx, **commit_args(new_draft()))
    appointment, participant, existing = saved_state(env)

    draft = PlanDraft(
        appointment_id=appointment["appointment_id"],
        travel_minutes=50,
        travel_source="API",
        prepare_minutes=60,
        before_prepare_minutes=30,
        kinds=["WAKE"],
    )
    inputs = inputs_of(draft, appointment=appointment, participant=participant, existing_alarms=existing)
    plan = compute_plan(inputs, API)
    second = await persist_appointment(
        env.ctx, plan=plan, inputs=inputs, draft=draft, replace_existing=True, edits=[], time_zone=TZ
    )

    assert len(env.db.rows("appointments")) == 1  # 약속은 다시 안 만들고 안 고침
    assert len(env.db.rows("appointment_participants")) == 1
    assert sorted(a["alarm_type"] for a in env.db.rows("alarms")) == ["ARRIVE", "WAKE"]
    removed = sorted(p["alarmId"] for p in first["ring_packages"] if p["type"] in ("PREPARE", "DEPARTURE"))
    assert sorted(second["canceled_alarm_ids"]) == removed
    assert [p["type"] for p in second["ring_packages"]] == ["WAKE", "ARRIVE"]
    assert (
        second["ring_packages"][0]["alarmId"] == first["ring_packages"][0]["alarmId"]
    )  # 같은 알람 id를 이어 씀


@sync
async def test_replace_existing_false_keeps_kinds_that_were_not_picked():
    """replace_existing false면 고르지 않은 종류를 지우지 않음"""
    env = setup()
    await persist_appointment(env.ctx, **commit_args(new_draft()))
    appointment, participant, existing = saved_state(env)
    draft = PlanDraft(
        appointment_id=appointment["appointment_id"],
        travel_minutes=50,
        travel_source="API",
        prepare_minutes=60,
        before_prepare_minutes=30,
        kinds=["WAKE"],
    )
    inputs = inputs_of(draft, appointment=appointment, participant=participant, existing_alarms=existing)
    body = await persist_appointment(
        env.ctx,
        plan=compute_plan(inputs, API),
        inputs=inputs,
        draft=draft,
        replace_existing=False,
        edits=[],
        time_zone=TZ,
    )
    assert len(env.db.rows("alarms")) == 4
    assert body["canceled_alarm_ids"] == []


@sync
async def test_an_event_without_alarms_creates_only_the_appointment_and_participant():
    """알람 없는 일정: 약속 · 참여 조건만 만들고 알람은 없음"""
    env = setup()
    body = await persist_appointment(env.ctx, **commit_args(new_draft(kinds=[])))
    assert len(env.db.rows("appointments")) == 1
    assert len(env.db.rows("appointment_participants")) == 1
    assert len(env.db.rows("alarms")) == 0
    assert [body["ring_packages"], body["canceled_alarm_ids"]] == [[], []]


@sync
async def test_a_past_wake_is_saved_off_left_out_of_the_package_and_warned():
    """지난 기상은 꺼서 저장하고 꾸러미에서 빼고 경고"""
    env = setup()
    late = CommitCtx(user_id=USER, user_db=env.db, service=env.db, now=kst(22, 478))  # 기상 8:00 < 7:58 + 5분
    inputs = inputs_of(new_draft(), now=late.now)
    body = await persist_appointment(
        late,
        plan=compute_plan(inputs, API),
        inputs=inputs,
        created={"title": "t"},
        draft=new_draft(),
        replace_existing=True,
        edits=[],
        time_zone=TZ,
    )
    assert next(a for a in env.db.rows("alarms") if a["alarm_type"] == "WAKE")["is_enabled"] is False
    assert [p["type"] for p in body["ring_packages"]] == ["PREPARE", "DEPARTURE", "ARRIVE"]
    assert [w["code"] for w in body["warnings"]] == ["PAST_ALARM_DISABLED"]


@sync
async def test_blocked_saves_leave_the_db_untouched():
    """저장 막는 경우: 출발까지 지남 · 순서 어긋남 · 서버가 구한 CACHED/ESTIMATE 이동시간 — 아무것도 안 씀"""
    env = setup()
    gone = CommitCtx(user_id=USER, user_db=env.db, service=env.db, now=kst(22, 545))
    inputs = inputs_of(new_draft(), now=gone.now)
    with pytest.raises(AppError) as info:
        await persist_appointment(
            gone,
            plan=compute_plan(inputs, API),
            inputs=inputs,
            created={"title": "t"},
            draft=new_draft(),
            replace_existing=True,
            edits=[],
            time_zone=TZ,
        )
    assert info.value.code == "UNPROCESSABLE"
    assert info.value.details["reason"] == "DEPARTURE_PASSED"

    order = inputs_of(new_draft(manual_times={"WAKE": kst(22, 600)}))
    with pytest.raises(AppError) as info:
        await persist_appointment(
            env.ctx,
            plan=compute_plan(order, API),
            inputs=order,
            created={"title": "t"},
            draft=order.draft,
            replace_existing=True,
            edits=[],
            time_zone=TZ,
        )
    assert info.value.code == "UNPROCESSABLE"
    assert info.value.details["reason"] == "ORDER"

    cached = ResolvedTravel(
        chosen=TravelChoice(45, "CACHED", False, "", sample_count=3), auto=None, warnings=[]
    )
    plan = compute_plan(inputs_of(new_draft()), cached)
    with pytest.raises(AppError) as info:
        assert_persistable(plan)
    assert info.value.code == "ROUTE_UNAVAILABLE"
    assert info.value.details["fallback"]["source"] == "CACHED"
    estimate = ResolvedTravel(chosen=TravelChoice(33, "ESTIMATE", True, ""), auto=None, warnings=[])
    with pytest.raises(AppError):
        assert_persistable(compute_plan(inputs_of(new_draft()), estimate))
    assert env.db.calls == []  # 어느 것도 DB를 건드리지 않음


@sync
async def test_a_failure_midway_removes_the_newly_created_appointment():
    """중간에 실패하면 이번에 새로 만든 약속을 지움 (알람 만들기 실패)"""
    env = setup()
    env.db.fail_on = [{"table": "alarms", "op": "insert"}]
    with pytest.raises(AppError):
        await persist_appointment(env.ctx, **commit_args(new_draft()))
    assert len(env.db.rows("appointments")) == 0
    assert len(env.db.rows("appointment_participants")) == 0
    assert len(env.db.rows("alarms")) == 0


def group_appointment(db, importance="NORMAL", title="공동"):
    db.rows("appointments").append(
        {
            "appointment_id": "ap-group",
            "creator_id": "someone",
            "group_id": "g1",
            "title": title,
            "appointment_at": iso_z(kst(22, 600)),
            "dest_name": "세종대학교",
            "importance": importance,
        }
    )
    return db.rows("appointments")[0]


@sync
async def test_adding_my_participation_to_an_existing_group_event_that_fails_removes_only_my_participation():
    """기존 약속에 내 참여 조건을 더하다 실패하면 참여 조건만 지우고 약속은 그대로 (공동 약속)"""
    env = setup()
    appointment = group_appointment(env.db)
    draft = PlanDraft(appointment_id="ap-group", travel_minutes=50, travel_source="API", prepare_minutes=60)
    inputs = inputs_of(draft, appointment=appointment)
    env.db.fail_on = [{"table": "alarms", "op": "insert"}]
    with pytest.raises(AppError):
        await persist_appointment(
            env.ctx,
            plan=compute_plan(inputs, API),
            inputs=inputs,
            draft=draft,
            replace_existing=True,
            edits=[],
            time_zone=TZ,
        )
    assert len(env.db.rows("appointments")) == 1
    assert len(env.db.rows("appointment_participants")) == 0


@sync
async def test_an_existing_group_event_is_not_touched_only_my_part_is_made():
    """기존 약속(공동 약속)이면 약속은 건드리지 않고 내 몫만 만듦"""
    env = setup()
    appointment = group_appointment(env.db, importance="HIGH", title="공동 회의")
    draft = PlanDraft(
        appointment_id="ap-group",
        travel_minutes=30,
        travel_source="MANUAL",
        prepare_minutes=40,
        before_prepare_minutes=10,
    )
    inputs = inputs_of(draft, appointment=appointment)
    manual = ResolvedTravel(chosen=TravelChoice(30, "MANUAL", False, ""), auto=None, warnings=[])
    body = await persist_appointment(
        env.ctx,
        plan=compute_plan(inputs, manual),
        inputs=inputs,
        draft=draft,
        replace_existing=True,
        edits=[],
        time_zone=TZ,
    )
    assert len(env.db.rows("appointments")) == 1
    assert env.db.rows("appointments")[0]["creator_id"] == "someone"
    (participant,) = env.db.rows("appointment_participants")
    assert [participant["user_id"], participant["travel_source"], participant["est_travel_minutes"]] == [
        USER,
        "MANUAL",
        30,
    ]
    assert body["ring_packages"][0]["title"] == "공동 회의"
    assert len(body["ring_packages"]) == 4
    assert env.db.rows("alarms")[0]["intensity"] == "STRONG"  # 매우 중요(HIGH) → 강


# ---- 일정 없는 알람 ----


@sync
async def test_standalone_alarm_makes_one_wake_alarm_with_recurrence_and_manual_time_source():
    """일정 없는 알람(D46): 약속 · 루틴 없이 기상 알람 하나 + 반복 규칙, 시각 출처 MANUAL"""
    env = setup()
    body = await commit_standalone(
        env.ctx,
        alarm={"scheduled_at": kst(23, 420), "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"},
        edits=[],
        time_zone=TZ,
    )
    (alarm,) = env.db.rows("alarms")
    assert [
        alarm["participant_id"],
        alarm["routine_id"],
        alarm["alarm_type"],
        alarm["recurrence_rule"],
        alarm["time_source"],
        alarm["intensity_source"],
        alarm["intensity"],
        alarm["is_enabled"],
    ] == [None, None, "WAKE", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "MANUAL", "AUTO", "NORMAL", True]
    assert len(env.db.rows("appointments")) == 0
    assert [[p["title"], p["subtitle"], p["type"], at_hm(p["atMillis"])] for p in body["ring_packages"]] == [
        ["알람", "오전 7:00", "WAKE", "07:00"]
    ]
    assert body["canceled_alarm_ids"] == []


@sync
async def test_standalone_alarm_follows_wake_sensitivity():
    """일정 없는 알람도 잠귀를 따름: 깊음이면 자동 강(STRONG), 가벼움이면 보통, 고른 칸은 그대로 (학습 기초 b)"""
    heavy = setup()
    heavy.db.rows("profiles").append({"user_id": USER, **PROFILE, "wake_sensitivity": "HEAVY"})
    await commit_standalone(heavy.ctx, alarm={"scheduled_at": kst(23, 420)}, edits=[], time_zone=TZ)
    assert [heavy.db.rows("alarms")[0]["intensity"], heavy.db.rows("alarms")[0]["intensity_source"]] == [
        "STRONG",
        "AUTO",
    ]

    light = setup()
    light.db.rows("profiles").append({"user_id": USER, **PROFILE, "wake_sensitivity": "LIGHT"})
    await commit_standalone(light.ctx, alarm={"scheduled_at": kst(23, 420)}, edits=[], time_zone=TZ)
    assert light.db.rows("alarms")[0]["intensity"] == "NORMAL"  # 약이 아니라 보통 (D69)

    picked = setup()
    picked.db.rows("profiles").append({"user_id": USER, **PROFILE, "wake_sensitivity": "HEAVY"})
    await commit_standalone(
        picked.ctx, alarm={"scheduled_at": kst(23, 420), "levels": {"vibration": 0}}, edits=[], time_zone=TZ
    )
    # D1 A: 진동 축을 직접 고르면 vibration 칸만 바뀌고 intensity는 전체 세기라 잠귀대로(강) 그대로
    (picked_row,) = picked.db.rows("alarms")
    assert [picked_row["intensity"], picked_row["vibration"], picked_row["intensity_source"]] == [
        "STRONG",
        "NONE",
        "MANUAL",
    ]


@sync
async def test_editing_a_standalone_alarm_off_cancels_it_and_recurrence_is_kept_unless_sent():
    """일정 없는 알람 고치기: 끄면 꾸러미에서 빠지고 canceled_alarm_ids에, 반복 규칙은 안 보내면 유지"""
    env = setup()
    created = await commit_standalone(
        env.ctx, alarm={"scheduled_at": kst(23, 420), "recurrence_rule": "FREQ=DAILY"}, edits=[], time_zone=TZ
    )
    alarm_id = created["alarm_id"]
    off = await commit_standalone(
        env.ctx, alarm={"alarm_id": alarm_id, "is_enabled": False}, edits=[], time_zone=TZ
    )
    assert env.db.rows("alarms")[0]["is_enabled"] is False
    assert env.db.rows("alarms")[0]["recurrence_rule"] == "FREQ=DAILY"
    assert [off["ring_packages"], off["canceled_alarm_ids"]] == [[], [alarm_id]]

    on = await commit_standalone(
        env.ctx,
        alarm={
            "alarm_id": alarm_id,
            "is_enabled": True,
            "scheduled_at": kst(23, 450),
            "recurrence_rule": None,
            "levels": {"vibration": 2},
        },
        edits=[],
        time_zone=TZ,
    )
    row = env.db.rows("alarms")[0]
    assert [row["recurrence_rule"], row["intensity"], row["vibration"], row["intensity_source"]] == [
        None,
        "NORMAL",
        "STRONG_CONTINUOUS",
        "MANUAL",
    ]
    assert [p["alarmId"] for p in on["ring_packages"]] == [alarm_id]
    package = on["ring_packages"][0]
    assert [package["intensity"], package["vibration"], package["soundKey"]] == [
        "NORMAL",
        "STRONG_CONTINUOUS",
        "theme_default",
    ]  # 꾸러미에는 해석된 값
    assert len(env.db.rows("alarms")) == 1


@sync
async def test_standalone_rejects_past_times_and_other_peoples_or_attached_alarms():
    """일정 없는 알람: 이미 지난 시각은 거절(끄는 요청은 허용), 남의 알람이나 약속에 달린 알람은 못 고침"""
    env = setup()
    with pytest.raises(AppError) as info:
        await commit_standalone(env.ctx, alarm={"scheduled_at": kst(22, 300)}, edits=[], time_zone=TZ)
    assert info.value.code == "UNPROCESSABLE"
    with pytest.raises(AppError):
        await commit_standalone(env.ctx, alarm={}, edits=[], time_zone=TZ)
    assert len(env.db.rows("alarms")) == 0

    env.db.rows("alarms").append(
        {
            "alarm_id": "other",
            "user_id": "someone-else",
            "participant_id": None,
            "routine_id": None,
            "alarm_type": "WAKE",
            "scheduled_at": iso_z(kst(23, 420)),
        }
    )
    env.db.rows("alarms").append(
        {
            "alarm_id": "attached",
            "user_id": USER,
            "participant_id": "pt",
            "routine_id": None,
            "alarm_type": "WAKE",
            "scheduled_at": iso_z(kst(23, 420)),
        }
    )
    for alarm_id in ("other", "attached"):
        with pytest.raises(AppError) as info:
            await commit_standalone(
                env.ctx, alarm={"alarm_id": alarm_id, "is_enabled": False}, edits=[], time_zone=TZ
            )
        assert info.value.code == "NOT_FOUND"


@sync
async def test_standalone_without_time_only_changes_level_or_enable_and_a_newly_sent_past_time_is_422():
    """일정 없는 알람: 시각 없이 강도만 · 켜기만 고치면 저장돼 있던 옛 시각이 지났어도 통과, 새로 보낸 지난 시각은 422"""
    env = setup()
    # 반복 알람의 첫 회차가 지나 옛 시각이 과거로 남은 상태 (D4 전)
    env.db.rows("alarms").append(
        {
            "alarm_id": "old",
            "user_id": USER,
            "participant_id": None,
            "routine_id": None,
            "alarm_type": "WAKE",
            "scheduled_at": iso_z(kst(21, 420)),
            "intensity": "NORMAL",
            "voice_tone": "FRIENDLY",
            "sound_key": "default",
            "is_enabled": False,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        }
    )

    strong = await commit_standalone(
        env.ctx, alarm={"alarm_id": "old", "levels": {"vibration": 2}}, edits=[], time_zone=TZ
    )
    row = env.db.rows("alarms")[0]
    # 진동을 강으로 직접 고르면 vibration 칸에 — 전체 세기(intensity)는 자동 단계(보통) 그대로
    assert [row["intensity"], row["vibration"], row["intensity_source"]] == [
        "NORMAL",
        "STRONG_CONTINUOUS",
        "MANUAL",
    ]
    assert strong["alarms"][0]["scheduled_at"] == iso_z(kst(21, 420))  # 시각은 그대로

    on = await commit_standalone(
        env.ctx, alarm={"alarm_id": "old", "is_enabled": True}, edits=[], time_zone=TZ
    )
    assert env.db.rows("alarms")[0]["is_enabled"] is True
    assert on["alarms"][0]["scheduled_at"] == iso_z(kst(21, 420))

    # 새로 보낸 시각이 지났으면 켜는 요청은 거절, 끄는 요청은 받음
    with pytest.raises(AppError) as info:
        await commit_standalone(
            env.ctx,
            alarm={"alarm_id": "old", "scheduled_at": kst(22, 300), "is_enabled": True},
            edits=[],
            time_zone=TZ,
        )
    assert info.value.code == "UNPROCESSABLE"
    assert info.value.details["reason"] == "PAST_TIME"
    with pytest.raises(AppError) as info:
        await commit_standalone(
            env.ctx, alarm={"alarm_id": "old", "scheduled_at": kst(22, 300)}, edits=[], time_zone=TZ
        )
    assert info.value.code == "UNPROCESSABLE"
    assert env.db.rows("alarms")[0]["scheduled_at"] == iso_z(kst(21, 420))  # 거절된 요청은 아무것도 안 바꿈
    await commit_standalone(
        env.ctx,
        alarm={"alarm_id": "old", "scheduled_at": kst(22, 300), "is_enabled": False},
        edits=[],
        time_zone=TZ,
    )
    assert env.db.rows("alarms")[0]["is_enabled"] is False


@sync
async def test_c1_w4_p80_preview_commit_persisted_rows_and_participant_minutes_match():
    """C1/W4/P80: preview -> commit -> persisted alarm rows and participant minutes match

    This covers the real persistence implementation using FakeDb, unlike the plan-only equality test.
    Both calls are given the same model output. A live network/inference consistency test is still required."""
    cases = [
        ("C1", {"model": "c1_mae", "minutes": 66}, "MODEL"),
        ("W4", {"model": "w4_mae", "minutes": 58}, "MODEL"),
        ("P80 fallback", None, "DEFAULT"),
    ]
    for label, ml_prediction, expected_source in cases:
        env = setup()
        draft = new_draft(
            prepare_minutes=None, before_prepare_minutes=20, with_wake=True
        )  # 자동 계산, 직접 입력 아님
        inputs = inputs_of(draft, ml_prediction=ml_prediction)
        preview = to_suggestion(compute_plan(inputs, API, "preview"), inputs)
        saved_plan = compute_plan(inputs, API, "save")
        assert preview["check"]["ok"] is True, label
        assert preview["prepare"]["source"] == expected_source, label

        response = await persist_appointment(
            env.ctx,
            plan=saved_plan,
            inputs=inputs,
            created={"title": "ML 저장 검증"},
            draft=draft,
            replace_existing=True,
            edits=[],
            time_zone=TZ,
        )
        (stored_participant,) = env.db.rows("appointment_participants")
        assert stored_participant["prepare_minutes"] == preview["prepare"]["minutes"], label
        assert response["participant_id"] == stored_participant["participant_id"], label

        keys = ("alarm_type", "scheduled_at", "is_enabled", "time_source")
        from_rows = sorted(
            ({k: a[k] for k in keys} for a in env.db.rows("alarms")), key=lambda a: a["alarm_type"]
        )
        from_preview = sorted(
            ({k: a[k] for k in keys} for a in preview["alarms"]), key=lambda a: a["alarm_type"]
        )
        assert from_rows == from_preview, label
        assert sorted(
            [a["alarm_type"], a["scheduled_at"], a["time_source"]] for a in response["alarms"]
        ) == sorted([a["alarm_type"], a["scheduled_at"], a["time_source"]] for a in from_rows), label
        assert len(env.db.rows("appointments")) == 1, label

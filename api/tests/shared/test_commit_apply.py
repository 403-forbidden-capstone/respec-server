"""정할 일 0-10 — 저장을 DB 함수(alarm_commit_apply) 하나로: 한 트랜잭션 · 요청 키 (본 레포 TS commit.test.ts에는 없는 서버판 새 시험)

가짜 DB의 with_commit_rpc()가 SQL 함수를 흉내 냄 (한 번에 적용, 어디서든 실패하면 전부 취소, 요청 키 재전송).
"""

import copy

import pytest

from app.core.errors import AppError
from app.shared.commit import commit_standalone, persist_appointment
from app.shared.commit_core import EditInput
from app.shared.plan_core import PlanDraft, compute_plan
from tests.fake_db import FakeDb

from .test_commit import API, TZ, USER, commit_args, inputs_of, new_draft, saved_state, setup
from .w2_helpers import kst, sync

TABLES = (
    "appointments",
    "appointment_participants",
    "alarms",
    "user_edit_logs",
    "private.request_keys",
)
KEY = "key-0123456789"
EDITS = [
    EditInput(screen="H05", field="scheduled_at", alarm_type="WAKE", auto_value="a", edited_value="b"),
    EditInput(screen="H05", field="buffer_minutes", auto_value=10, edited_value=15),
]


def snapshot(db: FakeDb):
    return {t: copy.deepcopy(db.rows(t)) for t in TABLES}


def resave_args(env, **over):
    """이미 저장한 약속을 다시 저장 — 기상만 남기고(나머지 알람은 지움) 준비 시간을 바꿔 고치기 · 지우기가 둘 다 생김"""
    appointment, participant, existing = saved_state(env)
    draft = PlanDraft(
        appointment_id=appointment["appointment_id"],
        travel_minutes=50,
        travel_source="API",
        prepare_minutes=45,
        before_prepare_minutes=30,
        kinds=["WAKE"],
    )
    inputs = inputs_of(draft, appointment=appointment, participant=participant, existing_alarms=existing)
    args = {
        "plan": compute_plan(inputs, API),
        "inputs": inputs,
        "draft": draft,
        "replace_existing": True,
        "edits": [],
        "time_zone": TZ,
    }
    return {**args, **over}


@sync
async def test_failure_while_writing_edit_logs_changes_nothing_not_even_updated_alarms():
    """고친 값 기록에서 실패하면 고친 · 지운 알람까지 전부 그대로 (예전엔 고친 알람이 남았음)"""
    env = setup()
    await persist_appointment(env.ctx, **commit_args(new_draft()))
    before = snapshot(env.db)
    env.db.fail_on = [{"table": "user_edit_logs", "op": "insert"}]
    with pytest.raises(AppError):
        await persist_appointment(
            env.ctx,
            **resave_args(
                env,
                edits=[EditInput(screen="H05", field="scheduled_at", alarm_type="WAKE", edited_value="b")],
            ),
            key=KEY,
            request_hash="h1",
        )
    assert snapshot(env.db) == before  # 요청 키 줄도 안 남음 → 같은 키로 다시 보낼 수 있음
    assert len(env.db.rows("alarms")) == 4


@sync
async def test_failure_after_creating_everything_leaves_no_appointment_participant_or_alarm():
    """새 약속: 마지막(고친 값 기록)에서 실패하면 약속 · 참여 조건 · 알람이 하나도 없음"""
    env = setup()
    env.db.fail_on = [{"table": "user_edit_logs", "op": "insert"}]
    with pytest.raises(AppError):
        await persist_appointment(env.ctx, **commit_args(new_draft(), {"edits": EDITS}))
    assert snapshot(env.db) == {t: [] for t in TABLES}


@sync
async def test_same_key_and_same_body_twice_gives_the_first_response_and_adds_no_rows():
    """같은 요청 키 · 같은 내용을 두 번 보내면 첫 응답 그대로, 한 줄도 안 늘어남"""
    env = setup()
    args = commit_args(new_draft(), {"edits": EDITS})
    first = await persist_appointment(env.ctx, **args, key=KEY, request_hash="h1")
    after_first = snapshot(env.db)
    second = await persist_appointment(env.ctx, **args, key=KEY, request_hash="h1")
    assert second == first
    assert snapshot(env.db) == after_first
    assert [len(env.db.rows(t)) for t in TABLES[:4]] == [1, 1, 4, 2]
    assert len(env.db.rpc_calls) == 2
    # 새 id를 또 만들어 보냈지만 함수가 첫 결과를 돌려줘서 응답의 id는 첫 번째 것
    assert env.db.rpc_calls[1]["args"]["p_appointment"]["appointment_id"] != first["appointment_id"]


@sync
async def test_same_key_and_same_body_replays_a_standalone_alarm_too():
    """일정 없는 알람도 같은 키로 다시 보내면 알람이 하나 더 생기지 않음"""
    env = setup()
    kwargs = {"alarm": {"scheduled_at": kst(23, 420)}, "edits": [], "time_zone": TZ}
    first = await commit_standalone(env.ctx, **kwargs, key=KEY, request_hash="h1")
    second = await commit_standalone(env.ctx, **kwargs, key=KEY, request_hash="h1")
    assert second == first
    assert len(env.db.rows("alarms")) == 1


@sync
async def test_same_key_with_a_different_body_is_a_conflict_and_changes_nothing():
    """같은 요청 키인데 내용이 다르면 CONFLICT (409) — 아무것도 안 바뀜"""
    env = setup()
    await persist_appointment(env.ctx, **commit_args(new_draft()), key=KEY, request_hash="h1")
    before = snapshot(env.db)
    with pytest.raises(AppError) as info:
        await persist_appointment(env.ctx, **commit_args(new_draft()), key=KEY, request_hash="h2")
    assert info.value.code == "CONFLICT"
    assert info.value.message == "같은 요청 키로 다른 내용을 보냈어요. 새로고침한 뒤 다시 시도해 주세요"
    assert snapshot(env.db) == before
    with pytest.raises(AppError) as info:
        await commit_standalone(
            env.ctx, alarm={"scheduled_at": kst(23, 420)}, edits=[], time_zone=TZ, key=KEY, request_hash="h9"
        )
    assert info.value.code == "CONFLICT"


@sync
async def test_the_key_is_per_user():
    """요청 키는 사용자별 — 다른 사람이 같은 키를 써도 따로 저장"""
    env = setup()
    await persist_appointment(env.ctx, **commit_args(new_draft()), key=KEY, request_hash="h1")
    other = type(env.ctx)(user_id="u-other", user_db=env.db, service=env.db, now=env.ctx.now)
    await persist_appointment(other, **commit_args(new_draft()), key=KEY, request_hash="h2")
    assert len(env.db.rows("appointments")) == 2


@sync
async def test_without_a_key_nothing_is_deduplicated_as_before():
    """키가 없으면 지금까지처럼 — 다시 보내면 새 약속이 하나 더 (키 줄도 안 만듦)"""
    env = setup()
    await persist_appointment(env.ctx, **commit_args(new_draft()))
    await persist_appointment(env.ctx, **commit_args(new_draft()))
    assert len(env.db.rows("appointments")) == 2
    assert env.db.rows("private.request_keys") == []
    for call in env.db.rpc_calls:
        assert call["args"]["p_key"] is None
        assert call["args"]["p_request_hash"] is None


@sync
async def test_a_missing_participant_row_is_not_found():
    """고칠 참여 조건이 내 것이 아니거나 없으면(P0002) NOT_FOUND — 아무것도 안 바뀜"""
    env = setup()
    await persist_appointment(env.ctx, **commit_args(new_draft()))
    before = snapshot(env.db)
    args = resave_args(env)
    args["inputs"].participant = {**args["inputs"].participant, "participant_id": "nope"}
    with pytest.raises(AppError) as info:
        await persist_appointment(env.ctx, **args)
    assert info.value.code == "NOT_FOUND"
    assert snapshot(env.db) == before


@sync
async def test_updates_are_limited_to_my_own_rows():
    """다른 사람의 알람 id를 고치려 하면(user_id 조건에 안 맞음) 못 찾음 — 남의 알람은 그대로"""
    env = setup()
    await persist_appointment(env.ctx, **commit_args(new_draft()))
    for a in env.db.rows("alarms"):
        a["user_id"] = "u-someone-else"
    before = snapshot(env.db)
    with pytest.raises(AppError) as info:
        await persist_appointment(env.ctx, **resave_args(env))
    assert info.value.code == "NOT_FOUND"
    assert snapshot(env.db) == before


@sync
async def test_rpc_payload_for_a_new_appointment_matches_the_spec():
    """새 약속 저장이 DB 함수에 보내는 인자 모양 (키 이름 · 값 모양 · id 미리 만들기)"""
    env = setup()
    body = await persist_appointment(
        env.ctx, **commit_args(new_draft(), {"edits": EDITS}), key=KEY, request_hash="h1"
    )
    (call,) = env.db.rpc_calls
    a = call["args"]
    assert call["name"] == "alarm_commit_apply"
    assert sorted(a) == sorted(
        [
            "p_user_id",
            "p_key",
            "p_request_hash",
            "p_mode",
            "p_appointment",
            "p_appointment_ref",
            "p_participant",
            "p_alarm_updates",
            "p_alarm_inserts",
            "p_alarm_removes",
            "p_edit_logs",
            "p_before",
        ]
    )
    assert [a["p_user_id"], a["p_key"], a["p_request_hash"], a["p_mode"]] == [USER, KEY, "h1", "APPOINTMENT"]
    assert a["p_appointment_ref"] is None
    assert sorted(a["p_appointment"]) == sorted(
        [
            "appointment_id",
            "creator_id",
            "group_id",
            "title",
            "appointment_at",
            "dest_name",
            "dest_lat",
            "dest_lng",
            "dest_radius_m",
            "importance",
            "recurrence_rule",
        ]
    )
    assert a["p_appointment"]["appointment_id"] == body["appointment_id"]
    assert a["p_participant"]["is_new"] is True
    assert a["p_participant"]["participant_id"] == body["participant_id"]
    assert sorted(a["p_participant"]["fields"]) == sorted(
        [
            "origin_name",
            "origin_lat",
            "origin_lng",
            "transport_type",
            "est_travel_minutes",
            "travel_source",
            "prepare_minutes",
            "buffer_minutes",
        ]
    )
    assert a["p_alarm_updates"] == [] and a["p_alarm_removes"] == [] and a["p_before"] == []
    inserts = a["p_alarm_inserts"]
    assert [i["alarm_type"] for i in inserts] == ["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]
    assert all(
        i["user_id"] == USER and i["participant_id"] == body["participant_id"] and i["alarm_id"]
        for i in inserts
    )
    assert len({i["alarm_id"] for i in inserts}) == 4
    logs = a["p_edit_logs"]
    assert [lg["alarm_id"] for lg in logs] == [inserts[0]["alarm_id"], None]  # 미리 만든 알람 id로 가리킴
    assert all(lg["appointment_id"] == body["appointment_id"] and lg["user_id"] == USER for lg in logs)
    assert [al["alarm_id"] for al in body["alarms"]] == [i["alarm_id"] for i in inserts]


@sync
async def test_rpc_payload_for_resave_and_standalone_matches_the_spec():
    """다시 저장(고치기 · 지우기)과 일정 없는 알람의 인자 모양"""
    env = setup()
    await persist_appointment(env.ctx, **commit_args(new_draft()))
    existing = saved_state(env)[2]
    env.db.rpc_calls.clear()
    await persist_appointment(env.ctx, **resave_args(env))
    a = env.db.rpc_calls[0]["args"]
    assert [
        a["p_key"],
        a["p_request_hash"],
        a["p_mode"],
        a["p_appointment"],
        a["p_participant"]["is_new"],
    ] == [
        None,
        None,
        "APPOINTMENT",
        None,
        False,
    ]
    assert sorted(a["p_appointment_ref"]) == ["appointment_at", "appointment_id", "title"]
    by_type = {e["alarm_type"]: e["alarm_id"] for e in existing}
    assert [u["alarm_id"] for u in a["p_alarm_updates"]] == [
        by_type["WAKE"],
        by_type["ARRIVE"],
    ]  # 남는 종류는 고침
    assert all(u["patch"] for u in a["p_alarm_updates"])
    assert a["p_alarm_inserts"] == []
    assert a["p_alarm_removes"] == [by_type["PREPARE"], by_type["DEPARTURE"]]  # 빠진 종류는 지움
    assert a["p_before"] == existing

    env.db.rpc_calls.clear()
    await commit_standalone(env.ctx, alarm={"scheduled_at": kst(23, 420)}, edits=[], time_zone=TZ)
    s = env.db.rpc_calls[0]["args"]
    assert [s["p_mode"], s["p_appointment"], s["p_appointment_ref"], s["p_participant"]] == [
        "STANDALONE",
        None,
        None,
        None,
    ]
    (inserted,) = s["p_alarm_inserts"]
    assert [inserted["participant_id"], inserted["routine_id"], inserted["user_id"]] == [None, None, USER]
    assert s["p_alarm_updates"] == [] and s["p_before"] == [] and s["p_edit_logs"] == []

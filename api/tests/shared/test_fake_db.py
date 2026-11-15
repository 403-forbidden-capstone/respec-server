"""`tests/fake_db.py`(fake-db.ts 서버판) 자체 시험 — fake-db.ts 머리 주석에 적힌 동작을 하나씩 확인 (원본에는 시험 파일이 없음)"""

import asyncio
from datetime import UTC, datetime

from tests.fake_db import FakeDb


def run(awaitable):
    async def go():
        return await awaitable

    return asyncio.run(go())


def seeded() -> FakeDb:
    db = FakeDb()
    db.tables["alarms"] = [
        {
            "alarm_id": "a1",
            "user_id": "u1",
            "alarm_type": "WAKE",
            "scheduled_at": "2026-11-15T07:00:00.000Z",
            "n": 1,
        },
        {
            "alarm_id": "a2",
            "user_id": "u1",
            "alarm_type": "DEPARTURE",
            "scheduled_at": "2026-11-15T08:30:00.000Z",
            "n": 2,
        },
        {
            "alarm_id": "a3",
            "user_id": "u2",
            "alarm_type": "WAKE",
            "scheduled_at": "2026-11-16T07:00:00.000Z",
            "n": 3,
        },
    ]
    return db


# --- 기본 쿼리 ---


def test_filters_eq_neq_is_in_not_gt_gte_lt_lte():
    db = seeded()
    rows = lambda q: [r["alarm_id"] for r in run(q).data]  # noqa: E731
    assert rows(db.from_("alarms").select("*").eq("user_id", "u1")) == ["a1", "a2"]
    assert rows(db.from_("alarms").select("*").neq("user_id", "u1")) == ["a3"]
    assert rows(db.from_("alarms").select("*").in_("alarm_id", ["a1", "a3"])) == ["a1", "a3"]
    assert rows(db.from_("alarms").select("*").is_("missing_col", None)) == ["a1", "a2", "a3"]
    assert rows(db.from_("alarms").select("*").not_("n", "is", None)) == ["a1", "a2", "a3"]
    assert rows(db.from_("alarms").select("*").gt("n", 1)) == ["a2", "a3"]
    assert rows(db.from_("alarms").select("*").gte("n", 2)) == ["a2", "a3"]
    assert rows(db.from_("alarms").select("*").lt("n", 2)) == ["a1"]
    assert rows(db.from_("alarms").select("*").lte("n", 2)) == ["a1", "a2"]


def test_timestamps_compare_as_instants_not_strings():
    db = seeded()
    # 문자열로 비교하면 '+09:00' 쪽이 다르게 나오는 경우 — 시각으로 비교
    result = run(db.from_("alarms").select("alarm_id").gte("scheduled_at", "2026-11-15T17:30:00+09:00"))
    assert [r["alarm_id"] for r in result.data] == ["a2", "a3"]
    result = run(db.from_("alarms").select("alarm_id").lt("scheduled_at", "2026-11-15T17:30:00+09:00"))
    assert [r["alarm_id"] for r in result.data] == ["a1"]


def test_eq_is_strict_like_js():
    db = seeded()
    assert run(db.from_("alarms").select("*").eq("n", "1")).data == []  # '1' !== 1
    assert len(run(db.from_("alarms").select("*").eq("n", 1)).data) == 1


def test_order_and_limit():
    db = seeded()
    ids = lambda q: [r["alarm_id"] for r in run(q).data]  # noqa: E731
    assert ids(db.from_("alarms").select("*").order("n", ascending=False)) == ["a3", "a2", "a1"]
    assert ids(db.from_("alarms").select("*").order("scheduled_at").limit(2)) == ["a1", "a2"]


def test_single_and_maybe_single():
    db = seeded()
    one = run(db.from_("alarms").select("*").eq("alarm_id", "a1").single())
    assert one.data["alarm_id"] == "a1" and one.error is None
    none = run(db.from_("alarms").select("*").eq("alarm_id", "zz").single())
    assert none.data is None and none.error["code"] == "PGRST116"
    many = run(db.from_("alarms").select("*").eq("user_id", "u1").single())
    assert many.error["code"] == "PGRST116"
    maybe_none = run(db.from_("alarms").select("*").eq("alarm_id", "zz").maybe_single())
    assert maybe_none.data is None and maybe_none.error is None
    maybe_many = run(db.from_("alarms").select("*").eq("user_id", "u1").maybe_single())
    assert maybe_many.error["code"] == "PGRST116"


def test_count_and_head():
    db = seeded()
    counted = run(db.from_("alarms").select("*", count="exact").eq("user_id", "u1"))
    assert len(counted.data) == 2 and counted.count == 2
    head = run(db.from_("alarms").select("alarm_id", count="exact", head=True).eq("user_id", "u1"))
    assert head.data is None and head.count == 2 and head.error is None
    assert run(db.from_("alarms").select("*")).count is None


def test_reads_return_copies():
    db = FakeDb()
    db.tables["conversation_sessions"] = [{"session_id": "s", "extracted_data": {"a": {"b": 1}}}]
    got = run(db.from_("conversation_sessions").select("*")).data
    got[0]["extracted_data"]["a"]["b"] = 99
    assert db.tables["conversation_sessions"][0]["extracted_data"]["a"]["b"] == 1


# --- 쓰기 ---


def test_insert_generates_primary_key_and_defaults():
    db = FakeDb()
    result = run(db.from_("todos").insert({"user_id": "u1", "title": "t"}).select("*"))
    row = result.data[0]
    assert row["todo_id"] == "00000000-0000-4000-8000-000000000001"
    assert row["is_done"] is False
    run(db.from_("todos").insert([{"title": "a"}, {"title": "b", "todo_id": "mine"}]))
    assert [r["todo_id"] for r in db.rows("todos")][1:] == ["00000000-0000-4000-8000-000000000002", "mine"]
    # select가 없으면 data는 None
    assert run(db.from_("todos").insert({"title": "z"})).data is None


def test_insert_copies_payload_jsonb():
    db = FakeDb()
    payload = {"title": "t", "tags": ["a"]}
    run(db.from_("todos").insert(payload))
    payload["tags"].append("b")
    assert db.rows("todos")[0]["tags"] == ["a"]


def test_update_and_delete_return_rows_only_with_select():
    db = seeded()
    result = run(db.from_("alarms").update({"n": 10}).eq("user_id", "u1").select("*"))
    assert [r["n"] for r in result.data] == [10, 10]
    assert run(db.from_("alarms").update({"n": 11}).eq("alarm_id", "a3")).data is None
    assert db.rows("alarms")[2]["n"] == 11
    gone = run(db.from_("alarms").delete().eq("alarm_id", "a1").select("*"))
    assert [r["alarm_id"] for r in gone.data] == ["a1"]
    assert [r["alarm_id"] for r in db.rows("alarms")] == ["a2", "a3"]


def test_alarms_unique_participant_and_type_rolls_back_the_whole_insert():
    db = FakeDb()
    run(db.from_("alarms").insert({"participant_id": "p1", "alarm_type": "WAKE", "user_id": "u"}))
    result = run(
        db.from_("alarms").insert(
            [
                {"participant_id": "p2", "alarm_type": "WAKE", "user_id": "u"},
                {"participant_id": "p1", "alarm_type": "WAKE", "user_id": "u"},
            ]
        )
    )
    assert result.error["code"] == "23505"
    assert "alarms_participant_type_key" in result.error["message"]
    assert len(db.rows("alarms")) == 1  # p2도 되돌림
    # 다른 종류는 가능
    assert (
        run(
            db.from_("alarms").insert({"participant_id": "p1", "alarm_type": "DEPARTURE", "user_id": "u"})
        ).error
        is None
    )


def test_cascade_delete_appointments_participants_alarms():
    db = FakeDb()
    db.tables["appointments"] = [{"appointment_id": "ap1"}, {"appointment_id": "ap2"}]
    db.tables["appointment_participants"] = [
        {"participant_id": "p1", "appointment_id": "ap1"},
        {"participant_id": "p2", "appointment_id": "ap2"},
    ]
    db.tables["alarms"] = [
        {"alarm_id": "a1", "participant_id": "p1"},
        {"alarm_id": "a2", "participant_id": "p2"},
        {"alarm_id": "a3", "participant_id": None},
    ]
    run(db.from_("appointments").delete().eq("appointment_id", "ap1"))
    assert [p["participant_id"] for p in db.rows("appointment_participants")] == ["p2"]
    assert [a["alarm_id"] for a in db.rows("alarms")] == ["a2", "a3"]
    run(db.from_("appointment_participants").delete().eq("participant_id", "p2"))
    assert [a["alarm_id"] for a in db.rows("alarms")] == ["a3"]


# --- upsert ---


def test_upsert_merges_existing_by_on_conflict_columns():
    db = FakeDb()
    db.tables["push_devices"] = [{"user_id": "u", "device_id": "d", "token": "old"}]
    result = run(
        db.from_("push_devices")
        .upsert({"user_id": "u", "device_id": "d", "token": "new"}, on_conflict="user_id,device_id")
        .select("*")
    )
    assert len(db.rows("push_devices")) == 1
    assert db.rows("push_devices")[0]["token"] == "new"
    assert result.data[0]["token"] == "new"


def test_upsert_ignore_duplicates_returns_only_new_rows():
    db = FakeDb()
    db.tables["t"] = [{"a": 1, "b": 1, "v": "old"}]
    result = run(
        db.from_("t")
        .upsert(
            [{"a": 1, "b": 1, "v": "dup"}, {"a": 1, "b": 2, "v": "new"}],
            on_conflict="a, b",
            ignore_duplicates=True,
        )
        .select("*")
    )
    assert result.data == [{"a": 1, "b": 2, "v": "new"}]
    assert db.rows("t")[0]["v"] == "old"
    assert len(db.rows("t")) == 2


def test_upsert_without_on_conflict_inserts():
    db = FakeDb()
    run(db.from_("t").upsert({"a": 1}))
    run(db.from_("t").upsert({"a": 1}))
    assert len(db.rows("t")) == 2


# --- 임베드 · 점 거르기 ---


def embed_db() -> FakeDb:
    db = FakeDb()
    db.tables["appointments"] = [
        {"appointment_id": "ap1", "title": "A", "appointment_at": "2026-11-15T09:00:00.000Z"},
        {"appointment_id": "ap2", "title": "B", "appointment_at": "2026-11-16T09:00:00.000Z"},
    ]
    db.tables["appointment_participants"] = [
        {"participant_id": "p1", "appointment_id": "ap1", "user_id": "u1"},
        {"participant_id": "p2", "appointment_id": "ap2", "user_id": "u1"},
        {"participant_id": "p3", "appointment_id": "gone", "user_id": "u1"},
    ]
    db.tables["alarms"] = [
        {"alarm_id": "a1", "participant_id": "p1", "alarm_type": "WAKE"},
        {"alarm_id": "a2", "participant_id": "p1", "alarm_type": "DEPARTURE"},
        {"alarm_id": "a3", "participant_id": "p2", "alarm_type": "WAKE"},
    ]
    return db


def test_embed_appointments_inner_with_dotted_filter_and_inner_join_drop():
    db = embed_db()
    result = run(
        db.from_("appointment_participants")
        .select("participant_id, appointments!inner(title, appointment_at)")
        .eq("user_id", "u1")
        .gte("appointments.appointment_at", "2026-11-16T00:00:00Z")
    )
    assert [r["participant_id"] for r in result.data] == ["p2"]
    assert result.data[0]["appointments"]["title"] == "B"
    # 약속이 없는 참여(p3)는 inner join이라 빠짐
    everything = run(db.from_("appointment_participants").select("participant_id, appointments!inner(title)"))
    assert [r["participant_id"] for r in everything.data] == ["p1", "p2"]


def test_embed_alarms_inner_keeps_only_matching_alarms():
    db = embed_db()
    result = run(
        db.from_("appointment_participants")
        .select("participant_id, alarms!inner(alarm_id)")
        .eq("alarms.alarm_type", "DEPARTURE")
    )
    assert [r["participant_id"] for r in result.data] == ["p1"]
    assert [a["alarm_id"] for a in result.data[0]["alarms"]] == ["a2"]


def test_dotted_filter_without_inner_embed_raises():
    db = embed_db()
    try:
        run(db.from_("appointment_participants").select("*").eq("alarms.alarm_type", "WAKE"))
    except RuntimeError as exc:
        assert "alarms!inner" in str(exc)
    else:
        raise AssertionError("임베드 없는 점 거르기는 던져야 함")


def test_left_embed_alarm_events_with_nested_participants():
    db = FakeDb()
    db.tables["alarm_events"] = [
        {"event_id": "e1", "alarm_id": "a1"},
        {"event_id": "e2", "alarm_id": "nope"},
    ]
    db.tables["alarms"] = [{"alarm_id": "a1", "participant_id": "p1"}]
    db.tables["appointment_participants"] = [{"participant_id": "p1", "user_id": "u"}]
    result = run(
        db.from_("alarm_events").select("event_id, alarms(alarm_id, appointment_participants(user_id))")
    )
    assert result.data[0]["alarms"]["appointment_participants"] == {"participant_id": "p1", "user_id": "u"}
    assert result.data[1]["alarms"] is None
    flat = run(db.from_("alarm_events").select("event_id, alarms(alarm_id)"))
    assert "appointment_participants" not in flat.data[0]["alarms"]


# --- jsonb 경로 ---


def test_jsonb_arrow_filters_for_optimistic_lock():
    db = FakeDb()
    db.tables["conversation_sessions"] = [
        {
            "session_id": "s1",
            "server_state": {"rev": 3, "flag": True},
            "extracted_data": {"session": {"reference_date": "2026-11-15"}},
        },
        {"session_id": "s2", "server_state": {"rev": 4}, "extracted_data": {}},
    ]
    s = lambda q: [r["session_id"] for r in run(q).data]  # noqa: E731
    assert s(db.from_("conversation_sessions").select("*").eq("server_state->>rev", "3")) == ["s1"]
    assert s(db.from_("conversation_sessions").select("*").eq("server_state->>rev", 3)) == []  # ->> 는 글자
    assert s(db.from_("conversation_sessions").select("*").eq("server_state->rev", 4)) == ["s2"]
    assert s(
        db.from_("conversation_sessions")
        .select("*")
        .eq("extracted_data->session->>reference_date", "2026-11-15")
    ) == ["s1"]
    assert s(
        db.from_("conversation_sessions").select("*").is_("extracted_data->session->>reference_date", None)
    ) == ["s2"]
    assert s(db.from_("conversation_sessions").select("*").eq("server_state->>flag", "true")) == ["s1"]


# --- L4 표 기본값 ---


def test_l4_tables_defaults_use_db_now():
    db = FakeDb()
    db.now = datetime(2026, 11, 15, 1, 2, 3, 456000, tzinfo=UTC)
    session = run(db.from_("conversation_sessions").insert({"user_id": "u"}).select("*")).data[0]
    assert session["session_id"].startswith("00000000-0000-4000-8000-")
    assert session["started_at"] == "2026-11-15T01:02:03.456Z"
    assert session["created_at"] == "2026-11-15T01:02:03.456Z"
    assert session["ended_at"] is None and session["apply_status"] == "PENDING"
    assert session["turns"] == [] and session["server_state"] == {}
    call = run(db.from_("ai_calls").insert({"user_id": "u"}).select("*")).data[0]
    assert (
        call["call_id"]
        and call["created_at"] == "2026-11-15T01:02:03.456Z"
        and call["flags"] == []
        and call["io"] is None
    )
    question = run(db.from_("user_questions").insert({"user_id": "u"}).select("*")).data[0]
    assert (
        question["question_id"]
        and question["asked_at"] == "2026-11-15T01:02:03.456Z"
        and question["answered_at"] is None
    )


# --- fill_series 트리거 · 반복 회차 유일 제약 ---


def test_fill_series_appointments_uses_creator_timezone():
    db = FakeDb()
    db.tables["profiles"] = [{"user_id": "u-ny", "timezone": "America/New_York"}]
    seoul = run(
        db.from_("appointments")
        .insert({"creator_id": "u1", "appointment_at": "2026-11-15T16:00:00.000Z"})
        .select("*")
    ).data[0]
    assert seoul["series_id"] == seoul["appointment_id"]
    assert seoul["occurrence_on"] == "2026-11-16"  # Asia/Seoul 기본: 16:00Z = 다음 날 01:00
    ny = run(
        db.from_("appointments")
        .insert({"creator_id": "u-ny", "appointment_at": "2026-11-15T02:00:00.000Z"})
        .select("*")
    ).data[0]
    assert ny["occurrence_on"] == "2026-11-14"  # 뉴욕: 14일 21:00
    assert seoul["exception_kind"] is None and seoul["memo"] is None  # 기본값


def test_fill_series_standalone_alarm_only():
    db = FakeDb()
    standalone = run(
        db.from_("alarms").insert({"user_id": "u1", "scheduled_at": "2026-11-15T00:00:00.000Z"}).select("*")
    ).data[0]
    assert standalone["series_id"] == standalone["alarm_id"] and standalone["occurrence_on"] == "2026-11-15"
    assert standalone["snooze_limit"] == 3 and standalone["recurrence_rule"] is None
    linked = run(
        db.from_("alarms")
        .insert(
            {
                "user_id": "u1",
                "participant_id": "p",
                "alarm_type": "WAKE",
                "scheduled_at": "2026-11-15T00:00:00.000Z",
            }
        )
        .select("*")
    ).data[0]
    assert "series_id" not in linked
    routine = run(
        db.from_("alarms")
        .insert({"user_id": "u1", "routine_id": "r", "scheduled_at": "2026-11-15T00:00:00.000Z"})
        .select("*")
    ).data[0]
    assert "series_id" not in routine


def test_series_occurrence_unique_constraints():
    db = FakeDb()
    first = {
        "creator_id": "u1",
        "series_id": "s",
        "occurrence_on": "2026-11-15",
        "appointment_at": "2026-11-15T00:00:00.000Z",
    }
    assert run(db.from_("appointments").insert(first)).error is None
    dup = run(db.from_("appointments").insert(first))
    assert dup.error["code"] == "23505" and "appointments_series_occurrence_key" in dup.error["message"]
    other_user = run(db.from_("appointments").insert({**first, "creator_id": "u2"}))
    assert other_user.error is None
    alarm = {
        "user_id": "u1",
        "scheduled_at": "2026-11-15T00:00:00.000Z",
        "series_id": "x",
        "occurrence_on": "2026-11-15",
    }
    assert run(db.from_("alarms").insert(alarm)).error is None
    dup_alarm = run(db.from_("alarms").insert(alarm))
    assert "alarms_series_occurrence_key" in dup_alarm.error["message"]


# --- exception_kind ---


def test_exception_kind_defaults_null_and_roll_due_series_excludes_exceptions():
    db = FakeDb().with_roll_rpc()
    db.now = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
    base = {"creator_id": "u1", "series_id": "s", "group_id": None, "recurrence_rule": "FREQ=DAILY"}
    db.tables["appointments"] = [
        {
            **base,
            "appointment_id": "a-11",
            "occurrence_on": "2026-11-14",
            "appointment_at": "2026-11-14T00:00:00.000Z",
            "exception_kind": None,
        },
        {
            **base,
            "appointment_id": "a-12",
            "occurrence_on": "2026-11-15",
            "appointment_at": "2026-11-15T00:00:00.000Z",
            "exception_kind": "EDITED",
        },
    ]
    result = run(db.rpc("roll_due_series", {"p_now": "2026-11-15T00:00:00.000Z", "p_limit": 10}))
    # 기준 회차(template_id)는 예외가 아닌 회차 a-11, 다음 회차 기준 시각은 가장 늦은 행(a-12)
    assert result.data == [{"user_id": "u1", "series_id": "s", "kind": "APPOINTMENT", "template_id": "a-11"}]
    db.tables["appointments"][0]["exception_kind"] = "CANCELED"
    result = run(db.rpc("roll_due_series", {"p_now": "2026-11-15T00:00:00.000Z"}))
    assert result.data[0]["template_id"] == "a-12"  # 취소 회차는 기준이 아님 → EDITED가 기준


# --- rpc ---


def test_rpc_unregistered_registered_and_throwing_handlers():
    db = FakeDb()
    missing = run(db.rpc("nope", {"x": 1}))
    assert missing.data is None and missing.error == {
        "message": "function nope does not exist",
        "code": "42883",
    }
    db.rpc_handlers["ok"] = lambda args: {"echo": args}
    assert run(db.rpc("ok", {"a": 1})).data == {"echo": {"a": 1}}

    def boom(args):
        raise RuntimeError("kaput")

    db.rpc_handlers["bad"] = boom
    bad = run(db.rpc("bad"))
    assert bad.error == {"message": "kaput", "code": "XX000"}
    assert db.rpc_calls == [
        {"name": "nope", "args": {"x": 1}},
        {"name": "ok", "args": {"a": 1}},
        {"name": "bad", "args": {}},
    ]
    assert db.calls == ["rpc nope", "rpc ok", "rpc bad"]


def test_rpc_call_is_recorded_before_await():
    db = FakeDb()
    db.rpc("later")
    assert db.rpc_calls == [{"name": "later", "args": {}}]


def test_with_roll_rpc_does_not_override_existing_handlers():
    db = FakeDb()
    db.rpc_handlers["roll_due_series"] = lambda args: ["mine"]
    assert db.with_roll_rpc() is db
    assert run(db.rpc("roll_due_series")).data == ["mine"]
    assert "roll_insert_occurrence" in db.rpc_handlers


def test_roll_insert_occurrence_writes_everything_and_second_call_returns_none():
    db = FakeDb().with_roll_rpc()
    args = {
        "p_appointment": {
            "creator_id": "u1",
            "series_id": "s",
            "occurrence_on": "2026-11-15",
            "title": "수업",
            "appointment_at": "2026-11-15T01:00:00.000Z",
            "dest_name": "학교",
        },
        "p_participant": {"origin_name": "집", "prepare_minutes": 30},
        "p_alarms": [
            {"alarm_type": "WAKE", "scheduled_at": "2026-11-14T22:00:00.000Z"},
            {"alarm_type": "DEPARTURE", "scheduled_at": "2026-11-14T23:00:00.000Z", "is_enabled": False},
        ],
        "p_items": [{"title": "  우산  "}, {"title": "   "}, {"title": "x" * 150}],
    }
    appointment_id = run(db.rpc("roll_insert_occurrence", args)).data
    assert appointment_id == db.rows("appointments")[0]["appointment_id"]
    assert db.rows("appointments")[0]["importance"] == "NORMAL"
    participant = db.rows("appointment_participants")[0]
    assert (
        participant["prepare_minutes"] == 30
        and participant["buffer_minutes"] == 10
        and participant["transport_type"] == "TRANSIT"
    )
    wake, departure = db.rows("alarms")
    assert (
        wake["participant_id"] == participant["participant_id"]
        and wake["intensity"] == "NORMAL"
        and wake["is_enabled"] is True
    )
    assert departure["is_enabled"] is False
    todos = db.rows("todos")
    assert [t["title"] for t in todos] == ["우산", "x" * 100]
    assert todos[0]["due_date"] == "2026-11-15" and todos[0]["todo_type"] == "ITEM"
    assert run(db.rpc("roll_insert_occurrence", args)).data is None  # 같은 (만든 사람, 묶음, 날짜)
    missing = run(db.rpc("roll_insert_occurrence", {"p_appointment": {}}))
    assert missing.error["code"] == "XX000"


def test_roll_due_series_standalone_routine_ordering_and_limit():
    db = FakeDb().with_roll_rpc()
    db.now = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
    db.tables["alarms"] = [
        {
            "alarm_id": "al1",
            "user_id": "u1",
            "series_id": "ss",
            "occurrence_on": "2026-11-15",
            "scheduled_at": "2026-11-15T05:00:00.000Z",
            "recurrence_rule": "FREQ=DAILY",
            "participant_id": None,
            "routine_id": None,
        },
        {
            "alarm_id": "al-far",
            "user_id": "u1",
            "series_id": "far",
            "occurrence_on": "2027-01-01",
            "scheduled_at": "2027-01-01T05:00:00.000Z",
            "recurrence_rule": "FREQ=DAILY",
            "participant_id": None,
            "routine_id": None,
        },
        {
            "alarm_id": "r-step",
            "user_id": "u2",
            "routine_id": "r1",
            "participant_id": None,
            "scheduled_at": "2026-11-14T22:00:00.000Z",
            "is_enabled": True,
        },
    ]
    db.tables["routines"] = [
        {"routine_id": "r1", "user_id": "u2", "is_active": True, "repeat_days": "MON,TUE"},
        {"routine_id": "r2", "user_id": "u2", "is_active": False, "repeat_days": "MON"},
    ]
    result = run(db.rpc("roll_due_series", {"p_now": "2026-11-15T00:00:00.000Z"})).data
    assert result == [
        {
            "user_id": "u2",
            "series_id": "r1",
            "kind": "ROUTINE",
            "template_id": "r1",
        },  # 가장 급한 순 (어제 22시)
        {"user_id": "u1", "series_id": "ss", "kind": "STANDALONE", "template_id": "al1"},
    ]
    assert len(run(db.rpc("roll_due_series", {"p_now": "2026-11-15T00:00:00.000Z", "p_limit": 1})).data) == 1


def test_roll_due_series_waits_for_next_try_and_retries():
    db = FakeDb().with_roll_rpc()
    base = {
        "creator_id": "u1",
        "series_id": "s",
        "group_id": None,
        "recurrence_rule": "FREQ=DAILY",
        "appointment_id": "a1",
        "occurrence_on": "2027-06-01",
        "appointment_at": "2027-06-01T00:00:00.000Z",
        "exception_kind": None,
    }
    db.tables["appointments"] = [base]  # 14일 밖 → 평소엔 대상 아님
    args = {"p_now": "2026-11-15T00:00:00.000Z"}
    assert run(db.rpc("roll_due_series", args)).data == []
    db.tables["roll_attempts"] = [
        {
            "user_id": "u1",
            "series_id": "s",
            "occurrence_on": "2026-11-15",
            "next_try_at": "2026-11-14T00:00:00.000Z",
        }
    ]
    assert len(run(db.rpc("roll_due_series", args)).data) == 1  # 재시도 때가 지난 날짜
    db.tables["roll_attempts"][0]["next_try_at"] = "2026-11-16T00:00:00.000Z"
    assert run(db.rpc("roll_due_series", args)).data == []  # 대기 중
    db.tables["roll_attempts"][0].update({"next_try_at": "2026-11-14T00:00:00.000Z", "skipped": True})
    assert run(db.rpc("roll_due_series", args)).data == []  # 건너뜀 표시


# --- 실패 주입 · 호출 기록 ---


def test_fail_on_table_and_op_and_calls_log():
    db = seeded()
    db.fail_on.append({"table": "alarms", "op": "insert"})
    failed = run(db.from_("alarms").insert({"alarm_id": "x"}))
    assert failed.data is None and failed.error == {
        "message": "injected failure: insert alarms",
        "code": "XX000",
    }
    assert [a["alarm_id"] for a in db.rows("alarms")] == ["a1", "a2", "a3"]
    assert run(db.from_("alarms").select("*")).error is None  # 다른 동작은 정상
    assert run(db.from_("todos").insert({"title": "t"})).error is None  # 다른 표도 정상
    assert db.calls == ["insert alarms", "select alarms", "insert todos"]


def test_with_embeds_on_unknown_relation_raises():
    db = embed_db()
    try:
        run(db.from_("alarms").select("*, appointments!inner(title)"))
    except RuntimeError as exc:
        assert "아직 흉내 내지 않아요" in str(exc)
    else:
        raise AssertionError("지원 안 하는 임베드는 던져야 함")

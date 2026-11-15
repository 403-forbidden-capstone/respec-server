"""`_shared/supabase.ts` — 원본에 시험 파일 없음. supabase-js(postgrest-js)가 만드는 PostgREST 요청 모양을 MockTransport로 확인
(메서드 · 경로 · 쿼리 · 머리글 · 본문, maybeSingle 0줄 처리, 개수, 오류 · 네트워크 실패)"""

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from app.core.errors import AppError
from app.shared.supabase import Db, configure, create_service_client, create_user_client

from ..conftest import make_settings

BASE = "http://supabase.test/rest/v1"
Responder = Callable[[httpx.Request], httpx.Response]


class Env:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responder: Responder = lambda request: httpx.Response(200, json=[])

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    @property
    def params(self) -> list[tuple[str, str]]:
        return list(self.last.url.params.multi_items())

    def body(self):
        return json.loads(self.last.content)


@pytest.fixture
def env():
    e = Env()
    client = httpx.AsyncClient(transport=httpx.MockTransport(e.handler))
    configure(
        make_settings(supabase_secret_key="sb_secret_test_value", supabase_publishable_key="anon-key"),
        client,
    )
    yield e
    configure(None, None)


def run(awaitable):
    async def go():
        return await awaitable

    return asyncio.run(go())


@pytest.fixture
def db() -> Db:
    return create_service_client()


# --- 클라이언트 · 머리글 ---


def test_service_client_new_secret_key_goes_in_apikey_only(env, db):
    run(db.from_("todos").select("*"))
    h = env.last.headers
    assert h["apikey"] == "sb_secret_test_value"
    assert "authorization" not in h
    assert h["x-client-info"] == "supabase-js-node/2.117.2"
    assert h["accept-profile"] == "public"


def test_service_client_legacy_jwt_key_also_in_authorization(env):
    configure(
        make_settings(supabase_secret_key="eyJhbGciOi.legacy.key"),
        httpx.AsyncClient(transport=httpx.MockTransport(env.handler)),
    )
    run(create_service_client().from_("todos").select("*"))
    assert env.last.headers["apikey"] == "eyJhbGciOi.legacy.key"
    assert env.last.headers["authorization"] == "Bearer eyJhbGciOi.legacy.key"


def test_user_client_uses_publishable_key_and_callers_authorization(env):
    run(create_user_client("Bearer user-token").from_("todos").select("*"))
    assert env.last.headers["apikey"] == "anon-key"
    assert env.last.headers["authorization"] == "Bearer user-token"


def test_clients_not_configured_without_keys():
    configure(make_settings(supabase_secret_key=None), None)
    try:
        with pytest.raises(AppError) as a:
            create_service_client()
        assert a.value.code == "NOT_CONFIGURED"
        with pytest.raises(AppError) as b:
            create_user_client("Bearer x")
        assert b.value.code == "NOT_CONFIGURED"
    finally:
        configure(None, None)


# --- 읽기 ---


def test_select_with_filters_order_limit(env, db):
    env.responder = lambda r: httpx.Response(200, json=[{"title": "a"}])
    result = run(
        db.from_("todos")
        .select("title, due_date")
        .eq("user_id", "u1")
        .eq("is_done", False)
        .gte("created_at", "2026-01-01T00:00:00.000Z")
        .order("created_at", ascending=False)
        .order("title")
        .limit(5)
    )
    assert env.last.method == "GET"
    assert env.last.url.path == "/rest/v1/todos"
    assert env.params == [
        ("select", "title,due_date"),
        ("user_id", "eq.u1"),
        ("is_done", "eq.false"),
        ("created_at", "gte.2026-01-01T00:00:00.000Z"),
        ("order", "created_at.desc,title.asc"),
        ("limit", "5"),
    ]
    assert "prefer" not in env.last.headers
    assert result.data == [{"title": "a"}]
    assert result.error is None
    assert result.count is None


def test_select_embed_columns_pass_through_and_quoted_spaces_kept(env, db):
    run(
        db.from_("appointment_participants").select(
            "participant_id, appointments!inner( title , appointment_at )"
        )
    )
    assert env.params == [("select", "participant_id,appointments!inner(title,appointment_at)")]
    run(db.from_("t").select('"a b",c'))
    assert env.params == [("select", '"a b",c')]


def test_filter_operators(env, db):
    run(
        db.from_("t")
        .neq("a", 1)
        .gt("b", 2.0)
        .lt("c", 3)
        .lte("d", "x")
        .is_("e", None)
        .is_("f", True)
        .not_("g", "is", None)
        .in_("h", ["a", "b,c", "d(e)", 4])
    )
    assert env.params == [
        ("a", "neq.1"),
        ("b", "gt.2"),
        ("c", "lt.3"),
        ("d", "lte.x"),
        ("e", "is.null"),
        ("f", "is.true"),
        ("g", "not.is.null"),
        ("h", 'in.(a,"b,c","d(e)",4)'),
    ]


def test_jsonb_path_and_dotted_embedded_columns_are_column_names(env, db):
    run(
        db.from_("conversation_sessions")
        .select("*")
        .eq("extracted_data->session->>reference_date", "2026-11-15")
    )
    assert ("extracted_data->session->>reference_date", "eq.2026-11-15") in env.params
    run(
        db.from_("appointment_participants").select("p, alarms!inner(a)").eq("alarms.alarm_type", "DEPARTURE")
    )
    assert ("alarms.alarm_type", "eq.DEPARTURE") in env.params


def test_count_head(env, db):
    env.responder = lambda r: httpx.Response(200, headers={"content-range": "*/7"})
    result = run(db.from_("alarms").select("alarm_id", count="exact", head=True).eq("user_id", "u1"))
    assert env.last.method == "HEAD"
    assert env.last.headers["prefer"] == "count=exact"
    assert env.params == [("select", "alarm_id"), ("user_id", "eq.u1")]
    assert result.data is None
    assert result.count == 7
    assert result.error is None


def test_count_with_rows(env, db):
    env.responder = lambda r: httpx.Response(200, json=[{"a": 1}], headers={"content-range": "0-0/42"})
    result = run(db.from_("alarms").select("*", count="exact"))
    assert env.last.method == "GET"
    assert result.count == 42
    assert result.data == [{"a": 1}]


# --- single / maybe_single ---


def test_single_accept_object_json(env, db):
    env.responder = lambda r: httpx.Response(200, json={"a": 1})
    result = run(db.from_("t").select("*").eq("id", "1").single())
    assert env.last.headers["accept"] == "application/vnd.pgrst.object+json"
    assert result.data == {"a": 1}


def test_single_zero_rows_is_error_passed_through(env, db):
    body = {
        "code": "PGRST116",
        "details": "The result contains 0 rows",
        "hint": None,
        "message": "JSON object requested, multiple (or no) rows returned",
    }
    env.responder = lambda r: httpx.Response(406, json=body)
    result = run(db.from_("t").select("*").single())
    assert result.data is None
    assert result.error == body


def test_maybe_single_read_zero_one_many_rows(env, db):
    env.responder = lambda r: httpx.Response(200, json=[])
    zero = run(db.from_("t").select("*").maybe_single())
    assert env.last.headers["accept"] == "application/json"
    assert zero.data is None and zero.error is None

    env.responder = lambda r: httpx.Response(200, json=[{"a": 1}])
    one = run(db.from_("t").select("*").maybe_single())
    assert one.data == {"a": 1} and one.error is None

    env.responder = lambda r: httpx.Response(200, json=[{"a": 1}, {"a": 2}])
    many = run(db.from_("t").select("*").maybe_single())
    assert many.data is None
    assert many.error["code"] == "PGRST116"
    assert many.error["message"] == "JSON object requested, multiple (or no) rows returned"
    assert "2 rows" in many.error["details"]


def test_maybe_single_on_write_zero_rows_is_not_error(env, db):
    body = {
        "code": "PGRST116",
        "details": "The result contains 0 rows",
        "hint": None,
        "message": "JSON object requested, multiple (or no) rows returned",
    }
    env.responder = lambda r: httpx.Response(406, json=body)
    result = run(db.from_("t").update({"a": 1}).eq("id", "1").select("*").maybe_single())
    assert env.last.method == "PATCH"
    assert env.last.headers["accept"] == "application/vnd.pgrst.object+json"
    assert env.last.headers["prefer"] == "return=representation"
    assert result.data is None
    assert result.error is None


def test_maybe_single_other_errors_still_errors(env, db):
    env.responder = lambda r: httpx.Response(
        400, json={"code": "22P02", "message": "bad", "details": None, "hint": None}
    )
    result = run(db.from_("t").select("*").maybe_single())
    assert result.error["code"] == "22P02"


# --- 쓰기 ---


def test_insert_without_select_has_no_prefer_and_returns_none(env, db):
    env.responder = lambda r: httpx.Response(201)
    result = run(db.from_("todos").insert({"title": "t", "user_id": "u1"}))
    assert env.last.method == "POST"
    assert env.last.url.path == "/rest/v1/todos"
    assert env.body() == {"title": "t", "user_id": "u1"}
    assert env.last.headers["content-type"] == "application/json"
    assert env.last.headers["content-profile"] == "public"
    assert "prefer" not in env.last.headers
    assert env.params == []
    assert result.data is None and result.error is None


def test_insert_many_sends_columns_union_and_select_returns_representation(env, db):
    env.responder = lambda r: httpx.Response(201, json=[{"todo_id": "1"}, {"todo_id": "2"}])
    result = run(db.from_("todos").insert([{"a": 1, "b": 2}, {"a": 3, "c": 4}]).select("todo_id"))
    assert env.params == [("columns", '"a","b","c"'), ("select", "todo_id")]
    assert env.last.headers["prefer"] == "return=representation"
    assert env.body() == [{"a": 1, "b": 2}, {"a": 3, "c": 4}]
    assert result.data == [{"todo_id": "1"}, {"todo_id": "2"}]


def test_insert_body_keeps_korean_and_null(env, db):
    run(db.from_("todos").insert({"title": "우산", "memo": None}))
    assert env.last.content == '{"title":"우산","memo":null}'.encode()


def test_update_patch_with_filters(env, db):
    run(
        db.from_("appointments")
        .update({"recurrence_rule": None})
        .eq("creator_id", "u1")
        .not_("recurrence_rule", "is", None)
    )
    assert env.last.method == "PATCH"
    assert env.body() == {"recurrence_rule": None}
    assert env.params == [("creator_id", "eq.u1"), ("recurrence_rule", "not.is.null")]
    assert "prefer" not in env.last.headers


def test_delete_with_select_returns_rows(env, db):
    env.responder = lambda r: httpx.Response(200, json=[{"alarm_id": "a"}])
    result = run(db.from_("alarms").delete().eq("alarm_id", "a").select("alarm_id"))
    assert env.last.method == "DELETE"
    assert env.last.headers["prefer"] == "return=representation"
    assert result.data == [{"alarm_id": "a"}]


def test_upsert_merge_on_conflict(env, db):
    run(db.from_("push_devices").upsert({"user_id": "u", "device_id": "d"}, on_conflict="user_id,device_id"))
    assert env.last.method == "POST"
    assert env.params == [("on_conflict", "user_id,device_id")]
    assert env.last.headers["prefer"] == "resolution=merge-duplicates"


def test_upsert_ignore_duplicates_with_select(env, db):
    run(db.from_("t").upsert([{"k": 1}], on_conflict="k", ignore_duplicates=True).select("k"))
    assert env.last.headers["prefer"] == "resolution=ignore-duplicates,return=representation"
    assert env.params == [("on_conflict", "k"), ("columns", '"k"'), ("select", "k")]


# --- rpc ---


def test_rpc_posts_args_to_rpc_path(env, db):
    env.responder = lambda r: httpx.Response(200, json="appointment-id")
    result = run(db.rpc("roll_insert_occurrence", {"p_appointment": {"title": "x"}, "p_items": []}))
    assert env.last.method == "POST"
    assert env.last.url.path == "/rest/v1/rpc/roll_insert_occurrence"
    assert env.body() == {"p_appointment": {"title": "x"}, "p_items": []}
    assert env.last.headers["content-profile"] == "public"
    assert result.data == "appointment-id"


def test_rpc_without_args_sends_empty_object_and_null_result(env, db):
    env.responder = lambda r: httpx.Response(200, content=b"null")
    result = run(db.rpc("do_it"))
    assert env.body() == {}
    assert result.data is None and result.error is None


# --- 오류 ---


def test_db_error_is_a_result_not_an_exception(env, db):
    env.responder = lambda r: httpx.Response(
        409, json={"code": "23505", "details": "Key exists", "hint": None, "message": "duplicate key"}
    )
    result = run(db.from_("t").insert({"a": 1}))
    assert result.data is None
    assert result.error == {
        "code": "23505",
        "details": "Key exists",
        "hint": None,
        "message": "duplicate key",
    }


def test_non_json_error_body_becomes_message(env, db):
    env.responder = lambda r: httpx.Response(502, text="Bad Gateway")
    result = run(db.from_("t").select("*"))
    assert result.error["message"] == "Bad Gateway"


def test_network_failure_is_an_error_result(env, db):
    def boom(request):
        raise httpx.ConnectError("down")

    env.responder = boom
    result = run(db.from_("t").select("*"))
    assert result.data is None
    assert result.error["message"] == "ConnectError: down"
    assert result.error["code"] == ""

"""POST /fn/alarm-suggest · /fn/alarm-commit — 요청 · 응답 · 오류 모양이 Edge와 같은지 HTTP 수준에서 확인 (test_places.py와 같은 틀).

DB는 FakeDb. 로그인 토큰은 conftest의 가짜 서명 키.
순서는 Edge와 같음: 토큰 확인(401) → 입력 검사(400) → 계산 · 읽기 → 오류 번역.
"""

import hashlib
import json

import pytest

from tests.fake_db import FakeDb

from .conftest import USER_ID, bearer
from .shared.w2_helpers import iso, kst

SUGGEST = "/fn/alarm-suggest"
COMMIT = "/fn/alarm-commit"
NO_COORDS = {
    "appointment": {
        "title": "팀 주간회의",
        "appointment_at": iso(22, 600),
        "destination": {"name": "세종대학교"},
    }
}
FULL = {
    "appointment": {
        "title": "팀 주간회의",
        "appointment_at": iso(22, 600),
        "destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07},
        "importance": "NORMAL",
    },
    "participant": {
        "origin": {"name": "집", "lat": 37.5547, "lng": 126.9707},
        "transport_type": "TRANSIT",
        "est_travel_minutes": 50,
        "travel_source": "API",
        "buffer_minutes": 10,
        "before_prepare_minutes": 30,
    },
}


@pytest.fixture
def api(make_client, signer, monkeypatch):
    """api.post(path, body)로 토큰을 실어 부름. DB는 api.db(FakeDb) 하나를 부른 사람 · 서버 클라이언트가 같이 씀, 시계는 api.clock"""
    db = FakeDb().with_commit_rpc()
    db.rows("profiles").append(
        {
            "user_id": USER_ID,
            "timezone": "Asia/Seoul",
            "default_buffer_minutes": 10,
            "default_transport": "TRANSIT",
            "home_name": "집",
            "home_lat": 37.5547,
            "home_lng": 126.9707,
        }
    )
    clock = {"now": kst(22, 400)}
    authorizations: list[str] = []

    def user_client(authorization):
        authorizations.append(authorization)
        return db

    for module in ("alarm_suggest", "alarm_commit"):
        monkeypatch.setattr(f"app.routers.{module}.create_user_client", user_client)
        monkeypatch.setattr(f"app.routers.{module}._now", lambda: clock["now"])
    monkeypatch.setattr("app.routers.alarm_commit.create_service_client", lambda: db)
    client = make_client()

    class Api:
        def post(self, path, body=None, *, raw=None, token=True, headers=None):
            headers = {**(bearer(signer.token()) if token else {}), **(headers or {})}
            if raw is not None:
                return client.post(path, content=raw, headers={**headers, "content-type": "application/json"})
            return client.post(path, json=body, headers=headers)

    api = Api()
    api.client = client
    api.db = db
    api.clock = clock
    api.authorizations = authorizations
    return api


def error_of(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"]["code"] == code
    return body["error"]


# ---------- 로그인 · 입력 검사 ----------


@pytest.mark.parametrize("path", [SUGGEST, COMMIT])
def test_token_is_checked_before_the_body_and_nothing_is_read(api, path):
    res = api.post(path, {}, token=False)  # 본문도 틀렸지만 401이 먼저
    error_of(res, 401, "AUTH_REQUIRED")
    assert api.db.calls == []
    assert api.authorizations == []


@pytest.mark.parametrize("path", [SUGGEST, COMMIT])
def test_a_broken_body_is_a_validation_failure_with_the_edge_messages(api, path):
    assert (
        error_of(api.post(path, raw=b""), 400, "VALIDATION_FAILED")["message"]
        == "요청 본문(JSON)이 비어 있어요"
    )
    assert (
        error_of(api.post(path, raw=b"{not json"), 400, "VALIDATION_FAILED")["message"]
        == "요청 본문이 JSON 형식이 아니에요"
    )
    assert (
        error_of(api.post(path, raw=b"[1]"), 400, "VALIDATION_FAILED")["message"]
        == "요청 본문은 JSON 객체여야 해요"
    )
    assert api.db.calls == []  # 입력 검사에서 막혀 DB를 안 부름


@pytest.mark.parametrize("path", [SUGGEST, COMMIT])
def test_a_wrong_field_is_a_validation_failure_with_issues_of_path_and_message(api, path):
    res = api.post(path, {"appointment": {"appointment_at": "not-a-date"}})
    error = error_of(res, 400, "VALIDATION_FAILED")
    assert error["message"] == "입력값을 확인해 주세요"
    issues = error["details"]["issues"]
    assert issues and all(set(issue) == {"path", "message"} for issue in issues)
    assert any(issue["path"] == "appointment.appointment_at" for issue in issues)
    assert api.db.calls == []


# ---------- alarm-suggest ----------


def test_user_client_gets_rebuilt_bearer_not_the_raw_header(api, signer):
    """TS는 `Bearer ${token}`으로 다시 만들어 넘김 — 소문자 bearer · 앞뒤 공백이 와도 PostgREST에는 같은 모양 (PR #9 리뷰 고칠 것 2)"""
    token = signer.token()
    res = api.client.post(SUGGEST, json=FULL, headers={"authorization": f"  bearer   {token}  "})
    assert res.status_code == 200, res.text
    assert api.authorizations == [f"Bearer {token}"]


def test_suggest_returns_the_preview_without_writing(api):
    res = api.post(SUGGEST, FULL)
    assert res.status_code == 200, res.text
    body = res.json()
    assert [a["alarm_type"] for a in body["alarms"]] == ["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]
    assert body["travel"]["source"] == "API" and body["travel"]["manual_required"] is False
    assert body["check"]["ok"] is True
    # 부른 사람의 토큰 그대로 RLS 클라이언트를 만듦
    assert api.authorizations and api.authorizations[0].startswith("Bearer ")
    # 쓰지 않음 — 읽기뿐
    assert api.db.rows("appointments") == [] and api.db.rows("alarms") == []
    assert all(not call.startswith(("insert", "update", "delete", "upsert")) for call in api.db.calls)


def test_suggest_without_coordinates_is_a_200_that_asks_for_manual_travel(api):
    res = api.post(SUGGEST, NO_COORDS)
    assert res.status_code == 200, res.text  # 오류가 아니라 "직접 입력해 주세요"
    body = res.json()
    assert body["travel"]["manual_required"] is True
    assert body["travel"]["minutes"] is None
    assert body["check"] == {**body["check"], "ok": False, "reason": "TRAVEL_REQUIRED"}
    assert body["alarms"] == []


def test_suggest_after_the_departure_time_is_422_with_the_calculated_suggestion(api):
    api.clock["now"] = kst(22, 545)  # 출발 9:00이 지남
    res = api.post(SUGGEST, FULL)
    error = error_of(res, 422, "UNPROCESSABLE")
    assert error["details"]["reason"] == "DEPARTURE_PASSED"
    assert (
        error["details"]["suggestion"]["check"]["ok"] is False
    )  # 화면이 이유를 보일 수 있게 계산 결과를 같이 줌


def test_suggest_works_without_a_kakao_key(api):
    """카카오 키가 없어도 오류가 아님 — 길찾기 단계만 건너뜀 (F10). 장소 함수와 달리 NOT_CONFIGURED가 아님"""
    body = {
        "appointment": FULL["appointment"],
        "participant": {"origin": FULL["participant"]["origin"]},
    }
    res = api.post(SUGGEST, body)
    assert res.status_code == 200, res.text
    assert any(w["code"] == "KAKAO_NOT_CONFIGURED" for w in res.json()["warnings"])


def test_suggest_unknown_appointment_is_not_found(api):
    res = api.post(SUGGEST, {"appointment_id": "00000000-0000-4000-8000-0000000000a1"})
    error_of(res, 404, "NOT_FOUND")


# ---------- alarm-commit ----------


def test_commit_saves_appointment_participant_and_alarms_and_returns_ring_packages(api):
    res = api.post(COMMIT, FULL)
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(api.db.rows("appointments")) == 1
    assert len(api.db.rows("appointment_participants")) == 1
    assert len(api.db.rows("alarms")) == 4
    assert body["appointment_id"] == api.db.rows("appointments")[0]["appointment_id"]
    assert [p["type"] for p in body["ring_packages"]] == ["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]
    assert body["canceled_alarm_ids"] == []  # 반드시 같이 줌


def test_commit_preview_and_save_give_the_same_alarm_times(api):
    shown = api.post(SUGGEST, FULL).json()["alarms"]
    saved = api.post(COMMIT, FULL).json()["alarms"]
    key = ("alarm_type", "scheduled_at", "is_enabled", "time_source")
    assert [{k: a[k] for k in key} for a in saved] == [{k: a[k] for k in key} for a in shown]


def test_commit_without_coordinates_is_502_route_unavailable_with_manual_required(api):
    res = api.post(COMMIT, NO_COORDS)
    error = error_of(res, 502, "ROUTE_UNAVAILABLE")
    assert error["details"]["manual_required"] is True
    assert any(w["code"] == "NO_COORDINATES" for w in error["details"]["warnings"])
    assert api.db.rows("appointments") == []  # 아무것도 안 씀


def test_commit_after_the_departure_time_is_422_and_writes_nothing(api):
    api.clock["now"] = kst(22, 545)
    res = api.post(COMMIT, FULL)
    error = error_of(res, 422, "UNPROCESSABLE")
    assert error["details"]["reason"] == "DEPARTURE_PASSED"
    assert api.db.rows("appointments") == [] and api.db.rows("alarms") == []


def test_commit_estimate_is_not_saved_until_the_user_confirms_it_as_manual(api):
    body = {"appointment": FULL["appointment"], "participant": {"origin": FULL["participant"]["origin"]}}
    res = api.post(COMMIT, body)  # 키 · 기록이 없어 직선거리 근사(ESTIMATE)
    error = error_of(res, 502, "ROUTE_UNAVAILABLE")
    assert error["details"]["fallback"]["source"] == "ESTIMATE"
    assert error["details"]["fallback"]["needs_confirmation"] is True
    assert api.db.rows("appointments") == []


def test_commit_a_new_group_event_needs_me_to_be_a_joined_member(api):
    body = {
        **FULL,
        "appointment": {**FULL["appointment"], "group_id": "00000000-0000-4000-8000-0000000000b1"},
    }
    error_of(api.post(COMMIT, body), 403, "FORBIDDEN")
    assert api.db.rows("appointments") == []

    api.db.rows("group_members").append(
        {"group_id": "00000000-0000-4000-8000-0000000000b1", "user_id": USER_ID, "invite_status": "JOINED"}
    )
    res = api.post(COMMIT, body)
    assert res.status_code == 200, res.text
    assert api.db.rows("appointments")[0]["group_id"] == "00000000-0000-4000-8000-0000000000b1"


def test_commit_standalone_alarm(api):
    body = {"type": "STANDALONE", "alarm": {"scheduled_at": iso(23, 420), "recurrence_rule": "FREQ=DAILY"}}
    res = api.post(COMMIT, body)
    assert res.status_code == 200, res.text
    out = res.json()
    assert [p["title"] for p in out["ring_packages"]] == ["알람"]
    (alarm,) = api.db.rows("alarms")
    assert [alarm["participant_id"], alarm["recurrence_rule"], alarm["time_source"]] == [
        None,
        "FREQ=DAILY",
        "MANUAL",
    ]
    assert out["warnings"] == []


def test_commit_standalone_past_time_is_422(api):
    body = {"type": "STANDALONE", "alarm": {"scheduled_at": iso(22, 300)}}
    error = error_of(api.post(COMMIT, body), 422, "UNPROCESSABLE")
    assert error["details"]["reason"] == "PAST_TIME"


# ---------- 요청 키 (Idempotency-Key) · DB 함수 인자 ----------

STANDALONE = {"type": "STANDALONE", "alarm": {"scheduled_at": iso(23, 420)}}


def last_rpc(api):
    (call,) = [c for c in api.db.rpc_calls if c["name"] == "alarm_commit_apply"][-1:]
    return call["args"]


def test_router_sends_the_key_and_a_canonical_body_hash_to_the_db_function(api):
    key = "req-2026.11:15_abcdef"
    res = api.post(COMMIT, FULL, headers={"Idempotency-Key": key})
    assert res.status_code == 200, res.text
    args = last_rpc(api)
    canonical = json.dumps(FULL, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert args["p_key"] == key
    assert args["p_request_hash"] == hashlib.sha256(canonical.encode()).hexdigest()
    # 키 순서 · 공백이 달라도 같은 내용이면 같은 해시
    reordered = json.dumps(dict(reversed(list(FULL.items()))), indent=3).encode()
    api.post(COMMIT, raw=reordered, headers={"Idempotency-Key": key})
    assert last_rpc(api)["p_request_hash"] == args["p_request_hash"]


@pytest.mark.parametrize(
    "value",
    ["short12", "a" * 101, "has space 123", "bad/char/12345", "bad+char+12345"],
)
def test_router_ignores_an_invalid_key_header(api, value):
    res = api.post(COMMIT, FULL, headers={"Idempotency-Key": value})
    assert res.status_code == 200, res.text
    assert [last_rpc(api)["p_key"], last_rpc(api)["p_request_hash"]] == [None, None]


@pytest.mark.parametrize("value", ["a" * 8, "a" * 100, "Ab0_.:-xyz"])
def test_router_accepts_keys_at_the_length_and_charset_limits(api, value):
    api.post(COMMIT, FULL, headers={"Idempotency-Key": value})
    assert last_rpc(api)["p_key"] == value


def test_router_without_a_header_sends_no_key_and_does_not_deduplicate(api):
    api.post(COMMIT, FULL)
    api.post(COMMIT, FULL)
    assert [last_rpc(api)["p_key"], last_rpc(api)["p_request_hash"]] == [None, None]
    assert len(api.db.rows("appointments")) == 2


def test_resending_with_the_same_key_returns_the_same_body_and_saves_once(api):
    headers = {"Idempotency-Key": "retry-key-0001"}
    first = api.post(COMMIT, FULL, headers=headers)
    second = api.post(COMMIT, FULL, headers=headers)
    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json() == first.json()
    assert len(api.db.rows("appointments")) == 1
    assert len(api.db.rows("alarms")) == 4


def test_same_key_with_another_body_is_409_conflict(api):
    headers = {"Idempotency-Key": "retry-key-0002"}
    api.post(COMMIT, FULL, headers=headers)
    other = {**FULL, "appointment": {**FULL["appointment"], "title": "다른 약속"}}
    error = error_of(api.post(COMMIT, other, headers=headers), 409, "CONFLICT")
    assert error["message"] == "같은 요청 키로 다른 내용을 보냈어요. 새로고침한 뒤 다시 시도해 주세요"
    assert len(api.db.rows("appointments")) == 1


def test_standalone_with_a_key_is_saved_once(api):
    headers = {"Idempotency-Key": "retry-key-0003"}
    first = api.post(COMMIT, STANDALONE, headers=headers)
    second = api.post(COMMIT, STANDALONE, headers=headers)
    assert second.json() == first.json()
    assert len(api.db.rows("alarms")) == 1
    assert last_rpc(api)["p_mode"] == "STANDALONE"

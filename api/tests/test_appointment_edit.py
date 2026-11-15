"""POST /fn/appointment-edit (완결점 3-4 ②) — 요청 · 응답 · 오류 모양이 Edge와 같은지 HTTP 수준에서 확인.
순서: 토큰 확인(401) → 입력 검사(400) → 계산(run_edit). 계산 자체(edit_run)는 tests/shared/test_edit_run.py가 가짜 DB로 시험하므로
여기서는 run_edit과 Supabase 클라이언트 만들기를 가짜로 바꿔 라우터가 맡은 것(인증 · 검증 · 맥락 만들기 · 오류 전달)만 본다."""

import pytest

from app.core.errors import AppError
from app.routers import appointment_edit as module

from .conftest import USER_ID, bearer

PATH = "/fn/appointment-edit"
ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def api(make_client, signer, monkeypatch):
    """api.post(body)로 토큰을 실어 부름. run_edit 호출은 api.calls에 남고, 결과는 api.result · api.error로 정함"""

    class Api:
        calls: list = []
        created: list = []
        result: dict = {"appointment_id": ID, "ring_packages": [], "canceled_alarm_ids": [], "warnings": []}
        error: Exception | None = None

        def __init__(self):
            self.client = make_client()

        def post(self, body=None, *, raw=None, token=True):
            headers = bearer(signer.token()) if token else {}
            if raw is not None:
                return self.client.post(
                    PATH, content=raw, headers={**headers, "content-type": "application/json"}
                )
            return self.client.post(PATH, json=body, headers=headers)

    state = Api()

    async def fake_run_edit(ctx, request):
        state.calls.append((ctx, request))
        if state.error:
            raise state.error
        return state.result

    def fake_user_client(authorization):
        state.created.append(("user", authorization))
        return "USER-DB"

    def fake_service_client():
        state.created.append(("service", None))
        return "SERVICE-DB"

    state.calls = []
    state.created = []
    monkeypatch.setattr(module, "run_edit", fake_run_edit)
    monkeypatch.setattr(module, "create_user_client", fake_user_client)
    monkeypatch.setattr(module, "create_service_client", fake_service_client)
    return state


def error_of(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"]["code"] == code
    return body["error"]


# ---------- 순서: 토큰 → 입력 ----------


def test_token_is_checked_before_the_body(api):
    res = api.post({}, token=False)  # 본문도 틀렸지만 401이 먼저
    error_of(res, 401, "AUTH_REQUIRED")
    assert api.calls == [] and api.created == []


def test_get_is_method_not_allowed(api):
    res = api.client.get(PATH)
    assert res.status_code == 405


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "요청 본문(JSON)이 비어 있어요"),
        (b"{not json", "요청 본문이 JSON 형식이 아니에요"),
        (b"[]", "요청 본문은 JSON 객체여야 해요"),
        (b"[1]", "요청 본문은 JSON 객체여야 해요"),
    ],
)
def test_empty_or_non_object_body_is_validation_failed(api, raw, message):
    error = error_of(api.post(raw=raw), 400, "VALIDATION_FAILED")
    assert error["message"] == message
    assert api.calls == []


# ---------- 입력 검사: edit-request 시험의 거절 경우가 HTTP에서도 같은 코드 · 같은 칸 ----------


def update(patch):
    return {"action": "UPDATE", "appointment_id": ID, "patch": patch}


REJECTIONS = [
    ({}, ["action", "appointment_id"]),
    ({"action": "MOVE", "appointment_id": ID}, ["action"]),
    ({"action": "UPDATE", "appointment_id": "nope", "patch": {}}, ["appointment_id"]),
    ({"action": "UPDATE", "appointment_id": ID}, ["patch"]),
    ({"action": "DELETE", "appointment_id": ID, "scope": "all"}, ["scope"]),
    ({"action": "DELETE", "appointment_id": ID, "scope": "THIS"}, ["scope"]),
    (update({"title": "   "}), ["patch.title"]),
    (update({"title": "x" * 101}), ["patch.title"]),
    (update({"appointment_at": "2026-10-29T10:00:00"}), ["patch.appointment_at"]),
    (update({"importance": "URGENT"}), ["patch.importance"]),
    (update({"recurrence_rule": "FREQ=MONTHLY"}), ["patch.recurrence_rule"]),
    (update({"recurrence_rule": "FREQ=WEEKLY"}), ["patch.recurrence_rule"]),
    (update({"destination": {}}), ["patch.destination.name"]),
    (update({"destination": {"name": "   "}}), ["patch.destination.name"]),
    (update({"destination": {"name": "강남역", "lat": 37.4}}), ["patch.destination.lng"]),
    # 범위 밖 lat은 "범위" 문제 + (lat이 비었다고 보고) "같이 있어야" 문제가 겹쳐 두 번 (Edge도 같음 — 원본 시험은 포함 여부만 봄)
    (update({"destination": {"name": "강남역", "lat": 91, "lng": 127}}), ["patch.destination.lat"] * 2),
]


@pytest.mark.parametrize(("body", "paths"), REJECTIONS)
def test_rejections_are_validation_failed_with_the_same_fields(api, body, paths):
    error = error_of(api.post(body), 400, "VALIDATION_FAILED")
    assert error["message"] == "입력값을 확인해 주세요"
    assert [issue["path"] for issue in error["details"]["issues"]] == paths
    assert api.calls == []  # 계산까지 가지 않음 · 클라이언트도 안 만듦
    assert api.created == []


# ---------- 맥락 만들기 · 통과 ----------


def test_valid_request_runs_with_the_callers_identity_and_passes_the_result_through(api):
    api.result = {
        "appointment_id": ID,
        "saved": True,
        "ring_packages": [],
        "canceled_alarm_ids": [],
        "warnings": [],
    }
    body = {
        "action": "UPDATE",
        "appointment_id": ID.upper(),
        "scope": "following",
        "confirmed": True,
        "patch": {"title": " 발표 ", "appointment_at": "2026-10-29T10:00:00+09:00", "recurrence_rule": None},
    }
    res = api.post(body)
    assert res.status_code == 200, res.text
    assert res.json() == api.result
    ((ctx, request),) = api.calls
    assert ctx.user_id == USER_ID  # 토큰의 sub (요청 본문에서 받지 않음)
    assert (ctx.user_db, ctx.service) == ("USER-DB", "SERVICE-DB")
    assert [c[0] for c in api.created] == ["user", "service"]
    assert api.created[0][1].startswith("Bearer ")  # 부른 사람의 토큰으로 RLS 클라이언트
    assert request["action"] == "UPDATE"
    assert request["appointment_id"] == ID  # 소문자로
    assert (request["scope"], request["confirmed"]) == ("following", True)
    assert request["patch"]["title"] == "발표"
    assert request["patch"]["recurrence_rule"] is None


@pytest.mark.parametrize("action", ["DELETE", "RESTORE", "LEAVE"])
def test_other_actions_are_read_and_run(api, action):
    res = api.post({"action": action, "appointment_id": ID})
    assert res.status_code == 200, res.text
    ((_, request),) = api.calls
    assert request["action"] == action
    if action == "DELETE":
        assert request["scope"] == "this"


def test_kakao_lookup_only_when_the_key_is_set(make_client, signer, api, monkeypatch):
    seen = {}

    async def fake_travel(client, key, *, origin, destination, transport):
        seen.update(key=key, origin=origin, destination=destination, transport=transport)
        return {"minutes": 20, "partial": False}

    monkeypatch.setattr("app.routers.appointment_edit.kakao_travel_minutes", fake_travel)
    api.post({"action": "LEAVE", "appointment_id": ID})
    assert api.calls[-1][0].kakao is None  # 키가 없어도 요청은 통과 (길찾기 없이 근사값)

    keyed = make_client(kakao_rest_api_key="KAKAO-TEST-KEY")
    res = keyed.post(PATH, json={"action": "LEAVE", "appointment_id": ID}, headers=bearer(signer.token()))
    assert res.status_code == 200, res.text
    lookup = api.calls[-1][0].kakao
    assert callable(lookup)
    import asyncio

    args = {"origin": {"lat": 1, "lng": 2}, "destination": {"lat": 3, "lng": 4}, "transport": "WALK"}
    assert asyncio.run(lookup(args)) == {"minutes": 20, "partial": False}
    assert seen == {"key": "KAKAO-TEST-KEY", **args}


# ---------- 계산에서 나온 오류는 코드 · 상태 · 본문 그대로 ----------


@pytest.mark.parametrize(
    ("code", "status", "details"),
    [
        ("NOT_FOUND", 404, None),
        ("FORBIDDEN", 403, None),
        ("UNPROCESSABLE", 422, {"reason": "HAS_RECORDS"}),
        ("UNPROCESSABLE", 422, {"reason": "PAST_APPOINTMENT"}),
        ("ROUTE_UNAVAILABLE", 502, {"manual_required": True, "warnings": []}),
        ("INTERNAL", 500, None),
    ],
)
def test_errors_from_the_run_keep_code_status_and_details(api, code, status, details):
    api.error = AppError(code, "시험용 문구", details)
    error = error_of(api.post({"action": "DELETE", "appointment_id": ID}), status, code)
    assert error["message"] == "시험용 문구"
    assert error.get("details") == details

"""오류 본문 모양 · CORS · OPTIONS · X-Request-Id — 본 레포 `_shared/errors.ts` · `cors.ts` · `http.ts`와 같은 모양인지"""

import re

import pytest
from fastapi import APIRouter

from app.core.errors import ALL_ERROR_STATUS, ERROR_STATUS, AppError, error_body
from app.core.http import CORS_HEADERS
from app.main import create_app
from tests.conftest import bearer, make_settings

# `supabase/functions/_shared/errors.ts` ERROR_STATUS를 붙여 넣은 사본 (11/13 기준). errors.ts가 바뀌면 사람이 같이 고쳐야 함 — 자동 대조는 본 레포와 서버를 함께 보는 계약 시험(2-1)의 몫
EDGE_ERROR_STATUS = {
    "VALIDATION_FAILED": 400,
    "AUTH_REQUIRED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "CONFLICT": 409,
    "UNPROCESSABLE": 422,
    "RATE_LIMITED": 429,
    "INTERNAL": 500,
    "ROUTE_UNAVAILABLE": 502,
    "PLACE_UNAVAILABLE": 502,
    "LLM_UNAVAILABLE": 502,
    "VOICE_UNAVAILABLE": 503,
    "NOT_CONFIGURED": 503,
}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def test_error_codes_and_statuses_equal_edge_table():
    assert ERROR_STATUS == EDGE_ERROR_STATUS
    assert set(ALL_ERROR_STATUS) - set(EDGE_ERROR_STATUS) == {
        "AUTH_UNAVAILABLE"
    }  # 서버에만 있는 코드는 이것 하나


def test_error_body_omits_details_key_when_absent():
    assert error_body("NOT_FOUND", "x") == {"error": {"code": "NOT_FOUND", "message": "x"}}
    assert error_body("CONFLICT", "x", {"a": 1}) == {
        "error": {"code": "CONFLICT", "message": "x", "details": {"a": 1}}
    }
    assert error_body("CONFLICT", "x", []) == {
        "error": {"code": "CONFLICT", "message": "x", "details": []}
    }  # 빈 배열은 값임 (TS도 undefined만 뺌)


def test_unknown_error_code_is_rejected():
    with pytest.raises(ValueError):
        AppError("NOPE", "x")


def test_error_response_has_edge_content_type_and_compact_korean_json(client):
    res = client.get("/me")
    assert res.headers["content-type"] == "application/json; charset=utf-8"
    assert (
        res.content.decode("utf-8") == '{"error":{"code":"AUTH_REQUIRED","message":"로그인이 필요해요"}}'
    )  # 공백 · \\u 없이


def test_options_without_token_is_204_with_cors(client):
    for path in ("/me", "/health", "/fn/place-search", "/no/such/path"):
        res = client.options(path)
        assert res.status_code == 204, path
        assert res.content == b""
        for key, value in CORS_HEADERS.items():
            assert res.headers[key] == value
        assert "x-request-id" in res.headers


def test_preflight_from_browser_gets_cors_headers(client):
    res = client.options(
        "/me",
        headers={
            "Origin": "https://respec.pages.dev",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert res.status_code == 204
    assert res.headers["access-control-allow-origin"] == "*"
    assert "authorization" in res.headers["access-control-allow-headers"]


def test_cors_headers_on_success_and_error(client, signer):
    ok = client.get("/me", headers=bearer(signer.token()))
    err = client.get("/me")
    for res in (ok, err):
        assert res.headers["access-control-allow-origin"] == "*"
        assert res.headers["access-control-expose-headers"] == "x-request-id"


def test_cors_allow_headers_equal_edge_list():
    # `_shared/cors.ts`의 허용 헤더 목록 그대로
    assert (
        CORS_HEADERS["Access-Control-Allow-Headers"]
        == "authorization, x-client-info, apikey, content-type, idempotency-key, x-request-id"
    )


def test_request_id_is_echoed_when_safe(client):
    res = client.get("/health", headers={"X-Request-Id": "abc-123_XYZ"})
    assert res.headers["x-request-id"] == "abc-123_XYZ"


@pytest.mark.parametrize("bad", ["has space", "x" * 65, "한글", "a/b", "a;b", ""])
def test_request_id_is_replaced_when_unsafe(client, bad):
    res = client.get("/health", headers={"X-Request-Id": bad.encode("utf-8")})
    assert UUID_RE.match(res.headers["x-request-id"])


def test_request_id_is_generated_when_missing_and_on_errors(client):
    ok = client.get("/health")
    err = client.get("/me", headers={"X-Request-Id": "trace-1"})
    assert UUID_RE.match(ok.headers["x-request-id"])
    assert err.headers["x-request-id"] == "trace-1"


def test_default_404_405_422_are_rewritten_in_edge_shape(client, signer):
    not_found = client.get("/no/such/path")
    assert not_found.status_code == 404
    assert not_found.json() == {"error": {"code": "NOT_FOUND", "message": "찾을 수 없어요"}}

    wrong_method = client.post("/me", headers=bearer(signer.token()))
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "GET" in wrong_method.headers["allow"]


def test_request_validation_error_is_400_validation_failed_without_input_values():
    router = APIRouter()

    @router.post("/echo")
    async def echo(q: int) -> dict:  # 쿼리 q는 정수여야 함
        return {"q": q}

    import httpx
    from fastapi.testclient import TestClient

    from tests.conftest import FakeSupabase, Signer

    app = create_app(
        make_settings(), transport=httpx.MockTransport(FakeSupabase(signer=Signer(kid="k")).handler)
    )
    app.include_router(router)
    with TestClient(app) as c:
        res = c.post("/echo?q=37.5665,126.9780-secret-place")  # 좌표 · 검색어가 들어 있을 수 있는 값
        assert res.status_code == 400
        body = res.json()
        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert body["error"]["details"]["fields"][0]["loc"] == ["query", "q"]
        assert "secret-place" not in res.text and "37.5665" not in res.text  # 입력값은 되돌려 주지 않음


def test_docs_and_openapi_are_off(client):
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        res = client.get(path)
        assert res.status_code == 404, path
        assert res.json()["error"]["code"] == "NOT_FOUND"


def test_unexpected_exception_is_500_internal_with_cors_and_no_leak(caplog):
    import httpx
    from fastapi.testclient import TestClient

    from tests.conftest import FakeSupabase, Signer

    router = APIRouter()

    @router.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("secret-internal-detail 37.5665")

    app = create_app(
        make_settings(), transport=httpx.MockTransport(FakeSupabase(signer=Signer(kid="k")).handler)
    )
    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as c:
        res = c.get("/boom", headers={"X-Request-Id": "trace-500"})
    assert res.status_code == 500
    assert res.json() == {"error": {"code": "INTERNAL", "message": "잠시 후 다시 시도해 주세요"}}
    assert res.headers["access-control-allow-origin"] == "*"
    assert res.headers["x-request-id"] == "trace-500"
    assert "secret-internal-detail" not in res.text
    assert any("trace-500" in r.getMessage() for r in caplog.records)  # 원문은 로그에만, 요청 번호와 함께


def test_access_log_has_no_query_string_or_headers(client, signer, caplog):
    caplog.set_level("INFO")
    client.get(
        "/health?lat=37.5665&q=카페",
        headers={"Authorization": f"Bearer {signer.token()}", "X-Request-Id": "trace-log"},
    )
    lines = [r.getMessage() for r in caplog.records if "trace-log" in r.getMessage()]
    assert lines and all(
        "37.5665" not in line and "카페" not in line and "Bearer" not in line for line in lines
    )
    assert "path=/health " in lines[0]


def test_edge_json_response_writes_non_finite_numbers_as_null():
    """JSON.stringify와 같게 응답의 NaN · ±Infinity는 null (PR #9 리뷰 고칠 것 1)"""
    from app.core.http import EdgeJSONResponse

    assert (
        EdgeJSONResponse({"a": float("inf"), "b": [float("nan")], "c": 1.5}).body
        == b'{"a":null,"b":[null],"c":1.5}'
    )

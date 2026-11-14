"""로컬 리뷰에서 나온 지적을 고정하는 시험 — 설정 검증 · 로그 금지 규칙 · JWKS 깨진 응답 · 캐시 동작"""

import asyncio
import logging

import httpx
import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import Settings
from app.core.jwks import JwksCache, JwksUnavailable
from app.main import create_app
from tests.conftest import FakeSupabase, Signer, bearer, make_settings

# ---------- 설정 검증 오류가 값을 찍지 않음 · LOG_LEVEL 소문자 ----------


def test_settings_validation_error_does_not_echo_secret_values():
    with pytest.raises(ValidationError) as excinfo:  # SUPABASE_URL이 빠짐
        Settings(supabase_secret_key="sb_secret_SECRETVALUE1234567890")
    assert "SECRETVALUE1234567890" not in str(excinfo.value)


def test_log_level_is_case_insensitive_and_rejects_unknown():
    assert Settings(supabase_url="http://x.test", log_level="info").log_level == "INFO"
    with pytest.raises(ValidationError):
        Settings(supabase_url="http://x.test", log_level="LOUD")


# ---------- 로그 금지 규칙: 나가는 요청 URL · 예외 메시지 · 경로의 줄바꿈 ----------


def _app_with(router: APIRouter, fake: FakeSupabase, **overrides):
    app = create_app(make_settings(**overrides), transport=httpx.MockTransport(fake.handler))
    app.include_router(router)
    return app


def test_outgoing_request_urls_are_not_logged(fake, caplog):
    router = APIRouter()

    @router.get("/probe")
    async def probe(request: Request) -> dict:
        # 다음 PR의 카카오 호출처럼 좌표 · 검색어가 쿼리에 실린 나가는 요청
        await request.app.state.http.get("http://kakao.test/v2/search?x=127.0276&y=37.4979&query=secretplace")
        return {"ok": True}

    caplog.set_level(logging.DEBUG)
    with TestClient(_app_with(router, fake)) as c:
        assert c.get("/probe").status_code == 200
    assert "secretplace" not in caplog.text and "127.0276" not in caplog.text


def test_unexpected_exception_message_is_not_logged(fake, caplog):
    router = APIRouter()

    @router.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("secretplace 37.5665 sb_secret_leak")

    caplog.set_level(logging.DEBUG)
    with TestClient(_app_with(router, fake), raise_server_exceptions=False) as c:
        res = c.get("/boom", headers={"X-Request-Id": "trace-7"})
    assert res.status_code == 500
    assert "secretplace" not in caplog.text and "sb_secret_leak" not in caplog.text
    assert "trace-7" in caplog.text and "RuntimeError" in caplog.text  # 요청 번호 · 예외 종류 · 위치는 남음


def test_decoded_newline_in_path_cannot_forge_log_lines(client, caplog):
    caplog.set_level(logging.INFO)
    client.get("/a%0Aforged_line_status=200")
    http_lines = [r.getMessage() for r in caplog.records if r.name == "respec.http"]
    assert http_lines and all("\n" not in line and "\r" not in line for line in http_lines)


# ---------- JWKS: 깨진 응답 · 알 수 없는 kid + 장애 ----------


@pytest.mark.parametrize("body", [[], "text", {"keys": "nope"}, {"keys": ["x", 1, None]}, {"nokeys": 1}])
def test_malformed_jwks_body_is_503_not_500(client, fake, signer, body):
    fake.jwks_body = body
    assert client.get("/me", headers=bearer(signer.token())).status_code == 503


def test_one_broken_key_does_not_discard_valid_keys(client, fake, signer):
    broken = {"kty": "EC", "kid": "broken", "crv": "P-256", "x": "!!", "y": "!!"}
    fake.jwks_keys = ["garbage", broken, signer.jwk]
    assert client.get("/me", headers=bearer(signer.token())).status_code == 200


def test_malformed_body_with_stale_cache_keeps_working(make_client, fake, signer):
    client = make_client(jwks_cache_ttl_seconds=0, jwks_min_refetch_seconds=0)
    assert client.get("/me", headers=bearer(signer.token())).status_code == 200
    fake.jwks_body = []  # 200인데 본문이 깨짐
    assert client.get("/me", headers=bearer(signer.token())).status_code == 200


def test_unknown_kid_while_jwks_down_is_503_not_401(make_client, fake, signer):
    """키 교체 직후의 새 kid인지 위조인지 가릴 수 없는 상태 → 401이면 정상 사용자가 로그아웃되므로 503"""
    client = make_client(jwks_min_refetch_seconds=0)
    assert client.get("/me", headers=bearer(signer.token())).status_code == 200
    fake.jwks_down = True
    res = client.get("/me", headers=bearer(Signer(kid="rotated-new").token()))
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "AUTH_UNAVAILABLE"


def test_unknown_kid_is_401_when_jwks_is_healthy(client, signer):
    assert client.get("/me", headers=bearer(signer.token())).status_code == 200
    assert client.get("/me", headers=bearer(Signer(kid="never-published").token())).status_code == 401


# ---------- JwksCache 직접: 시계 주입으로 "한 번 받고, 간격 안에서는 안 받음" 증명 · 락 · 동시 요청 ----------


def _cache(fake: FakeSupabase, clock, **kw) -> JwksCache:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return JwksCache(client, "http://supabase.test/auth/v1/.well-known/jwks.json", clock=clock, **kw)


def test_unknown_kid_refetch_follows_min_interval(fake, signer):
    now = [0.0]

    async def scenario():
        cache = _cache(fake, lambda: now[0], ttl_seconds=600, min_refetch_seconds=30)
        assert await cache.get_key(signer.kid) is not None  # t=0 처음 받기
        assert fake.jwks_calls == 1
        new = Signer(kid="rotated")
        fake.jwks_keys = [signer.jwk, new.jwk]  # 키 교체
        now[0] = 1.0
        assert await cache.get_key("rotated") is None  # 간격(30초) 안이라 다시 안 받음 → 모름
        assert fake.jwks_calls == 1
        now[0] = 31.0
        assert await cache.get_key("rotated") is not None  # 간격이 지나 한 번 새로 받음
        assert fake.jwks_calls == 2
        assert await cache.get_key("rotated") is not None  # 이제 캐시에 있어 더 안 받음
        assert fake.jwks_calls == 2

    asyncio.run(scenario())


def test_stale_cache_refresh_does_not_block_concurrent_requests(fake, signer):
    """TTL이 지나 새로 받는 동안(JWKS가 느림) 캐시가 멀쩡하면 다른 요청은 기다리지 않고 낡은 키로 검사"""
    now = [0.0]

    async def scenario():
        gate = asyncio.Event()
        slow = {"on": False}

        async def handler(request: httpx.Request) -> httpx.Response:
            if slow["on"]:
                await gate.wait()
            return fake.handler(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cache = JwksCache(
            client,
            "http://supabase.test/auth/v1/.well-known/jwks.json",
            ttl_seconds=600,
            min_refetch_seconds=30,
            clock=lambda: now[0],
        )
        assert await cache.get_key(signer.kid) is not None  # t=0 처음 받기 (빠름)
        fake.jwks_calls = 0
        slow["on"] = True
        now[0] = 700.0  # TTL(600초)이 지나 낡음 + 마지막 시도로부터 간격(30초)도 지남
        tasks = [asyncio.create_task(cache.get_key(signer.kid)) for _ in range(5)]
        done, pending = await asyncio.wait(tasks, timeout=0.3)
        assert len(done) == 4 and len(pending) == 1  # 새로 받는 1개만 대기, 나머지 4개는 바로 통과
        assert all(t.result() is not None for t in done)  # 낡은 키로 정상 검사
        gate.set()
        await asyncio.gather(*pending)
        assert fake.jwks_calls == 1  # 새로 받기는 한 번뿐

    asyncio.run(scenario())


def test_waiters_share_one_fetch_when_no_keys_yet(fake, signer):
    """키가 하나도 없을 땐 모두 기다리되 같은 결과를 나눠 씀 (대기자가 하나씩 다시 받아 직렬화되지 않음)"""

    async def scenario():
        gate = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            await gate.wait()
            return fake.handler(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cache = JwksCache(
            client,
            "http://supabase.test/auth/v1/.well-known/jwks.json",
            ttl_seconds=600,
            min_refetch_seconds=0,
        )
        tasks = [asyncio.create_task(cache.get_key(signer.kid)) for _ in range(5)]
        await asyncio.sleep(0.05)
        gate.set()
        keys = await asyncio.gather(*tasks)
        assert all(k is not None for k in keys)
        assert fake.jwks_calls == 1

    asyncio.run(scenario())


def test_no_keys_and_failed_fetch_raises_unavailable(fake, signer):
    fake.jwks_down = True

    async def scenario():
        cache = _cache(fake, lambda: 0.0, ttl_seconds=600, min_refetch_seconds=30)
        with pytest.raises(JwksUnavailable):
            await cache.get_key(signer.kid)

    asyncio.run(scenario())


# ---------- 응답 모양 보강: 성공 Content-Type · 슬래시 · 상태 번호 보존 ----------


def test_success_response_has_edge_content_type(client, signer):
    res = client.get("/me", headers=bearer(signer.token()))
    assert res.headers["content-type"] == "application/json; charset=utf-8"
    assert client.get("/health").headers["content-type"] == "application/json; charset=utf-8"


def test_trailing_slash_is_404_edge_shape_not_redirect(client, signer):
    res = client.get("/me/", headers=bearer(signer.token()), follow_redirects=False)
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    ("status", "code"), [(503, "INTERNAL"), (413, "VALIDATION_FAILED"), (429, "RATE_LIMITED")]
)
def test_http_exception_keeps_its_status_in_edge_shape(fake, status, code):
    router = APIRouter()

    @router.get("/raise")
    async def raise_it() -> dict:
        raise StarletteHTTPException(status_code=status, detail="secret detail")

    with TestClient(_app_with(router, fake)) as c:
        res = c.get("/raise")
    assert res.status_code == status
    assert res.json()["error"]["code"] == code
    assert "secret detail" not in res.text

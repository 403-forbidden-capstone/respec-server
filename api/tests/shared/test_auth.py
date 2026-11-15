"""본 레포 `_shared/auth.test.ts`의 서버판 — verify_jwt = false로 두 길을 코드에서 가르는 함수(alarm-roll · voice-commit)의 문지기

Edge 시험은 supabase-js를 가짜로 바꿔 "Auth 서버가 정해 둔 토큰만 사용자"로 쳤지만, 서버는 JWKS 서명 검사(core.auth)라
진짜 ES256 키쌍으로 만든 토큰이 "사용자 토큰", 나머지(anon 키 · 서비스 키 · 위조)는 거절.
서버의 서비스 키는 SUPABASE_SECRET_KEY 하나라 legacy JWT · sb_secret_ 두 경우를 설정만 바꿔 각각 돌림.
"""

import pytest
from starlette.requests import Request

from app.core.errors import AppError
from app.shared.auth import CurrentUserDb, is_service_call, require_service_call
from app.shared.supabase import configure

from ..conftest import USER_ID, bearer, make_settings


def req(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/fn/voice-commit", "headers": raw})


@pytest.fixture
def service_settings():
    """서비스 키 설정 — 시험이 끝나면 되돌림"""

    def _use(secret: str | None) -> None:
        configure(make_settings(supabase_secret_key=secret), None)

    yield _use
    configure(None, None)


def test_is_service_call_legacy_jwt_key(service_settings):
    """isServiceCall: 서비스 키(legacy JWT)만 배치 길"""
    service_settings("service-role-jwt")
    assert (
        is_service_call(req({"authorization": "Bearer service-role-jwt", "apikey": "service-role-jwt"}))
        is True
    )  # pg_net 배치 모양
    assert (
        is_service_call(req({"authorization": "Bearer user-jwt", "apikey": "anon-key"})) is False
    )  # 앱 요청
    assert is_service_call(req({"authorization": "Bearer anon-key"})) is False
    assert is_service_call(req({})) is False
    assert is_service_call(req({"authorization": "Bearer service-role-jwtX"})) is False
    assert is_service_call(req({"authorization": "Bearer ", "apikey": ""})) is False


def test_is_service_call_new_secret_key(service_settings):
    """isServiceCall: 서비스 키(sb_secret_)만 배치 길 — 새 키는 apikey로만 보냄"""
    service_settings("sb_secret_abc")
    assert is_service_call(req({"apikey": "sb_secret_abc"})) is True
    assert is_service_call(req({"apikey": "sb_secret_abd"})) is False


def test_is_service_call_no_server_key(service_settings):
    """isServiceCall: 서버에 서비스 키가 없으면 아무도 배치 길을 못 탐 (빈 키로 뚫리지 않음)"""
    service_settings(None)
    assert is_service_call(req({"authorization": "Bearer service-role-jwt"})) is False
    assert is_service_call(req({"apikey": ""})) is False


def test_require_service_call_raises_auth_required(service_settings):
    service_settings("sb_secret_abc")
    require_service_call(req({"apikey": "sb_secret_abc"}))
    with pytest.raises(AppError) as info:
        require_service_call(req({"apikey": "nope"}))
    assert info.value.code == "AUTH_REQUIRED"
    assert info.value.message == "서버 전용 함수예요"


def _add_route(client):
    async def whoami(user: CurrentUserDb):
        return {"userId": user.user_id, "token": user.token, "dbHeaders": dict(user.db.headers)}

    client.app.add_api_route("/_t/whoami", whoami, methods=["GET"])
    return client


@pytest.fixture
def api(make_client):
    return _add_route(
        make_client(supabase_publishable_key="anon-key", supabase_secret_key="service-role-jwt")
    )


def rejected(response) -> str:
    assert response.status_code == 401, response.text
    return response.json()["error"]["code"]


def test_require_user_db_user_token_gets_user_client(api, signer):
    """requireUser: 인정한 토큰이면 userId · token · 이 사용자의 RLS를 따르는 db (apikey = 공개 키, Authorization = 그 토큰)"""
    token = signer.token()
    response = api.get("/_t/whoami", headers=bearer(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["userId"] == USER_ID
    assert body["token"] == token
    assert body["dbHeaders"]["apikey"] == "anon-key"
    assert body["dbHeaders"]["Authorization"] == f"Bearer {token}"


def test_require_user_db_rejects_everything_else(api, signer):
    """requireUser: 없음 · 모양 틀림 · anon 키 · 서비스 키 · 가짜 토큰은 AUTH_REQUIRED"""
    assert rejected(api.get("/_t/whoami")) == "AUTH_REQUIRED"
    assert rejected(api.get("/_t/whoami", headers={"Authorization": signer.token()})) == "AUTH_REQUIRED"
    anon = {"Authorization": "Bearer anon-key", "apikey": "anon-key"}
    assert rejected(api.get("/_t/whoami", headers=anon)) == "AUTH_REQUIRED"
    assert (
        rejected(api.get("/_t/whoami", headers={"Authorization": "Bearer service-role-jwt"}))
        == "AUTH_REQUIRED"
    )
    assert (
        rejected(api.get("/_t/whoami", headers={"Authorization": "Bearer forged.jwt.token"}))
        == "AUTH_REQUIRED"
    )


def test_require_user_db_not_configured_without_publishable_key(make_client, signer):
    """공개 키 설정이 없으면 (토큰은 맞아도) 사용자 클라이언트를 못 만들어 NOT_CONFIGURED"""
    client = _add_route(make_client())
    response = client.get("/_t/whoami", headers=bearer(signer.token()))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NOT_CONFIGURED"

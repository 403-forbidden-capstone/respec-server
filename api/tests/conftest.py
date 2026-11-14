"""시험 도구 — 실제 ES256 키쌍으로 토큰을 만들고, Supabase는 가짜 네트워크(MockTransport)로 흉내냄.

fake_supabase.jwks_down = True 로 공개 키 목록 요청을 실패시키고, .calls 로 요청 횟수를 셈.
"""

import time
import uuid
from dataclasses import dataclass, field

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm

from app.core.config import Settings
from app.main import create_app

SUPABASE_URL = "http://supabase.test"
ISSUER = f"{SUPABASE_URL}/auth/v1"
USER_ID = "11111111-2222-3333-4444-555555555555"


@dataclass
class Signer:
    kid: str
    private_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    @property
    def jwk(self) -> dict:
        jwk = ECAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        return {**jwk, "kid": self.kid, "alg": "ES256", "use": "sig"}

    def token(
        self,
        *,
        sub: str = USER_ID,
        role: str = "authenticated",
        aud: str = "authenticated",
        iss: str = ISSUER,
        exp_in: int = 3600,
        kid: str | None = None,
        extra: dict | None = None,
    ) -> str:
        now = int(time.time())
        claims = {
            "sub": sub,
            "role": role,
            "aud": aud,
            "iss": iss,
            "iat": now,
            "exp": now + exp_in,
            **(extra or {}),
        }
        return jwt.encode(claims, self.private_key, algorithm="ES256", headers={"kid": kid or self.kid})


@dataclass
class FakeSupabase:
    """Supabase 흉내 — 공개 키 목록(JWKS)과 PostgREST(server_heartbeat)"""

    signer: Signer
    jwks_down: bool = False
    jwks_keys: list[dict] | None = None  # None이면 signer 하나
    jwks_body: object | None = None  # 값이 있으면 {"keys": ...} 대신 이 본문을 그대로 돌려줌 (깨진 응답 흉내)
    heartbeat: list[dict] = field(default_factory=lambda: [{"alive_at": None}])
    rest_down: bool = False
    jwks_calls: int = 0
    rest_requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/jwks.json"):
            self.jwks_calls += 1
            if self.jwks_down:
                raise httpx.ConnectError("jwks down")
            if self.jwks_body is not None:
                return httpx.Response(200, json=self.jwks_body)
            return httpx.Response(
                200, json={"keys": self.jwks_keys if self.jwks_keys is not None else [self.signer.jwk]}
            )
        if request.url.path.endswith("/rest/v1/server_heartbeat"):
            self.rest_requests.append(request)
            if self.rest_down:
                return httpx.Response(500, json={"message": "boom"})
            return httpx.Response(200, json=self.heartbeat)
        return httpx.Response(404)


def make_settings(**overrides) -> Settings:
    base = {
        "supabase_url": SUPABASE_URL,
        "supabase_secret_key": "sb_secret_test_value",
        "jwks_min_refetch_seconds": 30,
    }
    return Settings(**{**base, **overrides})


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """개발자 PC · CI에 설정 환경 변수가 있어도 시험이 흔들리지 않게 — Settings가 읽는 변수를 전부 비움"""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def signer() -> Signer:
    return Signer(kid=f"kid-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def fake(signer: Signer) -> FakeSupabase:
    return FakeSupabase(signer=signer)


@pytest.fixture
def make_client(fake: FakeSupabase):
    """make_client(**설정 덮어쓰기) → 열린 TestClient (with 블록과 같음). 끝나면 자동으로 닫음"""
    opened: list[TestClient] = []

    def _make(**overrides) -> TestClient:
        app = create_app(make_settings(**overrides), transport=httpx.MockTransport(fake.handler))
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        opened.append(client)
        return client

    yield _make
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

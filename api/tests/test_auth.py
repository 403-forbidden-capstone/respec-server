"""1-4 끝 칸 — 토큰 검사: /me 200, 401 일곱 가지, JWKS 장애, 탈퇴 계정 사실"""

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from tests.conftest import ISSUER, USER_ID, Signer, bearer

REQUIRED = {"error": {"code": "AUTH_REQUIRED", "message": "로그인이 필요해요"}}
EXPIRED = {"error": {"code": "AUTH_REQUIRED", "message": "로그인이 만료됐어요. 다시 로그인해 주세요"}}


def test_me_returns_id_of_token_owner(client, signer):
    res = client.get("/me", headers=bearer(signer.token()))
    assert res.status_code == 200
    assert res.json() == {"id": USER_ID}  # 지형님 계정 토큰 → id 일치 (여기서는 같은 모양의 시험 토큰)


def test_me_accepts_lowercase_bearer_and_extra_spaces(client, signer):
    res = client.get("/me", headers={"Authorization": f"  bearer   {signer.token()}  "})
    assert res.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},  # 헤더 없음
        {"Authorization": ""},
        {"Authorization": "Basic dXNlcjpwYXNz"},  # Bearer가 아님
        {"Authorization": "Bearer"},  # 토큰 없음
    ],
)
def test_missing_or_non_bearer_header_is_401_required(client, headers):
    res = client.get("/me", headers=headers)
    assert res.status_code == 401
    assert res.json() == REQUIRED


@pytest.mark.parametrize(
    "garbage", ["abc", "a.b.c", "....", "Bearer", "eyJhbGciOiJFUzI1NiJ9.e30.", "x" * 5000]
)
def test_garbled_token_is_401(client, garbage):
    res = client.get("/me", headers=bearer(garbage))
    assert res.status_code == 401
    assert res.json() == EXPIRED


def test_expired_token_is_401(client, signer):
    res = client.get("/me", headers=bearer(signer.token(exp_in=-3600)))
    assert res.status_code == 401
    assert res.json() == EXPIRED


def test_token_expired_within_leeway_still_passes_but_beyond_fails(client, signer):
    assert (
        client.get("/me", headers=bearer(signer.token(exp_in=-3))).status_code == 200
    )  # 시계 어긋남 10초 허용
    assert client.get("/me", headers=bearer(signer.token(exp_in=-30))).status_code == 401


@pytest.mark.parametrize("role", ["anon", "service_role"])
def test_anon_and_service_role_tokens_are_401(client, signer, role):
    # 서명도 맞고 aud · iss도 맞지만 role이 사용자가 아님
    res = client.get("/me", headers=bearer(signer.token(role=role)))
    assert res.status_code == 401
    assert res.json() == EXPIRED


def test_token_without_role_is_401(client, signer):
    now = int(time.time())
    token = jwt.encode(
        {"sub": USER_ID, "aud": "authenticated", "iss": ISSUER, "exp": now + 60},
        signer.private_key,
        algorithm="ES256",
        headers={"kid": signer.kid},
    )
    assert client.get("/me", headers=bearer(token)).status_code == 401


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _hs256_by_hand(claims: dict, key: bytes, kid: str) -> str:
    """PyJWT는 공개 키 모양의 비밀키로 HS256 서명하는 것을 막으므로 손으로 만듦 (공격자는 라이브러리 제한을 안 받음)"""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    payload = _b64(json.dumps(claims).encode())
    signing_input = header + b"." + payload
    signature = _b64(hmac.new(key, signing_input, hashlib.sha256).digest())
    return (signing_input + b"." + signature).decode()


def test_hs256_forgery_is_401(client, signer):
    """알고리즘 바꿔치기: 알려진 kid를 달고 HS256으로 서명 — 공개 키를 비밀키처럼 쓰거나(고전 공격), 아무 비밀로 서명"""
    public_pem = signer.private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    now = int(time.time())
    claims = {
        "sub": USER_ID,
        "role": "authenticated",
        "aud": "authenticated",
        "iss": ISSUER,
        "exp": now + 3600,
    }
    forged = [
        _hs256_by_hand(claims, public_pem, signer.kid),
        jwt.encode(
            claims,
            "any-guessed-secret-long-enough-for-hs256-0123456789",
            algorithm="HS256",
            headers={"kid": signer.kid},
        ),
    ]
    for token in forged:
        res = client.get("/me", headers=bearer(token))
        assert res.status_code == 401, res.text
        assert res.json() == EXPIRED


def test_alg_none_is_401(client, signer):
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    now = int(time.time())
    token = f"{b64({'alg': 'none', 'typ': 'JWT', 'kid': signer.kid})}.{b64({'sub': USER_ID, 'role': 'authenticated', 'aud': 'authenticated', 'iss': ISSUER, 'exp': now + 3600})}."
    assert client.get("/me", headers=bearer(token)).status_code == 401


def test_unknown_kid_is_401(client, signer):
    other = Signer(kid="some-other-kid")  # 목록에 없는 키로 서명하고 모르는 kid를 달음
    res = client.get("/me", headers=bearer(other.token()))
    assert res.status_code == 401
    assert res.json() == EXPIRED


def test_token_signed_by_other_key_with_known_kid_is_401(client, signer):
    forger = Signer(kid=signer.kid)  # kid는 맞지만 개인 키가 다름 → 서명 불일치
    assert client.get("/me", headers=bearer(forger.token())).status_code == 401


def test_token_without_kid_is_401(client, signer):
    token = jwt.encode(
        {
            "sub": USER_ID,
            "role": "authenticated",
            "aud": "authenticated",
            "iss": ISSUER,
            "exp": int(time.time()) + 60,
        },
        signer.private_key,
        algorithm="ES256",
    )
    assert client.get("/me", headers=bearer(token)).status_code == 401


@pytest.mark.parametrize("override", [{"aud": "other"}, {"iss": "http://evil.test/auth/v1"}])
def test_wrong_audience_or_issuer_is_401(client, signer, override):
    assert client.get("/me", headers=bearer(signer.token(**override))).status_code == 401


def test_unknown_kid_refetches_once_then_waits(client, fake, signer):
    """모르는 kid가 연달아 와도 공개 키 목록을 매번 새로 받지 않음 (아무 kid나 보내 두드리는 것 방지)"""
    client.get("/me", headers=bearer(signer.token()))  # 처음 받기
    assert fake.jwks_calls == 1
    other = Signer(kid="rotating")
    for _ in range(5):
        assert client.get("/me", headers=bearer(other.token())).status_code == 401
    assert fake.jwks_calls == 1  # 30초 안이라 다시 안 받음


def test_key_rotation_new_kid_is_picked_up_after_min_refetch(make_client, fake, signer, monkeypatch):
    new_signer = Signer(kid="rotated")
    client = make_client(jwks_min_refetch_seconds=0)  # 간격 제한 없이 → 키 교체를 바로 반영
    assert client.get("/me", headers=bearer(signer.token())).status_code == 200
    fake.jwks_keys = [signer.jwk, new_signer.jwk]
    assert client.get("/me", headers=bearer(new_signer.token())).status_code == 200


def test_jwks_down_with_cache_keeps_working(make_client, fake, signer):
    client = make_client(
        jwks_cache_ttl_seconds=0, jwks_min_refetch_seconds=0
    )  # 캐시가 바로 낡음 → 다시 받아 보지만 실패
    assert client.get("/me", headers=bearer(signer.token())).status_code == 200
    fake.jwks_down = True
    res = client.get("/me", headers=bearer(signer.token()))
    assert res.status_code == 200  # 낡은 캐시로 계속 검사
    assert fake.jwks_calls >= 2


def test_jwks_down_without_cache_is_503_not_401(client, fake, signer):
    fake.jwks_down = True
    res = client.get("/me", headers=bearer(signer.token()))
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "AUTH_UNAVAILABLE"


def test_jwks_down_without_cache_still_401_for_header_problems(client, fake):
    fake.jwks_down = True
    assert client.get("/me").status_code == 401  # 헤더 없음은 키가 없어도 401
    assert (
        client.get("/me", headers={"Authorization": "Bearer abc"}).status_code == 401
    )  # 깨진 문자열도 키까지 안 감


def test_jwks_recovers_after_failure(make_client, fake, signer):
    client = make_client(jwks_min_refetch_seconds=0)
    fake.jwks_down = True
    assert client.get("/me", headers=bearer(signer.token())).status_code == 503
    fake.jwks_down = False
    assert client.get("/me", headers=bearer(signer.token())).status_code == 200


def test_jwks_with_only_non_ec_keys_is_treated_as_unavailable(client, fake, signer):
    fake.jwks_keys = [
        {"kty": "oct", "kid": signer.kid, "k": "c2VjcmV0"}
    ]  # HS256 비밀키가 목록에 섞여 와도 안 씀
    assert client.get("/me", headers=bearer(signer.token())).status_code == 503


def test_deleted_account_token_passes_until_expiry(client, signer):
    """알려진 한계(기록): 서버는 DB · Auth에 안 물으므로, 이미 삭제된 계정의 sub라도 서명 · 만료가 맞으면 통과함.
    만료(기본 1시간)까지만 통한다. 되돌릴 수 없는 쓰기(계정 삭제 등)는 그 함수에서 Auth에 한 번 더 확인해야 함 (설계 S4)."""
    deleted_user = "99999999-0000-0000-0000-000000000000"  # 어떤 DB에도 없는 계정
    live = signer.token(sub=deleted_user)
    assert client.get("/me", headers=bearer(live)).json() == {"id": deleted_user}
    assert (
        client.get("/me", headers=bearer(signer.token(sub=deleted_user, exp_in=-3600))).status_code == 401
    )  # 만료되면 끝


def test_token_never_appears_in_logs_or_error_bodies(client, signer, caplog):
    caplog.set_level("DEBUG")
    token = signer.token()
    client.get("/me", headers=bearer(token))
    garbled = "Bearer.secret-looking-value.xyz"
    res = client.get("/me", headers=bearer(garbled))
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert token not in logs
    assert garbled not in logs and "secret-looking-value" not in logs
    assert "secret-looking-value" not in res.text

"""로그인 토큰 검사 — 본 레포 `_shared/auth.ts` `requireUser`의 서버판.

Edge는 요청마다 Supabase Auth에 "이 토큰 누구야?"를 물었지만(auth.getUser), 서버는 공개 키(JWKS)로 서명을 직접 확인함 (설계 S4).
확인하는 것: 알고리즘 ES256만 · kid가 공개 키 목록에 있음 · 서명 · 만료(exp) · 발급자(iss) · 대상(aud) · 역할(role == authenticated) · sub
→ 아래 중 하나라도 어긋나면 401 AUTH_REQUIRED
   헤더 없음 · 깨진 문자열 · 만료 · anon · service_role · HS256 위조("alg" 바꿔치기) · alg none · 모르는 kid · 다른 발급자 · 다른 대상
→ 공개 키 목록을 못 받고 캐시도 없을 때만 503 AUTH_UNAVAILABLE

알려진 한계: DB · Auth에 안 물으므로 탈퇴(삭제)한 계정의 토큰도 만료(기본 1시간)까지는 통과함.
  되돌릴 수 없는 쓰기(계정 삭제 등)는 해당 함수에서 Supabase Auth에 한 번 더 확인할 것 (설계 S4, 2차 이후).
토큰 값은 로그 · 오류 본문 · 예외 메시지에 안 넣음.
"""

import re
from dataclasses import dataclass
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request

from .config import Settings
from .errors import AppError
from .jwks import JwksCache, JwksUnavailable

# `_shared/auth.ts`와 같은 문구 · 같은 정규식 (대소문자 무시, 앞뒤 공백 제거)
_BEARER = re.compile(r"^Bearer\s+(.+)$", re.IGNORECASE)
MSG_REQUIRED = "로그인이 필요해요"
MSG_EXPIRED = "로그인이 만료됐어요. 다시 로그인해 주세요"
MSG_UNAVAILABLE = "로그인 확인이 잠시 안 돼요. 잠시 뒤에 다시 해 주세요"

ALLOWED_ALGORITHMS = ["ES256"]


@dataclass(frozen=True)
class AuthedUser:
    user_id: str
    claims: dict[str, Any]


def extract_bearer(header: str | None) -> str | None:
    match = _BEARER.match((header or "").strip())
    return match.group(1).strip() if match else None


async def verify_token(token: str, settings: Settings, jwks: JwksCache) -> AuthedUser:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise AppError("AUTH_REQUIRED", MSG_EXPIRED) from None

    kid = header.get("kid")
    # alg 바꿔치기(HS256 · none) 차단: 헤더가 뭐라 하든 ES256만 받음
    if header.get("alg") not in ALLOWED_ALGORITHMS or not isinstance(kid, str) or not kid:
        raise AppError("AUTH_REQUIRED", MSG_EXPIRED)

    try:
        key = await jwks.get_key(kid)
    except JwksUnavailable:
        raise AppError("AUTH_UNAVAILABLE", MSG_UNAVAILABLE) from None
    if key is None:
        raise AppError("AUTH_REQUIRED", MSG_EXPIRED)

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=ALLOWED_ALGORITHMS,
            audience=settings.jwt_audience,
            issuer=settings.issuer,
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError:
        raise AppError("AUTH_REQUIRED", MSG_EXPIRED) from None

    # anon · service_role 키로 서명된 토큰은 aud가 맞아도 사용자가 아님
    if claims.get("role") != "authenticated" or not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise AppError("AUTH_REQUIRED", MSG_EXPIRED)
    return AuthedUser(user_id=claims["sub"], claims=claims)


async def require_user(request: Request) -> AuthedUser:
    """FastAPI 의존성 — 라우터에 `user: CurrentUser`로 씀"""
    token = extract_bearer(request.headers.get("authorization"))
    if token is None:
        raise AppError("AUTH_REQUIRED", MSG_REQUIRED)
    return await verify_token(token, request.app.state.settings, request.app.state.jwks)


CurrentUser = Annotated[AuthedUser, Depends(require_user)]

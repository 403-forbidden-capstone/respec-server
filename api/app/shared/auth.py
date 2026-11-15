"""본 레포 supabase/functions/_shared/auth.ts의 서버판 (기준 b140f70) — 호출한 쪽 확인 (사용자 DB 클라이언트 · 서비스 키)

  require_user_db     — TS `requireUser`. 토큰 확인은 서버의 `app.core.auth.require_user`(JWKS 서명 검사)가 이미 함 → 그 결과에
                        `AuthedUser { userId, token, db }`의 token · db(= 이 사용자의 RLS를 따르는 클라이언트)만 더함
  is_service_call     — 배치 · 서버끼리 부르는 호출이 서비스 키를 실었는지 직접 비교 (apikey · Authorization 둘 다 봄)
  require_service_call — 같은 비교를 틀리면 AUTH_REQUIRED("서버 전용 함수예요")로 던짐
서버는 서비스 키가 SUPABASE_SECRET_KEY 하나라 `service_keys()`도 하나 (Edge는 legacy + 새 키 사전).
달라진 점: Edge는 사용자 클라이언트를 먼저 만들고(키 설정이 없으면 NOT_CONFIGURED) 토큰을 확인했지만, 서버는 토큰 확인 뒤에 클라이언트를 만듦.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from ..core.auth import extract_bearer, require_user
from ..core.errors import AppError
from .env import service_keys
from .keys import matches_any_key
from .supabase import Db, create_user_client


@dataclass(frozen=True)
class AuthedUserDb:
    user_id: str
    token: str
    db: Db  # 이 사용자의 RLS를 따르는 클라이언트


async def require_user_db(request: Request) -> AuthedUserDb:
    """FastAPI 의존성 — 라우터에 `user: CurrentUserDb`로 씀. 토큰이 없거나 틀리면 core.auth.require_user와 같은 AUTH_REQUIRED"""
    user = await require_user(request)
    token = extract_bearer(request.headers.get("authorization"))
    assert token is not None  # require_user가 통과했으면 있음
    return AuthedUserDb(user_id=user.user_id, token=token, db=create_user_client(f"Bearer {token}"))


CurrentUserDb = Annotated[AuthedUserDb, Depends(require_user_db)]


def is_service_call(request: Request) -> bool:
    return matches_any_key(request.headers, service_keys())


def require_service_call(request: Request) -> None:
    if not is_service_call(request):
        raise AppError("AUTH_REQUIRED", "서버 전용 함수예요")

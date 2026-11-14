"""GET /me — 토큰이 누구 것인지 확인 (1-4 시험 · 배포 직후 smoke.sh의 점검용 시험 계정 확인)

응답은 id만. 이메일 · 이름 · 토큰 안의 다른 값은 돌려주지 않음.
"""

from fastapi import APIRouter

from ..core.auth import CurrentUser

router = APIRouter()


@router.get("/me")
async def me(user: CurrentUser) -> dict[str, str]:
    return {"id": user.user_id}

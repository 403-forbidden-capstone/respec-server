"""POST /fn/alarm-roll — 본 레포 Edge `alarm-roll/index.ts`의 서버판 (반복 다음 회차 만들기, 상세설계서 5장 5.3 #17 · 7장 7.1.5)

두 길을 같은 주소가 가름 (Edge verify_jwt = false와 같음 — 키 모양이 둘일 수 있고 두 길이 한 함수라서 코드가 가름):
  서비스 호출(서버 키를 실은 요청)  배치 길 — 일이 있는 묶음 50개 · 길찾기 20번 · 40초 안. service_role이라 RLS를 안 타므로 읽기 · 쓰기마다 사용자로 직접 좁힘 (shared/roll.py)
  로그인 토큰 요청                  본인 길 — 읽기는 부른 사람의 RLS 클라이언트, 쓰기는 service_role. 길찾기 최대 4회차. 요청 { series_id? } (본문은 {} 라도 보냄)
응답 { created, moved_alarm_ids, pending, ring_packages, canceled_alarm_ids, kakao_calls } (본인 길만 ring_packages를 채움)
순서: 길 가르기(서비스 호출이면 본문은 안 읽음) → 토큰 확인(401) → 입력 검사(400) → 실행.

쓰는 이름: app.shared.auth.is_service_call(request) · app.shared.supabase.create_service_client() · create_user_client("Bearer <토큰>")
  · app.kakao.kakao_travel_minutes(client, key, *, origin, destination, transport) -> {"minutes", "partial"}
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from ..core.auth import extract_bearer, require_user
from ..core.read import Reader, read_json
from ..kakao import kakao_travel_minutes
from ..shared.auth import is_service_call
from ..shared.js import from_ms, ms
from ..shared.roll import RollDeps, TravelLookup, read_roll_request, run_roll
from ..shared.supabase import create_service_client, create_user_client

router = APIRouter()


def _lookup_of(request: Request) -> TravelLookup | None:
    """KAKAO_REST_API_KEY가 있으면 카카오 길찾기 1번, 없으면 None (roll이 AUTH와 같게 다룸)"""
    secret = request.app.state.settings.kakao_rest_api_key
    key = secret.get_secret_value().strip() if secret is not None else ""
    if not key:
        return None
    client = request.app.state.http

    async def lookup(*, origin: Any, destination: Any, transport: Any) -> dict[str, Any]:
        return await kakao_travel_minutes(
            client, key, origin=origin, destination=destination, transport=transport
        )

    return lookup


@router.post("/fn/alarm-roll")
async def alarm_roll(request: Request) -> dict[str, Any]:
    now = from_ms(ms(datetime.now(UTC)))  # Edge의 new Date() = 밀리초 단위
    lookup = _lookup_of(request)

    if is_service_call(request):
        service = create_service_client()
        return dict(
            await run_roll(RollDeps(path="batch", now=now, read=service, write=service, lookup=lookup))
        )

    user = await require_user(request)
    root = Reader.root(read_json(await request.body()))
    series_id = read_roll_request(root).get("seriesId")
    root.finish()
    token = extract_bearer(request.headers.get("authorization")) or ""
    user_db = create_user_client(f"Bearer {token}")
    deps = RollDeps(path="own", now=now, read=user_db, write=create_service_client(), lookup=lookup)
    return dict(await run_roll(deps, user_id=user.user_id, series_id=series_id))

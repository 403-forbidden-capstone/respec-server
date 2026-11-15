"""appointment-edit — 본 레포 Edge `appointment-edit/index.ts`의 서버판 (`POST /fn/appointment-edit`)

일정 고치기 · 지우기 · 되돌리기 · 공동 약속에서 빠지기 (상세설계서 7장 7.1.3 · 7.1.5, 화면 H03 편집 시트 · L5 PR 6, 이슈 #276)
  요청:  { action: 'UPDATE' | 'DELETE' | 'RESTORE' | 'LEAVE', appointment_id, scope?: 'this' | 'following', patch?, confirmed? } — 모양 · 설명은 app/shared/edit_request.py
  응답:  모두 { appointment_id, ring_packages, canceled_alarm_ids, warnings } + 동작별 칸 (app/shared/edit_run.py)
  오류:  VALIDATION_FAILED · NOT_FOUND · FORBIDDEN(공동 약속은 만든 사람만 — 만든 사람 칸이 빈 약속은 참여한 그룹원) · UNPROCESSABLE(출발 시각까지 지남 · 알람 순서 어긋남 · 기록 있는 일정 · 지난 공동 약속 · 공동 약속 반복 고치기) · ROUTE_UNAVAILABLE
순서는 Edge와 같음: 토큰 확인(401) → 입력 검사(400) → 계산. 카카오 키는 없어도 됨(장소를 바꿀 때만 쓰고, 없으면 길찾기 없이 근사값으로 내려감).
읽기 · 쓰기 모두 부른 사람의 RLS 클라이언트로 함 — service 클라이언트는 edit_run.py가 정한 곳에만.
"""

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from ..core.auth import CurrentUser
from ..core.read import Reader, read_json
from ..kakao import kakao_travel_minutes
from ..shared.edit_request import read_edit_request
from ..shared.edit_run import EditCtx, run_edit
from ..shared.notify import NotifyOptions
from ..shared.supabase import create_service_client, create_user_client

router = APIRouter()


def _now() -> datetime:
    """new Date() — 밀리초 단위 (Date는 마이크로초가 없음)"""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def _env(name: str) -> str | None:
    """getEnv: 앞뒤 공백을 지우고, 비면 없는 것으로"""
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _kakao_lookup(request: Request) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None:
    """키가 없으면 None — 장소를 바꿀 때 길찾기 없이 근사값으로 내려감"""
    secret = request.app.state.settings.kakao_rest_api_key
    key = secret.get_secret_value().strip() if secret is not None else ""
    if not key:
        return None
    http = request.app.state.http

    async def lookup(
        args: dict[str, Any],
    ) -> dict[str, Any]:  # route.py TravelDeps.kakao — {origin, destination, transport}
        return await kakao_travel_minutes(
            http,
            key,
            origin=args["origin"],
            destination=args["destination"],
            transport=args["transport"],
        )

    return lookup


@router.post("/fn/appointment-edit")
async def appointment_edit(request: Request, user: CurrentUser) -> dict[str, Any]:
    root = Reader.root(read_json(await request.body()))
    edit = read_edit_request(root)
    root.finish()

    ctx = EditCtx(
        user_id=user.user_id,
        user_db=create_user_client(request.headers["authorization"]),
        service=create_service_client(),
        now=_now(),
        kakao=_kakao_lookup(request),
        push=NotifyOptions(access_token=_env("EXPO_ACCESS_TOKEN")),
    )
    return await run_edit(ctx, edit)

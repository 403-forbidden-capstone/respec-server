"""상태 확인 — 토큰 없이 부름 (nginx · smoke.sh · 모니터링)

  GET /health       프로세스가 떠 있는가만 (DB · 외부를 안 부름 → 배포 직후 · 컨테이너 헬스체크용)
  GET /health/deep  공개 키(JWKS) · DB(PostgREST) · 스케줄러 최근 실행까지. 하나라도 실패하면 503
응답에는 검사 이름과 ok / fail만 — 주소 · 키 · 오류 원문은 안 실음.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Request

from ..core.http import EdgeJSONResponse

router = APIRouter()
log = logging.getLogger("respec.health")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _check_auth(request: Request) -> str:
    return "ok" if await request.app.state.jwks.refresh_if_stale() else "fail"


async def _read_heartbeat(request: Request) -> list[dict] | None:
    """server_heartbeat의 alive_at 최근 값들 (표는 본 레포 M1 마이그레이션). 못 읽으면 None"""
    settings = request.app.state.settings
    if settings.supabase_secret_key is None:
        return None
    secret = settings.supabase_secret_key.get_secret_value()
    headers = {"apikey": secret}
    # 옛 service_role JWT 키는 Authorization에도 싣고, 새 sb_secret_ 키는 apikey만
    if secret.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {secret}"
    url = f"{settings.supabase_url.rstrip('/')}/rest/v1/server_heartbeat"
    try:
        resp = await request.app.state.http.get(
            url,
            params={"select": "alive_at", "order": "alive_at.desc.nullslast", "limit": "1"},
            headers=headers,
        )
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("heartbeat 읽기 실패: %s", type(exc).__name__)
        return None
    return rows if isinstance(rows, list) else None


def _scheduler_ok(rows: list[dict], stale_minutes: int) -> bool:
    alive = rows[0].get("alive_at") if rows else None
    if not alive:
        return False
    try:
        at = datetime.fromisoformat(alive.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(UTC) - at <= timedelta(minutes=stale_minutes)


@router.get("/health/deep")
async def health_deep(request: Request) -> EdgeJSONResponse:
    settings = request.app.state.settings
    auth_result, rows = await asyncio.gather(_check_auth(request), _read_heartbeat(request))

    checks = {
        "auth": auth_result,
        "db": "ok" if rows is not None else "fail",
    }
    if settings.health_require_scheduler:
        checks["scheduler"] = (
            "ok" if rows is not None and _scheduler_ok(rows, settings.scheduler_stale_minutes) else "fail"
        )
    else:
        checks["scheduler"] = "skipped"

    healthy = all(v in ("ok", "skipped") for v in checks.values())
    return EdgeJSONResponse(
        {"status": "ok" if healthy else "fail", "checks": checks}, status_code=200 if healthy else 503
    )

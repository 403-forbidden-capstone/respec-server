"""상태 확인 — 토큰 없이 부름 (nginx · smoke.sh · 모니터링)

  GET · HEAD /health  프로세스가 떠 있는가만 (DB · 외부를 안 부름 → 배포 직후 · 컨테이너 헬스체크용). 본문에 version(배포 태그)
  GET · HEAD /health/deep  공개 키(JWKS) · DB(PostgREST) · 스케줄러 최근 실행까지. 하나라도 실패하면 503.
                      토큰 없이 부를 때마다 서버 키로 DB를 부르므로 결과를 짧게(기본 5초) 재사용 — 실패 결과도 (장애 중 DB를 두드리지 않게)
응답에는 검사 이름과 ok / fail만 — 주소 · 키 · 오류 원문은 안 실음.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Request

from ..core.http import EdgeJSONResponse

router = APIRouter()
log = logging.getLogger("respec.health")


def _now() -> float:
    return time.monotonic()


@router.api_route(
    "/health", methods=["GET", "HEAD"]
)  # HEAD: 감시 서비스(UptimeRobot 등)가 HEAD로 부르는 경우
async def health(request: Request) -> dict[str, str]:
    return {"status": "ok", "version": request.app.state.settings.app_version}


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


@router.api_route(
    "/health/deep", methods=["GET", "HEAD"]
)  # 감시가 이 주소를 부르므로 HEAD도 받음 (같은 캐시를 씀)
async def health_deep(request: Request) -> EdgeJSONResponse:
    settings = request.app.state.settings
    ttl = settings.health_deep_cache_seconds
    # 락 안에서 계산 → 동시에 여러 요청이 와도 확인은 한 번, 나머지는 그 결과를 같이 씀
    async with request.app.state.deep_lock:
        cached = request.app.state.deep_cache
        if ttl > 0 and cached is not None and _now() - cached[0] < ttl:
            payload, status = cached[1], cached[2]
        else:
            payload, status = await _deep_check(request)
            request.app.state.deep_cache = (_now(), payload, status)
    return EdgeJSONResponse(payload, status_code=status)


async def _deep_check(request: Request) -> tuple[dict, int]:
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
    return {"status": "ok" if healthy else "fail", "checks": checks}, 200 if healthy else 503

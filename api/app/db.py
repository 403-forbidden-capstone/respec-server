"""스케줄러가 쓰는 DB 접근 — PostgREST로 server_routes · server_heartbeat · DB 함수를 부름 (표는 본 레포 M1 마이그레이션)

서버 전용 키(service_role 권한)로 부르므로 RLS를 건너뜀 → 이 파일은 아래 네 가지만 함:
  server_routes(kind = 'job') 읽기 / server_heartbeat의 alive_at · last_success_at 쓰기 / DB 함수 부르기(읽기 전용 함수만 씀)
실패 로그에는 오류 종류만 남김 (주소 · 키 · 응답 본문은 안 찍음).
"""

import logging
from datetime import datetime

import httpx

from .core.config import Settings
from .core.supabase import rest_headers, rest_url

log = logging.getLogger("respec.db")


class DbError(Exception):
    """DB를 못 불렀음 (네트워크 · 상태 코드 · 모양) — 호출한 쪽이 작업 하나를 건너뛰고 계속 돎"""


class Db:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        if settings.supabase_secret_key is None:
            raise ValueError("SUPABASE_SECRET_KEY가 없음")
        self._base = settings.supabase_url
        self._client = httpx.Client(
            timeout=settings.http_timeout_seconds,
            transport=transport,
            headers=rest_headers(settings.supabase_secret_key.get_secret_value()),
        )

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, rest_url(self._base, path), **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 상태 번호는 남김 (401 = 키 문제 · 404 = 표 없음 · 5xx = Supabase 쪽을 가르는 데 필요). 주소 · 본문은 안 남김
            raise DbError(f"HTTPStatusError {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            raise DbError(type(exc).__name__) from None
        return response

    def read_job_modes(self) -> dict[str, str]:
        """작업(kind = 'job')별 mode. 못 읽으면 DbError — 부르는 쪽이 edge로 봄 (표를 못 읽으면 서버는 아무것도 안 함)"""
        response = self._call("GET", "server_routes", params={"select": "name,mode", "kind": "eq.job"})
        try:
            rows = response.json()
        except ValueError:
            raise DbError("모양 이상") from None
        if not isinstance(rows, list):
            raise DbError("모양 이상")
        return {r["name"]: r["mode"] for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)}

    def heartbeat(self, names: list[str], at: datetime) -> None:
        """alive_at = 스케줄러가 돌고 있음 (mode 무관). last_success_at은 안 건드림"""
        self._upsert([{"job_name": n, "alive_at": at.isoformat()} for n in names])

    def record_success(self, name: str, at: datetime) -> None:
        """last_success_at = 그 작업이 서버에서 mode = server로 성공함. pg_cron(run_job)이 이것만 보고 Edge 호출을 건너뜀"""
        self._upsert([{"job_name": name, "last_success_at": at.isoformat()}])

    def _upsert(self, rows: list[dict]) -> None:
        # merge-duplicates: 본문에 있는 칸만 고침 (alive_at만 보내면 last_success_at은 그대로)
        self._call(
            "POST",
            "server_heartbeat",
            params={"on_conflict": "job_name"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=rows,
        )

    def rpc(self, name: str, params: dict) -> list[dict]:
        response = self._call("POST", f"rpc/{name}", json=params)
        try:
            rows = response.json()
        except ValueError:
            raise DbError("모양 이상") from None
        if not isinstance(rows, list):
            raise DbError("모양 이상")
        return rows

"""공개 키(JWKS) 받기 · 캐시 — 토큰 서명(ES256) 확인용.

웹 비유: 로그인 토큰은 도장 찍힌 편지, JWKS는 도장 원본 목록. 서버가 목록을 받아 두면 요청마다 Supabase에 물어보지 않아도 도장을 대조할 수 있음.

동작
  · 처음 필요할 때 받아서 보관 (TTL 안이면 그대로 씀)
  · TTL이 지나면 새로 받아 봄. 이때 새로 받는 요청 하나만 기다리고, 나머지는 기다리지 않고 낡은 캐시로 검사
    (JWKS가 느려도 로그인한 사용자 요청이 줄줄이 늦어지지 않음). 못 받으면 낡은 캐시로 계속 검사
  · 캐시도 없고 못 받으면 JwksUnavailable → 503 (401이 아님: 401이면 앱이 사용자를 로그아웃시킴)
  · 모르는 kid가 오면 (키 교체 가능성) 새로 받아 봄. 단 min_refetch 간격 안에서는 다시 안 받음.
    새로 받아 봤는데도 없으면 401. 받기가 실패해서 가릴 수 없으면 503 (정상 사용자를 로그아웃시키지 않으려고)
  · 응답이 깨졌거나(객체 아님 · keys가 배열 아님) 키 하나가 깨졌으면 그 키만 건너뜀. 쓸 수 있는 키가 하나도 없으면 실패로 봄
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt

log = logging.getLogger("respec.jwks")

NO_KEY_RETRY_SECONDS = 5.0


class JwksUnavailable(Exception):
    """공개 키 목록이 없거나, 모르는 kid를 가려 줄 새 목록을 받을 수 없음"""


def _parse_keys(body: Any) -> dict[str, Any]:
    """JWKS 본문 → {kid: 공개 키}. ES256(타원곡선 EC) 키만 받고, 깨진 항목은 그 키만 건너뜀"""
    if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
        raise ValueError("JWKS 본문 모양이 아님")
    keys: dict[str, Any] = {}
    skipped = 0
    for jwk in body["keys"]:
        try:
            if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or not isinstance(jwk.get("kid"), str):
                skipped += 1  # HS256 비밀키 등 EC가 아닌 키는 목록에 있어도 안 씀
                continue
            keys[jwk["kid"]] = jwt.PyJWK(jwk).key
        except Exception:  # noqa: BLE001 — 키 하나가 어떻게 깨졌든 목록 전체를 버리지 않음
            skipped += 1
    if skipped:
        log.warning("JWKS에서 쓸 수 없는 항목 %d개를 건너뜀", skipped)
    if not keys:
        raise ValueError("JWKS에 쓸 수 있는 키가 없음")
    return keys


class JwksCache:
    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        ttl_seconds: float,
        min_refetch_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._url = url
        self._ttl = ttl_seconds
        self._min_refetch = min_refetch_seconds
        self._clock = clock
        self._keys: dict[str, Any] = {}  # kid → 공개 키 객체
        self._fetched_at: float | None = None  # 마지막으로 "시도한" 시각 (성공이든 실패든 — 연타 방지)
        self._ok_at: float | None = None  # 마지막으로 받기 성공한 시각
        self._last_failed = False  # 가장 최근 시도가 실패였는가
        self._generation = 0  # 시도가 끝날 때마다 +1 (기다리던 요청이 그 사이 시도가 있었는지 알아봄)
        self._lock = asyncio.Lock()

    async def _fetch(self) -> None:
        # await 전에 동기적으로 표시 → 같은 순간 들어온 다른 요청은 _may_refetch()가 거짓이라 다시 안 받음
        self._fetched_at = self._clock()
        try:
            resp = await self._client.get(self._url)
            resp.raise_for_status()
            self._keys = _parse_keys(resp.json())
            self._ok_at = self._clock()
            self._last_failed = False
        except Exception as exc:  # noqa: BLE001 — 네트워크 · 본문 · 파싱 어떤 실패든 낡은 캐시로 버팀
            self._last_failed = True
            # 주소 · 응답 본문은 안 남김 (종류만)
            log.warning("JWKS 갱신 실패: %s", type(exc).__name__)
        finally:
            self._generation += 1

    def _stale(self) -> bool:
        return self._ok_at is None or self._clock() - self._ok_at >= self._ttl

    def _may_refetch(self) -> bool:
        # 키가 하나도 없을 땐 더 자주 (처음 한 번 실패했다고 한참 전부 503이 되지 않게). 있을 땐 min_refetch 간격
        interval = self._min_refetch if self._keys else min(self._min_refetch, NO_KEY_RETRY_SECONDS)
        return self._fetched_at is None or self._clock() - self._fetched_at >= interval

    async def get_key(self, kid: str) -> Any | None:
        """kid에 맞는 공개 키. 없으면 None (→ 401). 목록이 없거나 가릴 수 없으면 JwksUnavailable (→ 503)"""
        # 낡은 캐시를 새로 받기 — 락 없이: 이 요청이 받는 동안 다른 요청은 _may_refetch()가 거짓이라 낡은 키로 바로 진행
        if self._keys and self._stale() and self._may_refetch():
            await self._fetch()
        key = self._keys.get(kid)
        if key is not None:
            return key

        # 키 목록이 없거나 모르는 kid — 새 목록이 필요. 락 안에서 한 번만 받고, 기다리던 요청은 그 결과를 같이 씀
        generation = self._generation
        async with self._lock:
            if self._generation == generation and self._may_refetch():
                await self._fetch()
        key = self._keys.get(kid)
        if key is not None:
            return key
        if not self._keys or self._last_failed:
            raise JwksUnavailable
        return None

    async def refresh_if_stale(self) -> bool:
        """/health/deep용 — 키가 있는가 (낡았으면 한 번 새로 받아 봄)"""
        if self._stale() and self._may_refetch():
            await self._fetch()
        return bool(self._keys)

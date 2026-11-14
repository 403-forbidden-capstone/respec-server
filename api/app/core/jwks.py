"""공개 키(JWKS) 받기 · 캐시 — 토큰 서명(ES256) 확인용.

웹 비유: 로그인 토큰은 도장 찍힌 편지, JWKS는 도장 원본 목록. 서버가 목록을 받아 두면 요청마다 Supabase에 물어보지 않아도 도장을 대조할 수 있음.

동작
  · 처음 필요할 때 받아서 보관 (TTL 안이면 그대로 씀)
  · TTL이 지나면 새로 받아 봄. 못 받으면 낡은 캐시로 계속 검사 (Supabase가 잠깐 안 돼도 로그인한 사용자는 계속 씀)
  · 캐시도 없고 못 받으면 JwksUnavailable → 503 (401이 아님: 401이면 앱이 사용자를 로그아웃시킴)
  · 모르는 kid가 오면 (키 교체 가능성) 한 번 새로 받아 봄. 단 min_refetch 간격 안에서는 다시 안 받음
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
    """공개 키 목록이 없고 받을 수도 없음"""


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
        self._lock = asyncio.Lock()

    @property
    def has_keys(self) -> bool:
        return bool(self._keys)

    @property
    def age_seconds(self) -> float | None:
        return None if self._ok_at is None else self._clock() - self._ok_at

    async def _fetch(self) -> None:
        self._fetched_at = self._clock()
        try:
            resp = await self._client.get(self._url)
            resp.raise_for_status()
            body = resp.json()
            keys: dict[str, Any] = {}
            for jwk in body.get("keys", []):
                kid = jwk.get("kid")
                if (
                    kid and jwk.get("kty") == "EC"
                ):  # ES256 = 타원곡선 키만 받음 (HS256 비밀키 등은 목록에 있어도 무시)
                    keys[kid] = jwt.PyJWK(jwk).key
            if not keys:
                raise ValueError("JWKS에 쓸 수 있는 키가 없음")
            self._keys = keys
            self._ok_at = self._clock()
        except (httpx.HTTPError, ValueError, jwt.PyJWTError, KeyError, TypeError) as exc:
            # 주소 · 응답 본문은 안 남김 (종류만)
            log.warning("JWKS 갱신 실패: %s", type(exc).__name__)

    def _stale(self) -> bool:
        return self._ok_at is None or self._clock() - self._ok_at >= self._ttl

    async def get_key(self, kid: str) -> Any | None:
        """kid에 맞는 공개 키. 없으면 None (→ 401). 목록 자체가 없고 못 받으면 JwksUnavailable"""
        async with self._lock:
            if self._stale() and self._may_refetch():
                await self._fetch()
            key = self._keys.get(kid)
            if key is None and self._keys and self._may_refetch():
                # 모르는 kid — 키 교체일 수 있어 한 번만 새로 받아 봄
                await self._fetch()
                key = self._keys.get(kid)
            if not self._keys:
                raise JwksUnavailable
            return key

    def _may_refetch(self) -> bool:
        # 키가 하나도 없을 땐 더 자주 (처음 한 번 실패했다고 30초간 전부 503이 되지 않게). 있을 땐 min_refetch 간격
        interval = self._min_refetch if self._keys else min(self._min_refetch, NO_KEY_RETRY_SECONDS)
        return self._fetched_at is None or self._clock() - self._fetched_at >= interval

    async def refresh_if_stale(self) -> bool:
        """/health/deep용 — 키가 있는가 (낡았으면 한 번 새로 받아 봄)"""
        async with self._lock:
            if self._stale() and self._may_refetch():
                await self._fetch()
            return bool(self._keys)

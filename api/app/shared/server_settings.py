"""서버 전용 설정 표 읽기 (완결점 2-3, 설계 S7a) — 예측(CatBoost) 켜기 조건 3개를 본 레포 `public.server_settings` 표에서 읽음

표(본 레포 #525, `20261115090000_server_settings.sql`)의 키 3개 — Edge 비밀 PREPARE_ML_ENABLED · _ALLOWED_USER_IDS · _CODEBOOK_VERSION과 같은 값 모양:
  prepare_ml_enabled            '0' · '1' (켜짐은 '1'일 때만)
  prepare_ml_allowed_user_ids   비어 있거나 쉼표로 이은 UUID (읽는 규칙은 prepare_ml.may_use_ml)
  prepare_ml_codebook_version   공식 매핑 판 이름 (비어 있으면 예측 안 함)
혜린님이 SSH 없이 Supabase SQL 편집기에서 바꾸면 서버가 곧(캐시 시간 안에) 읽음. 예측 서버 주소 · 토큰(PREPARE_ML_URL · _API_TOKEN)은 비밀이라 env 그대로.

**표가 항상 이김**: 이 3개는 env로 읽지 않음(Settings에 칸 자체가 없음 — 서버 api.env에 옛 PREPARE_ML_ENABLED 등이 남아 있어도 무시됨).

읽기: 서비스 클라이언트(RLS를 건너뜀)로 표 전체(3줄)를 한 번에, **메모리에 CACHE_SECONDS(30초)** 동안 재사용 — 요청마다 DB를 부르지 않음.
  · 읽기 실패(DB 오류 · 표 없음 · 서비스 키 없음)이면 **직전에 읽은 값을 계속 씀**(source "stale"), 처음부터 실패면 **예측 꺼짐**(source "unavailable").
    실패 뒤에는 RETRY_SECONDS(10초) 뒤에 다시 시도(장애 중 DB를 두드리지 않게). 로그는 오류 코드만(값 · 키 · 주소 없음).
  · 줄이 없거나 값이 비어 있으면 그 설정은 꺼짐(fail closed).
`/health/deep`의 예측 설정 항목이 이 값을 보여 줌(켜짐 · 코드북 판 · 허용 계정 수 — 계정 id는 안 보임).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from .prepare_ml import allowed_user_id_count
from .supabase import create_service_client

log = logging.getLogger("respec.server_settings")

TABLE = "server_settings"
KEYS = (
    "prepare_ml_enabled",
    "prepare_ml_allowed_user_ids",
    "prepare_ml_codebook_version",
    "pure_prep_apply_enabled",
    "pure_prep_allowed_user_ids",
    "pure_prep_survey_enabled",
    "pure_prep_survey_allowed_user_ids",
)
CACHE_SECONDS = 30.0  # 표를 바꾼 뒤 서버가 이 시간 안에 읽음 (/health/deep 결과 캐시 5초를 더해도 1분 안)
RETRY_SECONDS = 10.0  # 읽기에 실패한 뒤 다시 시도하기까지

Source = Literal["table", "stale", "unavailable"]


@dataclass(frozen=True)
class PredictSettings:
    enabled: bool = False
    allowed_user_ids: str | None = None  # 원문(쉼표로 이은 UUID) — may_use_ml이 읽음
    codebook_version: str | None = None  # 앞뒤 공백을 지운 판 이름, 비면 None
    # Independent, server-owned allowlists. All new switches default OFF.
    pure_prep_apply_enabled: bool = False
    pure_prep_allowed_user_ids: str | None = None
    pure_prep_survey_enabled: bool = False
    pure_prep_survey_allowed_user_ids: str | None = None

    @property
    def allowed_count(self) -> int:
        """허용 계정 수 (UUID 모양인 서로 다른 것만 — 계정 id 자체는 어디에도 안 보임)"""
        return allowed_user_id_count(self.allowed_user_ids)


OFF = PredictSettings()


@dataclass(frozen=True)
class SettingsRead:
    settings: PredictSettings
    source: Source  # table = 방금(캐시 안에서) 표에서 읽음 · stale = 읽기에 실패해 직전 값 · unavailable = 한 번도 못 읽어 꺼짐


class SettingsReadError(Exception):
    """읽기 실패 — 메시지에는 오류 코드만 (값 · 키 · 주소를 안 담음)"""


def parse_rows(rows: dict[str, str]) -> PredictSettings:
    """표 줄(키 → 값)을 설정으로. 줄이 없거나 비어 있으면 그 설정은 꺼짐"""
    allowed = rows.get("prepare_ml_allowed_user_ids")
    return PredictSettings(
        enabled=(rows.get("prepare_ml_enabled") or "").strip() == "1",
        allowed_user_ids=allowed if allowed and allowed.strip() else None,
        codebook_version=(rows.get("prepare_ml_codebook_version") or "").strip() or None,
        pure_prep_apply_enabled=(rows.get("pure_prep_apply_enabled") or "").strip() == "1",
        pure_prep_allowed_user_ids=(rows.get("pure_prep_allowed_user_ids") or "").strip() or None,
        pure_prep_survey_enabled=(rows.get("pure_prep_survey_enabled") or "").strip() == "1",
        pure_prep_survey_allowed_user_ids=(rows.get("pure_prep_survey_allowed_user_ids") or "").strip() or None,
    )


async def read_table() -> dict[str, str]:
    """표의 허용된 설정 줄을 한 번에 읽음. 실패는 SettingsReadError(코드만)"""
    result = await create_service_client().from_(TABLE).select("key, value")
    if result.error:
        raise SettingsReadError(str((result.error or {}).get("code") or "UNKNOWN"))
    data = result.data
    if not isinstance(data, list):
        raise SettingsReadError("BAD_SHAPE")
    rows: dict[str, str] = {}
    for row in data:
        if isinstance(row, dict) and row.get("key") in KEYS:
            rows[row["key"]] = "" if row.get("value") is None else str(row["value"])
    return rows


class SettingsCache:
    """읽은 값을 ttl초 동안 재사용. 동시에 여러 요청이 만료를 만나도 읽기는 한 번(나머지는 그 결과를 같이 씀)"""

    def __init__(
        self,
        fetch: Callable[[], Awaitable[dict[str, str]]] = read_table,
        *,
        ttl: float = CACHE_SECONDS,
        retry: float = RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._retry = retry
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        self._lock = asyncio.Lock()
        self._settings = OFF
        self._ever_read = False
        self._last_failed = False
        self._next_at = float("-inf")  # 이 시각이 지나야 다시 읽음

    def _read(self) -> SettingsRead:
        if not self._ever_read:
            return SettingsRead(OFF, "unavailable")
        return SettingsRead(self._settings, "stale" if self._last_failed else "table")

    async def get(self) -> SettingsRead:
        if self._clock() < self._next_at:
            return self._read()
        async with self._lock:
            if self._clock() < self._next_at:  # 기다리는 사이 다른 요청이 읽어 둠
                return self._read()
            try:
                settings = parse_rows(await self._fetch())
            except Exception as exc:  # noqa: BLE001 — 어떤 실패든 직전 값(없으면 꺼짐)으로 버팀. 로그는 코드만
                code = str(exc) if isinstance(exc, SettingsReadError) else type(exc).__name__
                log.warning("서버 설정 표 읽기 실패: %s", code)
                self._last_failed = True
                self._next_at = self._clock() + self._retry
            else:
                self._settings = settings
                self._ever_read = True
                self._last_failed = False
                self._next_at = self._clock() + self._ttl
            return self._read()


_cache = SettingsCache()


async def predict_settings() -> SettingsRead:
    """ML와 순수준비시간용 allowlist 플래그를 캐시해 전달함."""
    return await _cache.get()


def reset_cache() -> None:
    """시험이 캐시를 비움"""
    _cache.reset()

"""본 레포 supabase/functions/_shared/prepare-ml.ts의 서버판 (기준 b140f70) — C1/W4 CatBoost 예측 서버 호출 (서버 전용, 토큰은 앱에 안 나감)

실험 기능이고 기본은 꺼짐. 켜는 조건 6가지(plan.py `_optional_ml_prediction`): 기능 플래그 · 이 계획이 허용 · 사용자 UUID 허용 목록 ·
URL · 토큰 · 검증된 프로필(서버 전용 표). 어느 하나라도 어긋나거나 호출이 실패하면 None → 원래 P80 계산으로.
MlPrediction · MlRequest · MlProfile은 JSON 모양이라 dict(TypedDict) — 키 이름은 원본 그대로.
"""

import asyncio
import json
import math
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, NotRequired, TypedDict
from urllib.parse import urlsplit

import httpx

from ..core.read import js_trim
from ..kakao_parse import out_number
from .js import ms, parse_iso
from .tz import local_date_key

MlModel = Literal["c1_mae", "w4_mae"]


class MlPrediction(TypedDict):
    model: MlModel
    minutes: int


class MlProfile(TypedDict, total=False):
    ml_codes_verified: bool | None
    ml_age_code: str | None
    ml_holiday_code: str | None


class MlRequest(TypedDict):
    model: MlModel
    features: dict[str, str | int | float]


class MlConfig(TypedDict):
    enabled: bool
    url: NotRequired[str | None]
    token: NotRequired[str | None]
    timeoutMs: NotRequired[int | float | None]


ML_TIMEOUT_DEFAULT_MS = 1500  # 시간 제한 1,500ms (원본 기본값)

# Production inference is restricted to explicitly approved test user UUIDs.
# Empty or malformed allowlists fail closed, even when PREPARE_ML_ENABLED=1.
_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z", re.IGNORECASE)


def may_use_ml(
    *, enabled: bool, allow_for_plan: bool, user_id: str, allowed_user_ids: str | None = None
) -> bool:
    if not enabled or not allow_for_plan or not _UUID_PATTERN.match(user_id):
        return False
    items = [js_trim(s) for s in (allowed_user_ids or "").split(",")]
    return any(_UUID_PATTERN.match(s) and s.lower() == user_id.lower() for s in items)


def allowed_user_id_count(allowed_user_ids: str | None) -> int:
    """허용 목록의 서로 다른 UUID 수 — /health/deep에 숫자만 보임(계정 id는 안 보임). may_use_ml과 같은 쪼개기 · UUID 모양 검사"""
    items = (js_trim(s) for s in (allowed_user_ids or "").split(","))
    return len({s.lower() for s in items if _UUID_PATTERN.match(s)})


# A trusted Edge-service read is still not enough without a versioned codebook
# and recorded consent. Client-writable public.profiles fields are never accepted.
class VerifiedMlProfile(MlProfile, total=False):
    codebook_version: str | None
    consent_at: str | None
    verified_at: str | None


def _date_parse_ms(value: str) -> float:
    """JS Date.parse — 읽을 수 없으면 NaN"""
    try:
        return float(ms(parse_iso(value)))
    except (ValueError, TypeError, OverflowError):
        return float("nan")


def approved_ml_profile(profile: VerifiedMlProfile | None, expected_version: str | None) -> MlProfile | None:
    if (
        not profile
        or not js_trim(expected_version or "")
        or profile.get("codebook_version") != expected_version
        or not profile.get("consent_at")
        or not profile.get("verified_at")
    ):
        return None
    consent = _date_parse_ms(profile["consent_at"])  # type: ignore[arg-type]
    verified = _date_parse_ms(profile["verified_at"])  # type: ignore[arg-type]
    now = ms(datetime.now(UTC))
    if math.isnan(consent) or math.isnan(verified) or consent > now or verified > now:
        return None
    if not _valid_age_code(profile.get("ml_age_code")):
        return None
    holiday = profile.get("ml_holiday_code")
    return {
        "ml_codes_verified": True,
        "ml_age_code": profile.get("ml_age_code"),
        # Only the server-owned verified value reaches CatBoost. Missing stays missing.
        "ml_holiday_code": holiday if _valid_holiday_code(holiday) else None,
    }


MISSING = "__MISSING__"


# The training survey records codes 1..8 for 10s, 20s, ... 80+.
def _valid_holiday_code(value: Any) -> bool:
    return isinstance(value, str) and re.match(r"^[1-6]\Z", value) is not None


def _valid_age_code(value: Any) -> bool:
    return isinstance(value, str) and re.match(r"^[1-8]\Z", value) is not None


def previous_date_key(appointment_at: datetime, timezone: str) -> str:
    """The requested date in the user's time zone, not the server's UTC date — 그 전날의 'YYYY-MM-DD'"""
    year, month, day = (int(x) for x in local_date_key(appointment_at, timezone).split("-"))
    return (date(year, month, day) - timedelta(days=1)).isoformat()


def calendar_features(appointment_at: datetime, timezone: str) -> dict[str, str]:
    day_key = local_date_key(appointment_at, timezone)
    weekday = (date.fromisoformat(day_key).weekday() + 1) % 7  # JS getUTCDay: 일요일 = 0
    return {
        "weekday_code": str(weekday),
        "평토일구분코드": "3" if weekday == 0 else "2" if weekday == 6 else "1",
        # This is the RESPONDENT'S regular rest-day pattern, not a public holiday.
        # A verified personal answer (if any) replaces this fallback in build_ml_request.
        "휴일종류코드": MISSING,
    }


def build_ml_request(
    *,
    appointment_at: datetime,
    timezone: str,
    profile: MlProfile,
    previous_day_minutes: int | float | None,
) -> MlRequest | None:
    """Only server-verified age codes may be used; no direct client-supplied codes."""
    if not profile.get("ml_codes_verified"):
        return None
    age = profile.get("ml_age_code")
    if not _valid_age_code(age):
        return None
    previous = previous_day_minutes
    is_warm = (
        isinstance(previous, int | float)
        and not isinstance(previous, bool)
        and 10 <= previous <= 240  # NaN · 무한대는 비교가 거짓
    )
    holiday = profile.get("ml_holiday_code")
    features: dict[str, str | int | float] = {
        **calendar_features(appointment_at, timezone),
        "연령코드": age,  # type: ignore[dict-item]
        "휴일종류코드": holiday if _valid_holiday_code(holiday) else MISSING,  # type: ignore[dict-item]
    }
    if is_warm:
        features["previous_day_target_minutes"] = out_number(previous)  # type: ignore[arg-type]
    return {"model": "w4_mae" if is_warm else "c1_mae", "features": features}


def valid_ml_endpoint(url: str) -> bool:
    """Local HTTP only for developer-controlled loopback/host Docker. Hosted servers cannot reach your Mac's 127.0.0.1."""
    try:
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        if u.username or u.password or u.query or u.fragment:
            return False
        return u.scheme == "https" or u.hostname in ("localhost", "127.0.0.1", "host.docker.internal")
    except ValueError:
        return False


def _is_integer(value: Any) -> bool:
    """JS Number.isInteger — bool은 숫자가 아님, 71.0은 정수"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


async def request_ml_prediction(
    client: httpx.AsyncClient, request: MlRequest, config: MlConfig
) -> MlPrediction | None:
    """예측 서버 호출. 꺼짐 · 설정 부족 · 주소 부적합 · 실패 · 범위 밖 응답은 전부 None (선택 기능 — 호출한 쪽이 기존 준비시간 계산을 그대로 씀)"""
    url = config.get("url")
    token = config.get("token")
    if not config.get("enabled") or not url or not token or not valid_ml_endpoint(url):
        return None
    configured = config.get("timeoutMs")
    timeout_ms = max(100, min(5000, ML_TIMEOUT_DEFAULT_MS if configured is None else configured))

    async def call() -> MlPrediction | None:
        response = await client.post(
            (url[:-1] if url.endswith("/") else url) + "/predict",
            headers={"Content-Type": "application/json", "x-api-key": token},
            content=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
        )
        if not response.is_success:
            return None
        body = response.json()
        if not isinstance(body, dict):
            return None
        minutes = body.get("minutes")
        if (
            body.get("model") != request["model"]
            or body.get("prediction_type") != "point"
            or not _is_integer(minutes)
            or minutes < 10
            or minutes > 240
        ):
            return None
        return {"model": request["model"], "minutes": int(minutes)}

    try:
        # 요청 전체(응답 읽기까지)에 시간 제한 — 원본의 AbortController와 같음
        return await asyncio.wait_for(call(), timeout_ms / 1000)
    except Exception:
        return None  # Optional model: callers preserve the existing prepare-time calculation.

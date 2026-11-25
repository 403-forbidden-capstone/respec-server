"""Pure preparation interval (PREPARE_START -> DEPARTURE) candidate for personal AUTO.

Never use the C1/W4 wake-to-departure label for this candidate.
Original 2024 survey v2 is an explicitly restricted cross-domain pilot.
Failures preserve the user's pure-prep preference (or legacy when absent).
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime
from typing import Any

import httpx

from .prepare_ml import approved_ml_profile, calendar_features, previous_date_key
from .server_settings import predict_settings
from .supabase import create_service_client
from .tz import local_date_key

_UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
BASELINE = "survey_previous_day_app_proxy_transfer_v1"
MODEL = "prep_start_ratio_v2"
TARGET = "survey_start_cluster_proxy"
MODEL_STORED = "prep_start_ratio_v2_survey_pilot"
MIN_HISTORY = 10


def pure_profile_candidate(profile: dict[str, Any]) -> dict[str, Any] | None:
    v = profile.get("default_outing_prep_minutes")
    if type(v) not in (int, float) or not math.isfinite(v) or not 5 <= v <= 240:
        return None
    return {"source": "PROFILE", "minutes": round(v)}


def completed_history(
    rows: list[dict], cutoff: datetime, since: datetime | None
) -> list[tuple[datetime, float]]:
    unique: set[tuple[datetime, datetime]] = set()
    out = []
    for r in rows:
        if r.get("prepare_started_source") != "PREPARE_ALARM" or r.get("departed_source") != "LOCATION":
            continue
        try:
            a = datetime.fromisoformat(str(r["prepare_started_at"]).replace("Z", "+00:00"))
            b = datetime.fromisoformat(str(r["departed_at"]).replace("Z", "+00:00"))
            minutes = (b - a).total_seconds() / 60
        except (TypeError, ValueError, OverflowError):
            continue
        if a.tzinfo is None or b.tzinfo is None or b >= cutoff or b <= a or (since and a < since):
            continue
        if not 10 <= minutes <= 240 or (a, b) in unique:
            continue
        unique.add((a, b))
        out.append((b, minutes))
    return sorted(out, reverse=True)[:30]


async def request_v2(client: httpx.AsyncClient, endpoint: str, token: str, request: dict) -> int | None:
    if not endpoint.startswith("https://") and not endpoint.startswith(
        ("http://127.0.0.1:", "http://localhost:")
    ):
        return None
    try:

        async def call():
            return await client.post(
                endpoint.rstrip("/") + "/predict", json=request, headers={"x-api-key": token}
            )

        response = await asyncio.wait_for(call(), 1.5)
        if response.status_code != 200:
            return None
        data = response.json()
        v = request["features"]["baseline_observed"]
        if (
            not isinstance(data, dict)
            or data.get("model") != MODEL
            or data.get("training_target") != TARGET
            or data.get("baseline_definition") != BASELINE
            or data.get("prediction_type") != "ratio"
        ):
            return None
        minutes, ratio = data.get("minutes"), data.get("ratio")
        if (
            type(minutes) not in (float, int)
            or type(ratio) not in (float, int)
            or not math.isfinite(minutes)
            or not math.isfinite(ratio)
        ):
            return None
        if not (5 <= minutes <= 240 and 0.5 <= ratio <= 2 and abs(minutes - v * ratio) <= 2):
            return None
        return round(minutes)
    except Exception:
        return None


async def pure_prep_candidate(
    *,
    db: Any,
    user_id: str,
    profile: dict[str, Any],
    now: datetime,
    appointment_at: datetime,
    timezone: str,
    settings: Any,
    http: httpx.AsyncClient | None,
    allow_model: bool,
) -> dict[str, Any] | None:
    base = pure_profile_candidate(profile)
    if base is None:
        return None
    if (
        not allow_model
        or not settings
        or settings.pure_prep_survey_enabled is not True
        or not (_UUID.fullmatch(user_id) and settings.pure_prep_survey_allowed_user_id == user_id)
        or not profile.get("ml_profile_consented_at")
    ):
        return base
    endpoint = (settings.prepare_ml_url or "").strip()
    token = settings.prepare_ml_api_token.get_secret_value() if settings.prepare_ml_api_token else ""
    if not endpoint or not token:
        return base
    try:
        consent = datetime.fromisoformat(str(profile["ml_profile_consented_at"]).replace("Z", "+00:00"))
        if consent.tzinfo is None or consent > now:
            return base
        epoch = profile.get("default_outing_prep_updated_at")
        since = datetime.fromisoformat(str(epoch).replace("Z", "+00:00")) if epoch else None
        if since and (since.tzinfo is None or since > now):
            return base
        cutoff = min(now, appointment_at)
        # PostgREST Python client exposed through the project Db abstraction.
        query = (
            db.from_("appointment_participants")
            .select("prepare_started_at, departed_at, prepare_started_source, departed_source")
            .eq("user_id", user_id)
            .eq("prepare_started_source", "PREPARE_ALARM")
            .eq("departed_source", "LOCATION")
            .lt("departed_at", cutoff.isoformat())
        )
        if since:
            query = query.gte("prepare_started_at", since.isoformat())
        result = await query.order("departed_at", ascending=False).limit(100)
        if result.error or not isinstance(result.data, list):
            return base
        history = completed_history(result.data, cutoff, since)
        if len(history) < MIN_HISTORY:
            return base
        previous_key = previous_date_key(appointment_at, timezone)
        previous = next((m for when, m in history if local_date_key(when, timezone) == previous_key), None)
        if previous is None:
            return base
        config = (await predict_settings()).settings
        if not config.codebook_version:
            return base
        verified_result = (
            await create_service_client()
            .from_("prepare_ml_verified_profiles")
            .select("ml_age_code, ml_holiday_code, codebook_version, consent_at, verified_at")
            .eq("user_id", user_id)
            .maybe_single()
        )
        if verified_result.error or not verified_result.data:
            return base
        verified = approved_ml_profile(verified_result.data, config.codebook_version)
        if not verified:
            return base
        features = {
            **calendar_features(appointment_at, timezone),
            "연령코드": verified["ml_age_code"],
            "휴일종류코드": verified.get("ml_holiday_code") or "__MISSING__",
            "baseline_observed": previous,
        }
        payload = {
            "model": MODEL,
            "baseline_definition": BASELINE,
            "history_count": len(history),
            "features": features,
        }
        if http is not None:
            predicted = await request_v2(http, endpoint, token, payload)
        else:
            async with httpx.AsyncClient() as client:
                predicted = await request_v2(client, endpoint, token, payload)
        if predicted is None:
            return base
        return {"source": "MODEL", "model": MODEL_STORED, "minutes": predicted, "sampleCount": len(history)}
    except Exception:
        return base

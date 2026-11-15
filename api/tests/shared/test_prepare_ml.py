"""본 레포 supabase/functions/_shared/prepare-ml.test.ts의 서버판 — C1/W4 예측 서버 호출

tz · js는 app.shared.tz · app.shared.js의 것.
"""

import asyncio
import json
from datetime import UTC, datetime

import httpx

from app.shared.prepare_ml import (
    approved_ml_profile,
    build_ml_request,
    calendar_features,
    may_use_ml,
    previous_date_key,
    request_ml_prediction,
    valid_ml_endpoint,
)

PROFILE = {"ml_codes_verified": True, "ml_age_code": "2"}
AT = datetime(2026, 11, 9, 1, 0, tzinfo=UTC)


def build(**over):
    args = {"appointment_at": AT, "timezone": "Asia/Seoul", "profile": PROFILE, "previous_day_minutes": None}
    return build_ml_request(**{**args, **over})


def test_uses_appointment_local_date_and_previous_day():
    """uses appointment local date and previous day"""
    assert calendar_features(AT, "Asia/Seoul") == {
        "weekday_code": "1",
        "평토일구분코드": "1",
        "휴일종류코드": "__MISSING__",
    }
    assert previous_date_key(datetime(2026, 12, 1, 1, 0, tzinfo=UTC), "Asia/Seoul") == "2026-11-30"


def test_unverified_or_absent_demographics_cannot_trigger_ml():
    """unverified or absent demographics cannot trigger ML"""
    assert build(profile={}) is None
    assert build(profile={**PROFILE, "ml_age_code": None}) is None


def test_cold_uses_c1_and_consecutive_day_warm_uses_w4():
    """cold uses C1; consecutive-day warm uses W4"""
    cold = build()
    assert cold["model"] == "c1_mae"
    assert sorted(cold["features"]) == sorted(["weekday_code", "평토일구분코드", "휴일종류코드", "연령코드"])
    warm = build(previous_day_minutes=87)
    assert warm["model"] == "w4_mae"
    assert warm["features"]["previous_day_target_minutes"] == 87
    assert warm["features"]["휴일종류코드"] == "__MISSING__"
    approved_with_holiday = {**PROFILE, "ml_holiday_code": "5"}
    with_holiday = build(profile=approved_with_holiday, previous_day_minutes=87)
    assert with_holiday["model"] == "w4_mae"
    assert with_holiday["features"]["휴일종류코드"] == "5"
    bad_holiday = build(profile={**approved_with_holiday, "ml_holiday_code": "99"})
    assert bad_holiday["features"]["휴일종류코드"] == "__MISSING__"
    assert sorted(warm["features"]) == sorted(
        ["weekday_code", "평토일구분코드", "휴일종류코드", "연령코드", "previous_day_target_minutes"]
    )
    assert build(previous_day_minutes=400)["model"] == "c1_mae"


def test_feature_order_and_json_numbers_match_what_edge_sent():
    """(서버판 확인) 요청 본문의 칸 순서 · 숫자 모양이 Edge와 같음 — 휴일 칸은 제자리에서 덮이고, 87.0은 87"""
    request = build(profile={**PROFILE, "ml_holiday_code": "3"}, previous_day_minutes=87.0)
    assert list(request["features"]) == [
        "weekday_code",
        "평토일구분코드",
        "휴일종류코드",
        "연령코드",
        "previous_day_target_minutes",
    ]
    assert json.dumps(request["features"]["previous_day_target_minutes"]) == "87"


def test_calendar_features_weekend_codes():
    """(서버판 확인) 일요일 = 0 · 토요일 = 6 (JS getUTCDay) → 평토일구분코드 3 · 2"""
    sunday = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
    saturday = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)
    assert calendar_features(sunday, "Asia/Seoul")["weekday_code"] == "0"
    assert calendar_features(sunday, "Asia/Seoul")["평토일구분코드"] == "3"
    assert calendar_features(saturday, "Asia/Seoul")["weekday_code"] == "6"
    assert calendar_features(saturday, "Asia/Seoul")["평토일구분코드"] == "2"


def run_predict(request, config, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await request_ml_prediction(client, request, config)

    return asyncio.run(go())


def test_rejects_remote_plain_http_and_disabled_flag_prevents_fetch():
    """rejects remote plain HTTP; disabled flag prevents fetch"""
    assert valid_ml_endpoint("http://remote.example.com") is False
    assert valid_ml_endpoint("https://remote.example.com") is True
    request = build()
    calls = []

    def ok(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"model": "c1_mae", "minutes": 71, "prediction_type": "point"})

    cfg = {"enabled": True, "url": "https://remote.example.com", "token": "secret"}
    assert run_predict(request, {**cfg, "enabled": False}, ok) is None
    assert calls == []
    assert run_predict(request, cfg, ok) == {"model": "c1_mae", "minutes": 71}

    def offline(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    assert run_predict(request, cfg, offline) is None
    out_of_range = lambda req: httpx.Response(  # noqa: E731
        200, json={"model": "c1_mae", "minutes": 999, "prediction_type": "point"}
    )
    assert run_predict(request, cfg, out_of_range) is None


def test_the_request_goes_to_predict_with_the_key_header_and_a_json_body():
    """(서버판 확인) 주소 끝 슬래시 하나를 지우고 /predict, x-api-key 헤더, 본문은 요청 그대로(한글 칸 이름 포함)"""
    request = build()
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"model": "c1_mae", "minutes": 71.0, "prediction_type": "point"})

    cfg = {"enabled": True, "url": "https://remote.example.com/", "token": "secret"}
    assert run_predict(request, cfg, handler) == {"model": "c1_mae", "minutes": 71}  # 71.0도 정수
    assert str(seen[0].url) == "https://remote.example.com/predict"
    assert seen[0].method == "POST"
    assert seen[0].headers["x-api-key"] == "secret"
    assert seen[0].headers["content-type"] == "application/json"
    assert json.loads(seen[0].content) == request
    assert "평토일구분코드" in seen[0].content.decode("utf-8")


def test_bad_answers_are_all_none():
    """(서버판 확인) 응답이 이상하면 전부 None — 오류 상태 · 모델 이름 · 예측 종류 · 정수 아님 · 객체 아님 · 깨진 JSON"""
    request = build()
    cfg = {"enabled": True, "url": "https://remote.example.com", "token": "secret"}
    good = {"model": "c1_mae", "minutes": 71, "prediction_type": "point"}
    bodies = [
        httpx.Response(500, json=good),
        httpx.Response(200, json={**good, "model": "w4_mae"}),
        httpx.Response(200, json={**good, "prediction_type": "interval"}),
        httpx.Response(200, json={**good, "minutes": 71.5}),
        httpx.Response(200, json={**good, "minutes": True}),
        httpx.Response(200, json={**good, "minutes": 9}),
        httpx.Response(200, json=[good]),
        httpx.Response(200, content=b"not json"),
    ]
    for response in bodies:
        assert run_predict(request, cfg, lambda req, r=response: r) is None
    for missing in ("url", "token"):
        assert (
            run_predict(
                request,
                {k: v for k, v in cfg.items() if k != missing},
                lambda req: httpx.Response(200, json=good),
            )
            is None
        )


def test_the_call_stops_after_the_time_limit_and_the_limit_is_clamped():
    """(서버판 확인) 시간 제한(기본 1,500ms)을 넘기면 None, timeoutMs는 100~5000으로 잘림"""
    request = build()
    cfg = {"enabled": True, "url": "https://remote.example.com", "token": "secret"}

    async def go(config):
        async def slow(req: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.5)
            return httpx.Response(200, json={"model": "c1_mae", "minutes": 71, "prediction_type": "point"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            return await request_ml_prediction(client, request, config)

    assert asyncio.run(go({**cfg, "timeoutMs": 1})) is None  # 1 → 100ms로 올려 잡아도 0.5초 응답보다 빠름
    assert asyncio.run(go(cfg)) == {"model": "c1_mae", "minutes": 71}  # 기본 1,500ms 안에 도착


def test_real_ml_is_denied_unless_both_flag_and_server_side_uuid_allowlist_permit_the_user():
    """real ML is denied unless both flag and server-side UUID allowlist permit the user"""
    user_id = "22222222-2222-4222-8222-222222222222"
    allowed = "11111111-1111-4111-8111-111111111111, " + user_id.upper()
    base = {"enabled": True, "allow_for_plan": True, "user_id": user_id}
    assert may_use_ml(**base, allowed_user_ids=allowed) is True
    assert may_use_ml(**{**base, "enabled": False}, allowed_user_ids=allowed) is False
    assert may_use_ml(**{**base, "allow_for_plan": False}, allowed_user_ids=allowed) is False
    assert may_use_ml(**base, allowed_user_ids=None) is False
    assert may_use_ml(**base, allowed_user_ids="") is False
    assert may_use_ml(**base, allowed_user_ids="not-a-uuid") is False
    other = {**base, "user_id": "11111111-1111-4111-8111-111111111111"}
    assert may_use_ml(**other, allowed_user_ids=user_id) is False


def test_only_a_service_verified_consented_and_matching_codebook_profile_can_be_used():
    """only a service-verified, consented and matching codebook profile can be used"""
    good = {
        "ml_age_code": "2",
        "ml_holiday_code": "1",
        "codebook_version": "validated-v1",
        "consent_at": "2025-01-01T00:00:00Z",
        "verified_at": "2025-01-02T00:00:00Z",
    }
    assert approved_ml_profile(good, "validated-v1") == {
        "ml_codes_verified": True,
        "ml_age_code": "2",
        "ml_holiday_code": "1",
    }
    assert approved_ml_profile({**good, "ml_holiday_code": None}, "validated-v1") == {
        "ml_codes_verified": True,
        "ml_age_code": "2",
        "ml_holiday_code": None,
    }
    assert approved_ml_profile(good, "") is None
    assert approved_ml_profile(good, "other-version") is None
    assert approved_ml_profile({**good, "consent_at": None}, "validated-v1") is None
    assert approved_ml_profile({**good, "verified_at": None}, "validated-v1") is None
    assert approved_ml_profile({**good, "ml_age_code": "__MISSING__"}, "validated-v1") is None
    assert approved_ml_profile({**good, "ml_age_code": "9"}, "validated-v1") is None
    assert approved_ml_profile({**good, "ml_age_code": "0"}, "validated-v1") is None
    assert approved_ml_profile({**good, "consent_at": "3025-01-01T00:00:00Z"}, "validated-v1") is None

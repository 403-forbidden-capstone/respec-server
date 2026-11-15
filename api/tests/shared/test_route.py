"""본 레포 supabase/functions/_shared/route.test.ts의 서버판 — 이동시간 폴백 순서 (7장 7.1.2)

TravelSample은 app.shared.calc.travel의 dict(TypedDict, 키 camelCase).
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.kakao import KakaoError
from app.shared.calc.travel import TravelSample
from app.shared.route import TravelChoice, TravelDeps, resolve_auto_travel, resolve_travel

ORIGIN = {"lat": 37.5547, "lng": 126.9707}
DEST = {"lat": 37.4979, "lng": 127.0276}
ARGS = {"origin": ORIGIN, "destination": DEST, "transport": "TRANSIT"}


def sample(minutes: int) -> TravelSample:
    departed = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    return TravelSample(
        origin=ORIGIN,
        destination=DEST,
        transport="TRANSIT",
        departedAt=departed,
        arrivedAt=departed + timedelta(minutes=minutes),
    )


async def kakao_ok(args):
    return {"minutes": 38, "partial": False}


async def kakao_fail(args):
    raise KakaoError("TIMEOUT", message="late")


def samples_of(*minutes):
    async def read():
        return [sample(m) for m in minutes]

    return read


def test_1_kakao_ok_gives_api():
    """1 카카오가 되면 API"""
    r = asyncio.run(resolve_auto_travel(ARGS, TravelDeps(kakao=kakao_ok, samples=samples_of(40, 41, 42))))
    assert r.choice == TravelChoice(38, "API", False, "길찾기 기준")
    assert r.warnings == []


def test_kakao_partial_is_api_with_a_warning():
    """카카오가 일부 구간만 줬으면 API + 경고"""

    async def partial(args):
        return {"minutes": 30, "partial": True}

    r = asyncio.run(resolve_auto_travel(ARGS, TravelDeps(kakao=partial)))
    assert r.choice.source == "API"
    assert r.warnings[0]["code"] == "KAKAO_PARTIAL"


def test_2_kakao_failure_with_three_same_route_samples_gives_cached_median():
    """2 카카오 실패 + 같은 경로 실측 3건 이상 → CACHED 중앙값 (표시용, 저장은 안 함)"""
    r = asyncio.run(resolve_auto_travel(ARGS, TravelDeps(kakao=kakao_fail, samples=samples_of(40, 50, 45))))
    assert r.choice.source == "CACHED"
    assert r.choice.minutes == 45
    assert r.choice.sample_count == 3
    assert [[w["code"], w.get("detail")] for w in r.warnings] == [["KAKAO_FAILED", "TIMEOUT"]]


def test_no_key_leaves_a_warning_and_goes_on():
    """키가 없으면 경고만 남기고 다음 단계로 (F10)"""
    r = asyncio.run(resolve_auto_travel(ARGS, TravelDeps(samples=samples_of(40, 50, 45))))
    assert r.choice.source == "CACHED"
    assert r.warnings[0]["code"] == "KAKAO_NOT_CONFIGURED"


def test_3_too_few_samples_gives_a_straight_line_estimate_that_needs_confirmation():
    """3 실측 부족 → 직선거리 근사값, 사용자 확인 필수"""
    r = asyncio.run(resolve_auto_travel(ARGS, TravelDeps(kakao=kakao_fail, samples=samples_of(40))))
    assert r.choice.source == "ESTIMATE"
    assert r.choice.needs_confirmation is True
    assert 20 < r.choice.minutes < 50


def test_unreadable_records_fall_through_to_the_estimate():
    """기록을 못 읽어도 근사값으로"""

    async def broken():
        raise RuntimeError("db down")

    r = asyncio.run(resolve_auto_travel(ARGS, TravelDeps(samples=broken)))
    assert r.choice.source == "ESTIMATE"


def test_no_coordinates_means_no_way_manual_input_needed():
    """좌표가 없으면 길이 없음 (null) — 직접 입력해야 함"""
    r = asyncio.run(
        resolve_auto_travel({"transport": "TRANSIT", "destination": DEST}, TravelDeps(kakao=kakao_ok))
    )
    assert r.choice is None
    assert r.warnings[0]["code"] == "NO_COORDINATES"


def test_4_manual_input_comes_first_without_calls_unless_preview_wants_auto():
    """4 직접 입력은 MANUAL로 먼저, 호출 없이 (미리 보기가 아니면 자동 값은 구하지 않음)"""
    calls = 0

    async def kakao(args):
        nonlocal calls
        calls += 1
        return {"minutes": 38, "partial": False}

    deps = TravelDeps(kakao=kakao)
    r = asyncio.run(resolve_travel({**ARGS, "override": 70}, deps))
    assert r.chosen == TravelChoice(70, "MANUAL", False, "직접 입력")
    assert r.auto is None
    assert calls == 0

    preview = asyncio.run(resolve_travel({**ARGS, "override": 70}, deps, want_auto=True))
    assert preview.chosen.minutes == 70
    assert preview.auto.minutes == 38  # "자동으로 되돌리면" 이 값
    assert calls == 1


def test_manual_input_needs_no_coordinates():
    """직접 입력은 좌표가 없어도 됨"""
    r = asyncio.run(resolve_travel({"transport": "WALK", "override": 15}, TravelDeps()))
    assert r.chosen.source == "MANUAL"


def test_warning_detail_key_is_left_out_when_there_is_none():
    """(서버판 확인) 경고 JSON은 detail이 없으면 키 자체가 없음 — 원본의 undefined가 JSON에서 빠지는 것과 같음"""
    r = asyncio.run(resolve_auto_travel(ARGS, TravelDeps()))
    assert "detail" not in r.warnings[0]


@pytest.mark.parametrize("raised", [ValueError("x"), KakaoError("AUTH", 401)])
def test_any_kakao_exception_falls_through_and_only_kakao_errors_name_their_kind(raised):
    """(서버판 확인) 카카오 호출이 어떤 예외를 내도 다음 단계로 — KakaoError만 kind, 나머지는 UNKNOWN"""

    async def boom(args):
        raise raised

    r = asyncio.run(resolve_auto_travel(ARGS, TravelDeps(kakao=boom)))
    assert r.choice.source == "ESTIMATE"
    assert r.warnings[0]["detail"] == (raised.kind if isinstance(raised, KakaoError) else "UNKNOWN")

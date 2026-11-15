"""카카오 이동시간 읽기 · 호출 — 본 레포 Edge `_shared/kakao-parse.test.ts`(대중교통 · 도보 · 자동차 부분)와 `kakao.test`의 `kakaoTravelMinutes` 시험을 그대로 옮김.
샘플은 카카오 공식 문서(2026-10-15)의 응답 모양 — 실제 응답과 맞는지는 키가 온 뒤 한 번 불러 확인할 것."""

import asyncio

import httpx

from app.kakao import KakaoError, kakao_travel_minutes
from app.kakao_parse import parse_car_directions, parse_transit, parse_walk

ORIGIN = {"lat": 37.5547, "lng": 126.9707}
DEST = {"lat": 37.4979, "lng": 127.0276}

TRANSIT_JSON = {
    "status": "OK",
    "properties": {
        "total": 2,
        "bus": 1,
        "subway": 1,
        "busAndSubway": 0,
        "landingURL": "https://map.kakao.com/",
    },
    "routes": [
        {
            "properties": {
                "type": "BUS",
                "totalDistance": 9000,
                "totalTime": 2400,
                "transfers": 0,
                "fare": {"value": 1500, "min": 1500, "max": 1500},
            },
            "steps": [
                {
                    "properties": {"type": "BUS", "time": 2400, "distance": 9000},
                    "path": {"points": [[126.971, 37.555], [127.0, 37.52], [127.027, 37.498]]},
                }
            ],
        },
        {
            "properties": {
                "type": "SUBWAY",
                "totalDistance": 9500,
                "totalTime": 1800,
                "transfers": 1,
                "fare": {"value": 1500, "min": 1500, "max": 1500},
            },
            "steps": [
                {
                    "properties": {"type": "SUBWAY", "time": 900, "distance": 5000},
                    "path": {"points": [[126.9726, 37.5559], [126.99, 37.53]]},
                },
                {
                    "properties": {"type": "WALKING", "time": 120, "distance": 100},
                    "path": {"points": [[126.99, 37.53], [126.9905, 37.5302]]},
                },
                {
                    "properties": {"type": "SUBWAY", "time": 780, "distance": 4000},
                    "path": {"points": [[126.9905, 37.5302], [127.0276, 37.4979]]},
                },
            ],
        },
    ],
}


def test_transit_response_picks_the_fastest_route_and_bus_or_subway_only_falls_back_to_all():
    """대중교통 응답: 가장 빠른 경로, 버스만/지하철만 고를 수 있고 맞는 게 없으면 전체에서"""
    any_route = parse_transit(TRANSIT_JSON)
    assert any_route["ok"]
    assert any_route["seconds"] == 1800  # 지하철이 더 빠름
    assert [any_route["startsWithWalk"], any_route["endsWithWalk"]] == [False, False]
    assert any_route["firstPoint"] == {"lat": 37.5559, "lng": 126.9726}
    assert any_route["lastPoint"] == {"lat": 37.4979, "lng": 127.0276}

    bus = parse_transit(TRANSIT_JSON, "BUS")
    assert bus["ok"] and bus["seconds"] == 2400
    only_bus = {**TRANSIT_JSON, "routes": [TRANSIT_JSON["routes"][0]]}
    subway = parse_transit(only_bus, "SUBWAY")
    assert subway["ok"] and subway["seconds"] == 2400


def test_transit_response_marks_walking_first_and_last_steps_and_no_results_means_no_route():
    """대중교통 응답: 첫 · 마지막 단계가 도보면 표시, 상태가 NO_RESULTS면 경로 없음"""
    walky = {
        "status": "OK",
        "routes": [
            {
                "properties": {"type": "SUBWAY", "totalTime": 1000},
                "steps": [
                    {"properties": {"type": "WALKING"}, "path": {"points": [[1, 2], [3, 4]]}},
                    {"properties": {"type": "SUBWAY"}, "path": {"points": [[3, 4], [5, 6]]}},
                    {"properties": {"type": "WALKING"}, "path": {"points": [[5, 6], [7, 8]]}},
                ],
            }
        ],
    }
    r = parse_transit(walky)
    assert r["ok"]
    assert [r["startsWithWalk"], r["endsWithWalk"]] == [True, True]
    assert parse_transit({"status": "NO_RESULTS"}) == {
        "ok": False,
        "reason": "NO_ROUTE",
        "detail": "NO_RESULTS",
    }
    assert parse_transit({"status": "EQUAL_POINTS"}) == {
        "ok": True,
        "seconds": 0,
        "startsWithWalk": True,
        "endsWithWalk": True,
    }
    assert parse_transit("x") == {"ok": False, "reason": "BAD_RESPONSE"}


def test_walk_and_car_responses():
    """도보 · 자동차 응답"""
    assert parse_walk(
        {"status": "OK", "route": {"properties": {"totalDistance": 800, "totalTime": 600}}}
    ) == {
        "ok": True,
        "seconds": 600,
    }
    assert parse_walk({"status": "SAME_POINT"}) == {"ok": True, "seconds": 0}
    assert parse_walk({"status": "TOO_FAR_AWAY"}) == {
        "ok": False,
        "reason": "NO_ROUTE",
        "detail": "TOO_FAR_AWAY",
    }
    assert parse_car_directions(
        {"routes": [{"result_code": 0, "summary": {"distance": 3053, "duration": 885}}]}
    ) == {
        "ok": True,
        "seconds": 885,
    }
    assert parse_car_directions(
        {"routes": [{"result_code": 1, "result_msg": "길찾기 결과를 찾을 수 없음"}]}
    ) == {
        "ok": False,
        "reason": "NO_ROUTE",
        "detail": "result_code 1",
    }
    assert parse_car_directions({}) == {"ok": False, "reason": "BAD_RESPONSE"}


def test_parse_edge_cases_that_differ_between_js_and_python():
    """(서버판 확인) 상태가 OK가 아니거나 routes가 배열이 아니면 BAD_RESPONSE, 같은 소요시간이면 앞의 경로, 도보 알 수 없는 상태 · 같은 지점 자동차"""
    assert parse_transit({"status": "OK"}) == {"ok": False, "reason": "BAD_RESPONSE", "detail": "OK"}
    assert parse_transit({"status": "INVALID_REQUEST", "routes": []}) == {
        "ok": False,
        "reason": "BAD_RESPONSE",
        "detail": "INVALID_REQUEST",
    }
    assert parse_transit({"status": "OK", "routes": [{"properties": {}}]}) == {
        "ok": False,
        "reason": "NO_ROUTE",
    }
    tie = {
        "status": "OK",
        "routes": [
            {"properties": {"type": "BUS", "totalTime": 600}, "steps": []},
            {"properties": {"type": "SUBWAY", "totalTime": 600}, "steps": []},
        ],
    }
    first = parse_transit(tie)
    assert first["seconds"] == 600 and "firstPoint" not in first  # 단계가 없으면 지점 키도 없음
    assert parse_walk({"status": "OK", "route": {"properties": {}}}) == {
        "ok": False,
        "reason": "BAD_RESPONSE",
        "detail": "OK",
    }
    assert parse_walk({"status": "WHATEVER"}) == {"ok": False, "reason": "BAD_RESPONSE", "detail": "WHATEVER"}
    assert parse_car_directions({"routes": [{"result_code": 104}]}) == {"ok": True, "seconds": 0}
    assert parse_car_directions({"routes": [{"result_code": 0}]}) == {
        "ok": False,
        "reason": "BAD_RESPONSE",
        "detail": "summary.duration",
    }
    assert parse_car_directions({"routes": [{}]}) == {
        "ok": False,
        "reason": "NO_ROUTE",
        "detail": "result_code ?",
    }


def fake_kakao(handlers, calls=None):
    """주소별로 정해 둔 응답을 돌려주는 가짜 네트워크 (원본의 fakeFetch)"""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        make = handlers.get(f"{request.url.scheme}://{request.url.host}{request.url.path}")
        return make() if make else httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


def ok(body):
    return lambda: httpx.Response(200, json=body)


def travel(transport, handlers, calls=None, origin=ORIGIN, destination=DEST):
    async def go():
        async with httpx.AsyncClient(transport=fake_kakao(handlers, calls)) as client:
            return await kakao_travel_minutes(
                client, "KEY", origin=origin, destination=destination, transport=transport
            )

    return asyncio.run(go())


def test_transit_adds_the_missing_walks_with_the_walk_api_and_rounds_up_to_minutes():
    """대중교통: 대중교통 구간 + 빠진 앞뒤 도보를 도보 API로 더해 분 단위로 올림"""
    calls: list[httpx.Request] = []
    result = travel(
        "TRANSIT",
        {
            "https://dapi.kakao.com/v2/routing/publictraffic": ok(TRANSIT_JSON),
            "https://dapi.kakao.com/v2/routing/walk": ok(
                {"status": "OK", "route": {"properties": {"totalTime": 300}}}
            ),
        },
        calls,
    )
    assert result == {"minutes": 40, "partial": False}  # 1800 + 300 + 300 = 2400초
    assert len(calls) == 3
    assert all(c.headers["authorization"] == "KakaoAK KEY" for c in calls)
    first_url = str(calls[0].url)
    assert "start_x=126.9707" in first_url and "end_y=37.4979" in first_url


def test_transit_keeps_only_the_transit_part_and_says_partial_when_the_walk_api_fails():
    """대중교통: 도보 API가 실패하면 대중교통 구간만 (partial)"""
    result = travel(
        "TRANSIT",
        {
            "https://dapi.kakao.com/v2/routing/publictraffic": ok(TRANSIT_JSON),
            "https://dapi.kakao.com/v2/routing/walk": lambda: httpx.Response(500, text="x"),
        },
    )
    assert result == {"minutes": 30, "partial": True}


def test_walk_and_car_each_call_their_own_api_once():
    """도보 · 자동차는 각자 API 한 번"""
    calls: list[httpx.Request] = []
    handlers = {
        "https://dapi.kakao.com/v2/routing/walk": ok(
            {"status": "OK", "route": {"properties": {"totalTime": 1801}}}
        ),
        "https://apis-navi.kakaomobility.com/v1/directions": ok(
            {"routes": [{"result_code": 0, "summary": {"duration": 885}}]}
        ),
    }
    assert travel("WALK", handlers, calls) == {"minutes": 31, "partial": False}
    assert travel("CAR", handlers, calls) == {"minutes": 15, "partial": False}
    car_url = str(calls[1].url)
    assert "origin=126.9707%2C37.5547" in car_url and "priority=RECOMMEND" in car_url  # 경도,위도 순서


def test_failures_become_kakao_errors_by_kind():
    """실패는 종류별 KakaoError로"""

    def run(status):
        return lambda: travel(
            "WALK", {"https://dapi.kakao.com/v2/routing/walk": lambda: httpx.Response(status, json={})}
        )

    for status, kind in [(401, "AUTH"), (429, "RATE_LIMIT"), (500, "HTTP")]:
        try:
            run(status)()
        except KakaoError as error:
            assert error.kind == kind
        else:
            raise AssertionError(f"{status}에서 KakaoError가 안 남")

    try:
        travel("WALK", {"https://dapi.kakao.com/v2/routing/walk": ok({"status": "ROUTE_RESULT_NOT_FOUND"})})
    except KakaoError as error:
        assert error.kind == "NO_ROUTE"
        assert str(error) == "경로를 못 찾았어요 (ROUTE_RESULT_NOT_FOUND)"
    else:
        raise AssertionError("NO_ROUTE가 안 남")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("aborted")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            await kakao_travel_minutes(client, "KEY", origin=ORIGIN, destination=DEST, transport="WALK")

    try:
        asyncio.run(go())
    except KakaoError as error:
        assert error.kind == "TIMEOUT"
    else:
        raise AssertionError("TIMEOUT이 안 남")


def test_transit_without_readable_boarding_points_is_partial():
    """(서버판 확인) 응답에서 첫 승차 · 마지막 하차 지점을 못 읽으면 도보를 못 더하고 partial"""
    no_points = {
        "status": "OK",
        "routes": [
            {
                "properties": {"type": "SUBWAY", "totalTime": 601},
                "steps": [{"properties": {"type": "SUBWAY"}}],
            }
        ],
    }
    result = travel("SUBWAY", {"https://dapi.kakao.com/v2/routing/publictraffic": ok(no_points)})
    assert result == {"minutes": 11, "partial": True}  # 601초 → 올림 11분

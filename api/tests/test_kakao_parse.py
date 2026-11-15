"""카카오 응답 읽기 · 호출 — 본 레포 Edge `_shared/kakao-parse.test.ts`(장소 검색 · 좌표 → 주소 부분)의 샘플 · 기대값을 그대로 옮긴 계약 시험.

Edge가 이 기대값을 내므로 서버판이 같은 값을 내면 같은 응답이 나감 (끝 칸 1-12 ① 계약 시험).
샘플은 카카오 공식 문서(2026-10-15)의 응답 모양 — 실제 응답과 맞는지는 키가 온 뒤 재생 스크립트에서 한 번 더 확인.
"""

import asyncio

import httpx
import pytest

from app.kakao import KakaoError, reverse_address, search_places
from app.kakao_parse import (
    asks_for_station,
    js_number_str,
    parse_places,
    parse_reverse,
    rank_places,
    to_number,
)

KEYWORD = "https://dapi.kakao.com/v2/local/search/keyword.json"
COORD2ADDRESS = "https://dapi.kakao.com/v2/local/geo/coord2address.json"


def doc(place_id, name, x, y, category=""):
    return {
        "id": place_id,
        "place_name": name,
        "address_name": "주소",
        "x": str(x),
        "y": str(y),
        "category_name": category,
    }


def run_search(handler, **kwargs):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await search_places(client, "KEY", **kwargs)

    return asyncio.run(go())


def run_reverse(handler, **kwargs):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await reverse_address(client, "KEY", **kwargs)

    return asyncio.run(go())


def ok(body):
    return lambda request: httpx.Response(200, json=body)


# ---------- 장소 검색 응답 ----------


def test_parse_places_prefers_road_address_numbers_for_coordinates_and_drops_incomplete_items():
    places = parse_places(
        {
            "documents": [
                {
                    "id": "1",
                    "place_name": "세종대학교",
                    "address_name": "서울 광진구 능동 98",
                    "road_address_name": "서울 광진구 능동로 209",
                    "x": "127.0736",
                    "y": "37.5509",
                    "category_name": "교육 > 대학교",
                },
                {
                    "id": "2",
                    "place_name": "지번만",
                    "address_name": "서울 어딘가",
                    "road_address_name": "",
                    "x": "127.1",
                    "y": "37.5",
                },
                {"id": "3", "place_name": "좌표 없음", "address_name": "x"},
            ]
        }
    )
    assert places == [
        {
            "id": "1",
            "name": "세종대학교",
            "address": "서울 광진구 능동로 209",
            "lat": 37.5509,
            "lng": 127.0736,
            "category": "교육 > 대학교",
        },
        {"id": "2", "name": "지번만", "address": "서울 어딘가", "lat": 37.5, "lng": 127.1},
    ]
    assert list(places[0]) == ["id", "name", "address", "lat", "lng", "category"]  # 키 순서까지 Edge와 같음
    assert parse_places(None) == []


def test_parse_places_number_reading_matches_js():
    # JS Number('') === 0 · 앞뒤 공백 무시 · 정수로 떨어지면 37 (37.0 아님) · 무한대 · NaN · 파이썬만 받는 글자는 거절
    def one(x, y):
        return parse_places({"documents": [{"id": "a", "place_name": "p", "x": x, "y": y}]})

    assert one(" 127.5 ", "37")[0]["lat"] == 37 and isinstance(one(" 127.5 ", "37")[0]["lat"], int)
    assert one("", "")[0]["lat"] == 0 and one("", "")[0]["lng"] == 0
    for bad in ("abc", "Infinity", "-Infinity", "NaN", "nan", "1_000", "١٢", "inf", None, True, [], {}):
        assert one(bad, "37") == [], bad
    assert one(127.5, 37)[0]["lng"] == 127.5  # 숫자로 와도 됨
    assert to_number(float("inf")) is None and to_number(10**400) is None


def test_js_number_str_matches_js_string_conversion():
    assert js_number_str(127) == "127"
    assert js_number_str(127.0) == "127"
    assert js_number_str(37.5) == "37.5"
    assert js_number_str(-0.5) == "-0.5"
    assert js_number_str(1e-7) == "1e-7"


# ---------- 장소 검색 호출 ----------


def test_search_is_accuracy_sorted_with_x_as_longitude_y_as_latitude_and_size_clamped_to_15():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"documents": []})

    assert run_search(handler, query="세종대", near={"lat": 37.5, "lng": 127}, size=99) == []
    assert len(calls) == 1
    params = dict(calls[0].url.params)
    assert params == {"query": "세종대", "size": "15", "sort": "accuracy", "x": "127", "y": "37.5"}
    assert "category_group_code" not in params and params["sort"] != "distance"
    assert calls[0].headers["authorization"] == "KakaoAK KEY"
    assert str(calls[0].url).startswith(KEYWORD)


def test_search_size_default_is_10_and_minimum_is_1():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"documents": []})

    run_search(handler, query="스타벅스")
    run_search(handler, query="스타벅스", size=0)
    assert [c.url.params["size"] for c in calls] == ["10", "1"]
    assert "x" not in calls[0].url.params  # 기준 좌표가 없으면 x · y를 안 보냄


def test_station_query_finds_subway_separately_and_puts_it_first_without_duplicates():
    calls: list[httpx.Request] = []
    station = doc("S1", "강남역 2호선", 127.0276, 37.4979, "교통,수송 > 지하철,전철 > 수도권2호선 > 강남역")

    def handler(request):
        calls.append(request)
        if request.url.params.get("category_group_code") == "SW8":
            return httpx.Response(200, json={"documents": [station]})
        documents = [
            doc("F1", "강남교자 뚝섬역점", 127.04, 37.54),
            doc("F2", "압구정로데오역", 127.04, 37.527),
            station,
            doc("F3", "파스타집", 127.03, 37.5),
        ]
        return httpx.Response(200, json={"documents": documents})

    result = run_search(handler, query="강남역", near={"lat": 37.5547, "lng": 126.9707})
    assert len(calls) == 2
    assert result[0]["name"] == "강남역 2호선"
    assert [p["id"] for p in result] == ["S1", "F1", "F2", "F3"]  # 지하철역이 한 번만, 나머지는 원래 순서
    sizes = sorted(c.url.params["size"] for c in calls)
    assert sizes == ["10", "5"]  # 보조 호출은 5건


def test_station_helper_failure_still_returns_the_main_result():
    def handler(request):
        if request.url.params.get("category_group_code") == "SW8":
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"documents": [doc("A", "시청역", 126.97, 37.56)]})

    assert [p["id"] for p in run_search(handler, query="시청역")] == ["A"]


def test_main_search_failure_is_raised_even_when_the_station_helper_succeeds():
    def handler(request):
        if request.url.params.get("category_group_code") == "SW8":
            return httpx.Response(200, json={"documents": [doc("S", "강남역", 127, 37)]})
        return httpx.Response(500, json={})

    with pytest.raises(KakaoError) as info:
        run_search(handler, query="강남역")
    assert info.value.kind == "HTTP"


@pytest.mark.parametrize("query", ["세종대", "스타벅스", "역", "역삼동"])
def test_non_station_queries_make_a_single_call(query):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"documents": []})

    run_search(handler, query=query)
    assert len(calls) == 1


def test_asks_for_station_rule():
    assert asks_for_station("강남역") and asks_for_station(" 서울 시청역 ") and asks_for_station("a역")
    assert not asks_for_station("역") and not asks_for_station("역삼동") and not asks_for_station("")


def test_js_number_str_follows_number_prototype_to_string_for_small_and_large_values():
    cases = {
        0.00005: "0.00005",  # JS는 1e-6 미만에서만 지수 표기 (파이썬 repr은 1e-4 미만부터 → '5e-05')
        -0.00005: "-0.00005",
        0.000001: "0.000001",
        1e-7: "1e-7",
        1.5e-7: "1.5e-7",
        -1.2345e-7: "-1.2345e-7",
        123456.789: "123456.789",
        1e21: "1e+21",
        1.5e21: "1.5e+21",
        123456789012345680000.0: "123456789012345680000",
        0.5: "0.5",
        100.0: "100",
        -0.0: "0",
        5e-324: "5e-324",
    }
    for value, expected in cases.items():
        assert js_number_str(value) == expected, value


def test_asks_for_station_uses_js_whitespace_not_python_whitespace():
    # JS \S는 \x1c~\x1f · \x85를 공백이 아닌 글자로 보고 U+FEFF를 공백으로 봄 (파이썬 \S는 반대)
    for ch in (chr(0x85), chr(0x1C), chr(0x1F)):
        assert asks_for_station("x" + ch + "역"), hex(ord(ch))
    assert not asks_for_station("x" + chr(0xFEFF) + "역")
    assert not asks_for_station("x" + chr(0x3000) + "역") and not asks_for_station("x" + chr(0xA0) + "역")


def test_station_helper_gets_the_same_bias_coordinates_as_the_main_call():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"documents": []})

    run_search(handler, query="강남역", near={"lat": 37.5, "lng": 127})
    assert len(calls) == 2
    for call in calls:
        assert (
            call.url.params["x"] == "127"
            and call.url.params["y"] == "37.5"
            and call.url.params["sort"] == "accuracy"
        )


def test_station_helper_swallows_any_exception_not_only_kakao_errors():
    def handler(request):
        if request.url.params.get("category_group_code") == "SW8":
            raise RuntimeError("boom")  # Edge `.catch(() => null)`는 모든 실패를 삼킴
        return httpx.Response(200, json={"documents": [doc("A", "시청역", 126.97, 37.56)]})

    assert [p["id"] for p in run_search(handler, query="시청역")] == ["A"]


def test_main_and_station_calls_run_concurrently():
    """순차로 바꾸면 p95(완결점 1-12 ③)에 직접 영향 — 본 호출이 보조 호출이 시작될 때까지 기다리게 해서 순차면 시간 초과로 실패"""
    started = {"station": asyncio.Event()}

    async def handler(request):
        if request.url.params.get("category_group_code") == "SW8":
            started["station"].set()
            return httpx.Response(200, json={"documents": []})
        await asyncio.wait_for(started["station"].wait(), timeout=2)  # 보조 호출이 동시에 시작돼야 풀림
        return httpx.Response(200, json={"documents": [doc("A", "시청역", 126.97, 37.56)]})

    assert [p["id"] for p in run_search(handler, query="시청역")] == ["A"]


def test_same_name_branches_are_ranked_through_the_whole_search_path():
    near = {"lat": 37.5547, "lng": 126.9707}
    documents = [
        doc("far", "스타벅스", 127.0276, 37.4979),
        doc("x", "스타벅스 리저브", 127.1, 37.6),
        doc("near", "스타벅스", 126.971, 37.555),
    ]
    assert [p["id"] for p in run_search(ok({"documents": documents}), query="스타벅스", near=near)] == [
        "near",
        "x",
        "far",
    ]
    assert [p["id"] for p in run_search(ok({"documents": documents}), query="스타벅스")] == [
        "far",
        "x",
        "near",
    ]  # 기준 좌표 없음


def test_search_keeps_accuracy_order_and_university_comes_first():
    documents = [
        doc("U", "세종대학교", 127.07, 37.55, "교육,학문 > 대학교"),
        doc("C", "세종대 앞 카페", 127.07, 37.551),
    ]
    result = run_search(ok({"documents": documents}), query="세종대", near={"lat": 37.5, "lng": 127})
    assert result[0]["name"] == "세종대학교"


def test_same_name_branches_are_ordered_by_distance_while_other_names_keep_their_slots():
    near = {"lat": 37.5547, "lng": 126.9707}

    def p(place_id, name, lat, lng):
        return {"id": place_id, "name": name, "address": "", "lat": lat, "lng": lng}

    ranked = rank_places(
        [
            p("far", "스타벅스", 37.4979, 127.0276),
            p("x", "스타벅스 리저브", 37.6, 127.1),
            p("near", "스타벅스", 37.555, 126.971),
            p("mid", "스타벅스", 37.52, 127.0),
        ],
        near,
    )
    assert [r["id"] for r in ranked] == ["near", "x", "mid", "far"]
    same = rank_places([p("a", "스타벅스", 1, 1), p("b", "스타벅스", 0, 0)], None)
    assert [r["id"] for r in same] == ["a", "b"]  # 기준 좌표가 없으면 그대로


# ---------- 좌표 → 주소 응답 ----------


def test_parse_reverse_road_address_first_with_building_name_jibun_only_and_empty_results():
    assert parse_reverse(
        {
            "meta": {"total_count": 1},
            "documents": [
                {
                    "road_address": {
                        "address_name": "서울 광진구 능동로 209",
                        "building_name": "세종대학교 광개토관",
                        "region_1depth_name": "서울",
                    },
                    "address": {"address_name": "서울 광진구 군자동 98"},
                }
            ],
        }
    ) == {
        "address": "서울 광진구 능동로 209",
        "roadAddress": "서울 광진구 능동로 209",
        "buildingName": "세종대학교 광개토관",
    }
    # 도로명은 있고 건물 이름이 빈 글자
    assert parse_reverse(
        {
            "documents": [
                {
                    "road_address": {"address_name": "서울 광진구 능동로 209", "building_name": ""},
                    "address": {"address_name": "서울 광진구 군자동 98"},
                }
            ]
        }
    ) == {"address": "서울 광진구 능동로 209", "roadAddress": "서울 광진구 능동로 209", "buildingName": None}
    # 지번만 있음 (road_address: null)
    assert parse_reverse(
        {"documents": [{"road_address": None, "address": {"address_name": "서울 광진구 군자동 98"}}]}
    ) == {"address": "서울 광진구 군자동 98", "roadAddress": None, "buildingName": None}
    # 주소가 없는 곳 (바다 · 국외) · 깨진 응답
    assert parse_reverse({"meta": {"total_count": 0}, "documents": []}) is None
    assert parse_reverse({"documents": [{"road_address": None, "address": None}]}) is None
    assert (
        parse_reverse(
            {"documents": [{"road_address": {"address_name": " "}, "address": {"address_name": ""}}]}
        )
        is None
    )
    assert parse_reverse({"documents": "x"}) is None
    assert parse_reverse(None) is None
    assert parse_reverse("oops") is None


def test_reverse_call_uses_x_longitude_y_latitude_and_key_header_and_none_when_no_result():
    calls: list[httpx.Request] = []

    def hit(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "documents": [
                    {
                        "road_address": {"address_name": "서울 광진구 능동로 209", "building_name": ""},
                        "address": None,
                    }
                ]
            },
        )

    assert run_reverse(hit, lat=37.5509, lng=127.0736) == {
        "address": "서울 광진구 능동로 209",
        "roadAddress": "서울 광진구 능동로 209",
        "buildingName": None,
    }
    assert dict(calls[0].url.params) == {"x": "127.0736", "y": "37.5509"}
    assert calls[0].headers["authorization"] == "KakaoAK KEY" and str(calls[0].url).startswith(COORD2ADDRESS)
    assert run_reverse(ok({"documents": []}), lat=0, lng=0) is None


# ---------- 실패는 종류별 KakaoError로 (오류 글에 좌표 · 검색어가 없음) ----------


@pytest.mark.parametrize(
    "status,kind",
    [(401, "AUTH"), (403, "AUTH"), (429, "RATE_LIMIT"), (500, "HTTP"), (404, "HTTP"), (302, "HTTP")],
)
def test_failure_kinds_by_status(status, kind):
    handler = lambda request: httpx.Response(status, json={})  # noqa: E731
    for call in (
        lambda: run_reverse(handler, lat=37.5509, lng=127.0736),
        lambda: run_search(handler, query="세종대"),
    ):
        with pytest.raises(KakaoError) as info:
            call()
        assert info.value.kind == kind
        assert "37.55" not in str(info.value) and "세종대" not in str(
            info.value
        )  # 좌표 · 검색어는 오류 글에 없음


def test_failure_kinds_for_timeout_connection_and_broken_body():
    def timeout(request):
        raise httpx.ReadTimeout("slow")

    def refused(request):
        raise httpx.ConnectError("down")

    not_json = lambda request: httpx.Response(200, text="not json")  # noqa: E731
    for handler, kind in ((timeout, "TIMEOUT"), (refused, "HTTP"), (not_json, "BAD_RESPONSE")):
        with pytest.raises(KakaoError) as info:
            run_reverse(handler, lat=37.5509, lng=127.0736)
        assert info.value.kind == kind


def test_the_kakao_call_has_a_4_second_timeout():
    seen: list[dict] = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"documents": []})

    run_reverse(handler, lat=1, lng=1)
    assert seen[0]["read"] == 4.0 and seen[0]["connect"] == 4.0  # 설계 7.1.2 "타임아웃 4초"

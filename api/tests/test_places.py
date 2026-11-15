"""POST /fn/place-search · /fn/place-reverse (완결점 1-12) — 요청 · 응답 · 오류 모양이 Edge와 같은지 HTTP 수준에서 확인.
카카오는 가짜 네트워크로 흉내냄. 순서: 토큰 확인(401) → 입력 검사(400) → 키 확인(503) → 카카오 호출 → 오류 번역."""

import json
import logging

import httpx
import pytest

from .conftest import bearer

KEY = "KAKAO-TEST-KEY-1234567890"
SEARCH = "/fn/place-search"
REVERSE = "/fn/place-reverse"


def kakao_doc(place_id, name, x, y, category=""):
    return {
        "id": place_id,
        "place_name": name,
        "address_name": "지번 주소",
        "road_address_name": "도로명 주소",
        "x": str(x),
        "y": str(y),
        "category_name": category,
    }


@pytest.fixture
def api(make_client, fake, signer):
    """api.post(path, body)로 토큰을 실어 부름. 카카오 설정 · 응답은 fake.kakao로"""
    fake.kakao = lambda request: httpx.Response(200, json={"documents": []})
    client = make_client(kakao_rest_api_key=KEY)

    class Api:
        def post(self, path, body=None, *, raw=None, token=True, **kwargs):
            headers = bearer(signer.token()) if token else {}
            if raw is not None:
                return client.post(path, content=raw, headers={**headers, "content-type": "application/json"})
            return client.post(path, json=body, headers=headers, **kwargs)

    return Api()


def error_of(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"]["code"] == code
    return body["error"]


# ---------- 순서: 토큰 → 입력 → 키 → 카카오 ----------


@pytest.mark.parametrize("path", [SEARCH, REVERSE])
def test_token_is_checked_before_the_body(api, fake, path):
    res = api.post(path, {}, token=False)  # 본문도 틀렸지만 401이 먼저
    error_of(res, 401, "AUTH_REQUIRED")
    assert fake.kakao_requests == []


@pytest.mark.parametrize("path", [SEARCH, REVERSE])
def test_validation_comes_before_the_key_check(make_client, fake, signer, path):
    client = make_client()  # 카카오 키 없음
    res = client.post(path, json={}, headers=bearer(signer.token()))
    error_of(res, 400, "VALIDATION_FAILED")  # 키가 없어도 입력이 틀리면 400


def test_missing_key_is_not_configured_with_each_functions_own_message(make_client, signer):
    client = make_client()
    search = client.post(SEARCH, json={"query": "세종대"}, headers=bearer(signer.token()))
    assert error_of(search, 503, "NOT_CONFIGURED")["message"] == "장소 검색 키가 아직 설정되지 않았어요"
    reverse = client.post(REVERSE, json={"lat": 37.5, "lng": 127}, headers=bearer(signer.token()))
    assert error_of(reverse, 503, "NOT_CONFIGURED")["message"] == "주소 조회 키가 아직 설정되지 않았어요"


def test_blank_key_counts_as_missing_and_stray_whitespace_around_a_key_is_removed(make_client, fake, signer):
    fake.kakao = lambda request: httpx.Response(200, json={"documents": []})
    blank = make_client(kakao_rest_api_key="  \n")
    res = blank.post(SEARCH, json={"query": "세종대"}, headers=bearer(signer.token()))
    error_of(res, 503, "NOT_CONFIGURED")
    assert fake.kakao_requests == []
    padded = make_client(kakao_rest_api_key=f"  {KEY}\n")  # 줄바꿈이 끼면 그대로는 헤더 오류(502)가 됨
    res = padded.post(SEARCH, json={"query": "세종대"}, headers=bearer(signer.token()))
    assert res.status_code == 200
    assert fake.kakao_requests[0].headers["authorization"] == f"KakaoAK {KEY}"


def test_empty_key_env_counts_as_missing(make_client, signer, monkeypatch):
    monkeypatch.setenv("KAKAO_REST_API_KEY", "")  # compose가 빈 값을 넣어도 "없음"
    monkeypatch.setenv("SUPABASE_URL", "http://supabase.test")
    from app.core.config import Settings

    assert Settings().kakao_rest_api_key is None


# ---------- place-search: 입력 검사 ----------


def issues(res):
    return error_of(res, 400, "VALIDATION_FAILED")["details"]["issues"]


def test_search_validation_issues_and_messages_match_edge(api):
    assert issues(api.post(SEARCH, {})) == [{"path": "query", "message": "필수 값이에요"}]
    assert issues(api.post(SEARCH, {"query": "   "})) == [{"path": "query", "message": "1자 이상이어야 해요"}]
    assert issues(api.post(SEARCH, {"query": "a" * 101})) == [
        {"path": "query", "message": "100자 이하여야 해요"}
    ]
    assert issues(api.post(SEARCH, {"query": "😀" * 51})) == [
        {"path": "query", "message": "100자 이하여야 해요"}
    ]
    assert issues(api.post(SEARCH, {"query": 5})) == [{"path": "query", "message": "글자여야 해요"}]
    assert issues(api.post(SEARCH, {"query": "a", "limit": 0})) == [
        {"path": "limit", "message": "1 이상이어야 해요"}
    ]
    assert issues(api.post(SEARCH, {"query": "a", "limit": 16})) == [
        {"path": "limit", "message": "15 이하여야 해요"}
    ]
    assert issues(api.post(SEARCH, {"query": "a", "limit": 2.5})) == [
        {"path": "limit", "message": "정수여야 해요"}
    ]
    assert issues(api.post(SEARCH, {"query": "a", "lat": "37", "lng": 127})) == [
        {"path": "lat", "message": "숫자여야 해요"},
        {
            "path": "lat",
            "message": "lat · lng는 같이 있어야 해요",
        },  # lat이 무효라 짝이 없는 것으로도 봄 (Edge와 같은 순서)
    ]


def test_search_lat_and_lng_must_come_together(api):
    assert issues(api.post(SEARCH, {"query": "a", "lat": 37.5})) == [
        {"path": "lng", "message": "lat · lng는 같이 있어야 해요"}
    ]
    assert issues(api.post(SEARCH, {"query": "a", "lng": 127})) == [
        {"path": "lat", "message": "lat · lng는 같이 있어야 해요"}
    ]
    assert issues(api.post(SEARCH, {"query": "a", "lat": 91, "lng": 181})) == [
        {"path": "lat", "message": "90 이하여야 해요"},
        {"path": "lng", "message": "180 이하여야 해요"},
    ]
    api_ok = api.post(SEARCH, {"query": "a", "lat": None, "lng": None})  # null은 없음
    assert api_ok.status_code == 200


@pytest.mark.parametrize(
    "raw,message",
    [
        (b"", "요청 본문(JSON)이 비어 있어요"),
        (b"{oops", "요청 본문이 JSON 형식이 아니에요"),
        (b"[1]", "요청 본문은 JSON 객체여야 해요"),
        (b'{"query":"a","lat":NaN,"lng":1}', "요청 본문이 JSON 형식이 아니에요"),
        (b'{"query":"' + b"a" * 100_000 + b'"}', "요청이 너무 커요"),
    ],
)
def test_search_body_level_failures_use_the_same_messages(api, raw, message):
    error = error_of(api.post(SEARCH, raw=raw), 400, "VALIDATION_FAILED")
    assert error["message"] == message and "details" not in error


def test_search_accepts_integral_float_limit_and_trims_the_query(api, fake):
    res = api.post(SEARCH, {"query": "  세종대  ", "limit": 5.0})
    assert res.status_code == 200
    params = dict(fake.kakao_requests[0].url.params)
    assert params["query"] == "세종대" and params["size"] == "5"


# ---------- place-search: 정상 · 카카오 호출 ----------


def test_search_success_shape_numbers_and_kakao_request(api, fake):
    fake.kakao = lambda request: httpx.Response(
        200,
        json={
            "documents": [
                kakao_doc("1", "세종대학교", "127.0736", "37.5509", "교육 > 대학교"),
                kakao_doc("2", "정수 좌표", "127", "37"),
            ]
        },
    )
    res = api.post(SEARCH, {"query": "세종대", "lat": 37.5, "lng": 127, "limit": 3})
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/json; charset=utf-8"
    assert res.json() == {
        "places": [
            {
                "id": "1",
                "name": "세종대학교",
                "address": "도로명 주소",
                "lat": 37.5509,
                "lng": 127.0736,
                "category": "교육 > 대학교",
            },
            {"id": "2", "name": "정수 좌표", "address": "도로명 주소", "lat": 37, "lng": 127},
        ]
    }
    assert '"lat":37,"lng":127}' in res.text  # JS처럼 37.0이 아니라 37, 공백 없는 JSON
    (call,) = fake.kakao_requests
    assert dict(call.url.params) == {
        "query": "세종대",
        "size": "3",
        "sort": "accuracy",
        "x": "127",
        "y": "37.5",
    }
    assert call.headers["authorization"] == f"KakaoAK {KEY}"
    assert res.headers["x-request-id"]


def test_search_size_defaults_to_10_and_the_result_is_cut_to_the_requested_limit(api, fake):
    api.post(SEARCH, {"query": "세종대"})
    assert fake.kakao_requests[-1].url.params["size"] == "10"  # limit을 안 주면 10
    fake.kakao_requests.clear()

    station = kakao_doc("S1", "강남역 2호선", "127.0276", "37.4979")

    def kakao(request):
        if request.url.params.get("category_group_code") == "SW8":
            return httpx.Response(200, json={"documents": [station]})
        docs = [kakao_doc(f"F{i}", f"가게{i}", "127.0", "37.5") for i in range(1, 4)]
        return httpx.Response(200, json={"documents": docs})

    fake.kakao = kakao
    res = api.post(SEARCH, {"query": "강남역", "limit": 2})
    assert [p["id"] for p in res.json()["places"]] == [
        "S1",
        "F1",
    ]  # 지하철역 1 + 본 검색 3 = 4건 중 limit 2건만


def test_search_with_no_results_is_an_empty_list_not_an_error(api):
    assert api.post(SEARCH, {"query": "없는곳"}).json() == {"places": []}


def test_search_station_query_puts_the_subway_station_first(api, fake):
    station = kakao_doc("S1", "강남역 2호선", "127.0276", "37.4979", "교통 > 지하철")

    def kakao(request):
        if request.url.params.get("category_group_code") == "SW8":
            return httpx.Response(200, json={"documents": [station]})
        docs = [kakao_doc("F1", "강남교자", "127.04", "37.54"), station]
        return httpx.Response(200, json={"documents": docs})

    fake.kakao = kakao
    res = api.post(SEARCH, {"query": "강남역"})
    assert [p["id"] for p in res.json()["places"]] == ["S1", "F1"]
    assert len(fake.kakao_requests) == 2


# ---------- 카카오 오류 번역 (Edge와 같은 코드 · 상태 · 문구) ----------


@pytest.mark.parametrize(
    "status,code,http,message",
    [
        (401, "NOT_CONFIGURED", 503, "장소 검색 키가 맞지 않거나 카카오 로컬 사용 설정이 꺼져 있어요"),
        (403, "NOT_CONFIGURED", 503, "장소 검색 키가 맞지 않거나 카카오 로컬 사용 설정이 꺼져 있어요"),
        (429, "RATE_LIMITED", 429, "잠시 후 다시 검색해 주세요"),
        (500, "PLACE_UNAVAILABLE", 502, "장소 검색을 지금은 쓸 수 없어요. 잠시 후 다시 시도해 주세요"),
        (404, "PLACE_UNAVAILABLE", 502, "장소 검색을 지금은 쓸 수 없어요. 잠시 후 다시 시도해 주세요"),
    ],
)
def test_search_kakao_status_translation(api, fake, status, code, http, message):
    fake.kakao = lambda request: httpx.Response(status, json={})
    error = error_of(api.post(SEARCH, {"query": "세종대"}), http, code)
    assert error["message"] == message and "details" not in error


@pytest.mark.parametrize("failure", ["timeout", "connect", "not_json"])
def test_search_kakao_transport_failures_are_place_unavailable(api, fake, failure):
    def kakao(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("slow")
        if failure == "connect":
            raise httpx.ConnectError("down")
        return httpx.Response(200, text="not json")

    fake.kakao = kakao
    error_of(api.post(SEARCH, {"query": "세종대"}), 502, "PLACE_UNAVAILABLE")


def test_search_failure_of_the_main_call_wins_over_a_successful_station_helper(api, fake):
    def kakao(request):
        if request.url.params.get("category_group_code") == "SW8":
            return httpx.Response(200, json={"documents": [kakao_doc("S", "강남역", "127", "37")]})
        return httpx.Response(500, json={})

    fake.kakao = kakao
    error_of(api.post(SEARCH, {"query": "강남역"}), 502, "PLACE_UNAVAILABLE")


# ---------- place-reverse ----------


def test_reverse_validation_issues_in_order(api):
    assert issues(api.post(REVERSE, {})) == [
        {"path": "lat", "message": "필수 값이에요"},
        {"path": "lng", "message": "필수 값이에요"},
    ]
    assert issues(api.post(REVERSE, {"lat": 91, "lng": -181})) == [
        {"path": "lat", "message": "90 이하여야 해요"},
        {"path": "lng", "message": "-180 이상이어야 해요"},
    ]
    assert issues(api.post(REVERSE, {"lat": "37", "lng": True})) == [
        {"path": "lat", "message": "숫자여야 해요"},
        {"path": "lng", "message": "숫자여야 해요"},
    ]
    assert api.post(REVERSE, {"lat": 90, "lng": -180}).status_code == 200  # 경계는 포함


def test_reverse_success_and_kakao_request(api, fake):
    fake.kakao = lambda request: httpx.Response(
        200,
        json={
            "documents": [
                {
                    "road_address": {"address_name": "서울 광진구 능동로 209", "building_name": "광개토관"},
                    "address": {"address_name": "서울 광진구 군자동 98"},
                }
            ]
        },
    )
    res = api.post(REVERSE, {"lat": 37.5509, "lng": 127.0736})
    assert res.status_code == 200
    assert res.json() == {
        "place": {
            "address": "서울 광진구 능동로 209",
            "roadAddress": "서울 광진구 능동로 209",
            "buildingName": "광개토관",
        }
    }
    (call,) = fake.kakao_requests
    assert dict(call.url.params) == {"x": "127.0736", "y": "37.5509"}  # x = 경도, y = 위도
    assert call.headers["authorization"] == f"KakaoAK {KEY}"


def test_reverse_no_address_is_place_null(api):
    assert api.post(REVERSE, {"lat": 0, "lng": 0}).json() == {"place": None}


@pytest.mark.parametrize(
    "status,code,http,message",
    [
        (401, "NOT_CONFIGURED", 503, "주소 조회 키가 맞지 않거나 카카오 로컬 사용 설정이 꺼져 있어요"),
        (429, "RATE_LIMITED", 429, "잠시 후 다시 시도해 주세요"),
        (500, "PLACE_UNAVAILABLE", 502, "주소를 지금은 불러올 수 없어요. 잠시 후 다시 시도해 주세요"),
    ],
)
def test_reverse_kakao_status_translation(api, fake, status, code, http, message):
    fake.kakao = lambda request: httpx.Response(status, json={})
    error = error_of(api.post(REVERSE, {"lat": 37.5, "lng": 127}), http, code)
    assert error["message"] == message


# ---------- 공통 ----------


@pytest.mark.parametrize("path", [SEARCH, REVERSE])
def test_get_is_method_not_allowed_in_edge_error_shape(make_client, signer, path):
    client = make_client(kakao_rest_api_key=KEY)
    res = client.get(path, headers=bearer(signer.token()))
    error_of(res, 405, "METHOD_NOT_ALLOWED")


@pytest.mark.parametrize("path", [SEARCH, REVERSE])
def test_options_preflight_is_204_with_cors(make_client, path):
    res = make_client().options(path)
    assert res.status_code == 204
    assert "authorization" in res.headers["access-control-allow-headers"]


def test_nothing_sensitive_reaches_logs_or_error_bodies(api, fake, caplog):
    """토큰 · 키 · 검색어 · 좌표가 로그와 오류 본문에 없음 (끝 칸 1-12 ④의 단위 시험판)"""
    caplog.set_level(logging.DEBUG)
    secret_query, lat, lng = "SECRETQUERYTERM", 37.123456789, 127.987654321
    results = [
        api.post(SEARCH, {"query": secret_query, "lat": lat, "lng": lng}),  # 성공
        api.post(REVERSE, {"lat": lat, "lng": lng}),
        api.post(SEARCH, {"query": secret_query, "lat": "x"}),  # 입력 오류
    ]
    for status in (401, 429, 500):
        fake.kakao = lambda request, s=status: httpx.Response(s, json={})
        results.append(api.post(SEARCH, {"query": secret_query, "lat": lat, "lng": lng}))
        results.append(api.post(REVERSE, {"lat": lat, "lng": lng}))

    def boom(request):
        raise httpx.ConnectError(
            f"cannot connect to {request.url}"
        )  # 예외 글에 URL(검색어 · 좌표)이 들어 있어도

    fake.kakao = boom
    results.append(api.post(SEARCH, {"query": secret_query, "lat": lat, "lng": lng}))
    results.append(api.post(REVERSE, {"lat": lat, "lng": lng}))

    haystack = caplog.text + "".join(r.text for r in results)
    for needle in (secret_query, "37.123456789", "127.987654321", KEY, "Bearer"):
        assert needle not in haystack, needle


def test_bodies_that_the_app_receives_parse_as_plain_json(api):
    res = api.post(SEARCH, {"query": "세종대"})
    assert json.loads(res.text) == {"places": []} and " " not in res.text

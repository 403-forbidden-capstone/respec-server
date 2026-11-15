"""카카오 호출 — 본 레포 `supabase/functions/_shared/kakao.ts`의 서버판 (장소 검색 · 좌표 → 주소 + 이동시간 `kakao_travel_minutes`).

인증은 `Authorization: KakaoAK {REST API 키}`. 응답은 저장 · 재사용하지 않음 (카카오 약관) — 아무것도 기억하지 않고 돌려주기만 함.
좌표 · 검색어는 URL 쿼리로만 나가고 로그 · 오류 글에는 안 남김 (httpx 로그는 WARNING 이상만 — main.py).
"""

import asyncio
import json
import math
from typing import Any

import httpx

from .kakao_parse import (
    Place,
    asks_for_station,
    js_number_str,
    parse_car_directions,
    parse_places,
    parse_reverse,
    parse_transit,
    parse_walk,
    rank_places,
)

KAKAO_TIMEOUT_SECONDS = 4.0  # 설계 7.1.2 "타임아웃 4초"

LOCAL_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
COORD2ADDRESS_URL = "https://dapi.kakao.com/v2/local/geo/coord2address.json"

SUBWAY_CATEGORY = "SW8"  # 카카오 카테고리 그룹 코드: 지하철역
SUBWAY_FIRST_SIZE = 5

TRANSIT_URL = "https://dapi.kakao.com/v2/routing/publictraffic"
WALK_URL = "https://dapi.kakao.com/v2/routing/walk"
CAR_URL = "https://apis-navi.kakaomobility.com/v1/directions"


class KakaoError(Exception):
    """kind: TIMEOUT · AUTH · RATE_LIMIT · NO_ROUTE · HTTP · BAD_RESPONSE. 메시지에는 좌표 · 검색어를 안 넣음"""

    def __init__(self, kind: str, status: int | None = None, message: str | None = None) -> None:
        super().__init__(message or kind)
        self.kind = kind
        self.status = status


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, str], key: str) -> Any:
    try:
        response = await client.get(
            url, params=params, headers={"Authorization": f"KakaoAK {key}"}, timeout=KAKAO_TIMEOUT_SECONDS
        )
    except httpx.TimeoutException:
        raise KakaoError("TIMEOUT") from None
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
        # UnicodeError: 키 · 검색어를 헤더 · URL로 못 만들 때(Edge는 fetch TypeError → 502). 메시지에 값이 있을 수 있어 안 이음(from None)
        raise KakaoError("HTTP") from None
    if response.status_code in (401, 403):
        raise KakaoError("AUTH", response.status_code)
    if response.status_code == 429:
        raise KakaoError("RATE_LIMIT", response.status_code)
    if not response.is_success:
        raise KakaoError("HTTP", response.status_code)
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        raise KakaoError("BAD_RESPONSE") from None


def _keyword_params(query: str, size: int, near: dict[str, float] | None) -> dict[str, str]:
    # 정확도순(기본)에 기준 좌표는 치우침으로만 — 거리순으로 부르면 "강남역"이 먼 곳의 가까운 음식점 뒤로 밀림 (x = 경도, y = 위도)
    params = {"query": query, "size": str(size), "sort": "accuracy"}
    if near:
        params["x"] = js_number_str(near["lng"])
        params["y"] = js_number_str(near["lat"])
    return params


async def search_places(
    client: httpx.AsyncClient,
    key: str,
    *,
    query: str,
    near: dict[str, float] | None = None,
    size: int = 10,
) -> list[Place]:
    size = min(15, max(1, size))
    main = asyncio.ensure_future(
        _get_json(client, LOCAL_KEYWORD_URL, _keyword_params(query, size, near), key)
    )
    if not asks_for_station(query):
        return rank_places(parse_places(await main), near)

    # "…역"이면 지하철역 분류만 따로 찾아 맨 위에 둠. 이 보조 호출이 실패해도 검색 자체는 계속
    station_params = {
        **_keyword_params(query, SUBWAY_FIRST_SIZE, near),
        "category_group_code": SUBWAY_CATEGORY,
    }

    async def station_only() -> Any:
        try:
            return await _get_json(client, LOCAL_KEYWORD_URL, station_params, key)
        except Exception:  # noqa: BLE001 — Edge `.catch(() => null)`처럼 보조 호출의 어떤 실패든 삼킴 (본 검색은 계속)
            return None

    station = asyncio.ensure_future(station_only())
    try:
        main_json = await main
    except BaseException:
        station.cancel()
        await asyncio.gather(station, return_exceptions=True)  # 정리 — 안 기다린 작업의 예외가 새지 않게
        raise
    station_json = await station
    stations = parse_places(station_json) if station_json else []
    seen = {p["id"] for p in stations}
    return [*stations, *(p for p in rank_places(parse_places(main_json), near) if p["id"] not in seen)][:size]


async def reverse_address(
    client: httpx.AsyncClient, key: str, *, lat: float, lng: float
) -> dict[str, Any] | None:
    """지도 핀 좌표의 주소. 주소가 없는 곳(바다 · 국외 등)은 None. 좌표를 로그 · 오류 글에 남기지 않음"""
    params = {"x": js_number_str(lng), "y": js_number_str(lat)}  # x = 경도, y = 위도
    return parse_reverse(await _get_json(client, COORD2ADDRESS_URL, params, key))


# ---------------------------------------------------------------------------
# 이동시간 (alarm-suggest · alarm-commit)
# ---------------------------------------------------------------------------


def _coord(point: dict[str, float]) -> str:
    return f"{js_number_str(point['lng'])},{js_number_str(point['lat'])}"


def _to_minutes(seconds: int | float) -> int:
    return max(0, math.ceil(seconds / 60))  # 올림: 모자라게 잡아 늦는 것보다 낫게


def _unwrap(parsed: dict[str, Any]) -> dict[str, Any]:
    if parsed["ok"]:
        return parsed
    kind = "NO_ROUTE" if parsed["reason"] == "NO_ROUTE" else "BAD_RESPONSE"
    raise KakaoError(kind, message=f"경로를 못 찾았어요 ({parsed.get('detail') or parsed['reason']})")


async def _walk_seconds(
    client: httpx.AsyncClient, key: str, start: dict[str, float], end: dict[str, float]
) -> int | float:
    params = {
        "start_x": js_number_str(start["lng"]),
        "start_y": js_number_str(start["lat"]),
        "end_x": js_number_str(end["lng"]),
        "end_y": js_number_str(end["lat"]),
    }
    return _unwrap(parse_walk(await _get_json(client, WALK_URL, params, key)))["seconds"]


async def kakao_travel_minutes(
    client: httpx.AsyncClient,
    key: str,
    *,
    origin: dict[str, float],
    destination: dict[str, float],
    transport: str,
) -> dict[str, Any]:
    """이동 수단에 맞는 카카오 API로 분 단위 이동시간을 구함 → {minutes, partial}. 실패하면 KakaoError.
    대중교통 · 버스 · 지하철: 대중교통 경로 + (응답에 빠진) 출발지 → 첫 승차 · 마지막 하차 → 목적지 도보를 도보 API로 더함.
    앞뒤 도보를 못 구하면 대중교통 구간만 (partial: True)"""
    if transport == "CAR":
        params = {"origin": _coord(origin), "destination": _coord(destination), "priority": "RECOMMEND"}
        car = _unwrap(parse_car_directions(await _get_json(client, CAR_URL, params, key)))
        return {"minutes": _to_minutes(car["seconds"]), "partial": False}

    if transport == "WALK":
        return {
            "minutes": _to_minutes(await _walk_seconds(client, key, origin, destination)),
            "partial": False,
        }

    params = {
        "start_x": js_number_str(origin["lng"]),
        "start_y": js_number_str(origin["lat"]),
        "end_x": js_number_str(destination["lng"]),
        "end_y": js_number_str(destination["lat"]),
    }
    wanted = "BUS" if transport == "BUS" else "SUBWAY" if transport == "SUBWAY" else None
    transit = _unwrap(parse_transit(await _get_json(client, TRANSIT_URL, params, key), wanted))

    seconds = transit["seconds"]
    partial = False
    legs = []
    if not transit["startsWithWalk"]:
        if "firstPoint" in transit:
            legs.append(_walk_seconds(client, key, origin, transit["firstPoint"]))
        else:
            partial = True  # 첫 승차 지점을 응답에서 못 읽음
    if not transit["endsWithWalk"]:
        if "lastPoint" in transit:
            legs.append(_walk_seconds(client, key, transit["lastPoint"], destination))
        else:
            partial = True
    for leg in await asyncio.gather(*legs, return_exceptions=True):
        if isinstance(leg, BaseException):
            partial = True
        else:
            seconds += leg
    return {"minutes": _to_minutes(seconds), "partial": partial}

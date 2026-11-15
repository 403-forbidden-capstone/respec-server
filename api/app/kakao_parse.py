"""카카오 응답 읽기 — 본 레포 `supabase/functions/_shared/kakao-parse.ts`(장소 검색 · 좌표 → 주소 부분)의 서버판.

순수 함수라 샘플 JSON으로 시험함 (tests/test_kakao_parse.py — Edge의 kakao-parse.test.ts 샘플을 그대로 옮김).
이동시간(대중교통 · 도보 · 자동차) 읽기(parse_transit · parse_walk · parse_car_directions)는 알람 계산 옮기기(W2)에서 더함 — 결과는 Edge와 같은 키 이름의 dict (없는 값은 키 자체를 뺌).
JS와 같게 맞춘 곳: 숫자 문자열 읽기(빈 글자 = 0, 앞뒤 공백 무시, 무한대 · NaN 거절) · trim · 정수로 떨어지는 숫자는 정수로 내보냄(37.0이 아니라 37).
"""

import math
import re
from typing import Any

from .core.read import JS_WHITESPACE, js_trim
from .shared.js import js_number, js_number_to_string

Place = dict[str, Any]  # {id, name, address, lat, lng, category?} — 키 순서까지 Edge와 같음


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def out_number(value: int | float) -> int | float:
    """JS는 37.0과 37을 구분하지 않아 `37`로 찍음 — 정수로 떨어지는 값은 정수로"""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    return value


def to_number(value: Any) -> int | float | None:
    """Edge `num`: 글자는 Number(글자), 숫자는 그대로. 유한한 숫자만 — 아니면 None"""
    if isinstance(value, str):
        number: int | float = js_number(value)  # Number(글자) — 빈 글자 0 · 16진수 · NaN
    elif isinstance(value, bool) or not isinstance(value, int | float):
        return None
    else:
        number = value
    try:
        if not math.isfinite(float(number)):
            return None
    except OverflowError:
        return None
    return out_number(number)


def text_of(value: Any) -> str:
    return js_trim(value) if isinstance(value, str) else ""


def js_number_str(value: int | float) -> str:
    """JS `String(n)` — 카카오에 보내는 좌표 글자. 127 → '127', 37.5 → '37.5', 1e-7 → '1e-7' (app.shared.js.js_number_to_string과 같음)"""
    return js_number_to_string(value)


# JS `/\S역$/`의 \S — JS 공백이 아닌 글자 (파이썬 \S는 ~ · 를 공백으로 보고 U+FEFF는 공백으로 안 봐서 따로 만듦)
_STATION_QUERY = re.compile(r"[^" + re.escape(JS_WHITESPACE) + r"]역\Z")


def asks_for_station(query: str) -> bool:
    """검색어가 "…역"으로 끝나면 지하철역을 찾는 것으로 봄 ("강남역", "서울 시청역"). 한 글자 "역"은 제외"""
    return _STATION_QUERY.search(js_trim(query)) is not None


def _meters_between(a: dict[str, float], b: Place) -> float:
    # 순서를 매기는 데만 쓰는 근사 (위도 1도 ≈ 111km, 경도는 위도에 따라 줄임) — Edge와 같은 식
    dy = (a["lat"] - b["lat"]) * 111_000
    dx = (a["lng"] - b["lng"]) * 111_000 * math.cos(a["lat"] * math.pi / 180)
    return math.hypot(dx, dy)


def rank_places(places: list[Place], near: dict[str, float] | None) -> list[Place]:
    """같은 이름이 여러 곳(스타벅스 여러 지점)이면 그 이름끼리는 기준 좌표에서 가까운 순으로 — 이름 사이의 순서(정확도순)는 그대로"""
    if not near:
        return places
    slots: dict[str, list[int]] = {}
    for index, place in enumerate(places):
        slots.setdefault(place["name"], []).append(index)
    out = list(places)
    for indexes in slots.values():
        if len(indexes) < 2:
            continue
        ordered = sorted((places[i] for i in indexes), key=lambda p: _meters_between(near, p))  # 안정 정렬
        for slot, place in zip(indexes, ordered, strict=True):
            out[slot] = place
    return out


def parse_places(json_value: Any) -> list[Place]:
    docs = json_value.get("documents") if _is_object(json_value) else None
    if not isinstance(docs, list):
        return []
    out: list[Place] = []
    for doc in docs:
        if not _is_object(doc):
            continue
        lat = to_number(doc.get("y"))
        lng = to_number(doc.get("x"))
        place_id = text_of(doc.get("id"))
        name = text_of(doc.get("place_name"))
        if lat is None or lng is None or not place_id or not name:
            continue
        category = text_of(doc.get("category_name"))
        place: Place = {
            "id": place_id,
            "name": name,
            "address": text_of(doc.get("road_address_name")) or text_of(doc.get("address_name")),
            "lat": lat,
            "lng": lng,
        }
        if category:
            place["category"] = category
        out.append(place)
    return out


def parse_reverse(json_value: Any) -> dict[str, Any] | None:
    """좌표 → 주소 응답. 첫 결과만 읽고, 도로명이 있으면 address도 도로명 (지번만 있으면 지번). 주소가 없으면 None"""
    docs = json_value.get("documents") if _is_object(json_value) else None
    if not isinstance(docs, list):
        return None
    first = next((d for d in docs if _is_object(d)), None)
    if first is None:
        return None
    road = first.get("road_address") if _is_object(first.get("road_address")) else None
    jibun = first.get("address") if _is_object(first.get("address")) else None
    road_address = text_of(road.get("address_name")) if road else ""
    jibun_address = text_of(jibun.get("address_name")) if jibun else ""
    address = road_address or jibun_address
    if not address:
        return None
    building_name = text_of(road.get("building_name")) if road else ""
    return {
        "address": address,
        "roadAddress": road_address or None,
        "buildingName": building_name or None,
    }


def _num_at(items: Any, index: int) -> int | float | None:
    """JS `num(p[index])` — 배열 밖 칸(undefined)은 None"""
    return to_number(items[index]) if isinstance(items, list) and 0 <= index < len(items) else None


def _point(value: Any) -> dict[str, int | float] | None:
    if not isinstance(value, list):
        return None
    lng = _num_at(value, 0)
    lat = _num_at(value, 1)
    return {"lat": lat, "lng": lng} if lat is not None and lng is not None else None


def parse_transit(json_value: Any, wanted: str | None = None) -> dict[str, Any]:
    """가장 빨리 가는 경로 하나. wanted('BUS' · 'SUBWAY')로 버스만 · 지하철만 고를 수 있고, 맞는 게 없으면 전체에서 고름.
    성공: {ok: True, seconds, startsWithWalk, endsWithWalk, firstPoint?, lastPoint?} · 실패: {ok: False, reason, detail?}"""
    if not _is_object(json_value):
        return {"ok": False, "reason": "BAD_RESPONSE"}
    status = text_of(json_value.get("status"))
    if status == "EQUAL_POINTS":
        return {"ok": True, "seconds": 0, "startsWithWalk": True, "endsWithWalk": True}
    if status in ("NO_RESULTS", "STARTNODES_NULL", "ENDNODES_NULL"):
        return {"ok": False, "reason": "NO_ROUTE", "detail": status}
    raw_routes = json_value.get("routes")
    if status != "OK" or not isinstance(raw_routes, list):
        return {"ok": False, "reason": "BAD_RESPONSE", "detail": status}

    routes: list[dict[str, Any]] = []
    for route in raw_routes:
        if not _is_object(route):
            continue
        props = route.get("properties") if _is_object(route.get("properties")) else {}
        seconds = to_number(props.get("totalTime"))
        if seconds is not None:
            routes.append({"r": route, "seconds": seconds, "type": text_of(props.get("type"))})
    if not routes:
        return {"ok": False, "reason": "NO_ROUTE"}
    preferred = [x for x in routes if x["type"] == wanted] if wanted else routes
    pool = preferred or routes
    best = pool[0]
    for candidate in pool[1:]:  # JS reduce((a, b) => (b.seconds < a.seconds ? b : a)) — 같으면 앞의 것
        if candidate["seconds"] < best["seconds"]:
            best = candidate

    raw_steps = best["r"].get("steps")
    steps = [s for s in raw_steps if _is_object(s)] if isinstance(raw_steps, list) else []

    def type_of(step: dict[str, Any] | None) -> str:
        return text_of(step["properties"].get("type")) if step and _is_object(step.get("properties")) else ""

    def points(step: dict[str, Any] | None) -> list[Any]:
        if step and _is_object(step.get("path")) and isinstance(step["path"].get("points"), list):
            return step["path"]["points"]
        return []

    first = steps[0] if steps else None
    last = steps[-1] if steps else None
    first_points = points(first)
    last_points = points(last)
    result: dict[str, Any] = {
        "ok": True,
        "seconds": best["seconds"],
        "startsWithWalk": type_of(first) == "WALKING",
        "endsWithWalk": type_of(last) == "WALKING",
    }
    first_point = _point(first_points[0]) if first_points else None
    last_point = _point(last_points[-1]) if last_points else None
    if first_point is not None:
        result["firstPoint"] = first_point
    if last_point is not None:
        result["lastPoint"] = last_point
    return result


def parse_walk(json_value: Any) -> dict[str, Any]:
    if not _is_object(json_value):
        return {"ok": False, "reason": "BAD_RESPONSE"}
    status = text_of(json_value.get("status"))
    if status == "SAME_POINT":
        return {"ok": True, "seconds": 0}
    route = json_value.get("route")
    if status == "OK" and _is_object(route) and _is_object(route.get("properties")):
        seconds = to_number(route["properties"].get("totalTime"))
        if seconds is not None:
            return {"ok": True, "seconds": seconds}
        return {"ok": False, "reason": "BAD_RESPONSE", "detail": status}
    no_route = (
        "START_LINK_NOT_FOUND",
        "END_LINK_NOT_FOUND",
        "TOO_MANY_SEARCH_LINK",
        "TOO_FAR_AWAY",
        "ROUTE_RESULT_NOT_FOUND",
    )
    return {"ok": False, "reason": "NO_ROUTE" if status in no_route else "BAD_RESPONSE", "detail": status}


def parse_car_directions(json_value: Any) -> dict[str, Any]:
    routes = json_value.get("routes") if _is_object(json_value) else None
    first = next((r for r in routes if _is_object(r)), None) if isinstance(routes, list) else None
    if first is None:
        return {"ok": False, "reason": "BAD_RESPONSE"}
    code = to_number(first.get("result_code"))
    if code == 104:
        return {"ok": True, "seconds": 0}  # 출발지와 목적지가 같음
    if code != 0:
        shown = "?" if code is None else js_number_str(code)
        return {"ok": False, "reason": "NO_ROUTE", "detail": f"result_code {shown}"}
    seconds = to_number(first["summary"].get("duration")) if _is_object(first.get("summary")) else None
    if seconds is None:
        return {"ok": False, "reason": "BAD_RESPONSE", "detail": "summary.duration"}
    return {"ok": True, "seconds": seconds}

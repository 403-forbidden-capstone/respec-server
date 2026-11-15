"""카카오 응답 읽기 — 본 레포 `supabase/functions/_shared/kakao-parse.ts`(장소 검색 · 좌표 → 주소 부분)의 서버판.

순수 함수라 샘플 JSON으로 시험함 (tests/test_kakao_parse.py — Edge의 kakao-parse.test.ts 샘플을 그대로 옮김).
이동시간(대중교통 · 도보 · 자동차) 읽기는 알람 계산을 옮길 때(2-6) 같이 옮김.
JS와 같게 맞춘 곳: 숫자 문자열 읽기(빈 글자 = 0, 앞뒤 공백 무시, 무한대 · NaN 거절) · trim · 정수로 떨어지는 숫자는 정수로 내보냄(37.0이 아니라 37).
"""

import math
import re
from typing import Any

from .core.read import js_trim

Place = dict[str, Any]  # {id, name, address, lat, lng, category?} — 키 순서까지 Edge와 같음


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def out_number(value: int | float) -> int | float:
    """JS는 37.0과 37을 구분하지 않아 `37`로 찍음 — 정수로 떨어지는 값은 정수로"""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return int(value)
    return value


def to_number(value: Any) -> int | float | None:
    """Edge `num`: 글자는 Number(글자), 숫자는 그대로. 유한한 숫자만 — 아니면 None"""
    if isinstance(value, str):
        text = js_trim(value)
        if not text:
            number: int | float = 0  # JS Number('') === 0
        else:
            if "_" in text or not text.isascii():  # 파이썬은 받아주지만 JS는 NaN
                return None
            try:
                number = float(text)
            except ValueError:
                return None
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
    """JS `String(n)` — 카카오에 보내는 좌표 글자 (127 → '127', 37.5 → '37.5')"""
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value).replace("e-0", "e-").replace("e+0", "e+")
    return str(value)


def asks_for_station(query: str) -> bool:
    """검색어가 "…역"으로 끝나면 지하철역을 찾는 것으로 봄 ("강남역", "서울 시청역"). 한 글자 "역"은 제외"""
    return re.search(r"\S역\Z", js_trim(query)) is not None


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

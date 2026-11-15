"""본 레포 supabase/functions/_shared/calc/travel.ts의 서버판 (기준 b140f70) — 이동시간 폴백의 계산 부분, 상세설계서 7장 7.1.2

순서는 _shared/route.ts가 정함: 1 카카오 길찾기(API)  2 같은 경로 실측 이동시간 3건 이상 → 중앙값(CACHED)  3 좌표 직선거리 근사값  4 직접 입력(MANUAL)
카카오맵 API 응답은 저장 · 재사용할 수 없으므로 2단계는 "내 실제 출발 · 도착 기록"만 씀. 중앙값은 계산해서 보여 주기만 하고 travel_source = CACHED로 쓰지 않음 (D5 열림)
좌표 = {lat, lng} dict, 지난 이동 기록(TravelSample) = {origin, destination, transport, departedAt, arrivedAt} dict
"""

import math
from datetime import datetime
from typing import Any, TypedDict

from ..js import js_floor, js_round, ms
from .time import MINUTE_MS

__all__ = [
    "AVERAGE_SPEED_KMH",
    "DETOUR_FACTOR",
    "SAME_ROUTE_MIN_SAMPLES",
    "SAME_ROUTE_RADIUS_METERS",
    "TravelSample",
    "estimate_travel_minutes",
    "haversine_km",
    "median_same_route",
]


class TravelSample(TypedDict):
    """지난 이동 기록 한 건 (TS TravelSample) — 출발 · 도착 좌표 {lat, lng} · 이동 수단 · 출발 · 도착 시각"""

    origin: dict[str, float]
    destination: dict[str, float]
    transport: str
    departedAt: datetime
    arrivedAt: datetime


SAME_ROUTE_RADIUS_METERS = 300  # 같은 출발지 · 목적지로 보는 반경
SAME_ROUTE_MIN_SAMPLES = 3
DETOUR_FACTOR = 1.3  # 직선거리 → 도로거리 우회 계수
# 이동 수단별 평균 속도 (km/h). BUS · SUBWAY는 대중교통과 같게
AVERAGE_SPEED_KMH = {"WALK": 4, "TRANSIT": 22, "BUS": 22, "SUBWAY": 22, "CAR": 30}


def haversine_km(a: dict[str, float], b: dict[str, float]) -> float:
    def rad(d: float) -> float:
        return (d * math.pi) / 180

    d_lat = rad(b["lat"] - a["lat"])
    d_lng = rad(b["lng"] - a["lng"])
    h = (
        math.sin(d_lat / 2) ** 2
        + math.cos(rad(a["lat"])) * math.cos(rad(b["lat"])) * math.sin(d_lng / 2) ** 2
    )
    return 2 * 6371 * math.asin(min(1, math.sqrt(h)))


def estimate_travel_minutes(from_: dict[str, float], to: dict[str, float], transport: str) -> int:
    """근사식: haversine 거리(km) ÷ 속도 × 60 × 우회 계수 1.3. 사용자 확인 없이 저장하지 않음"""
    minutes = (haversine_km(from_, to) / AVERAGE_SPEED_KMH[transport]) * 60 * DETOUR_FACTOR
    return max(1, js_round(minutes))


def median_same_route(
    samples: list[dict[str, Any]],
    origin: dict[str, float],
    destination: dict[str, float],
    transport: str,
) -> dict[str, int] | None:
    """같은 출발지(300m) · 목적지(300m) · 이동 수단의 실측 이동시간(도착 − 출발) 3건 이상이면 중앙값 {minutes, sampleCount}
    1분 미만 · 5시간 초과는 기록 오류로 보고 뺌"""
    radius_km = SAME_ROUTE_RADIUS_METERS / 1000
    durations = (
        (ms(s["arrivedAt"]) - ms(s["departedAt"])) / MINUTE_MS
        for s in samples
        if s["transport"] == transport
        and haversine_km(s["origin"], origin) <= radius_km
        and haversine_km(s["destination"], destination) <= radius_km
    )
    minutes = sorted(m for m in durations if 1 <= m <= 300)
    if len(minutes) < SAME_ROUTE_MIN_SAMPLES:
        return None
    mid = js_floor(len(minutes) / 2)
    median = minutes[mid] if len(minutes) % 2 else (minutes[mid - 1] + minutes[mid]) / 2
    return {"minutes": max(1, js_round(median)), "sampleCount": len(minutes)}

"""calc/travel.test.ts — 이동시간 폴백 계산: 7장 7.1.2"""

from app.shared.calc.travel import estimate_travel_minutes, haversine_km, median_same_route
from app.shared.js import from_ms, js_round, ms, parse_iso

SEOUL_STATION = {"lat": 37.5547, "lng": 126.9707}
GANGNAM_STATION = {"lat": 37.4979, "lng": 127.0276}


def test_haversine_서울역_강남역_약_8_1km():
    km = haversine_km(SEOUL_STATION, GANGNAM_STATION)
    assert 7.9 < km < 8.3, str(km)
    assert haversine_km(SEOUL_STATION, SEOUL_STATION) == 0


def test_근사식_거리_나누기_속도_곱하기_60_곱하기_1_3_도보_4_대중교통_22_자차_30_km_h():
    km = haversine_km(SEOUL_STATION, GANGNAM_STATION)
    assert estimate_travel_minutes(SEOUL_STATION, GANGNAM_STATION, "TRANSIT") == js_round(
        (km / 22) * 60 * 1.3
    )
    assert estimate_travel_minutes(SEOUL_STATION, GANGNAM_STATION, "CAR") == js_round((km / 30) * 60 * 1.3)
    assert estimate_travel_minutes(SEOUL_STATION, GANGNAM_STATION, "WALK") == js_round((km / 4) * 60 * 1.3)
    assert estimate_travel_minutes(SEOUL_STATION, GANGNAM_STATION, "SUBWAY") == estimate_travel_minutes(
        SEOUL_STATION, GANGNAM_STATION, "BUS"
    )
    assert estimate_travel_minutes(SEOUL_STATION, SEOUL_STATION, "WALK") == 1  # 최소 1분


T0 = parse_iso("2026-10-01T00:00:00Z")


def sample(minutes, **over):
    return {
        "origin": SEOUL_STATION,
        "destination": GANGNAM_STATION,
        "transport": "TRANSIT",
        "departedAt": T0,
        "arrivedAt": from_ms(ms(T0) + minutes * 60_000),
        **over,
    }


def test_같은_경로_실측_3건_이상이면_중앙값():
    r = median_same_route([sample(40), sample(50), sample(45)], SEOUL_STATION, GANGNAM_STATION, "TRANSIT")
    assert r == {"minutes": 45, "sampleCount": 3}
    # 짝수 건이면 가운데 둘의 평균
    assert (
        median_same_route(
            [sample(40), sample(50), sample(45), sample(60)], SEOUL_STATION, GANGNAM_STATION, "TRANSIT"
        )["minutes"]
        == 48
    )


def test_3건_미만이면_null():
    assert median_same_route([sample(40), sample(50)], SEOUL_STATION, GANGNAM_STATION, "TRANSIT") is None


def test_이동_수단이_다르거나_300m_밖이면_세지_않음():
    far = {"lat": SEOUL_STATION["lat"] + 0.01, "lng": SEOUL_STATION["lng"]}  # 약 1.1km
    items = [sample(40), sample(50), sample(45, transport="CAR"), sample(30, origin=far)]
    assert median_same_route(items, SEOUL_STATION, GANGNAM_STATION, "TRANSIT") is None
    assert (
        median_same_route([*items, sample(60)], SEOUL_STATION, GANGNAM_STATION, "TRANSIT")["sampleCount"] == 3
    )


def test_기록_오류_1분_미만_5시간_초과는_뺌():
    items = [sample(0), sample(400), sample(40), sample(50)]
    assert median_same_route(items, SEOUL_STATION, GANGNAM_STATION, "TRANSIT") is None


def test_중앙값_절반_반올림은_위쪽_짝수_건_평균이_x_5일_때():
    # 40 · 41 → 40.5 → Math.round 41 (Python round는 40), 경계 1분 · 300분은 포함
    assert (
        median_same_route([sample(40), sample(41), sample(100)], SEOUL_STATION, GANGNAM_STATION, "TRANSIT")[
            "minutes"
        ]
        == 41
    )
    assert median_same_route(
        [sample(1), sample(40), sample(41), sample(300)], SEOUL_STATION, GANGNAM_STATION, "TRANSIT"
    ) == {"minutes": 41, "sampleCount": 4}
    assert (
        median_same_route([sample(1), sample(1), sample(1)], SEOUL_STATION, GANGNAM_STATION, "TRANSIT")[
            "minutes"
        ]
        == 1
    )

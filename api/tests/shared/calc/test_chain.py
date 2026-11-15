"""calc/chain.test.ts — 알람 역산: 앱 목업 계산(mobile/src/mocks/calc.ts)과 같은 숫자인지 확인하는 교차 검증
목업 시나리오: 2025년 5월 22일(목) 한국 시각, 10시 팀 주간회의 (mocks/data.ts a1 ~ a4)"""

from app.shared.calc.chain import alarm_chain, departure_at, manual_times
from app.shared.js import from_ms, ms, utc_ms


def kst(day, minutes):
    """2025년 5월 day일 한국 시각 minutes분 (mocks/data.ts kst와 같음)"""
    return from_ms(utc_ms(2025, 4, day) + (minutes - 9 * 60) * 60_000)


def hm(d):
    m = ((d.hour * 60 + d.minute) + 9 * 60) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def text(chain):
    return [f"{x['kind']} {hm(x['at'])}{'*' if 'auto' in x else ''}" for x in chain]


# mocks/data.ts 일정 (travelMinutes · bufferMinutes · beforePrepMinutes · predictedPrepMinutes · withWake)
A1 = {
    "at": kst(22, 600),
    "travelMinutes": 50,
    "bufferMinutes": 10,
    "beforePrepareMinutes": 30,
    "prepareMinutes": 60,
    "withWake": True,
}
A2 = {
    "at": kst(22, 1080),
    "travelMinutes": 20,
    "bufferMinutes": 5,
    "beforePrepareMinutes": 0,
    "prepareMinutes": 25,
    "withWake": False,
}
A3 = {
    "at": kst(23, 540),
    "travelMinutes": 50,
    "bufferMinutes": 10,
    "beforePrepareMinutes": 10,
    "prepareMinutes": 40,
    "withWake": True,
}


def test_목업_a1_10시_팀_주간회의_기상_8시_준비_8시30_출발_9시_도착_목표_9시50():
    assert text(alarm_chain(A1)) == ["WAKE 08:00", "PREPARE 08:30", "DEPARTURE 09:00", "ARRIVE 09:50"]


def test_목업_a2_그날_첫_일정이_아니면_기상_없이_준비부터_준비_17시10_출발_17시35_도착_목표_17시55():
    assert text(alarm_chain(A2)) == ["PREPARE 17:10", "DEPARTURE 17:35", "ARRIVE 17:55"]


def test_목업_a3_다음_날_9시_수업_기상_7시20_준비_7시30_출발_8시_도착_목표_8시50():
    assert text(alarm_chain(A3)) == ["WAKE 07:20", "PREPARE 07:30", "DEPARTURE 08:00", "ARRIVE 08:50"]


def test_준비_전_활동이_0이면_기상_알람_뒤_준비_알람이_없음():
    assert text(alarm_chain({**A1, "beforePrepareMinutes": 0})) == [
        "WAKE 08:00",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]


def test_고른_종류만_도착_목표는_울리는_알람이_하나라도_있을_때만():
    assert text(alarm_chain({**A1, "kinds": ["WAKE"]})) == ["WAKE 08:00", "ARRIVE 09:50"]
    assert text(alarm_chain({**A1, "kinds": ["DEPARTURE", "PREPARE"]})) == [
        "PREPARE 08:30",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]
    assert (
        alarm_chain({**A1, "kinds": []}) == []
    )  # 알람 없는 일정 (목업 a4 치과 예약). JS에서 []는 truthy라 빈 목록도 거름


def test_기상_알람이_없는_일정에서_기상만_고르면_알람_없음_그날_첫_일정이_아님():
    assert text(alarm_chain({**A2, "kinds": ["WAKE"]})) == []


def test_직접_고른_시각이_자동_계산보다_먼저_자동값은_auto로_남김_D42_C():
    chain = alarm_chain({**A1, "manualTimes": {"WAKE": kst(22, 450)}})
    assert text(chain) == [
        "WAKE 07:30*",
        "PREPARE 08:00",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]  # 준비는 직접 고른 기상 뒤 30분
    assert hm(chain[0]["auto"]) == "08:00"
    assert "auto" not in chain[1]  # 따라온 준비는 직접 고른 것이 아님 (JS: chain[1].auto === undefined)
    assert manual_times(chain) == {"WAKE": kst(22, 450)}


def test_자동값과_같은_시각을_고르면_직접_고른_것이_아님():
    chain = alarm_chain({**A1, "manualTimes": {"WAKE": kst(22, 480), "DEPARTURE": kst(22, 540)}})
    assert text(chain) == ["WAKE 08:00", "PREPARE 08:30", "DEPARTURE 09:00", "ARRIVE 09:50"]
    assert manual_times(chain) == {}


def test_도착_목표는_직접_고를_수_없음():
    times = {"ARRIVE": kst(22, 580)}
    assert text(alarm_chain({**A1, "manualTimes": times})) == [
        "WAKE 08:00",
        "PREPARE 08:30",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]


def test_이동_여유를_바꾸면_그대로_따라_움직임():
    assert text(alarm_chain({**A1, "travelMinutes": 70})) == [
        "WAKE 07:40",
        "PREPARE 08:10",
        "DEPARTURE 08:40",
        "ARRIVE 09:50",
    ]
    assert text(alarm_chain({**A1, "bufferMinutes": 20})) == [
        "WAKE 07:50",
        "PREPARE 08:20",
        "DEPARTURE 08:50",
        "ARRIVE 09:40",
    ]


def test_출발_시각은_출발_알람을_안_만들어도_구할_수_있음_단축_준비_안내_departAt():
    assert hm(departure_at({**A1, "kinds": ["WAKE"]})) == "09:00"
    assert hm(departure_at({**A1, "kinds": [], "manualTimes": {"DEPARTURE": kst(22, 550)}})) == "09:10"


# 준비 알람 당김 (안건 D111 1 · 2번) — a1: 기상 8:00 · 준비 8:30(준비 전 활동 30분) · 출발 9:00
def test_준비_알람_당김_준비_전_활동_안이면_준비_알람만_앞당기고_기상_출발은_그대로():
    assert text(alarm_chain({**A1, "prepareAdvanceMinutes": 10})) == [
        "WAKE 08:00",
        "PREPARE 08:20",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]


def test_준비_알람_당김_준비_전_활동을_다_쓰면_준비_알람이_기상과_같은_시각으로_남고_넘는_만큼만_기상도_앞당김():
    assert text(alarm_chain({**A1, "prepareAdvanceMinutes": 30})) == [
        "WAKE 08:00",
        "PREPARE 08:00",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]
    assert text(alarm_chain({**A1, "prepareAdvanceMinutes": 40})) == [
        "WAKE 07:50",
        "PREPARE 07:50",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]


def test_준비_알람_당김_기상_알람이_없는_일정은_준비_알람만_앞당김_0이나_없으면_예전과_같음():
    assert text(alarm_chain({**A2, "prepareAdvanceMinutes": 10})) == [
        "PREPARE 17:00",
        "DEPARTURE 17:35",
        "ARRIVE 17:55",
    ]
    assert text(alarm_chain({**A1, "prepareAdvanceMinutes": 0})) == text(alarm_chain(A1))
    assert ms(departure_at({**A1, "prepareAdvanceMinutes": 40})) == ms(departure_at(A1))

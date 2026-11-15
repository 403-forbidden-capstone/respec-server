"""calc/validate.test.ts — 저장 검증: 8장 8.2.1 · 7장 7.1.4"""

from app.shared.calc.chain import alarm_chain, departure_at
from app.shared.calc.order import move_alarm
from app.shared.calc.validate import PAST_GRACE_MINUTES, check_alarms
from app.shared.js import from_ms, utc_ms


def kst(day, minutes):
    return from_ms(utc_ms(2025, 4, day) + (minutes - 9 * 60) * 60_000)


# 기상 8:00 · 준비 8:30 · 출발 9:00 · 도착 목표 9:50
A1 = {
    "at": kst(22, 600),
    "travelMinutes": 50,
    "bufferMinutes": 10,
    "beforePrepareMinutes": 30,
    "prepareMinutes": 60,
    "withWake": True,
}


def run(input, now):
    return check_alarms(alarm_chain(input), departure_at(input), now)


def test_여유_있게_앞이면_전부_켜서_저장():
    r = run(A1, kst(22, 420))
    assert r["ok"]
    assert [x["enabled"] for x in r["alarms"]] == [True, True, True, True]


def test_기상이_지금_5분보다_앞이면_기상만_끄고_나머지는_저장_8_2_1():
    r = run(A1, kst(22, 478))  # 기상 8:00 < 7:58 + 5분
    assert r["ok"]
    assert [f"{x['kind']}:{str(x['enabled']).lower()}" for x in r["alarms"]] == [
        "WAKE:false",
        "PREPARE:true",
        "DEPARTURE:true",
        "ARRIVE:true",
    ]
    assert r["alarms"][0]["disabledReason"] == "PASSED"


def test_정확히_지금_5분이면_아직_켬_앞일_때만_끔():
    r = run(A1, kst(22, 480 - PAST_GRACE_MINUTES))
    assert r["ok"]
    assert r["alarms"][0]["enabled"] is True
    assert "disabledReason" not in r["alarms"][0]


def test_기상_준비가_지났어도_출발_전이면_저장_출발_도착_목표는_켬():
    r = run(A1, kst(22, 535))
    assert r["ok"]
    assert [f"{x['kind']}:{str(x['enabled']).lower()}" for x in r["alarms"]] == [
        "WAKE:false",
        "PREPARE:false",
        "DEPARTURE:true",
        "ARRIVE:true",
    ]


def test_출발_시각까지_지났으면_저장_막음_UNPROCESSABLE():
    r = run(A1, kst(22, 541))
    assert r["ok"] is False
    assert r["reason"] == "DEPARTURE_PASSED"
    assert r["message"] == "출발 시각이 이미 지나서 저장할 수 없어요"
    assert "detail" not in r


def test_출발_알람을_안_만들어도_출발_시각이_지났으면_막음():
    input = {**A1, "kinds": ["WAKE"]}
    r = check_alarms(alarm_chain(input), departure_at(input), kst(22, 541))
    assert r["ok"] is False and r["reason"] == "DEPARTURE_PASSED"


def test_알람을_하나도_안_만든_일정은_시각_검사를_하지_않음():
    r = check_alarms(alarm_chain({**A1, "kinds": []}), departure_at(A1), kst(22, 700))
    assert r == {"ok": True, "alarms": []}


def test_순서가_뒤집히면_막음_기상을_출발보다_늦게_고름():
    moved = move_alarm(alarm_chain(A1), "WAKE", kst(22, 600))
    r = check_alarms(moved, kst(22, 540), kst(22, 420))
    assert r["ok"] is False and r["reason"] == "ORDER"
    assert r["message"] == "출발 시각이 준비보다 빠를 수 없어요"
    assert r["detail"] == {"earlier": "PREPARE", "later": "DEPARTURE"}


def test_직접_고른_기상이_과거면_그_알람만_끔():
    r = run({**A1, "manualTimes": {"WAKE": kst(22, 400)}}, kst(22, 420))
    assert r["ok"]
    assert r["alarms"][0]["enabled"] is False
    assert r["alarms"][1]["enabled"] is True
    assert r["alarms"][0]["auto"] == kst(22, 480)  # 직접 고른 표시(auto)는 그대로 실려 나옴

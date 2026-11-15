"""calc/order.test.ts — 알람 시각 직접 고르기 · 순서 확인: 앱 features/alarm/__tests__/adjust.test.ts와 같은 케이스"""

from app.shared.calc.chain import manual_times
from app.shared.calc.order import auto_time, is_manual_time, move_alarm, order_error, reset_alarm
from app.shared.js import from_ms, utc_ms


def kst(day, minutes):
    return from_ms(utc_ms(2025, 4, day) + (minutes - 9 * 60) * 60_000)


def at(minutes):
    return kst(22, minutes)


def alarm(kind, minutes):
    return {"kind": kind, "at": at(minutes)}


def chain():
    """팀 주간회의: 기상 8:00 · 준비 8:30 · 출발 9:00 · 도착 목표 9:50"""
    return [alarm("WAKE", 480), alarm("PREPARE", 510), alarm("DEPARTURE", 540), alarm("ARRIVE", 590)]


def hm(d):
    m = ((d.hour * 60 + d.minute) + 9 * 60) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def times(alarms):
    return [f"{x['kind']} {hm(x['at'])}{'*' if is_manual_time(x) else ''}" for x in alarms]


def test_기상을_앞당기면_준비도_같은_만큼_출발_도착_목표는_그대로_직접_고른_기상에만_표시():
    moved = move_alarm(chain(), "WAKE", at(450))
    assert times(moved) == ["WAKE 07:30*", "PREPARE 08:00", "DEPARTURE 09:00", "ARRIVE 09:50"]
    assert auto_time(moved[0]) == at(480)  # 자동 계산값은 그대로 남김


def test_기상을_늦춰도_같은_간격():
    assert times(move_alarm(chain(), "WAKE", at(495))) == [
        "WAKE 08:15*",
        "PREPARE 08:45",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]


def test_준비_출발은_그_알람만_옮김():
    assert times(move_alarm(chain(), "PREPARE", at(520))) == [
        "WAKE 08:00",
        "PREPARE 08:40*",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]
    assert times(move_alarm(chain(), "DEPARTURE", at(530))) == [
        "WAKE 08:00",
        "PREPARE 08:30",
        "DEPARTURE 08:50*",
        "ARRIVE 09:50",
    ]


def test_자동_계산값과_같은_시각으로_옮기면_직접_고른_것이_아님():
    once = move_alarm(chain(), "PREPARE", at(520))
    assert times(move_alarm(once, "PREPARE", at(510))) == [
        "WAKE 08:00",
        "PREPARE 08:30",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]


def test_직접_고른_준비가_있으면_기상을_옮길_때_같이_옮기고_자동값과의_차이는_그대로():
    prepared = move_alarm(chain(), "PREPARE", at(520))
    moved = move_alarm(prepared, "WAKE", at(450))
    assert times(moved) == ["WAKE 07:30*", "PREPARE 08:10*", "DEPARTURE 09:00", "ARRIVE 09:50"]
    assert auto_time(moved[1]) == at(480)


def test_여러_번_나눠_옮겨도_합친_간격만큼():
    alarms = chain()
    for minute in (481, 482, 483, 470, 450):
        alarms = move_alarm(alarms, "WAKE", at(minute))
    assert times(alarms) == ["WAKE 07:30*", "PREPARE 08:00", "DEPARTURE 09:00", "ARRIVE 09:50"]


def test_자동으로_되돌리면_기상과_따라온_준비가_함께_원래대로():
    moved = move_alarm(chain(), "WAKE", at(450))
    assert times(reset_alarm(moved, "WAKE")) == [
        "WAKE 08:00",
        "PREPARE 08:30",
        "DEPARTURE 09:00",
        "ARRIVE 09:50",
    ]
    assert reset_alarm(chain(), "WAKE") == chain()


def test_없는_종류를_옮기려_하면_그대로():
    no_wake = [x for x in chain() if x["kind"] != "WAKE"]
    assert move_alarm(no_wake, "WAKE", at(450)) == no_wake


def test_직접_고른_시각만_모음_따라온_준비는_자동이라_빠짐():
    assert manual_times(move_alarm(chain(), "WAKE", at(450))) == {"WAKE": at(450)}
    assert manual_times(move_alarm(move_alarm(chain(), "WAKE", at(450)), "PREPARE", at(490))) == {
        "WAKE": at(450),
        "PREPARE": at(490),
    }
    assert manual_times(chain()) == {}


def test_순서_확인_맞으면_null_같은_시각은_허용_만든_알람만_비교():
    assert order_error(chain()) is None
    assert order_error(move_alarm(chain(), "PREPARE", at(540))) is None  # 준비 = 출발
    assert (
        order_error([alarm("PREPARE", 510), alarm("DEPARTURE", 540), alarm("ARRIVE", 590)]) is None
    )  # 기상 알람 없는 일정
    assert order_error([]) is None  # 알람 없는 일정


def test_기상을_크게_늦추면_따라온_준비가_출발을_넘어_순서가_뒤집힘():
    err = order_error(move_alarm(chain(), "WAKE", at(600)))  # 기상 10:00 · 준비 10:30 · 출발 9:00
    assert err["message"] == "출발 시각이 준비보다 빠를 수 없어요"
    assert [err["earlier"], err["later"]] == ["PREPARE", "DEPARTURE"]


def test_뒤집히면_어느_둘이_어긋났는지():
    assert (
        order_error(move_alarm(chain(), "PREPARE", at(470)))["message"]
        == "준비 시각이 기상보다 빠를 수 없어요"
    )
    assert (
        order_error(move_alarm(chain(), "DEPARTURE", at(600)))["message"]
        == "출발 시각이 도착 목표보다 늦을 수 없어요"
    )

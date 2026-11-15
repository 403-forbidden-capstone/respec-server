"""calc/depart.test.ts — 출발 여유: 상세설계서 7장 7.5.2 표 그대로 (약속 10:00 · 이동 50 · 여유 10 · 기상~출발 60)"""

from app.shared import calc
from app.shared.calc.depart import DEPART_COMFORT_MIN_SLACK, depart_now, depart_slack, depart_status
from app.shared.js import from_ms, ms, utc_ms


def kst(minutes):
    """2025년 5월 22일 한국 시각 minutes분 (mocks/data.ts kst와 같음)"""
    return from_ms(utc_ms(2025, 4, 22) + (minutes - 9 * 60) * 60_000)


def hm(d):
    m = ((d.hour * 60 + d.minute) + 9 * 60) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


APPOINTMENT = kst(600)  # 10:00
BASE = {"appointmentAt": APPOINTMENT, "travelMinutes": 50}
PREPARE_MINUTES = 60  # 기상 ~ 출발 (여유 10분은 이동 앞에 따로)


def after_wake(wake_minutes):
    """기상 직후(남은 준비 = 준비 전체)의 여유"""
    return depart_slack({**BASE, "now": kst(wake_minutes), "remainingMinutes": PREPARE_MINUTES})


def test_7_5_2_표_08시_기상_여유_10분_여유():
    assert after_wake(480) == {"slackMinutes": 10, "status": "COMFORT"}


def test_7_5_2_표_08시05_기상_여유_5분_빠듯():
    assert after_wake(485) == {"slackMinutes": 5, "status": "TIGHT"}


def test_7_5_2_표_08시25_기상_여유_마이너스_15분_지각_위험():
    assert after_wake(505) == {"slackMinutes": -15, "status": "LATE_RISK"}


def test_출발_한계는_약속_빼기_이동_약속_앞_여유_10분을_빼지_않음_09시10에_나가면_slack_0_빠듯():
    # 알람 체인의 출발은 09:00 (여유 10분을 먼저 뺌) — slack은 그 여유만큼 남은 것으로 읽음
    assert depart_slack({**BASE, "now": kst(540), "remainingMinutes": 0}) == {
        "slackMinutes": 10,
        "status": "COMFORT",
    }
    assert depart_slack({**BASE, "now": kst(550), "remainingMinutes": 0}) == {
        "slackMinutes": 0,
        "status": "TIGHT",
    }
    assert depart_slack({**BASE, "now": kst(551), "remainingMinutes": 0}) == {
        "slackMinutes": -1,
        "status": "LATE_RISK",
    }


def test_경계_10분은_여유_9분은_빠듯_0분은_빠듯_마이너스_1분은_지각_위험():
    assert DEPART_COMFORT_MIN_SLACK == 10
    assert [depart_status(s) for s in (10, 9, 0, -1)] == ["COMFORT", "TIGHT", "TIGHT", "LATE_RISK"]


def test_남은_준비가_줄수록_같은_시각의_여유가_늘어남_08시25에_일어나도_준비를_35분으로_줄이면_여유_10분():
    assert depart_slack({**BASE, "now": kst(505), "remainingMinutes": 35}) == {
        "slackMinutes": 10,
        "status": "COMFORT",
    }


def test_지금_나가면_09시_출발_09시50_도착_여유_10분():
    r = depart_now({**BASE, "now": kst(540)})
    assert hm(r["arriveAt"]) == "09:50"
    assert [r["slackMinutes"], r["status"]] == [10, "COMFORT"]


def test_지금_나가면_늦게_나가면_도착이_약속_뒤_여유가_음수():
    r = depart_now({**BASE, "now": kst(570)})  # 09:30 출발 → 10:20 도착
    assert hm(r["arriveAt"]) == "10:20"
    assert [r["slackMinutes"], r["status"]] == [-20, "LATE_RISK"]


def test_depart_now는_남은_준비_0인_depart_slack과_같은_값_도착_약속_빼기_여유():
    for now in (kst(500), kst(540), kst(550), kst(560)):
        a = depart_now({**BASE, "now": now})
        b = depart_slack({**BASE, "now": now, "remainingMinutes": 0})
        assert [a["slackMinutes"], a["status"]] == [b["slackMinutes"], b["status"]]
        assert a["slackMinutes"] == round((ms(APPOINTMENT) - ms(a["arriveAt"])) / 60_000)


def test_분_단위는_기존_calc처럼_반올림_초가_있는_지금도_마이너스_0이_아닌_0():
    now = from_ms(ms(kst(550)) + 20_000)  # 09:10:20
    r = depart_slack({**BASE, "now": now, "remainingMinutes": 0})
    # JS는 Object.is(x, 0) — 파이썬 정수에는 -0이 없음. 부호 있는 0 float도 아님을 확인
    assert r["slackMinutes"] == 0 and isinstance(r["slackMinutes"], int)
    assert r["status"] == "TIGHT"


def test_calc_묶음에서도_불림():
    assert calc.depart_slack is depart_slack
    assert calc.depart_now is depart_now

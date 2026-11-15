"""calc/wake.test.ts — "그날 첫 일정" 판정: 약속 시각이 앞선 약속의 켜진 기상만 셈 (알람이 울리는 시각이 아님)"""

from datetime import UTC, datetime, timedelta

from app.shared.calc.wake import has_earlier_wake, is_evening_wake
from app.shared.js import from_ms, ms, parse_iso, utc_ms


def kst(minutes):
    """2025년 5월 22일 한국 시각 minutes분"""
    return from_ms(utc_ms(2025, 4, 22) + (minutes - 9 * 60) * 60_000)


def wake_of(appointment_minutes, **over):
    return {"appointmentAt": kst(appointment_minutes), **over}


def test_10시_약속_기상_8시가_있는_날_8시30_새_약속_앞선_약속이_없으니_기상_있음_그날_첫_일정():
    assert has_earlier_wake(kst(510), [wake_of(600)]) is False


def test_8시_약속이_이미_있는_날_10시_새_약속_앞선_약속의_기상이_있으니_기상_없음():
    assert has_earlier_wake(kst(600), [wake_of(480)]) is True


def test_같은_시각_약속_둘_서로_앞선_게_아니라_둘_다_기상을_가짐():
    assert has_earlier_wake(kst(600), [wake_of(600)]) is False
    # 한 분이라도 앞서면 앞선 것
    assert has_earlier_wake(kst(600), [wake_of(599)]) is True
    assert has_earlier_wake(kst(600), [wake_of(601)]) is False


def test_앞선_약속이_둘_이상이거나_뒤의_약속이_섞여_있어도_앞선_것이_하나라도_있으면_기상_없음():
    assert has_earlier_wake(kst(720), [wake_of(900), wake_of(480), wake_of(600)]) is True
    assert has_earlier_wake(kst(720), [wake_of(900), wake_of(1080)]) is False


def test_꺼_둔_기상은_안_셈_isEnabled를_안_주면_켜짐():
    assert has_earlier_wake(kst(600), [wake_of(480, isEnabled=False)]) is False
    assert has_earlier_wake(kst(600), [wake_of(480, isEnabled=False), wake_of(540, isEnabled=True)]) is True
    assert has_earlier_wake(kst(600), [wake_of(480)]) is True
    assert (
        has_earlier_wake(kst(600), [wake_of(480, isEnabled=None)]) is True
    )  # JS null · undefined는 꺼진 게 아님 (!== false)


def test_이_약속_자신의_기상은_안_셈_기존_약속을_다시_계산할_때():
    own = wake_of(480, appointmentId="ap1")
    assert has_earlier_wake(kst(600), [own], "ap1") is False
    assert has_earlier_wake(kst(600), [own], "ap2") is True
    assert has_earlier_wake(kst(600), [own]) is True  # 새 약속이면 id가 없음
    # id가 없는 항목이 id 없는 호출과 같은 것으로 취급돼 빠지면 안 됨
    assert has_earlier_wake(kst(600), [wake_of(480)], None) is True


def test_같은_날_다른_약속이_없으면_기상_있음():
    assert has_earlier_wake(kst(600), []) is False


# ---- 저녁 기상 (#320) ----


def test_기상_시각이_11시59면_저녁_아님_12시_정각부터_저녁_내_시간대_기준():
    assert is_evening_wake(kst(11 * 60 + 59), "Asia/Seoul") is False
    assert is_evening_wake(kst(12 * 60), "Asia/Seoul") is True
    assert is_evening_wake(kst(23 * 60 + 30), "Asia/Seoul") is True
    assert is_evening_wake(kst(0), "Asia/Seoul") is False


def test_시간대_서울_12시_UTC_03시_경계_시간대가_없거나_틀리면_서울():
    assert is_evening_wake(parse_iso("2025-05-22T02:59:00Z"), "Asia/Seoul") is False
    assert is_evening_wake(parse_iso("2025-05-22T03:00:00Z"), "Asia/Seoul") is True
    assert is_evening_wake(parse_iso("2025-05-22T03:00:00Z")) is True
    assert is_evening_wake(parse_iso("2025-05-22T03:00:00Z"), "No/Such_Zone") is True
    # 같은 순간도 다른 시간대면 낮이 다름: UTC 03:00은 뉴욕에서 전날 밤 23:00 → 저녁, UTC 15:00은 뉴욕 11:00 → 아님
    assert is_evening_wake(parse_iso("2025-05-22T03:00:00Z"), "America/New_York") is True
    assert is_evening_wake(parse_iso("2025-05-22T15:00:00Z"), "America/New_York") is False


def test_약속_날짜보다_전날인_기상_새벽_약속의_전날_밤_기상은_저녁_기상이_아님():
    appointment_at = kst(60)  # 1:00
    wake_at = from_ms(ms(kst(60)) - 110 * 60_000)  # 전날 23:10
    assert is_evening_wake(wake_at, "Asia/Seoul") is True
    assert is_evening_wake(wake_at, "Asia/Seoul", appointment_at) is False


def test_UTC_aware가_아닌_시각은_받지_않음():
    # 규칙: naive datetime 금지 — 조용히 UTC로 읽지 않고 오류
    import pytest

    with pytest.raises(ValueError):
        has_earlier_wake(datetime(2025, 5, 22), [wake_of(480)])
    assert (
        is_evening_wake(datetime(2025, 5, 22, 12, 0, tzinfo=UTC) + timedelta(hours=3), "Asia/Seoul") is False
    )  # 15:00 UTC = 한국 0시

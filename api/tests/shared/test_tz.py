"""tz.test.ts — 시간대 도우미: 사용자 시간대로 "그날"과 화면 시각 글자를 계산"""

import pytest

from app.shared.js import RangeError, parse_iso, to_iso
from app.shared.tz import (
    format_ko_duration,
    format_ko_spoken_time,
    format_ko_time,
    local_date_key,
    local_day_range,
    local_minute_of_day,
    offset_minutes,
    safe_time_zone,
    zoned_time,
)


def d(text):
    return parse_iso(text)


def test_서울은_UTC보다_540분_앞섬():
    assert offset_minutes(d("2026-10-20T00:00:00Z"), "Asia/Seoul") == 540


def test_시각_글자_오전_10시_오후_6시05_낮_12시_자정():
    assert format_ko_time(d("2026-10-20T01:00:00Z"), "Asia/Seoul") == "오전 10:00"
    assert format_ko_time(d("2026-10-20T09:05:00Z"), "Asia/Seoul") == "오후 6:05"
    assert format_ko_time(d("2026-10-20T03:00:00Z"), "Asia/Seoul") == "오후 12:00"
    assert format_ko_time(d("2026-10-19T15:00:00Z"), "Asia/Seoul") == "오전 12:00"


def test_말로_읽는_시각_오전_10시_오후_2시_30분_낮_12시_자정_오전_오후를_글자로():
    assert format_ko_spoken_time(d("2026-10-20T01:00:00Z"), "Asia/Seoul") == "오전 10시"
    assert format_ko_spoken_time(d("2026-10-19T23:40:00Z"), "Asia/Seoul") == "오전 8시 40분"
    assert format_ko_spoken_time(d("2026-10-20T05:30:00Z"), "Asia/Seoul") == "오후 2시 30분"
    assert format_ko_spoken_time(d("2026-10-20T03:00:00Z"), "Asia/Seoul") == "오후 12시"
    assert format_ko_spoken_time(d("2026-10-19T15:20:00Z"), "Asia/Seoul") == "오전 12시 20분"


def test_시간_길이_30분_1시간_1시간_10분_0분():
    assert [format_ko_duration(m) for m in (0, 5, 30, 59, 60, 70, 120, 125.4, -5)] == [
        "0분",
        "5분",
        "30분",
        "59분",
        "1시간",
        "1시간 10분",
        "2시간",
        "2시간 5분",
        "0분",
    ]


def test_시간_길이_절반_반올림은_Math_round_규칙():
    # 0.5 → 1분(Python round는 0), 59.5 → 60 → 1시간, 124.5 → 125 → 2시간 5분, -0.5 → 0 → 0분
    assert [format_ko_duration(m) for m in (0.5, 59.5, 124.5, -0.5, 1.5, 2.5)] == [
        "1분",
        "1시간",
        "2시간 5분",
        "0분",
        "2분",
        "3분",
    ]


def test_그날의_0시_다음_날_0시_서울():
    r = local_day_range(d("2026-10-20T01:00:00Z"), "Asia/Seoul")  # 한국 10/20 10:00
    assert to_iso(r["start"]) == "2026-10-19T15:00:00.000Z"
    assert to_iso(r["end"]) == "2026-10-20T15:00:00.000Z"
    # 한국 시각 새벽은 UTC로는 전날이지만 한국 기준 그날
    assert (
        to_iso(local_day_range(d("2026-10-19T16:00:00Z"), "Asia/Seoul")["start"])
        == "2026-10-19T15:00:00.000Z"
    )


def test_일광절약시간_시간대도_하루_길이가_맞음_뉴욕_2026_11_01은_25시간():
    r = local_day_range(d("2026-11-01T12:00:00Z"), "America/New_York")
    assert to_iso(r["start"]) == "2026-11-01T04:00:00.000Z"
    assert to_iso(r["end"]) == "2026-11-02T05:00:00.000Z"


def test_모르는_시간대는_서울로():
    assert safe_time_zone("Mars/Olympus") == "Asia/Seoul"
    assert safe_time_zone(None) == "Asia/Seoul"
    assert safe_time_zone("America/New_York") == "America/New_York"


def test_safeTimeZone_빈_글자_대소문자_이상한_값():
    assert safe_time_zone("") == "Asia/Seoul"  # JS `'' || 기본값`
    assert (
        safe_time_zone("asia/seoul") == "asia/seoul"
    )  # Intl은 대소문자를 가리지 않고 받음 — 받은 글자 그대로 돌려줌
    assert offset_minutes(d("2026-10-20T00:00:00Z"), "asia/seoul") == 540
    assert safe_time_zone("../etc/passwd") == "Asia/Seoul"
    assert safe_time_zone("UTC") == "UTC"


def test_내_시간대의_날짜_글자와_0시부터_분_새벽_1시는_UTC로는_전날이지만_한국_날짜():
    dawn = d("2026-11-10T16:00:00Z")  # 한국 11/11 01:00
    assert local_date_key(dawn, "Asia/Seoul") == "2026-11-11"
    assert local_minute_of_day(dawn, "Asia/Seoul") == 60
    assert local_date_key(dawn, "America/Los_Angeles") == "2026-11-10"  # 같은 순간이 LA에서는 11/10 08:00
    assert local_minute_of_day(dawn, "America/Los_Angeles") == 480
    assert local_date_key(d("2026-10-19T14:59:00Z"), "Asia/Seoul") == "2026-10-19"
    assert local_date_key(d("2026-10-19T15:00:00Z"), "Asia/Seoul") == "2026-10-20"
    assert local_minute_of_day(d("2026-10-19T15:00:00Z"), "Asia/Seoul") == 0
    assert local_minute_of_day(d("2026-10-20T14:59:00Z"), "Asia/Seoul") == 23 * 60 + 59


def test_zonedTime_날짜_0시부터_분_시각_서울():
    assert to_iso(zoned_time("2026-11-11", 450, "Asia/Seoul")) == "2026-11-10T22:30:00.000Z"  # 오전 7:30
    assert to_iso(zoned_time("2026-11-11", 60, "Asia/Seoul")) == "2026-11-10T16:00:00.000Z"  # 새벽 1시
    assert (
        to_iso(zoned_time("2026-11-11", 0, "Asia/Seoul")) == "2026-11-10T15:00:00.000Z"
    )  # 0시 = localDayRange의 시작
    assert (
        to_iso(zoned_time("2026-11-11", 1470, "Asia/Seoul")) == "2026-11-11T15:30:00.000Z"
    )  # 24:30 = 다음 날 00:30 (자정을 넘는 루틴 단계)
    assert to_iso(zoned_time("2026-02-28", 1440, "Asia/Seoul")) == to_iso(
        zoned_time("2026-03-01", 0, "Asia/Seoul")
    )  # 달 넘김
    with pytest.raises(RangeError):
        zoned_time("2026-11-1", 0, "Asia/Seoul")
    with pytest.raises(RangeError):
        zoned_time("내일", 0, "Asia/Seoul")
    with pytest.raises(RangeError):
        zoned_time("2026-11-11\n", 0, "Asia/Seoul")  # JS $는 끝의 줄바꿈을 안 받음 (파이썬 $는 받음)
    with pytest.raises(RangeError):
        zoned_time("２０２６-11-11", 0, "Asia/Seoul")  # 전각 숫자는 JS \d가 아님


def test_zonedTime_일광절약시간_시간대도_같은_벽시계_시각_뉴욕():
    ny = "America/New_York"
    assert to_iso(zoned_time("2026-07-01", 9 * 60, ny)) == "2026-07-01T13:00:00.000Z"  # EDT -4
    assert to_iso(zoned_time("2026-12-01", 9 * 60, ny)) == "2026-12-01T14:00:00.000Z"  # EST -5
    assert (
        to_iso(zoned_time("2026-11-01", 12 * 60, ny)) == "2026-11-01T17:00:00.000Z"
    )  # 시계가 되돌아간 날 낮 (EST)
    assert (
        to_iso(zoned_time("2026-11-01", 90, ny)) == "2026-11-01T05:30:00.000Z"
    )  # 01:30은 두 번 — 먼저 오는 쪽(EDT)
    assert (
        to_iso(zoned_time("2026-03-08", 150, ny)) == "2026-03-08T07:30:00.000Z"
    )  # 02:30은 없는 시각 — 03:30 EDT로 밂
    assert to_iso(zoned_time("2026-03-08", 3 * 60 + 30, ny)) == "2026-03-08T07:30:00.000Z"


def test_zonedTime은_localDateKey_localMinuteOfDay의_반대_여러_시간대_하루_모든_분_봄에_건너뛰는_구간만_뒤로_밀림():
    # 봄에 시계가 건너뛰는 날: 시간대 → [날짜, 건너뛰기 시작 분, 건너뛰는 길이(분)]
    gaps = {
        "America/New_York": ("2026-03-08", 120, 60),
        "Europe/London": ("2026-03-29", 60, 60),
        "Australia/Lord_Howe": ("2026-10-04", 120, 30),
    }
    for tz in ("Asia/Seoul", "America/New_York", "Europe/London", "Australia/Lord_Howe"):
        for date in ("2026-03-08", "2026-03-29", "2026-10-04", "2026-10-25", "2026-11-01", "2026-11-11"):
            gap = gaps.get(tz) if gaps.get(tz, (None,))[0] == date else None
            for minute in range(0, 1440, 5):
                at = zoned_time(date, minute, tz)
                pushed = (
                    gap[2] if gap and gap[1] <= minute < gap[1] + gap[2] else 0
                )  # 건너뛰는 구간이면 건너뛰는 길이만큼 뒤
                assert local_date_key(at, tz) == date, f"{tz} {date} {minute}"
                assert local_minute_of_day(at, tz) == minute + pushed, f"{tz} {date} {minute}"
    for tz in ("Asia/Seoul", "America/Los_Angeles"):
        at = d("2026-11-10T16:00:00Z")
        assert to_iso(zoned_time(local_date_key(at, tz), local_minute_of_day(at, tz), tz)) == to_iso(at)

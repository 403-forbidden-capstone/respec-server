"""app/shared/js.py — JS와 달라지기 쉬운 곳 (반올림 · Date · 오프셋 · Number 변환 · 음수 나머지)"""

import math
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.shared.js import (
    from_ms,
    js_ceil,
    js_floor,
    js_json,
    js_mod,
    js_number,
    js_round,
    ms,
    pad_start,
    parse_iso,
    to_iso,
    utc_ms,
    utc_weekday,
    utc_ymd,
)


def test_js_round_절반은_양의_무한대_쪽():
    # Math.round: 0.5 → 1, 1.5 → 2, 2.5 → 3 (Python round는 0, 2, 2)
    assert [js_round(x) for x in (0.5, 1.5, 2.5, 3.5)] == [1, 2, 3, 4]
    # 음수의 .5는 0 쪽(위쪽)으로: -0.5 → -0, -1.5 → -1, -2.5 → -2
    assert [js_round(x) for x in (-0.5, -1.5, -2.5, -3.5)] == [0, -1, -2, -3]
    assert [js_round(x) for x in (0.4, 0.6, -0.4, -0.6, -1.4, -1.6)] == [0, 1, 0, -1, -1, -2]
    assert [js_round(x) for x in (49.5, -49.5, 1e15 + 0.5)] == [50, -49, 1_000_000_000_000_001]


def test_js_round_경계값():
    assert js_round(0.49999999999999994) == 0  # x + 0.5로 풀면 1이 되는 값 (JS는 0)
    assert js_round(-0.49999999999999994) == 0
    assert js_round(4503599627370497.0) == 4503599627370497  # 2^52 + 1 이상은 이미 정수
    assert js_round(7) == 7 and isinstance(js_round(7), int)
    assert math.isnan(js_round(math.nan))
    assert js_round(math.inf) == math.inf and js_round(-math.inf) == -math.inf


def test_js_floor_ceil():
    assert [js_floor(x) for x in (1.9, -1.1, -0.0, 3.0)] == [1, -2, 0, 3]
    assert [js_ceil(x) for x in (1.1, -1.9, -0.5, 3.0)] == [2, -1, 0, 3]
    assert js_floor(math.inf) == math.inf and js_ceil(-math.inf) == -math.inf
    assert math.isnan(js_floor(math.nan))


def test_js_mod_부호는_나눠지는_수를_따름():
    assert js_mod(-1, 7) == -1  # Python -1 % 7 == 6
    assert js_mod(8, 7) == 1
    assert js_mod(-8, 7) == -1
    assert js_mod(7, -3) == 1
    assert js_mod(5.5, 2) == 1.5
    assert js_mod(-5.5, 2) == -1.5
    assert math.isnan(js_mod(1, 0)) and math.isnan(js_mod(math.inf, 2))
    assert js_mod(3, math.inf) == 3


def test_js_number():
    assert js_number("60") == 60 and isinstance(js_number("60"), int)
    assert js_number("60.5") == 60.5
    assert js_number("  12  ") == 12
    assert js_number("") == 0 and js_number("   ") == 0
    assert js_number(None) == 0 and js_number(True) == 1 and js_number(False) == 0
    assert (
        js_number("1e3") == 1000
        and js_number(".5") == 0.5
        and js_number("5.") == 5
        and js_number("-0.25") == -0.25
    )
    assert js_number("0x10") == 16 and js_number("0b11") == 3 and js_number("0o7") == 7
    assert js_number("Infinity") == math.inf and js_number("-Infinity") == -math.inf
    for bad in (
        "abc",
        "1_000",
        "nan",
        "inf",
        "1 2",
        "--1",
        "1e",
        "0x",
        "٣",
    ):  # 파이썬 float()은 일부를 받지만 JS Number()는 NaN
        assert math.isnan(js_number(bad)), bad
    assert js_number(3.5) == 3.5 and math.isnan(js_number(math.nan))
    assert math.isnan(js_number([1]))


def test_ms_from_ms_왕복():
    d = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
    assert ms(d) == utc_ms(2026, 9, 20, 1) == int(d.timestamp() * 1000)
    assert from_ms(ms(d)) == d
    assert ms(from_ms(0)) == 0 and from_ms(0) == datetime(1970, 1, 1, tzinfo=UTC)
    assert ms(from_ms(-1)) == -1
    # 소수 밀리초는 0 쪽으로 버림 (new Date(1.9) → 1, new Date(-1.9) → -1)
    assert ms(from_ms(1.9)) == 1 and ms(from_ms(-1.9)) == -1
    with pytest.raises(ValueError):
        from_ms(math.nan)
    with pytest.raises(ValueError):
        ms(datetime(2026, 1, 1))  # naive 금지


def test_ms는_다른_오프셋도_같은_순간():
    kst = timezone(timedelta(hours=9))
    assert ms(datetime(2026, 10, 20, 10, 0, tzinfo=kst)) == ms(datetime(2026, 10, 20, 1, 0, tzinfo=UTC))


def test_to_iso_밀리초_3자리_Z():
    assert to_iso(datetime(2026, 10, 20, 1, 0, tzinfo=UTC)) == "2026-10-20T01:00:00.000Z"
    assert to_iso(datetime(2026, 10, 20, 1, 2, 3, 456000, tzinfo=UTC)) == "2026-10-20T01:02:03.456Z"
    assert (
        to_iso(datetime(2026, 10, 20, 1, 2, 3, 999999, tzinfo=UTC)) == "2026-10-20T01:02:03.999Z"
    )  # 밀리초 아래는 버림
    assert to_iso(datetime(2026, 10, 20, 1, 2, 3, 7000, tzinfo=UTC)) == "2026-10-20T01:02:03.007Z"
    # 다른 오프셋이어도 UTC로
    assert (
        to_iso(datetime(2026, 10, 20, 10, 0, tzinfo=timezone(timedelta(hours=9))))
        == "2026-10-20T01:00:00.000Z"
    )
    # 세 자리 미만 연도도 4자리로 (리눅스 strftime은 0을 안 채움)
    assert to_iso(datetime(50, 1, 1, tzinfo=UTC)) == "0050-01-01T00:00:00.000Z"
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 1, 1))


def test_parse_iso_오프셋_Z_밀리초():
    assert to_iso(parse_iso("2026-10-20T10:00:00+09:00")) == "2026-10-20T01:00:00.000Z"
    assert to_iso(parse_iso("2026-10-20T10:00:00Z")) == "2026-10-20T10:00:00.000Z"
    assert to_iso(parse_iso("2026-10-20T10:00:00.123Z")) == "2026-10-20T10:00:00.123Z"
    assert to_iso(parse_iso("2026-10-20T10:00:00.123+09:00")) == "2026-10-20T01:00:00.123Z"
    assert to_iso(parse_iso("2026-10-20T10:00:00-05:30")) == "2026-10-20T15:30:00.000Z"
    assert to_iso(parse_iso("2026-10-20T10:00Z")) == "2026-10-20T10:00:00.000Z"  # 초 없음
    assert to_iso(parse_iso("2026-10-20T10:00:00.1Z")) == "2026-10-20T10:00:00.100Z"
    assert (
        to_iso(parse_iso("2026-10-20T10:00:00.123456789Z")) == "2026-10-20T10:00:00.123Z"
    )  # 밀리초 아래 버림
    assert parse_iso("2026-10-20T10:00:00+09:00").tzinfo is not None


def test_parse_iso_오프셋이_없으면_UTC로():
    # JS도 날짜만 있는 글자는 UTC 0시. 날짜 + 시각에 오프셋이 없으면 JS는 지역 시각이지만 서버는 UTC 기준
    assert to_iso(parse_iso("2026-10-20")) == "2026-10-20T00:00:00.000Z"
    assert to_iso(parse_iso("2026-10-20T10:00:00")) == "2026-10-20T10:00:00.000Z"


def test_parse_iso_24시와_공백_거절():
    assert (
        to_iso(parse_iso("2026-10-20T24:00:00Z")) == "2026-10-21T00:00:00.000Z"
    )  # JS는 24:00을 다음 날 0시로 받음
    assert to_iso(parse_iso("2026-12-31T24:00:00+09:00")) == "2026-12-31T15:00:00.000Z"
    assert to_iso(parse_iso("  2026-10-20T10:00:00Z  ")) == "2026-10-20T10:00:00.000Z"
    for bad in ("", "내일", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z"):
        with pytest.raises(ValueError):
            parse_iso(bad)


def test_utc_ms는_Date_UTC와_같음():
    assert utc_ms(2025, 4, 22) == 1_747_872_000_000  # Date.UTC(2025, 4, 22)
    assert utc_ms(1970, 0, 1) == 0
    assert utc_ms(2026, 12, 1) == utc_ms(2027, 0, 1)  # 13월 → 다음 해 1월
    assert utc_ms(2026, -1, 1) == utc_ms(2025, 11, 1)
    assert utc_ms(2026, 1, 29) == utc_ms(2026, 2, 1)  # 2월 29일(없음) → 3월 1일
    assert utc_ms(2026, 0, 1, 0, 1470) == utc_ms(2026, 0, 2, 0, 30)  # 분 넘침
    assert utc_ms(2026, 0, 0) == utc_ms(2025, 11, 31)  # 0일 → 전달 말일
    assert utc_ms(50, 0, 1) == utc_ms(1950, 0, 1)  # 0~99년은 1900년대 (JS 규칙)
    assert utc_ms(2026, 0, 1, 0, 0, 0, 1.9) == utc_ms(2026, 0, 1) + 1  # 소수는 0 쪽으로 버림
    assert utc_ms(1969, 11, 31, 23, 59, 59, 999) == -1


def test_utc_ymd_utc_weekday():
    assert utc_ymd(0) == (1970, 1, 1)
    assert utc_ymd(-1) == (1969, 12, 31)
    assert utc_ymd(utc_ms(2026, 1, 28)) == (2026, 2, 28)
    assert utc_ymd(utc_ms(2024, 1, 29)) == (2024, 2, 29)
    assert utc_ymd(utc_ms(2026, 11, 31)) == (2026, 12, 31)
    assert utc_weekday(0) == 4  # 1970-01-01은 목요일
    assert utc_weekday(-1) == 3  # 수요일
    assert utc_weekday(utc_ms(2026, 10, 2)) == 1  # 2026-11-02 월요일 → getUTCDay 1
    assert utc_weekday(utc_ms(2026, 10, 8)) == 0  # 2026-11-08 일요일 → 0


def test_pad_start():
    assert pad_start(5, 2) == "05" and pad_start(2026, 4) == "2026" and pad_start(12345, 4) == "12345"
    assert pad_start(7, 4) == "0007"
    assert pad_start(-5, 4) == "00-5"  # JS String(-5).padStart(4, '0')


def test_js_json_유한하지_않은_숫자는_null():
    """JSON.stringify: NaN · ±Infinity → null (객체 · 배열 안도), 한글 그대로, 공백 없음 (PR #9 리뷰 고칠 것 1)"""
    assert (
        js_json({"a": math.inf, "b": [1.5, -math.inf, math.nan], "c": "한글", "d": None})
        == '{"a":null,"b":[1.5,null,null],"c":"한글","d":null}'
    )

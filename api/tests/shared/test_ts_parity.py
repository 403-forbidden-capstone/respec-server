"""TS 대조(차분 시험)에서 나온 JS ↔ Python 차이 회귀 시험 — 입력은 대조 보고의 최소 재현, 기대값은 Node에서 나온 값"""

import math
from datetime import UTC, datetime

import pytest

from app.core.read import Reader
from app.kakao_parse import to_number
from app.shared import edit, plan_core, roll
from app.shared.calc.suggest import detect_overrun
from app.shared.js import js_json, js_max, js_min, js_number_to_string
from app.shared.tz import safe_time_zone


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0x10", 16), ("0b11", 3), ("0o17", 15), ("", 0), ("  7 ", 7), ("1e2", 100)],
)
def test_Number_글자_복사본이_JS와_같음(text, expected):
    """Number('0x10') = 16 · Number('') = 0"""
    assert to_number(text) == expected
    assert plan_core._js_number(text) == expected
    assert roll._js_number(text) == expected
    assert edit._number(text) == expected


@pytest.mark.parametrize("text", ["inf", "1_0", "nan", "0x", "1e", "١٢٣", "4 5"])
def test_Number_글자_JS가_NaN인_것(text):
    """Number('inf') · Number('1_0') = NaN"""
    assert to_number(text) is None  # Edge num: 유한한 숫자만
    assert math.isnan(plan_core._js_number(text))
    assert math.isnan(roll._js_number(text))
    assert math.isnan(edit._number(text))


def test_Math_max_NaN은_NaN():
    """Math.max(0, NaN) = NaN, Math.min(NaN, 1) = NaN (Python max는 순서에 따라 NaN을 버림)"""
    assert math.isnan(js_max(0, math.nan))
    assert math.isnan(js_max(math.nan, 0))
    assert math.isnan(js_min(1, math.nan))
    assert js_max(1, 2) == 2
    assert js_min(1, 2) == 1


def test_inferBeforePrepare_준비시간이_숫자가_아니면_undefined():
    """inferBeforePrepare(existing, 5, '0x') — Number('0x') = NaN → 계산이 NaN → undefined(None)"""
    at = datetime(1970, 1, 3, 13, 53, 49, 13000, tzinfo=UTC)
    dep = datetime(1970, 1, 3, 15, 13, 49, 13000, tzinfo=UTC)

    def alarm(kind, when):
        return {
            "alarm_id": kind,
            "alarm_type": kind,
            "scheduled_at": when.isoformat().replace("+00:00", "Z"),
            "time_source": "AUTO",
        }

    existing = [alarm("WAKE", at), alarm("PREPARE", at), alarm("DEPARTURE", dep)]
    assert plan_core.infer_before_prepare(existing, 5, "0x") is None
    assert plan_core.infer_before_prepare(existing, 5, "80") == 5  # 숫자 글자면 그대로 읽음


def test_detectOverrun_빈_잔차와_window_0이면_null():
    """detectOverrun([], {window:0,minMean:3}) = null (0/0 = NaN은 어떤 비교도 false)"""
    assert detect_overrun([], {"window": 0, "minMean": 3}) is None


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1e21, "1e+21"),
        (1e-7, "1e-7"),
        (123456789012345678901, "123456789012345680000"),
        (5.0, "5"),
        (-0.0, "0"),
        (0.000001, "0.000001"),
        (1.5e300, "1.5e+300"),
        (-2.5e-9, "-2.5e-9"),
        (100, "100"),
        (math.nan, "NaN"),
        (-math.inf, "-Infinity"),
    ],
)
def test_숫자_글자가_String_n과_같음(n, expected):
    """String(1e21) = '1e+21' · String(1e-7) = '1e-7' · String(-0) = '0'"""
    assert js_number_to_string(n) == expected


def test_JSON_글자_수_숫자는_JS_글자_NaN은_null():
    """JSON.stringify({a:1e21,b:1e-7,c:NaN,d:5}) = '{"a":1e+21,"b":1e-7,"c":null,"d":5}'"""
    assert (
        js_json({"a": 1e21, "b": 1e-7, "c": math.nan, "d": 5.0, "e": -0.0})
        == '{"a":1e+21,"b":1e-7,"c":null,"d":5,"e":0}'
    )


def _json_issues(value, max_chars):
    issues: list[dict[str, str]] = []
    Reader({"a": value}, "", issues).json("a", max_chars)
    return issues


def test_Reader_json_글자_수는_JS_기준():
    """json 칸 maxChars — 1e21은 '1e+21'(5자)이라 5자 제한 안, 파이썬 '1e+21'과 같고 1e-7은 '1e-7'(4자, 파이썬 '1e-07'은 5자)"""
    assert _json_issues(1e21, 5) == []
    assert _json_issues(1e21, 4) != []
    assert _json_issues(1e-7, 4) == []


def test_Reader_json_NaN_Infinity는_null_네_글자():
    """JSON.stringify([Infinity]) = '[null]'(6자) — 파이썬 json.dumps는 '[Infinity]'(10자)"""
    assert _json_issues([math.inf], 6) == []
    assert _json_issues([math.inf], 5) != []
    assert _json_issues([math.nan], 6) == []


@pytest.mark.parametrize("name", ["Asia/Seoul ", " Asia/Seoul", "Asia/Seoul\n", "Asia/Seoul\u00a0"])
def test_safeTimeZone_앞뒤_공백은_기본값(name):
    """Intl.DateTimeFormat은 'Asia/Seoul '을 RangeError로 거절 → safeTimeZone이 Asia/Seoul로"""
    assert safe_time_zone(name) == "Asia/Seoul"
    assert safe_time_zone("Europe/Berlin") == "Europe/Berlin"


def test_byTimeThenId_글자_순서는_UTF16_코드_단위():
    """같은 시각 id 비교 — 이모지(UTF-16 0xD83D…)는 U+FFEE보다 앞이라 JS는 '￮' > '😀'"""
    at = datetime(1970, 1, 1, tzinfo=UTC)
    a = edit.DayWakeRow(id="\uffee", at=at, wake_enabled=True)
    b = edit.DayWakeRow(id="😀", at=at, wake_enabled=True)
    # JS: '\uffee' < '😀' 는 false ('\uffee' > '\ud83d') → 비교 함수는 1
    assert edit.by_time_then_id(a, b) == 1
    assert edit.by_time_then_id(b, a) == -1

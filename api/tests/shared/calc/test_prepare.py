"""calc/prepare.test.ts — 예측 준비 시간: 앱 목업 estimateAppointment의 "공통 P80 + 개인 보정"과 같은 숫자"""

import math

from app.shared.calc.prepare import (
    DEFAULT_COMMON_P80,
    margin_for_importance,
    percentile,
    personal_prepare_of,
    predict_prepare,
    prepare_recent_label,
    prepare_source_label,
    prepare_stats_of,
    recent_prepare_of,
    to_morning_minutes,
    wake_margin_of,
)
from app.shared.js import js_round


def test_목업_시나리오_기상부터_공통_50_n_10_k_10_개인_오차_P80_20_보정_10_예측_60():
    p = predict_prepare({"commonP80": 50, "personalP80": 70, "sampleCount": 10, "prior": 10})
    assert [p["commonP80"], p["weight"], p["residualP80"], p["correction"], p["minutes"]] == [
        50,
        0.5,
        20,
        10,
        60,
    ]


def test_목업_시나리오_같은_날_앞선_일정이_있음_공통_15_개인_오차_20이면_예측_25():
    p = predict_prepare(
        {"commonP80": DEFAULT_COMMON_P80["sameDay"], "personalP80": 35, "sampleCount": 10, "prior": 10}
    )
    assert p["minutes"] == 25


def test_기록이_없으면_개인_보정_없이_공통_P80_그대로_출처_DEFAULT_신뢰_LOW():
    p = predict_prepare({"commonP80": 50, "personalP80": None, "sampleCount": 0})
    assert [p["minutes"], p["correction"], p["source"], p["confidence"]] == [50, 0, "DEFAULT", "LOW"]


def test_기록이_5건_이상이면_출처_PERSONAL_15건_이상이면_신뢰_HIGH_7장_7_2_1_7_2_4():
    assert predict_prepare({"commonP80": 50, "personalP80": 60, "sampleCount": 5})["source"] == "PERSONAL"
    assert predict_prepare({"commonP80": 50, "personalP80": 60, "sampleCount": 5})["confidence"] == "MEDIUM"
    assert predict_prepare({"commonP80": 50, "personalP80": 60, "sampleCount": 15})["confidence"] == "HIGH"


def test_직접_고른_값이_먼저_자르지_않음_자동_예측은_autoMinutes에_남김():
    p = predict_prepare({"commonP80": 50, "personalP80": 70, "sampleCount": 10, "prior": 10, "override": 5})
    assert [p["minutes"], p["autoMinutes"], p["source"]] == [5, 60, "MANUAL"]


def test_목업과_같게_보정_상한_예측_범위로_자르지_않음_7장_7_0_5_30분_7_2_2_15_240분은_D56_뒤():
    # 목업: correction = round(n ÷ (n + k) × 개인 오차 P80), 예측 = 공통 P80 + correction
    high = predict_prepare({"commonP80": 50, "personalP80": 400, "sampleCount": 1000, "prior": 10})
    assert [high["correction"], high["minutes"]] == [
        js_round((1000 / 1010) * 350),
        50 + js_round((1000 / 1010) * 350),
    ]
    low = predict_prepare({"commonP80": 15, "personalP80": -400, "sampleCount": 1000, "prior": 10})
    assert [low["correction"], low["minutes"]] == [
        js_round((1000 / 1010) * -415),
        15 + js_round((1000 / 1010) * -415),
    ]


def test_predict_prepare_음수_절반_반올림은_양의_무한대_쪽():
    # weight = 0.5: residual -5 → -2.5 → Math.round는 -2 / residual -3 → -1.5 → Math.round는 -1 (Python round는 -2)
    assert (
        predict_prepare({"commonP80": 50, "personalP80": 45, "sampleCount": 10, "prior": 10})["correction"]
        == -2
    )
    assert (
        predict_prepare({"commonP80": 50, "personalP80": 47, "sampleCount": 10, "prior": 10})["correction"]
        == -1
    )
    # 양수 2.5 → 3 (Python round는 2)
    assert (
        predict_prepare({"commonP80": 50, "personalP80": 55, "sampleCount": 10, "prior": 10})["correction"]
        == 3
    )
    # sampleCount는 내림, prior 0 · n 0이면 가중치 0
    assert (
        predict_prepare({"commonP80": 50, "personalP80": 70, "sampleCount": 10.9, "prior": 10})["sampleCount"]
        == 10
    )
    assert predict_prepare({"commonP80": 50, "personalP80": 70, "sampleCount": 0, "prior": 0})["weight"] == 0
    assert predict_prepare({"commonP80": 50, "personalP80": 70, "sampleCount": -3})["sampleCount"] == 0


def test_toMorningMinutes_준비_더하기_준비_전_활동_알람_계산은_기록이_이미_기상_출발이라_부르지_않음_plan_core_test_ts():
    assert to_morning_minutes(40, 20) == 60
    assert to_morning_minutes(39.6, 20) == 60


def test_P80은_percentile_cont_0_8과_같은_선형_보간():
    assert percentile([], 0.8) is None
    assert percentile([10], 0.8) == 10
    assert percentile([40, 45, 50, 50, 55, 55, 60, 65, 75, 100], 0.8) == 67  # 위치 7.2 → 65 + 0.2 × 10
    assert percentile([5, 1, 3], 0.5) == 3


def test_prepareStatsOf_10_240분만_앞_최신에서_30건_31번째_오래된_기록과_5분_400분_빈_값은_무시():
    recent30 = [60] * 30
    stats = prepare_stats_of(
        [5, 400, None, None, *recent30, 200, 200]
    )  # 범위 밖 · 빈 값은 창을 차지하지 않고, 31번째부터(250)는 버림
    assert stats == {"sampleCount": 30, "p80": 60, "values": recent30}
    assert prepare_stats_of([10, 240]) == {"sampleCount": 2, "p80": 194, "values": [10, 240]}  # 경계값 포함
    assert prepare_stats_of([9, 241, 0, -5, math.nan]) == {"sampleCount": 0, "p80": None, "values": []}
    assert prepare_stats_of(["60", "80"]) == {
        "sampleCount": 2,
        "p80": 76,
        "values": [60, 80],
    }  # numeric 칸이 글자로 와도


def test_prepareStatsOf_글자_변환은_JS_Number_규칙():
    # Number('')는 0(범위 밖), Number(' 60 ')은 60, Number('6e1')은 60, Number('abc')는 NaN, Number('0x3c')은 60
    assert prepare_stats_of(["", " 60 ", "6e1", "abc", "0x3c", "1_0", "nan"])["values"] == [60, 60, 60]
    assert prepare_stats_of([True, "60.5"])["values"] == [60.5]  # Number(true) = 1 → 범위 밖


def test_prepareSourceLabel_4건까지_배우는_중_N_5_5건부터_내_기록_기반_N건_식_P80_같은_말은_안_보임_D47():
    assert [prepare_source_label(n) for n in (0, 3, 4)] == ["배우는 중 0/5", "배우는 중 3/5", "배우는 중 4/5"]
    assert [prepare_source_label(n) for n in (5, 12, 30)] == [
        "내 기록 기반 5건",
        "내 기록 기반 12건",
        "내 기록 기반 30건",
    ]
    assert not any(any(w in prepare_source_label(n) for w in ("P80", "분위수", "(n")) for n in (0, 4, 5, 30))


# ---- 깨우는 여유 (학습 루프 기초 b, D63) ----

# 10건 (최신순으로 섞어 둠) — 정렬하면 40 45 50 55 60 65 70 75 80 100: 0.7 → 70 + 0.3 * 5 = 71.5, 0.8 → 75 + 0.2 * 5 = 76, 0.9 → 80 + 0.1 * 20 = 82
TEN = [70, 100, 40, 80, 45, 75, 55, 60, 50, 65]


def test_깨우는_여유_빠듯_보통_넉넉_0_7_0_8_0_9_보통은_예전_P80과_정확히_같음():
    stats = prepare_stats_of(TEN)
    tight = personal_prepare_of(stats, "TIGHT", "NORMAL")
    normal = personal_prepare_of(stats, "NORMAL", "NORMAL")
    loose = personal_prepare_of(stats, "LOOSE", "NORMAL")
    assert tight < normal < loose, f"{tight} < {normal} < {loose}"
    assert normal == stats["p80"]  # 기존 P80 고정과 같은 값
    assert normal == percentile(TEN, 0.8)
    assert [tight, normal, loose] == [percentile(TEN, 0.7), percentile(TEN, 0.8), percentile(TEN, 0.9)]
    assert [tight, normal, loose] == [71.5, 76, 82]


def test_깨우는_여유_옛_모양_values_없음은_p80_그대로_기록_없음은_null():
    assert personal_prepare_of({"sampleCount": 10, "p80": 70}, "LOOSE", "NORMAL") == 70
    assert personal_prepare_of(prepare_stats_of([]), "LOOSE", "HIGH") is None
    assert personal_prepare_of({"sampleCount": 0, "p80": None}, "TIGHT", "NORMAL") is None


def test_매우_중요_HIGH면_한_단계_위_빠듯_보통_보통_넉넉_넉넉은_그대로_중요_보통_일정은_그대로():
    assert [margin_for_importance(m, "HIGH") for m in ("TIGHT", "NORMAL", "LOOSE")] == [
        "NORMAL",
        "LOOSE",
        "LOOSE",
    ]
    for importance in ("LOW", "NORMAL"):
        assert [margin_for_importance(m, importance) for m in ("TIGHT", "NORMAL", "LOOSE")] == [
            "TIGHT",
            "NORMAL",
            "LOOSE",
        ]
    stats = prepare_stats_of(TEN)
    assert personal_prepare_of(stats, "TIGHT", "HIGH") == personal_prepare_of(stats, "NORMAL", "NORMAL")
    assert personal_prepare_of(stats, "NORMAL", "HIGH") == personal_prepare_of(stats, "LOOSE", "NORMAL")
    assert personal_prepare_of(stats, "LOOSE", "HIGH") == personal_prepare_of(stats, "LOOSE", "NORMAL")


def test_wakeMarginOf_모르는_값_빈_값은_보통():
    assert [wake_margin_of(v) for v in ("TIGHT", "NORMAL", "LOOSE", "x", "", None)] == [
        "TIGHT",
        "NORMAL",
        "LOOSE",
        "NORMAL",
        "NORMAL",
        "NORMAL",
    ]


# ---- 최근 5번 (⑦) ----


def test_최근_값_목록_0건_빈_배열_3건_3개_최신순_7건_최신_5개():
    assert recent_prepare_of(prepare_stats_of([])) == []
    assert recent_prepare_of(prepare_stats_of([75, 70, 62])) == [75, 70, 62]
    assert recent_prepare_of(prepare_stats_of([75, 70, 62, 55, 48, 90, 91])) == [75, 70, 62, 55, 48]
    assert recent_prepare_of(prepare_stats_of([5, 400, 60.4, 61.6])) == [
        60,
        62,
    ]  # 범위 밖은 거르고, 분 단위로 반올림
    assert recent_prepare_of({"sampleCount": 3, "p80": 70}) == []  # 옛 모양


def test_prepareRecentLabel_오래된_것_최신_순서로_왼쪽_오른쪽_한_건도_없으면_null_식_분위수_말은_안_보임_D47():
    assert prepare_recent_label([75, 70, 62, 55, 48]) == "최근 5번 48 · 55 · 62 · 70 · 75분"
    assert prepare_recent_label([62, 55, 48]) == "최근 3번 48 · 55 · 62분"
    assert prepare_recent_label([60]) == "최근 1번 60분"
    assert prepare_recent_label([]) is None
    assert prepare_recent_label([1, 2, 3, 4, 5, 6, 7]) == "최근 5번 5 · 4 · 3 · 2 · 1분"  # 5개까지만
    label = prepare_recent_label([75, 70]) or ""
    assert not any(w in label for w in ("P80", "분위수", "0.7", "0.8", "0.9"))

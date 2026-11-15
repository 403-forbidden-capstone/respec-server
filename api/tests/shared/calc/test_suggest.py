"""calc/suggest.test.ts — 알람 조정 제안 (ALG-07): 목업 S01 예시 · 준비 전 시간이 모자랄 때(안건 초안 D111 2번 B) · 문턱 · 상한"""

import math

from app.shared.calc.suggest import detect_overrun, size_advance, suggest_prepare_advance, wake_shift


def test_목업_S01_예시_최근_3번_10_14_12분_준비_전_10분이면_준비만_10분_당김():
    s = suggest_prepare_advance({"residuals": [2, 10, 14, 12], "beforePrepareMinutes": 10, "capMinutes": 10})
    assert s == {"minutes": 10, "wakeMinutes": 0, "evidence": [10, 14, 12], "overrun": 12}


def test_준비_전_시간이_모자라면_모자란_만큼만_기상도_당김():
    s = suggest_prepare_advance({"residuals": [16, 18, 17], "beforePrepareMinutes": 10, "capMinutes": 30})
    assert s["minutes"] == 15
    assert s["wakeMinutes"] == 5
    assert wake_shift(10, 20) == 0
    assert wake_shift(10, 0) == 10


def test_기상_알람이_없는_일정은_기상을_당기지_않음():
    s = suggest_prepare_advance(
        {"residuals": [20, 20, 20], "beforePrepareMinutes": math.inf, "capMinutes": 30}
    )
    assert s["wakeMinutes"] == 0


def test_최근_3번_중_하나라도_예측_안이면_제안_안_함():
    assert detect_overrun([12, -1, 14]) is None
    assert detect_overrun([12, 0, 14]) is None


def test_평균_초과가_5분_미만이거나_기록이_3번_미만이면_제안_안_함():
    assert (
        suggest_prepare_advance({"residuals": [3, 4, 6], "beforePrepareMinutes": 20, "capMinutes": 10})
        is None
    )
    assert (
        suggest_prepare_advance({"residuals": [12, 14], "beforePrepareMinutes": 20, "capMinutes": 10}) is None
    )


def test_5분_단위로_내리고_D62_상한을_넘지_않음():
    assert size_advance(12, 10) == 10
    assert size_advance(24, 10) == 10
    assert size_advance(24, 5) == 5
    assert size_advance(7, 3) == 0


def test_소수_평균과_경계_5분_정확히는_제안_4_99는_안_함():
    # 평균 5 정확히 → 문턱 이상(>=)이라 제안, 5분 단위 내림 5
    assert (
        suggest_prepare_advance({"residuals": [5, 5, 5], "beforePrepareMinutes": 20, "capMinutes": 10})[
            "minutes"
        ]
        == 5
    )
    assert detect_overrun([4.9, 5, 5]) is None  # 평균 4.9666…
    # 평균 12.333…은 10으로 내림, 증거는 최근 3개 그대로(평균에 쓴 값)
    s = suggest_prepare_advance(
        {"residuals": [1, 12, 12.5, 12.5], "beforePrepareMinutes": 20, "capMinutes": 15}
    )
    assert s["minutes"] == 10 and s["evidence"] == [12, 12.5, 12.5]
    assert size_advance(math.inf, math.inf) == math.inf  # JS Math.floor(Infinity)는 Infinity

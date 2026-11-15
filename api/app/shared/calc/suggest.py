"""본 레포 supabase/functions/_shared/calc/suggest.ts의 서버판 (기준 b140f70) — 알람 조정 제안 (ALG-07)

준비 알람을 당길지 · 몇 분 당길지 (순수 함수, 새벽 배치 · 앱 미리 보기가 같이 씀 — 아직 부르는 곳 없음)
  준비 = 루틴 첫 단계 시작 ~ 출발 (없으면 준비 알람을 끈 시각부터). 잔차 = 실제 준비 − 예측 준비(적용한 제안 분 포함)
  당기는 알람은 준비 알람. 준비 전 시간(기상 → 준비 알람)이 모자라면 모자란 만큼만 기상도 당김 (팀 회의 · 안건 초안 D111 1 · 2번)
  판단 · 크기 = 안건 초안 D111 4번 기본안 A: 최근 3번 모두 예측보다 길고 평균 5분 이상 → 평균을 5분 단위로 내림, D62 상한 안
입력 SuggestInput = {residuals(오래된 → 최근), beforePrepareMinutes(기상 알람이 없으면 inf), capMinutes, rule?{window, minMean}}
결과 PrepareAdvance = {minutes, wakeMinutes, evidence, overrun}
"""

import math
from typing import Any, NotRequired, TypedDict

from ..js import js_floor, js_max, js_min

__all__ = [
    "SUGGEST_DEFAULT_RULE",
    "SUGGEST_ROUND_TO",
    "PrepareAdvance",
    "SuggestInput",
    "detect_overrun",
    "size_advance",
    "suggest_prepare_advance",
    "wake_shift",
]


class SuggestInput(TypedDict):
    """제안 입력 (TS SuggestInput) — residuals는 오래된 → 최근 순, 기상 알람이 없으면 beforePrepareMinutes = inf"""

    residuals: list[float]
    beforePrepareMinutes: float
    capMinutes: float
    rule: NotRequired[dict[str, Any]]


class PrepareAdvance(TypedDict):
    """제안 결과 (TS PrepareAdvance)"""

    minutes: int
    wakeMinutes: float
    evidence: list[float]
    overrun: float


SUGGEST_DEFAULT_RULE = {"window": 3, "minMean": 5}
SUGGEST_ROUND_TO = 5  # 제안 분은 5분 단위로 내림 (12분 → 10분)


def _mean(xs: list[float]) -> float:
    total = 0
    for x in xs:  # sum()은 소수를 보정해 더해서 JS reduce와 끝자리가 다를 수 있어 앞에서부터 그대로 더함
        total = total + x
    return total / len(xs) if xs else math.nan  # JS 0/0 = NaN (비교는 전부 false)


def detect_overrun(residuals: list[float], rule: dict[str, Any] | None = None) -> list[float] | None:
    """예측이 계속 짧았는지 — 최근 window번 모두 +이고 평균이 minMean분 이상이면 그 잔차를 돌려줌 (아니면 None)"""
    rule = rule if rule is not None else SUGGEST_DEFAULT_RULE
    if len(residuals) < rule["window"]:
        return None
    recent = (
        residuals[-rule["window"] :] if rule["window"] else list(residuals)
    )  # slice(-0)은 전체 (JS와 같게)
    if any(r <= 0 for r in recent):
        return None
    return list(recent) if _mean(recent) >= rule["minMean"] else None


def size_advance(overrun: float, cap_minutes: float) -> int:
    """준비 알람을 얼마나 당길지 — 5분 단위로 내림, 상한 안. 5분이 안 되면 0 (제안 안 함)"""
    floored = js_floor(overrun / SUGGEST_ROUND_TO) * SUGGEST_ROUND_TO
    capped = js_min(floored, js_floor(cap_minutes / SUGGEST_ROUND_TO) * SUGGEST_ROUND_TO)
    return capped if capped >= SUGGEST_ROUND_TO else 0


def wake_shift(advance: float, before_prepare_minutes: float) -> float:
    """준비 전 시간이 모자란 만큼만 기상도 — 준비 알람은 기상보다 앞설 수 없음"""
    return js_max(0, advance - js_max(0, before_prepare_minutes))


def suggest_prepare_advance(input: SuggestInput) -> PrepareAdvance | None:
    evidence = detect_overrun(input["residuals"], input.get("rule"))
    if not evidence:
        return None
    overrun = _mean(evidence)
    minutes = size_advance(overrun, input["capMinutes"])
    if not minutes:
        return None
    return {
        "minutes": minutes,
        "wakeMinutes": wake_shift(minutes, input["beforePrepareMinutes"]),
        "evidence": evidence,
        "overrun": overrun,
    }

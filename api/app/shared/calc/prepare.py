"""본 레포 supabase/functions/_shared/calc/prepare.ts의 서버판 (기준 b140f70) — 예측 준비 시간(기상 ~ 출발)

앱 목업의 "공통 P80 + 개인 보정"(features/alarm/api.ts estimateAppointment)을 그대로 옮김
  예측 = 공통 P80 + round(가중치 × 개인 오차 P80),  가중치 = n ÷ (n + k)
  n = 내 기록 수, k = 축소 강도(prior), 개인 오차 P80 = 내 P80 − 공통 P80
  기록이 없으면(n = 0) 개인 보정 0 → 공통 P80 그대로. 직접 고른 값이 있으면 그 값이 먼저
  보정 상한(7장 ±30분)과 예측 범위(7장 clamp(15, 240))는 목업 계산에 없어서 넣지 않음. 7장 clamp는 D56 뒤에 넣음
입력 · 결과는 JSON 모양 dict (PrepareInput · PrepareBasis · PrepareStats — 키 이름 그대로, 값이 없는 칸은 키를 뺌)
"""

import math
from typing import Any, Literal, NotRequired, TypedDict

from ..js import js_ceil, js_floor, js_number, js_round
from .types import Importance

__all__ = [
    "DEFAULT_BEFORE_PREPARE_MINUTES",
    "DEFAULT_COMMON_P80",
    "PERSONAL_MIN_SAMPLES",
    "PREPARE_PRIOR_K",
    "PREPARE_RECORD_MAX",
    "PREPARE_RECORD_MIN",
    "PREPARE_WINDOW",
    "PrepareBasis",
    "PrepareInput",
    "PrepareStats",
    "RECENT_PREPARE_COUNT",
    "WAKE_MARGIN_QUANTILE",
    "margin_for_importance",
    "percentile",
    "personal_prepare_of",
    "predict_prepare",
    "prepare_recent_label",
    "prepare_source_label",
    "prepare_stats_of",
    "recent_prepare_of",
    "to_morning_minutes",
    "wake_margin_of",
]


class PrepareInput(TypedDict):
    commonP80: float
    personalP80: NotRequired[float | None]
    sampleCount: float
    prior: NotRequired[float | None]
    override: NotRequired[float | None]


class PrepareBasis(TypedDict):
    """예측 준비 시간 (TS PrepareBasis) — mlModel은 prepare-ml이 모델 값을 썼을 때만"""

    minutes: float
    autoMinutes: float
    commonP80: float
    correction: float
    sampleCount: int
    prior: float
    weight: float
    residualP80: float
    source: Literal["MANUAL", "PERSONAL", "DEFAULT", "MODEL"]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    mlModel: NotRequired[str]


class PrepareStats(TypedDict):
    """내 기상~출발 기록 요약 (TS PrepareStats) — values 없는 옛 모양도 받음"""

    sampleCount: int
    p80: float | None
    values: NotRequired[list[float]]


# 공통 P80 기본값 — 목업의 값. 전체 사용자 통계 칸이 DB에 없어 임시로 고정 (그날 첫 일정이면 기상부터, 아니면 외출 준비만)
DEFAULT_COMMON_P80 = {"withWake": 50, "sameDay": 15}
# 기상 뒤 준비 전 활동 기본값 (기상 알람이 있는 일정). 7장 pre_prep_minutes 기본 10분과 다르게 목업 값(20분)
DEFAULT_BEFORE_PREPARE_MINUTES = 20
PREPARE_PRIOR_K = 10  # 목업 k
PERSONAL_MIN_SAMPLES = 5  # 7장 7.2.1 N_min — 이 아래면 출처를 DEFAULT(첫 값)로 표시
# 학습에 쓰는 기록 범위 (학습 루프 기초 ③, 7장 7.0.5 N_win) — 최근 30건, 기상~출발 10~240분만. 상한 240 = D107 기본안 A
PREPARE_WINDOW = 30
PREPARE_RECORD_MIN = 10
PREPARE_RECORD_MAX = 240


def predict_prepare(input: PrepareInput) -> PrepareBasis:
    """input = {commonP80, personalP80?, sampleCount, prior?, override?} → PrepareBasis"""
    n = max(0, js_floor(input["sampleCount"]))
    prior = input.get("prior")
    k = prior if prior is not None else PREPARE_PRIOR_K
    weight = n / (n + k) if n + k > 0 else 0
    personal = input.get("personalP80")
    residual = personal - input["commonP80"] if personal is not None and n > 0 else 0
    correction = js_round(weight * residual)
    auto = input["commonP80"] + correction
    override = input.get("override")
    manual = override is not None
    return {
        "minutes": override if manual else auto,
        "autoMinutes": auto,
        "commonP80": input["commonP80"],
        "correction": correction,
        "sampleCount": n,
        "prior": k,
        "weight": weight,
        "residualP80": residual,
        "source": "MANUAL" if manual else "PERSONAL" if n >= PERSONAL_MIN_SAMPLES else "DEFAULT",
        "confidence": "HIGH" if n >= 15 else "MEDIUM" if n >= PERSONAL_MIN_SAMPLES else "LOW",
    }


def to_morning_minutes(prepare_p80: float, before_prepare_minutes: float) -> int:
    """준비 전 활동을 더해 "기상 ~ 출발"로 바꾸는 계산 (7장 7.1.1: prep_minutes = morning_minutes − pre_prep_minutes)
    지금 알람 계산(plan-core)은 부르지 않음 — 기록이 이미 기상~출발이라 20분을 또 더하면 알람이 일찍 당겨지는 버그였음. 공용 export라 남겨 둠"""
    return js_round(prepare_p80 + before_prepare_minutes)


def percentile(values: list[float], p: float) -> float | None:
    """P80 (percentile_cont(0.8)과 같은 선형 보간 — 뷰 v_prepare_time_stats와 같은 값). 빈 목록이면 None"""
    if not len(values):
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * p
    lo = js_floor(pos)
    hi = js_ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


# 깨우는 여유 (D63, 온보딩 한 문항) — 빠듯 · 보통 · 넉넉 = 내 기록의 0.7 · 0.8 · 0.9 분위수. 보통(0.8)이 예전 P80 고정과 같은 값
WAKE_MARGIN_QUANTILE = {"TIGHT": 0.7, "NORMAL": 0.8, "LOOSE": 0.9}


def wake_margin_of(value: str | None) -> str:
    """DB 칸(profiles.wake_margin) → 계산에 넘기는 값. 모르는 값 · 빈 값은 NORMAL"""
    return value if value == "TIGHT" or value == "LOOSE" else "NORMAL"


# 매우 중요(HIGH) 일정은 여유를 한 단계 위로 — 빠듯 → 보통, 보통 → 넉넉, 넉넉은 그대로 (D63, 학습 루프 기초 1절 ①)
_MARGIN_UP = {"TIGHT": "NORMAL", "NORMAL": "LOOSE", "LOOSE": "LOOSE"}


def margin_for_importance(margin: str, importance: Importance) -> str:
    return _MARGIN_UP[margin] if importance == "HIGH" else margin


def prepare_stats_of(minutes_newest_first: list[Any]) -> dict[str, Any]:
    """기록 목록(최신순)에서 학습에 쓸 것만 — 10~240분, 앞에서 30건. 개수(n)와 분위수가 같은 30건 기준 (학습 루프 기초 ③)
    값이 소수 · 문자여도 숫자로 읽고 유한한 값만 셈. 결과 = {sampleCount, p80, values}"""
    used: list[float] = []
    for raw in minutes_newest_first:
        if len(used) >= PREPARE_WINDOW:
            break
        if raw is None:
            continue
        m = js_number(raw)
        if math.isfinite(m) and PREPARE_RECORD_MIN <= m <= PREPARE_RECORD_MAX:
            used.append(m)
    return {"sampleCount": len(used), "p80": percentile(used, 0.8), "values": used}


def personal_prepare_of(stats: dict[str, Any], margin: str, importance: Importance) -> float | None:
    """내 기록에서 깨우는 여유(+ 중요도 올림)에 맞는 값 — 기록이 없으면 None. values가 없는 옛 모양이면 p80 그대로 (보통 여유와 같음)"""
    values = stats.get("values")
    if not values:
        return stats.get("p80")
    return percentile(values, WAKE_MARGIN_QUANTILE[margin_for_importance(margin, importance)])


# H03 "최근 5번"에 보일 값 — 최신순 최대 5개, 분 단위로 반올림. 기록이 5건 미만이면 있는 것만, 0건이면 빈 목록
RECENT_PREPARE_COUNT = 5


def recent_prepare_of(stats: dict[str, Any]) -> list[int]:
    return [js_round(m) for m in (stats.get("values") or [])[:RECENT_PREPARE_COUNT]]


def prepare_source_label(sample_count: float) -> str:
    """계산 근거에 보이는 출처 한 줄 (H03 · H05) — 5건부터 "내 기록 기반 N건", 4건까지는 "배우는 중 N/5". 통계 식 · P80 같은 말은 화면에 안 보임 (D47)"""
    n = max(0, js_floor(sample_count))
    return f"내 기록 기반 {n}건" if n >= PERSONAL_MIN_SAMPLES else f"배우는 중 {n}/{PERSONAL_MIN_SAMPLES}"


def _is_finite_number(m: Any) -> bool:
    """Number.isFinite — 숫자 타입(bool 아님)이면서 유한"""
    return isinstance(m, int | float) and not isinstance(m, bool) and math.isfinite(m)


def prepare_recent_label(recent_newest_first: list[float]) -> str | None:
    """H03 "최근 5번 48 · 55 · 62 · 70 · 75분" — 오래된 것 → 최신 순서로 왼쪽 → 오른쪽 (받는 값은 최신순). 한 건도 없으면 None(줄을 안 보임)"""
    shown = [m for m in recent_newest_first[:RECENT_PREPARE_COUNT] if _is_finite_number(m)]
    if not shown:
        return None
    return f"최근 {len(shown)}번 {' · '.join(str(js_round(m)) for m in reversed(shown))}분"

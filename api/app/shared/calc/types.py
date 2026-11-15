"""본 레포 supabase/functions/_shared/calc/types.ts의 서버판 (기준 b140f70) — 알람 계산 공용 타입 · 상수

값 이름은 DB(alarm_type · importance_level · transport_type · alarm_intensity · voice_tone)와 같음.
시각은 UTC aware datetime. 객체(TS type)는 JSON 모양 그대로 dict(TypedDict) — 키 이름도 camelCase 그대로, 값이 없는 칸(undefined)은 키를 뺌.
"""

from datetime import datetime
from typing import Literal, NotRequired, TypedDict

__all__ = [
    "ALARM_KINDS",
    "INTENSITY_AXES",
    "KIND_LABEL",
    "RING_KINDS",
    "AlarmIntensity",
    "AlarmKind",
    "AlarmVibration",
    "ChainAlarm",
    "ChainInput",
    "Importance",
    "IntensityAxis",
    "Level",
    "RingKind",
    "Transport",
    "VoiceTone",
]

RingKind = Literal["WAKE", "PREPARE", "DEPARTURE"]
AlarmKind = Literal["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]
Importance = Literal["LOW", "NORMAL", "HIGH"]
Transport = Literal["WALK", "TRANSIT", "BUS", "SUBWAY", "CAR"]
AlarmIntensity = Literal["SOFT", "NORMAL", "STRONG"]
VoiceTone = Literal["CALM", "NEUTRAL", "DIRECT"]
# 진동 — DB alarms.vibration 값. 약 NONE · 보통 SHORT_REPEAT · 강 STRONG_CONTINUOUS
AlarmVibration = Literal["NONE", "SHORT_REPEAT", "STRONG_CONTINUOUS"]

# 약 · 중 · 강 (0 · 1 · 2)
Level = Literal[0, 1, 2]
# 배경음 · 말소리 · 진동 — DB 칸: sound = sound_key, voice = voice_tone, vibration = vibration
IntensityAxis = Literal["sound", "voice", "vibration"]

RING_KINDS: tuple[RingKind, ...] = ("WAKE", "PREPARE", "DEPARTURE")
ALARM_KINDS: tuple[AlarmKind, ...] = ("WAKE", "PREPARE", "DEPARTURE", "ARRIVE")
INTENSITY_AXES: tuple[IntensityAxis, ...] = ("sound", "voice", "vibration")

KIND_LABEL: dict[AlarmKind, str] = {"WAKE": "기상", "PREPARE": "준비", "DEPARTURE": "출발", "ARRIVE": "도착"}


class ChainAlarm(TypedDict):
    """알람 하나 — at이 쓰이는 시각, auto가 있으면 직접 고른 시각이고 auto는 자동 계산값 (D42 C "자동으로 되돌리기"용)"""

    kind: AlarmKind
    at: datetime
    auto: NotRequired[datetime]


class ChainInput(TypedDict):
    """알람 역산 입력 (7장 7.1.1 + 목업 ALG-01)"""

    at: datetime  # 약속 시각
    travelMinutes: float  # 적용되는 예상 이동 (직접 입력했으면 그 값)
    bufferMinutes: float  # 여유
    prepareMinutes: float  # 적용되는 준비 시간 = 기상 ~ 출발 (직접 고른 값 또는 P80 예측)
    beforePrepareMinutes: float  # 기상 뒤 준비 전 활동 — 0이면 기상 알람 뒤 준비 알람 없음
    withWake: bool  # 그날 첫 일정이라 기상 알람이 있음
    kinds: NotRequired[
        list[RingKind] | tuple[RingKind, ...] | None
    ]  # 만든 알람 종류. 없으면 계산된 알람 전부, 빈 목록이면 알람 없는 일정
    manualTimes: NotRequired[dict[RingKind, datetime] | None]  # 직접 고른 알람 시각 (D42 C)
    prepareAdvanceMinutes: NotRequired[
        float | None
    ]  # 받아들인 준비 알람 당김 분 (#484 · 안건 D111). 없으면 0

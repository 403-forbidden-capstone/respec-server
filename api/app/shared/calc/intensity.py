"""본 레포 supabase/functions/_shared/calc/intensity.ts의 서버판 (기준 b140f70) — ALG-02 알람 강도, D56 1 기본안 → D69

(정한 것, 10/26: 기본 강도는 보통, 약은 사용자가 직접 고를 때만)
  자동 = 보통(1)에서 시작해 올림 규칙을 단계로 더함, 상한 강(2). 중요도 낮음(LOW)이어도 기본은 보통(약으로 내려가지 않음)
    · 중요도 매우 중요(HIGH)면 한 단계 올림 (평소 · 중요는 올림 없음)
    · 최근 4주 늦잠 지각이 2회 이상이면 한 단계 올림
    · 잠귀가 무거움(wakeSensitivity = HEAVY)이면 곧바로 강
  직접 고른 칸(배경음 · 말소리 · 진동)이 있으면 그 칸은 그 값, 나머지는 자동 (약은 이 경우에만 나옴)
축별 고르기 (D1 A, 이슈 #312) — DB 칸 대응:
  · alarms.intensity = "전체 세기"(볼륨 · 다시 울림). 늘 자동 단계(auto)가 들어감 — 직접 고른 축은 안 바꿈
  · 배경음 축 → sound_key, 말소리 축 → voice_tone (자동이면 자동 단계 값이 들어감)
  · 진동 축 → vibration. 직접 고른 값만 들어가고 자동이면 NULL(= 강도를 따름) — 폰 · 앱은 vibration_of로 풀어 씀
  직접 고른 축이 하나라도 있으면 intensity_source = MANUAL (규칙 그대로)
문서(7장 7.4.1 · 4장 정책표)와 다른 점은 D69를 따름 (원본 주석 참고): 낮음도 기본 보통, profiles.intensity_offset은 안 읽음,
  "지각"은 is_late 전체, 잠귀는 wake_sensitivity_of로 HEAVY만 HEAVY(나머지 NORMAL), 7.4.1 표의 theme_focus는 안 씀
결과 객체(ResolvedIntensity)는 JSON 모양 dict: auto · importancePoint · lateCount · latePoint · heavySleeper · axes · manual · fields
"""

from typing import Any

from ..js import js_trim
from .types import AlarmIntensity, AlarmVibration, Importance, IntensityAxis, Level

__all__ = [
    "AXES_PICKER_ENABLED",
    "BASE_LEVEL",
    "IMPORTANCE_POINT",
    "LATE_COUNT_THRESHOLD",
    "LEVEL_FIELDS",
    "WakeSensitivity",
    "auto_level",
    "level_of_intensity",
    "level_of_sound_key",
    "level_of_vibration",
    "level_of_voice_tone",
    "resolve_intensity",
    "sound_key_of",
    "vibration_of",
    "wake_sensitivity_of",
]

# 중요도로 올리는 단계 수 — 매우 중요(HIGH)만 1 (D69: 중요도는 한 단계 올릴지에만 씀). 앱 라벨 보통(LOW) · 중요(NORMAL) · 매우 중요(HIGH)
IMPORTANCE_POINT: dict[Importance, int] = {"LOW": 0, "NORMAL": 0, "HIGH": 1}

# 가입 때 "잠귀" 한 문항 (D69). 계산에는 무거움(HEAVY)이냐 아니냐만 쓰므로 DB 값 LIGHT · NORMAL은 NORMAL로 묶음. 안 주면 NORMAL
WakeSensitivity = str  # 'HEAVY' | 'NORMAL'


def wake_sensitivity_of(value: str | None) -> WakeSensitivity:
    """DB 칸(profiles.wake_sensitivity 'LIGHT' | 'NORMAL' | 'HEAVY') → 계산에 넘기는 값. 모르는 값 · 빈 값은 NORMAL
    (L6 안건후보: 모르는 값 기본안 = NORMAL — 강으로 올리면 알람이 시끄러워지는 쪽이라 모르면 올리지 않음)"""
    return "HEAVY" if value == "HEAVY" else "NORMAL"


# 축별 선택(배경음 · 말소리 · 진동을 따로 고름, D1 A)을 쓸지 — 앱 H03 선택 UI(lib/flags.ts ALARM_AXES_PICKER)와 서버 · 폰 계산이 같은 값을 봄.
#   false: 이미 저장된 축 값은 지우지 않되 계산에서 무시하고 모두 강도를 따름. 다시 true로 켜면 저장돼 있던 값이 그대로 살아남
AXES_PICKER_ENABLED = True

# 기본 강도 = 보통 (D69)
BASE_LEVEL: Level = 1
_MAX_LEVEL: Level = 2

# 늦잠 지각이 이 횟수 이상이면 한 단계 올림 (7장 7.0.5 L_late = 2)
LATE_COUNT_THRESHOLD = 2

# 단계 → DB 칸 값. 앱 LEVELS(약 '차분 · 다정' · 중 '일정 안내형' · 강 '직설형')와 7장 7.4.1 표의 톤 · 배경음 이름
#   vibration: 약 없음 · 중 짧게 반복 · 강 강하게 연속
LEVEL_FIELDS: tuple[dict[str, str], ...] = (
    {"intensity": "SOFT", "voiceTone": "CALM", "soundKey": "theme_calm", "vibration": "NONE"},
    {"intensity": "NORMAL", "voiceTone": "NEUTRAL", "soundKey": "theme_default", "vibration": "SHORT_REPEAT"},
    {
        "intensity": "STRONG",
        "voiceTone": "DIRECT",
        "soundKey": "theme_urgent",
        "vibration": "STRONG_CONTINUOUS",
    },
)


def auto_level(
    importance: Importance, late_count_4w: float, wake_sensitivity: WakeSensitivity | None = None
) -> Level:
    """기본 보통(1) + 중요도 올림 + 지각 올림, 잠귀가 무거우면 최대 — 상한 강(2)"""
    if wake_sensitivity == "HEAVY":
        return _MAX_LEVEL
    late = 1 if late_count_4w >= LATE_COUNT_THRESHOLD else 0
    return min(_MAX_LEVEL, BASE_LEVEL + IMPORTANCE_POINT[importance] + late)  # type: ignore[return-value]


def resolve_intensity(
    importance: Importance,
    late_count_4w: float,
    overrides: dict[IntensityAxis, Level | None] | None = None,
    wake_sensitivity: WakeSensitivity | None = None,
) -> dict[str, Any]:
    """overrides = {sound?, voice?, vibration?} — 직접 고른 단계(없거나 None이면 자동)"""
    overrides = overrides if overrides is not None else {}
    auto = auto_level(importance, late_count_4w, wake_sensitivity)

    def axis(a: IntensityAxis) -> dict[str, Any]:
        picked = overrides.get(a)
        return {"level": picked, "manual": True} if picked is not None else {"level": auto, "manual": False}

    axes = {"sound": axis("sound"), "voice": axis("voice"), "vibration": axis("vibration")}
    return {
        "auto": auto,
        "importancePoint": IMPORTANCE_POINT[importance],
        "lateCount": late_count_4w,
        "latePoint": 1 if late_count_4w >= LATE_COUNT_THRESHOLD else 0,
        "heavySleeper": wake_sensitivity == "HEAVY",
        "axes": axes,
        "manual": axes["sound"]["manual"] or axes["voice"]["manual"] or axes["vibration"]["manual"],
        # DB 칸 값: intensity = 전체 세기(자동 단계), voice_tone = 말소리 칸, sound_key = 배경음 칸,
        #   vibration = 진동 칸 — 직접 고른 값만, 자동이면 None(강도를 따름)
        "fields": {
            "intensity": LEVEL_FIELDS[auto]["intensity"],
            "voiceTone": LEVEL_FIELDS[axes["voice"]["level"]]["voiceTone"],
            "soundKey": LEVEL_FIELDS[axes["sound"]["level"]]["soundKey"],
            "vibration": LEVEL_FIELDS[axes["vibration"]["level"]]["vibration"]
            if axes["vibration"]["manual"]
            else None,
        },
    }


def _level_of(field: str, v: str | None) -> Level | None:
    for i, f in enumerate(LEVEL_FIELDS):
        if f[field] == v:
            return i  # type: ignore[return-value]
    return None


# DB 칸 값 → 단계 (이미 저장된 알람을 읽을 때). 모르는 값이면 None
def level_of_intensity(v: str | None) -> Level | None:
    return _level_of("intensity", v)


def level_of_voice_tone(v: str | None) -> Level | None:
    return _level_of("voiceTone", v)


def level_of_sound_key(v: str | None) -> Level | None:
    return _level_of("soundKey", v)


def level_of_vibration(v: str | None) -> Level | None:
    return _level_of("vibration", v)


def vibration_of(vibration: str | None, intensity: AlarmIntensity) -> AlarmVibration:
    """저장된 알람이 실제로 쓸 진동 — vibration ?? 강도에서 (약 NONE · 보통 SHORT_REPEAT · 강 STRONG_CONTINUOUS)"""
    level = level_of_vibration(vibration)
    if level is None:
        level = level_of_intensity(intensity)
    if level is None:
        level = BASE_LEVEL
    return LEVEL_FIELDS[level]["vibration"]  # type: ignore[return-value]


def sound_key_of(sound_key: str | None, intensity: AlarmIntensity) -> str:
    """저장된 알람이 실제로 쓸 배경음 — sound_key ?? 강도에서 (약 theme_calm · 보통 theme_default · 강 theme_urgent).
    sound_key는 앱이 아는 이름 아무거나 가능해 모르는 값도 그대로 씀.
    앞뒤 공백은 폰(Kotlin)과 모듈 plan.ts가 trim하므로 같게 — 안 맞으면 서버 값과 폰 값이 계속 달라 매번 재예약됨"""
    trimmed = js_trim(sound_key) if sound_key is not None else None
    if trimmed:
        return trimmed
    level = level_of_intensity(intensity)
    return LEVEL_FIELDS[level if level is not None else BASE_LEVEL]["soundKey"]

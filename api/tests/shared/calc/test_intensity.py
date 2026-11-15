"""calc/intensity.test.ts — 알람 강도 D69: 기본 보통 · 매우 중요 / 지각 2회 / 잠귀 무거움 올림"""

from app.shared.calc.intensity import (
    LATE_COUNT_THRESHOLD,
    auto_level,
    level_of_intensity,
    level_of_sound_key,
    level_of_vibration,
    level_of_voice_tone,
    resolve_intensity,
    sound_key_of,
    vibration_of,
    wake_sensitivity_of,
)

# D69 규칙 표 — 기본 보통(1), 매우 중요(HIGH) +1, 최근 4주 늦잠 지각 2회 이상 +1, 잠귀 HEAVY면 강, 상한 강(2)
#   중요도 DB 값 LOW · NORMAL · HIGH = 앱 라벨 평소 · 중요 · 매우 중요 — 올림은 HIGH만
IMPORTANCES = ["LOW", "NORMAL", "HIGH"]
RULE_TABLE = [
    # 중요도, 지각 횟수, 잠귀, 기대 단계(0 약 · 1 중 · 2 강)
    ("LOW", 0, "NORMAL", 1), ("LOW", 0, "HEAVY", 2),
    ("LOW", 1, "NORMAL", 1), ("LOW", 1, "HEAVY", 2),
    ("LOW", 2, "NORMAL", 2), ("LOW", 2, "HEAVY", 2),
    ("NORMAL", 0, "NORMAL", 1), ("NORMAL", 0, "HEAVY", 2),
    ("NORMAL", 1, "NORMAL", 1), ("NORMAL", 1, "HEAVY", 2),
    ("NORMAL", 2, "NORMAL", 2), ("NORMAL", 2, "HEAVY", 2),
    ("HIGH", 0, "NORMAL", 2), ("HIGH", 0, "HEAVY", 2),
    ("HIGH", 1, "NORMAL", 2), ("HIGH", 1, "HEAVY", 2),
    ("HIGH", 2, "NORMAL", 2), ("HIGH", 2, "HEAVY", 2),  # 올림이 겹쳐도 상한 강
]  # fmt: skip


def test_규칙_표_18줄_중요도_3_x_지각_0_1_2_x_잠귀_보통_무거움():
    assert len(RULE_TABLE) == len(IMPORTANCES) * 3 * 2
    for importance, late, sensitivity, want in RULE_TABLE:
        assert auto_level(importance, late, sensitivity) == want, (
            f"{importance} · 지각 {late} · 잠귀 {sensitivity}"
        )
        assert resolve_intensity(importance, late, {}, sensitivity)["auto"] == want, (
            f"resolve {importance} · 지각 {late} · 잠귀 {sensitivity}"
        )


def test_잠귀를_안_주면_보통_취급_DB_칸이_생기기_전_약은_자동으로는_안_나옴():
    for importance in IMPORTANCES:
        for late in (0, 1, 2, 5):
            assert auto_level(importance, late) == auto_level(importance, late, "NORMAL")
            assert auto_level(importance, late) != 0  # D69: 약은 사용자가 직접 고를 때만
            assert auto_level(importance, late, "HEAVY") != 0


def test_지각_올림은_2회부터_상한_강():
    assert auto_level("NORMAL", LATE_COUNT_THRESHOLD - 1) == 1
    assert auto_level("NORMAL", LATE_COUNT_THRESHOLD) == 2
    assert auto_level("HIGH", 5) == 2


def test_직접_고른_칸만_그_값_나머지는_자동_intensity는_전체_세기_자동_진동_칸이_vibration_자동이면_null_말소리가_voice_tone_배경음이_sound_key():
    auto = resolve_intensity("NORMAL", 1)
    assert auto["auto"] == 1
    assert auto["manual"] is False
    assert auto["fields"] == {
        "intensity": "NORMAL",
        "voiceTone": "NEUTRAL",
        "soundKey": "theme_default",
        "vibration": None,
    }

    picked = resolve_intensity("NORMAL", 1, {"sound": 0, "vibration": 2, "voice": None})
    assert picked["manual"] is True
    assert picked["axes"] == {
        "sound": {"level": 0, "manual": True},
        "voice": {"level": 1, "manual": False},
        "vibration": {"level": 2, "manual": True},
    }
    # 진동을 강으로 직접 골라도 전체 세기(intensity)는 자동(보통) 그대로 — 진동은 vibration 칸
    assert picked["fields"] == {
        "intensity": "NORMAL",
        "voiceTone": "NEUTRAL",
        "soundKey": "theme_calm",
        "vibration": "STRONG_CONTINUOUS",
    }


def test_직접_고른_칸_사례_약은_직접_고를_때만_나머지_칸은_자동_보통():
    # 중요도 낮음 · 지각 없음 = 자동 보통. 말소리만 약으로 고르면 그 칸만 약 · MANUAL
    soft = resolve_intensity("LOW", 0, {"voice": 0})
    assert soft["auto"] == 1
    assert soft["manual"] is True
    assert soft["axes"] == {
        "sound": {"level": 1, "manual": False},
        "voice": {"level": 0, "manual": True},
        "vibration": {"level": 1, "manual": False},
    }
    assert soft["fields"] == {
        "intensity": "NORMAL",
        "voiceTone": "CALM",
        "soundKey": "theme_default",
        "vibration": None,
    }

    # 세 칸 모두 약으로 직접 고르면 약 (매우 중요 · 잠귀 무거움이어도 직접 고른 값이 먼저)
    all_soft = resolve_intensity("HIGH", 3, {"sound": 0, "voice": 0, "vibration": 0}, "HEAVY")
    assert all_soft["auto"] == 2
    # 전체 세기는 직접 고른 축과 상관없이 자동 단계(매우 중요 · 지각 · 잠귀 → 강)
    assert all_soft["fields"] == {
        "intensity": "STRONG",
        "voiceTone": "CALM",
        "soundKey": "theme_calm",
        "vibration": "NONE",
    }
    assert all_soft["manual"] is True

    # 고르지 않은 칸(null · 없음)은 자동 — 올림이 반영된 값
    partial = resolve_intensity("HIGH", 0, {"sound": 1, "voice": None})
    assert partial["axes"] == {
        "sound": {"level": 1, "manual": True},
        "voice": {"level": 2, "manual": False},
        "vibration": {"level": 2, "manual": False},
    }


def test_근거_칸_중요도_올림_늦잠_횟수_지각_점수_잠귀():
    r = resolve_intensity("NORMAL", 3)
    assert [r["importancePoint"], r["lateCount"], r["latePoint"], r["heavySleeper"], r["auto"]] == [
        0,
        3,
        1,
        False,
        2,
    ]
    high = resolve_intensity("HIGH", 0)
    assert [high["importancePoint"], high["latePoint"], high["auto"]] == [1, 0, 2]
    heavy = resolve_intensity("LOW", 0, {}, "HEAVY")
    assert [heavy["heavySleeper"], heavy["auto"]] == [True, 2]


def test_저장된_DB_값에서_단계_읽기():
    assert level_of_intensity("STRONG") == 2
    assert level_of_voice_tone("CALM") == 0
    assert level_of_sound_key("theme_default") == 1
    assert level_of_sound_key("theme_focus") is None
    assert level_of_intensity(None) is None
    assert level_of_vibration("SHORT_REPEAT") == 1
    assert level_of_vibration("NONE") == 0
    assert level_of_vibration("WOBBLE") is None
    assert level_of_vibration(None) is None


def test_진동_칸_자동이면_null_직접_고르면_그_값_세_값_모두_다른_축_전체_세기는_안_바뀜():
    values = [
        resolve_intensity("NORMAL", 0, {"vibration": level})["fields"]["vibration"] for level in (0, 1, 2)
    ]
    assert values == ["NONE", "SHORT_REPEAT", "STRONG_CONTINUOUS"]
    strong = resolve_intensity("NORMAL", 0, {"vibration": 2})
    assert strong["manual"] is True
    assert [strong["fields"]["intensity"], strong["fields"]["voiceTone"], strong["fields"]["soundKey"]] == [
        "NORMAL",
        "NEUTRAL",
        "theme_default",
    ]
    # null · 없음 = 자동으로 → vibration null (직접 고른 뒤 "자동으로" 되돌리는 경우)
    assert resolve_intensity("NORMAL", 0, {"vibration": None})["fields"]["vibration"] is None
    assert resolve_intensity("NORMAL", 0, {"vibration": None})["manual"] is False
    # 말소리만 직접 골라도 진동은 null(강도를 따름) — MANUAL이어도 진동 칸은 비어 있음
    voice_only = resolve_intensity("NORMAL", 0, {"voice": 2})
    assert voice_only["manual"] is True
    assert voice_only["fields"]["vibration"] is None
    # 전체 세기는 자동 단계만 따라감 (중요 일정 → 강)
    assert (
        resolve_intensity("HIGH", 0, {"sound": 0, "voice": 0, "vibration": 0})["fields"]["intensity"]
        == "STRONG"
    )


def test_꾸러미에_실을_값_풀기_진동_vibration_or_강도에서_배경음_sound_key_or_강도에서():
    assert [vibration_of(None, i) for i in ("SOFT", "NORMAL", "STRONG")] == [
        "NONE",
        "SHORT_REPEAT",
        "STRONG_CONTINUOUS",
    ]
    assert [sound_key_of(None, i) for i in ("SOFT", "NORMAL", "STRONG")] == [
        "theme_calm",
        "theme_default",
        "theme_urgent",
    ]
    # 직접 고른 값이 먼저 (강도와 달라도)
    assert vibration_of("NONE", "STRONG") == "NONE"
    assert sound_key_of("theme_focus", "SOFT") == "theme_focus"
    # 모르는 진동 값 · 빈 글자는 강도를 따름
    assert vibration_of("WOBBLE", "STRONG") == "STRONG_CONTINUOUS"
    assert vibration_of(None, "SOFT") == "NONE"  # JS undefined와 null이 같음
    assert sound_key_of("", "STRONG") == "theme_urgent"


def test_sound_key_앞뒤_공백은_JS_trim과_같이_지움():
    assert sound_key_of("  theme_focus \n", "SOFT") == "theme_focus"
    assert sound_key_of("　﻿", "STRONG") == "theme_urgent"  # 전각 공백 · BOM도 JS가 지우는 글자


def test_wake_sensitivity_of_DB_값_HEAVY만_HEAVY_LIGHT_NORMAL_모르는_값_빈_값은_NORMAL_약은_직접_고를_때만_D69():
    assert [wake_sensitivity_of(v) for v in ("HEAVY", "LIGHT", "NORMAL", "x", "", None)] == [
        "HEAVY",
        "NORMAL",
        "NORMAL",
        "NORMAL",
        "NORMAL",
        "NORMAL",
    ]
    # 가벼움 + 보통 일정 → 보통 (약 아님)
    assert auto_level("NORMAL", 0, wake_sensitivity_of("LIGHT")) == 1

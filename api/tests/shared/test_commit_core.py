"""commit-core.ts → commit_core.py 시험 (원본 commit-core.test.ts의 모든 경우) — alarm-commit이 쓸 내용 정하기 + 예약 꾸러미 + 취소 목록"""

import pytest

from app.shared.calc.prepare import PrepareStats
from app.shared.commit_core import AlarmDiff, EditInput, build_edit_log_rows, diff_alarms
from app.shared.draft import PlaceInput
from app.shared.plan_core import PlanDraft, PlanInputs, compute_plan
from app.shared.ring_package import build_ring_packages, canceled_alarm_ids, subtitle_for
from tests.shared.plan_helpers import PROFILE_BARE, alarm_row, api, hm, iso_of, kst

NOW = kst(22, 400)  # 6:40


def travel():
    return api(50)


def plan(draft: dict | None = None, **over):
    base_draft = {
        "at": kst(22, 600),
        "destination": PlaceInput(name="세종대학교"),
        "prepare_minutes": 60,
        "before_prepare_minutes": 30,
        **(draft or {}),
    }
    inputs = PlanInputs(
        **{
            "now": NOW,
            "draft": PlanDraft(**base_draft),
            "profile": PROFILE_BARE,
            "existing_alarms": [],
            "stats": PrepareStats(sampleCount=0, p80=None),
            "late_count4w": 1,
            "same_day_wake": False,
            **over,
        }
    )
    return compute_plan(inputs, travel())


def row(alarm_type: str, minutes: float, **over):
    return alarm_row(
        alarm_type, minutes, alarm_id=f"id-{alarm_type}", participant_id="pt", user_id="u", **over
    )


def test_new_appointment_creates_four_alarms_all_auto():
    """새 약속: 4개 알람을 만듦 (기상 · 준비 · 출발 · 도착 목표), 모두 자동 출처"""
    diff = diff_alarms(plan(), [], now=NOW, replace_existing=True)
    assert [
        f"{a['alarm_type']} {hm(a['scheduled_at'])} {str(a['is_enabled']).lower()} {a['time_source']}/{a['intensity_source']}"
        for a in diff.insert
    ] == [
        "WAKE 08:00 true AUTO/AUTO",
        "PREPARE 08:30 true AUTO/AUTO",
        "DEPARTURE 09:00 true AUTO/AUTO",
        "ARRIVE 09:50 true AUTO/AUTO",
    ]
    assert diff.update == []
    assert diff.remove == []
    assert [diff.insert[0]["intensity"], diff.insert[0]["voice_tone"], diff.insert[0]["sound_key"]] == [
        "NORMAL",
        "NEUTRAL",
        "theme_default",
    ]
    assert all(a["vibration"] is None for a in diff.insert)  # 진동은 자동이면 null (강도를 따름)


def test_manual_time_and_level_are_manual_source():
    """직접 고른 시각 · 강도는 MANUAL 출처로 (따라온 준비는 AUTO)"""
    p = plan({"manual_times": {"WAKE": kst(22, 450)}, "levels": {"vibration": 2}})
    diff = diff_alarms(p, [], now=NOW, replace_existing=True)
    by = {a["alarm_type"]: a for a in diff.insert}
    assert by["WAKE"]["time_source"] == "MANUAL"
    assert by["PREPARE"]["time_source"] == "AUTO"
    assert hm(by["PREPARE"]["scheduled_at"]) == "08:00"
    # 진동을 강으로 직접 골랐다: 진동 칸만 STRONG_CONTINUOUS, 전체 세기(intensity)는 자동 단계(보통) 그대로
    assert all(
        a["intensity_source"] == "MANUAL"
        and a["intensity"] == "NORMAL"
        and a["vibration"] == "STRONG_CONTINUOUS"
        for a in diff.insert
    )


def test_no_alarm_schedule_creates_nothing_and_removes_existing():
    """알람 없는 일정(kinds [])은 아무것도 만들지 않고 있던 알람은 지움"""
    none = diff_alarms(plan({"kinds": []}), [], now=NOW, replace_existing=True)
    assert none == AlarmDiff(insert=[], update=[], remove=[])
    had = diff_alarms(
        plan({"kinds": []}), [row("WAKE", 480), row("ARRIVE", 590)], now=NOW, replace_existing=True
    )
    assert sorted(had.remove) == ["id-ARRIVE", "id-WAKE"]


def test_same_kind_updates_and_missing_kinds_removed():
    """같은 종류는 고치고(update) 없어진 종류만 지움, replace_existing false면 안 지움"""
    existing = [row("WAKE", 470), row("PREPARE", 500), row("DEPARTURE", 540), row("ARRIVE", 590)]
    p = plan({"kinds": ["WAKE"]}, existing_alarms=existing)
    diff = diff_alarms(p, existing, now=NOW, replace_existing=True)
    assert [[u["alarm_id"], hm(u["patch"]["scheduled_at"])] for u in diff.update] == [
        ["id-WAKE", "08:00"],
        ["id-ARRIVE", "09:50"],
    ]
    assert sorted(diff.remove) == ["id-DEPARTURE", "id-PREPARE"]
    assert diff_alarms(p, existing, now=NOW, replace_existing=False).remove == []


def test_passed_alarm_saved_off_user_off_stays_off_passed_off_turns_on_again():
    """지난 알림은 꺼서 저장, 사용자가 끈 알람은 꺼진 채로, 지나서 꺼졌던 알람은 새 시각으로 다시 켬"""
    # 지금 7:58 → 기상 8:00은 지난 알림 (8.2.1)
    passed = diff_alarms(plan(now=kst(22, 478)), [], now=kst(22, 478), replace_existing=True)
    assert [a["is_enabled"] for a in passed.insert] == [False, True, True, True]

    existing = [row("WAKE", 480, is_enabled=False), row("DEPARTURE", 540, is_enabled=False)]
    p = plan(existing_alarms=existing)
    diff = diff_alarms(p, existing, now=NOW, replace_existing=True)
    by_type = {u["patch"]["alarm_type"]: u["patch"]["is_enabled"] for u in diff.update}
    assert by_type["WAKE"] is False  # 옛 시각 8:00이 아직 안 지남 → 사용자가 끈 것
    assert by_type["DEPARTURE"] is False

    # 옛 시각이 이미 지난 알람은 지나서 꺼진 것으로 봄 → 새 시각으로 켬
    stale = [
        row("WAKE", 480, is_enabled=False),
        row("DEPARTURE", 490, is_enabled=False),
    ]  # 출발의 옛 시각 8:10 — 지금 8:20이면 이미 지남
    later = diff_alarms(
        plan(existing_alarms=stale, now=kst(22, 500)), stale, now=kst(22, 500), replace_existing=True
    )
    wake = next((u for u in later.update if u["patch"]["alarm_type"] == "WAKE"), None)
    assert wake["patch"]["is_enabled"] is False  # 새 기상 8:00도 이미 지났으므로 꺼짐
    assert (
        next(u for u in later.update if u["patch"]["alarm_type"] == "DEPARTURE")["patch"]["is_enabled"]
        is True
    )


def test_requested_enabled_comes_first():
    """요청의 enabled가 먼저 (H03 종류별 켜기 · 끄기)"""
    existing = [
        row("WAKE", 480, is_enabled=False),
        row("PREPARE", 510),
        row("DEPARTURE", 540),
        row("ARRIVE", 590),
    ]
    p = plan(existing_alarms=existing)
    diff = diff_alarms(p, existing, now=NOW, enabled={"WAKE": True, "PREPARE": False}, replace_existing=True)
    by_type = {u["patch"]["alarm_type"]: u["patch"]["is_enabled"] for u in diff.update}
    assert by_type == {"WAKE": True, "PREPARE": False, "DEPARTURE": True, "ARRIVE": True}


def test_unchecked_plan_is_not_written():
    """검증을 통과 못 한 계획은 쓰지 않음"""
    with pytest.raises(Exception):  # noqa: B017 — 원본도 assert.throws(fn) 그대로 (Error 종류 안 봄)
        diff_alarms(plan(now=kst(22, 545)), [], now=kst(22, 545), replace_existing=True)


def test_ring_packages_only_enabled_core_field_names_time_order():
    """예약 꾸러미: 켜진 알람만, 알람 코어 RingPackage 칸 이름, 시각순"""
    alarms = [
        {
            "alarm_id": "a2",
            "alarm_type": "DEPARTURE",
            "scheduled_at": iso_of(kst(22, 540)),
            "intensity": "NORMAL",
            "sound_key": "theme_default",
            "vibration": None,
            "voice_tone": "NEUTRAL",
            "snooze_limit": 3,
            "is_enabled": True,
        },
        {
            "alarm_id": "a1",
            "alarm_type": "WAKE",
            "scheduled_at": iso_of(kst(22, 480)),
            "intensity": "STRONG",
            "sound_key": "theme_urgent",
            "vibration": None,
            "voice_tone": "DIRECT",
            "snooze_limit": 3,
            "is_enabled": True,
        },
        {
            "alarm_id": "a3",
            "alarm_type": "PREPARE",
            "scheduled_at": iso_of(kst(22, 510)),
            "intensity": "NORMAL",
            "sound_key": None,
            "vibration": None,
            "voice_tone": "NEUTRAL",
            "snooze_limit": 3,
            "is_enabled": False,
        },
    ]
    from app.shared.js import ms

    pkgs = build_ring_packages(
        alarms=alarms, title="팀 주간회의", subtitle="오전 10:00 · 세종대학교", depart_at=kst(22, 540)
    )
    assert pkgs == [
        {
            "alarmId": "a1",
            "type": "WAKE",
            "atMillis": ms(kst(22, 480)),
            "title": "팀 주간회의",
            "subtitle": "오전 10:00 · 세종대학교",
            "intensity": "STRONG",
            "snoozeLimit": 3,
            "soundKey": "theme_urgent",
            "vibration": "STRONG_CONTINUOUS",
            "departAt": ms(kst(22, 540)),
        },
        {
            "alarmId": "a2",
            "type": "DEPARTURE",
            "atMillis": ms(kst(22, 540)),
            "title": "팀 주간회의",
            "subtitle": "오전 10:00 · 세종대학교",
            "intensity": "NORMAL",
            "snoozeLimit": 3,
            "soundKey": "theme_default",
            "vibration": "SHORT_REPEAT",
            "departAt": ms(kst(22, 540)),
        },
    ]


def test_canceled_alarm_ids():
    """취소할 예약: 지워졌거나 꺼진 알람 (canceled_alarm_ids)"""
    before = [{"alarm_id": "a"}, {"alarm_id": "b"}, {"alarm_id": "c"}]
    after = [
        {"alarm_id": "a", "is_enabled": True},
        {"alarm_id": "b", "is_enabled": False},
        {"alarm_id": "n", "is_enabled": True},
    ]
    assert canceled_alarm_ids(before=before, after=after) == ["b", "c"]
    assert canceled_alarm_ids(before=[], after=after) == []


def test_subtitle():
    """꾸러미 부제: 오전 10:00 · 장소"""
    assert subtitle_for(kst(22, 600), "세종대학교", "Asia/Seoul") == "오전 10:00 · 세종대학교"
    assert subtitle_for(kst(22, 600), None, "Asia/Seoul") == "오전 10:00"


def test_edit_log_rows():
    """고친 값 기록: 알람 종류로 가리킨 줄에 저장된 알람 id를 붙임, 되돌림은 edited_value null"""
    rows = build_edit_log_rows(
        "u",
        "ap",
        [{"alarm_id": "w", "alarm_type": "WAKE"}],
        [
            EditInput(
                screen="H05", field="scheduled_at", alarm_type="WAKE", auto_value="a", edited_value="b"
            ),
            EditInput(screen="H03", field="buffer_minutes", auto_value=10, edited_value=20),
            EditInput(
                screen="H03", field="scheduled_at", alarm_type="PREPARE", auto_value="c", edited_value=None
            ),
        ],
    )
    assert rows[0] == {
        "user_id": "u",
        "appointment_id": "ap",
        "alarm_id": "w",
        "screen": "H05",
        "field": "scheduled_at",
        "alarm_type": "WAKE",
        "auto_value": "a",
        "edited_value": "b",
    }
    assert rows[1]["alarm_id"] is None
    assert rows[1]["alarm_type"] is None
    assert rows[2]["alarm_id"] is None  # 그 종류의 알람이 없으면 비움
    assert rows[2]["edited_value"] is None

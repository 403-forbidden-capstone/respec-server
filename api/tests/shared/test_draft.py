"""draft.ts → draft.py 시험 (원본 draft.test.ts의 모든 경우)"""

from datetime import UTC, datetime

import pytest

from app.core.errors import AppError
from app.core.read import Reader
from app.shared.draft import (
    RRULE_PATTERN,
    UNSET,
    AlarmFields,
    ParticipantFields,
    PlaceInput,
    read_alarm_fields,
    read_participant_fields,
    read_place,
    read_recurrence,
)


def test_place_name_and_coordinates_together():
    """장소: 이름 · 좌표(둘이 같이)"""
    r = Reader.root(
        {
            "destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07},
            "origin": {"name": "집"},
            "bad": {"lat": 37.5},
        }
    )
    assert read_place(r, "destination") == PlaceInput(name="세종대학교", lat=37.55, lng=127.07)
    assert read_place(r, "origin") == PlaceInput(name="집")
    assert read_place(r, "missing") is None
    read_place(r, "bad")
    assert len(r.issues) == 1
    assert r.issues[0]["path"] == "bad.lng"


def test_destination_radius_only_for_destination():
    """목적지 감지 반경 radius_m: 목적지만 읽음(100~500 정수), 출발지는 와도 무시"""
    r = Reader.root(
        {"destination": {"name": "세종대학교", "radius_m": 250}, "origin": {"name": "집", "radius_m": 9999}}
    )
    assert read_place(r, "destination", radius=True) == PlaceInput(name="세종대학교", radius_m=250)
    assert read_place(r, "origin") == PlaceInput(name="집")
    assert len(r.issues) == 0
    assert read_place(Reader.root({"p": {"name": "a", "radius_m": 100}}), "p", radius=True) == PlaceInput(
        name="a", radius_m=100
    )
    assert read_place(Reader.root({"p": {"name": "a", "radius_m": 500}}), "p", radius=True) == PlaceInput(
        name="a", radius_m=500
    )
    assert read_place(Reader.root({"p": {"name": "a"}}), "p", radius=True) == PlaceInput(name="a")
    for bad in [99, 501, 1.5, 150.5, "abc", "200", True]:
        b = Reader.root({"p": {"name": "a", "radius_m": bad}})
        read_place(b, "p", radius=True)
        assert [i["path"] for i in b.issues] == ["p.radius_m"], str(bad)


def test_out_of_range_coordinates_are_problems():
    """범위를 벗어난 좌표는 문제"""
    r = Reader.root({"p": {"lat": 91, "lng": 127}})
    read_place(r, "p")
    assert r.issues[0]["path"] == "p.lat"


def test_participant_fields():
    """출발 · 이동 · 여유 칸"""
    r = Reader.root(
        {
            "origin": {"name": "학교"},
            "transport_type": "CAR",
            "buffer_minutes": 5,
            "before_prepare_minutes": 0,
            "with_wake": False,
        }
    )
    assert read_participant_fields(r) == ParticipantFields(
        origin=PlaceInput(name="학교"),
        transport="CAR",
        buffer_minutes=5,
        before_prepare_minutes=0,
        with_wake=False,
    )
    assert read_participant_fields(Reader.root({})) == ParticipantFields()
    bad = Reader.root({"transport_type": "ROCKET", "buffer_minutes": -1})
    read_participant_fields(bad)
    assert len(bad.issues) == 2


def test_alarm_fields_kinds_enabled_manual_times_levels():
    """알람 칸: 종류 · 켜기 · 직접 시각(null = 자동으로 되돌림) · 강도 단계"""
    r = Reader.root(
        {
            "kinds": ["WAKE", "DEPARTURE"],
            "enabled": {"WAKE": False},
            "manual_times": {"WAKE": "2026-10-20T07:30:00+09:00", "PREPARE": None},
            "levels": {"sound": 0, "voice": None, "vibration": 2},
        }
    )
    f = read_alarm_fields(r)
    r.finish()
    assert f.kinds == ["WAKE", "DEPARTURE"]
    assert f.enabled == {"WAKE": False}
    assert f.manual_times == {"WAKE": datetime(2026, 10, 19, 22, 30, tzinfo=UTC), "PREPARE": None}
    assert f.levels == {"sound": 0, "voice": None, "vibration": 2}


def test_missing_fields_are_none_not_empty():
    """칸이 아예 없으면 undefined — "저장된 값 그대로"와 "전부 자동"을 구분"""
    f = read_alarm_fields(Reader.root({}))
    assert f.kinds is None
    assert f.manual_times is None
    assert f.levels is None
    assert read_alarm_fields(Reader.root({"kinds": []})).kinds == []
    assert read_alarm_fields(Reader.root({"manual_times": {}})).manual_times == {}
    assert read_alarm_fields(Reader.root({})) == AlarmFields()


def test_arrive_is_not_pickable_and_levels_are_0_to_2():
    """도착은 직접 고르는 종류가 아님 · 강도는 0~2"""
    r = Reader.root({"kinds": ["ARRIVE"], "levels": {"sound": 3}, "manual_times": {"WAKE": "내일 7시"}})
    read_alarm_fields(r)
    with pytest.raises(AppError):
        r.finish()
    assert len(r.issues) == 3


def test_recurrence_rule_two_shapes_only():
    """반복 규칙 (D44): 요일 칩 · 매일 · 평일 — 앱이 만드는 두 모양만"""
    for ok in [
        "FREQ=DAILY",
        "FREQ=WEEKLY;BYDAY=TH",
        "FREQ=WEEKLY;BYDAY=MO,WE",
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        "FREQ=WEEKLY;BYDAY=SA,SU",
    ]:
        assert RRULE_PATTERN.search(ok), ok
    # 두 모양 밖은 거절: 월간 · 연간 · 간격(INTERVAL) · 횟수 · 요일 없는 주간 · 모르는 요일 코드 · 소문자 · RRULE: 접두사
    bad = [
        "",
        "DAILY",
        "FREQ=HOURLY",
        "FREQ=DAILY;DROP TABLE",
        "BYDAY=MO",
        "FREQ=MONTHLY",
        "FREQ=MONTHLY;BYMONTHDAY=3",
        "FREQ=YEARLY",
        "FREQ=WEEKLY",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH",
        "FREQ=WEEKLY;BYDAY=TH;COUNT=3",
        "FREQ=DAILY;INTERVAL=2",
        "FREQ=DAILY;BYDAY=MO",
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ=WEEKLY;BYDAY=MO,",
        "freq=daily",
        "RRULE:FREQ=DAILY",
        "FREQ=DAILY ",
        " FREQ=DAILY",
    ]
    for rule in bad:
        assert not RRULE_PATTERN.search(rule), rule
    assert read_recurrence(Reader.root({"recurrence_rule": "FREQ=DAILY"})) == "FREQ=DAILY"
    assert (
        read_recurrence(Reader.root({"recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,WE"}))
        == "FREQ=WEEKLY;BYDAY=MO,WE"
    )
    assert read_recurrence(Reader.root({"recurrence_rule": None})) is None
    assert read_recurrence(Reader.root({})) is UNSET  # undefined
    # 두 모양 밖은 읽는 쪽에서 문제로 기록됨 → 요청 전체가 VALIDATION_FAILED
    for rule in ["FREQ=MONTHLY", "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH"]:
        r = Reader.root({"recurrence_rule": rule})
        read_recurrence(r)
        with pytest.raises(AppError):
            r.finish()
        assert [i["path"] for i in r.issues] == ["recurrence_rule"]

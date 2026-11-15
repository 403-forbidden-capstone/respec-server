"""ring-package.ts → ring_package.py 시험 (원본 ring-package.test.ts의 모든 경우) — 예약 꾸러미의 알림 문구: 준비 · 출발 · 기상(voiceText)"""

import logging
import re
from typing import Any

import pytest

from app.shared import ring_package
from app.shared.ring_package import (
    AppointmentNotice,
    axes_of,
    build_ring_packages,
    departure_notice,
    prepare_notice,
    wake_voice_text,
)
from tests.shared.plan_helpers import iso_of, kst

TZ = "Asia/Seoul"


def row(alarm_type: str, minutes: float, **over: Any) -> dict[str, Any]:
    return {
        "alarm_id": f"a-{alarm_type}",
        "alarm_type": alarm_type,
        "scheduled_at": iso_of(kst(22, minutes)),
        "intensity": "NORMAL",
        "sound_key": None,
        "snooze_limit": 3,
        "is_enabled": True,
        **over,
    }


# 10시 약속 · 이동 50 · 여유 10 · 준비 60 · 준비 전 활동 30 → 기상 8:00 · 준비 8:30 · 출발 9:00 · 도착 9:50
CHAIN = [row("WAKE", 480), row("PREPARE", 510), row("DEPARTURE", 540), row("ARRIVE", 590)]
APPOINTMENT = AppointmentNotice(appointment_at=kst(22, 600), travel_minutes=50, time_zone=TZ)


def build(alarms, appointment=APPOINTMENT, title="전공 수업"):
    return build_ring_packages(
        alarms=alarms,
        title=title,
        subtitle="오전 10:00 · 광개토관",
        depart_at=kst(22, 540),
        appointment=appointment,
    )


def by_type(pkgs):
    return {p["type"]: p for p in pkgs}


def test_notices_and_voice_text_on_appointment_packages_arrive_unchanged():
    """준비 · 출발 · 기상 문구가 약속 알람 꾸러미에 실림, 도착은 그대로"""
    pkgs = by_type(build(CHAIN))
    assert pkgs["PREPARE"]["notice"] == "오전 9:00 출발까지 30분"
    assert pkgs["DEPARTURE"]["notice"] == "지금 나가면 오전 9:50 도착 · 여유 10분"
    assert (
        pkgs["WAKE"]["voiceText"]
        == "좋은 아침이에요. 오늘 첫 일정은 오전 10시 전공 수업이에요. 오전 9시에 나가면 돼요."
    )
    assert "notice" not in pkgs["WAKE"]  # 기상 화면 줄(일어날 시간이에요)은 알람 코어의 기본 그대로
    assert [pkgs["ARRIVE"].get("notice"), pkgs["ARRIVE"].get("voiceText")] == [None, None]
    assert [pkgs["PREPARE"].get("voiceText"), pkgs["DEPARTURE"].get("voiceText")] == [None, None]


def test_without_appointment_no_text_keys_at_all():
    """appointment를 안 넘기면(일정 없는 알람 · 루틴) 문구 칸이 아예 없음"""
    pkgs = build_ring_packages(alarms=CHAIN, title="알람", subtitle="오전 10:00")
    assert len(pkgs) == 4
    for p in pkgs:
        assert [k for k in p if k in ("notice", "voiceText")] == []


def test_departure_notice_arrival_is_departure_plus_travel():
    """출발 문구의 도착은 출발 알람 시각 + 이동분 (직접 고른 출발이면 약속 − 여유로 세지 않음)"""

    def at(minutes):
        return by_type(build([row("PREPARE", 500), row("DEPARTURE", minutes)]))["DEPARTURE"].get("notice")

    assert at(530) == "지금 나가면 오전 9:40 도착 · 여유 20분"  # 9:00보다 10분 일찍 직접 고름
    assert at(550) == "지금 나가면 오전 10:00 도착 · 여유 없음"
    assert at(560) == "지금 나가면 오전 10:10 도착 · 10분 늦어요"
    assert at(480) == "지금 나가면 오전 8:50 도착 · 여유 1시간 10분"  # 60분 넘으면 시간 단위
    assert at(420) == "지금 나가면 오전 7:50 도착 · 여유 2시간 10분"


def test_prepare_notice_hours_and_none_when_not_before_departure():
    """준비 문구: 60분 이상이면 1시간 10분처럼, 출발과 같거나 뒤면 문구 없음"""
    assert (
        prepare_notice(prepare_at=kst(22, 470), depart_at=kst(22, 540), time_zone=TZ)
        == "오전 9:00 출발까지 1시간 10분"
    )
    assert (
        prepare_notice(prepare_at=kst(22, 480), depart_at=kst(22, 540), time_zone=TZ)
        == "오전 9:00 출발까지 1시간"
    )
    assert prepare_notice(prepare_at=kst(22, 540), depart_at=kst(22, 540), time_zone=TZ) is None
    assert prepare_notice(prepare_at=kst(22, 545), depart_at=kst(22, 540), time_zone=TZ) is None


def test_disabled_departure_drops_prepare_notice_and_wake_depart_sentence():
    """출발 알람이 꺼져 있으면 준비 문구 · 기상의 나갈 시각 문장이 빠짐 (출발 알람이 없는 일정)"""
    pkgs = by_type(build([row("WAKE", 480), row("PREPARE", 510), row("DEPARTURE", 540, is_enabled=False)]))
    assert pkgs["PREPARE"].get("notice") is None
    assert "DEPARTURE" not in pkgs
    assert pkgs["WAKE"]["voiceText"] == "좋은 아침이에요. 오늘 첫 일정은 오전 10시 전공 수업이에요."


@pytest.mark.parametrize("travel_minutes", [None, float("nan")])
def test_unknown_travel_only_drops_departure_notice(travel_minutes):
    """이동 시간을 모르면 출발 문구만 빠짐 (원본의 null · undefined · NaN — undefined는 파이썬에서 None과 같음)"""
    appt = AppointmentNotice(appointment_at=kst(22, 600), travel_minutes=travel_minutes, time_zone=TZ)
    pkgs = by_type(build(CHAIN, appt))
    assert pkgs["DEPARTURE"].get("notice") is None
    assert pkgs["PREPARE"]["notice"] == "오전 9:00 출발까지 30분"


def test_wake_voice_text_josa_and_spoken_minutes():
    """기상 음성 문장: 받침에 따라 이에요 · 예요, 분이 있으면 풀어 읽음"""
    assert (
        wake_voice_text(
            title="팀 주간회의", appointment_at=kst(22, 600), depart_at=kst(22, 520), time_zone=TZ
        )
        == "좋은 아침이에요. 오늘 첫 일정은 오전 10시 팀 주간회의예요. 오전 8시 40분에 나가면 돼요."
    )
    assert (
        wake_voice_text(title="전공 수업", appointment_at=kst(22, 870), depart_at=kst(22, 810), time_zone=TZ)
        == "좋은 아침이에요. 오늘 첫 일정은 오후 2시 30분 전공 수업이에요. 오후 1시 30분에 나가면 돼요."
    )
    assert (
        wake_voice_text(title="  ", appointment_at=kst(22, 600), time_zone=TZ)
        == "좋은 아침이에요. 오늘 첫 일정은 오전 10시 일정이에요."
    )


class _RangeError(ValueError):
    """JS RangeError 자리 — 시각 서식이 값을 못 읽고 던지는 오류"""


def test_text_failure_keeps_package_and_logs_one_line(monkeypatch, caplog):
    """문구를 못 만들어도(이상한 시각) 꾸러미는 그대로 만들어지고, 무엇이 실패했는지 한 줄 남김

    원본은 약속 시각을 NaN 날짜로 넘겨 Intl이 RangeError를 던지게 함 — 파이썬 datetime에는 NaN이 없어서
    기상 음성 문장의 시각 서식(format_ko_spoken_time)이 RangeError를 던지게 바꿔 같은 경로(try_text)를 시험함.
    그래서 원본과 달리 출발 문구(format_ko_time 사용)는 정상으로 남음.
    """

    def boom(*_args, **_kwargs):
        raise _RangeError("Invalid time value")

    monkeypatch.setattr(ring_package, "format_ko_spoken_time", boom)
    with caplog.at_level(logging.WARNING, logger="app.shared.ring_package"):
        pkgs = by_type(build(CHAIN))
    assert len(pkgs) == 4
    assert pkgs["WAKE"].get("voiceText") is None
    assert pkgs["PREPARE"]["notice"] == "오전 9:00 출발까지 30분"  # 약속 시각과 상관없는 문구는 남음
    # 던진 문구만 한 줄 — 알람 종류 · id · 오류만, 제목 · 장소 같은 사용자 글자는 없음
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1
    assert re.match(r"^\[ring-package\] WAKE 문구를 못 만들어", lines[0])
    assert re.search(r"알람 a-WAKE\)", lines[0])
    assert "_RangeError" in lines[0]
    assert not re.search("전공 수업|광개토관", lines[0])


def test_no_warning_when_text_is_made(caplog):
    """문구가 잘 만들어지면 경고를 남기지 않음"""
    with caplog.at_level(logging.WARNING, logger="app.shared.ring_package"):
        build(CHAIN)
    assert len(caplog.records) == 0


def test_copula_digits_brackets_and_other():
    """기상 음성 문장의 이에요 · 예요: 제목 끝이 숫자면 읽는 소리, 닫는 괄호면 괄호 앞 글자, 영문 · 기타는 받침 없음"""

    def sentence(title: str) -> str:
        text = wake_voice_text(title=title, appointment_at=kst(22, 600), time_zone=TZ)
        return re.sub(r"\.$", "", text.split("오전 10시 ")[1])

    # 숫자: 0 영 · 1 일 · 3 삼 · 6 육 · 7 칠 · 8 팔은 받침 있음, 2 · 4 · 5 · 9는 없음
    assert sentence("영어 1") == "영어 1이에요"
    assert sentence("캡스톤 2") == "캡스톤 2예요"
    for digit, batchim in {
        0: True,
        1: True,
        2: False,
        3: True,
        4: False,
        5: False,
        6: True,
        7: True,
        8: True,
        9: False,
    }.items():
        assert sentence(f"실습 {digit}") == f"실습 {digit}{'이에요' if batchim else '예요'}"
    assert sentence("실습 10") == "실습 10이에요"  # 십 · 끝자리 0은 늘 받침
    # 닫는 괄호: 괄호 안은 빼고 그 앞 글자로
    assert sentence("자료구조(월)") == "자료구조(월)예요"
    assert sentence("전공 수업(월)") == "전공 수업(월)이에요"
    assert sentence("영어 (3)") == "영어 (3)예요"  # 괄호 앞은 "영어"
    assert sentence("영어 2 (3)") == "영어 2 (3)예요"  # 괄호 앞 글자는 2
    assert sentence("영어 1 (월)") == "영어 1 (월)이에요"
    assert sentence("자료구조[월]") == "자료구조[월]예요"
    assert sentence("자료구조（월）") == "자료구조（월）예요"
    assert sentence("자료구조 (A반(월))") == "자료구조 (A반(월))예요"  # 겹친 괄호도 통째로 뺌
    assert sentence("자료구조(월)(화)") == "자료구조(월)(화)예요"  # 괄호가 이어져도 앞 글자로
    assert sentence("밥(월)(화)") == "밥(월)(화)이에요"  # 앞 글자 밥은 받침
    # 영문 · 기타는 지금처럼 받침 없음
    assert sentence("Capstone") == "Capstone예요"
    assert sentence("팀 회의!") == "팀 회의!예요"
    # 괄호만 있거나 짝이 안 맞으면 있는 그대로
    assert sentence("(월)") == "(월)예요"
    assert sentence("밥)") == "밥)예요"


def test_packages_keep_fields_and_are_time_sorted_regardless_of_input_order():
    """문구가 붙어도 제목 · 부제 · 출발 시각 칸은 그대로이고, 들어온 순서와 상관없이 같은 꾸러미 (시각순)"""
    from app.shared.js import ms

    wake, prepare, departure, arrive = build(CHAIN)
    assert [
        [p["type"], p["title"], p["subtitle"], p["departAt"]] for p in (wake, prepare, departure, arrive)
    ] == [
        [t, "전공 수업", "오전 10:00 · 광개토관", ms(kst(22, 540))]
        for t in ("WAKE", "PREPARE", "DEPARTURE", "ARRIVE")
    ]
    assert build(list(reversed(CHAIN))) == build(CHAIN)  # 들어온 순서와 상관없이 시각순


def test_departure_notice_alone():
    """출발 문구 단독"""
    assert (
        departure_notice(depart_at=kst(22, 540), appointment_at=kst(22, 600), travel_minutes=50, time_zone=TZ)
        == "지금 나가면 오전 9:50 도착 · 여유 10분"
    )


def test_axes_disabled_ignores_stored_axes_and_follows_intensity():
    """축별 선택을 끄면(axesEnabled false) 저장된 소리 · 진동 · 말소리를 무시하고 강도를 따름 — 켜면 저장된 값이 그대로 살아 있음 (D1 A flag, 헤드 확정 10/30)"""
    stored = {"sound_key": "theme_urgent", "vibration": "NONE", "voice_tone": "DIRECT", "intensity": "NORMAL"}
    assert axes_of(stored, True) == {"soundKey": "theme_urgent", "vibration": "NONE", "voiceTone": "DIRECT"}
    assert axes_of(stored, False) == {
        "soundKey": "theme_default",
        "vibration": "SHORT_REPEAT",
        "voiceTone": None,
    }
    assert axes_of({**stored, "intensity": "STRONG"}, False) == {
        "soundKey": "theme_urgent",
        "vibration": "STRONG_CONTINUOUS",
        "voiceTone": None,
    }

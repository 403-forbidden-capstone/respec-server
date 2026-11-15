"""alarm-roll · appointment-edit 경우"""

from __future__ import annotations

from datetime import datetime, timedelta

import fx
from fx import A, B, at
from harness import Case

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def when_plus(base: str, days: int) -> str:
    return (datetime.fromisoformat(base) + timedelta(days=days)).isoformat()


def series_rows(
    user: str,
    first_n: int,
    first_when: str,
    rule: str | None,
    *,
    count: int = 1,
    step_days: int = 1,
    group: str | None = None,
    travel: int = 40,
    kinds: tuple[str, ...] = ("WAKE", "PREPARE", "DEPARTURE", "ARRIVE"),
    canceled: tuple[int, ...] = (),
) -> str:
    """반복 묶음: 첫 회차 id가 series_id. 회차 count개(step_days 간격)를 약속 · 참여 · 알람까지 넣음. canceled = 이번만 지운 회차 번호(0부터)"""
    out = ""
    for i in range(count):
        n = first_n + i
        when = when_plus(first_when, i * step_days)
        out += fx.appointment(
            n,
            user,
            when,
            rule=rule,
            series=first_n,
            group=group,
            exception="CANCELED" if i in canceled else None,
        )
        out += fx.participant(n, n, user, est=travel)
        out += fx.full_set(1000 + n * 10, n, n, user, when, travel=travel, kinds=kinds)
    return out


def standalone_series(user: str, first_n: int, first_when: str, rule: str, *, count: int = 1) -> str:
    """일정 없는 반복 알람의 묶음 — 알람 행이 회차 하나씩, series_id · occurrence_on 이 있음"""
    out = ""
    for i in range(count):
        when = when_plus(first_when, i)
        out += fx.alarm(
            first_n + i, None, user, "WAKE", when, rule=rule, series=first_n, occurrence=fx.kst_date(when)
        )
    return out


def roll_cases() -> list[Case]:
    t1 = at(1, 9)
    weekday = WEEKDAYS[datetime.fromisoformat(t1).weekday()]
    weekly = f"FREQ=WEEKLY;BYDAY={weekday}"
    two_days = f"FREQ=WEEKLY;BYDAY={WEEKDAYS[datetime.fromisoformat(t1).weekday()]},{WEEKDAYS[(datetime.fromisoformat(t1).weekday() + 3) % 7]}"
    return [
        Case(
            "roll/본인 길 · 매일 반복 하나(14일치 만듦)",
            "alarm-roll",
            "A",
            {},
            setup=series_rows(A, 1, t1, "FREQ=DAILY"),
        ),
        Case("roll/본인 길 · 주간 반복", "alarm-roll", "A", {}, setup=series_rows(A, 1, t1, weekly)),
        Case("roll/본인 길 · 주 2회 반복", "alarm-roll", "A", {}, setup=series_rows(A, 1, t1, two_days)),
        Case(
            "roll/본인 길 · 이미 회차가 여러 개 있음",
            "alarm-roll",
            "A",
            {},
            setup=series_rows(A, 1, t1, "FREQ=DAILY", count=5),
        ),
        Case(
            "roll/본인 길 · 이미 14일치 다 있음(아무것도 안 만듦)",
            "alarm-roll",
            "A",
            {},
            setup=series_rows(A, 1, t1, "FREQ=DAILY", count=15),
        ),
        Case(
            "roll/본인 길 · 기상 없는 묶음(준비 · 출발 · 도착만)",
            "alarm-roll",
            "A",
            {},
            setup=series_rows(A, 1, t1, "FREQ=DAILY", kinds=("PREPARE", "DEPARTURE", "ARRIVE")),
        ),
        Case(
            "roll/본인 길 · 알람 없는 일정 묶음",
            "alarm-roll",
            "A",
            {},
            setup=series_rows(A, 1, t1, "FREQ=DAILY", kinds=()),
        ),
        Case(
            "roll/본인 길 · 이번만 지운 회차(CANCELED)는 다시 안 만듦",
            "alarm-roll",
            "A",
            {},
            setup=series_rows(A, 1, t1, "FREQ=DAILY", count=3, canceled=(1,)),
        ),
        Case(
            "roll/본인 길 · series_id를 지정",
            "alarm-roll",
            "A",
            {"series_id": fx.uid(1)},
            setup=series_rows(A, 1, t1, "FREQ=DAILY") + series_rows(A, 50, at(1, 15), "FREQ=DAILY"),
        ),
        Case(
            "roll/본인 길 · 남의 series_id(아무것도 안 만듦)",
            "alarm-roll",
            "A",
            {"series_id": fx.uid(60)},
            setup=series_rows(A, 1, t1, "FREQ=DAILY") + series_rows(B, 60, at(1, 15), "FREQ=DAILY"),
        ),
        Case(
            "roll/본인 길 · 묶음이 둘(내 것만)",
            "alarm-roll",
            "A",
            {},
            setup=series_rows(A, 1, t1, "FREQ=DAILY") + series_rows(B, 60, at(1, 15), "FREQ=DAILY"),
        ),
        Case(
            "roll/본인 길 · 일정 없는 반복 알람",
            "alarm-roll",
            "A",
            {},
            setup=standalone_series(A, 700, t1, "FREQ=DAILY"),
        ),
        Case(
            "roll/본인 길 · 일정 없는 주간 반복 알람",
            "alarm-roll",
            "A",
            {},
            setup=standalone_series(A, 700, t1, weekly),
        ),
        Case(
            "roll/본인 길 · 반복 아닌 약속(아무것도 안 만듦)",
            "alarm-roll",
            "A",
            {},
            setup=series_rows(A, 1, t1, None),
        ),
        Case(
            "roll/본인 길 · 사용자 C(뉴욕 시간대)",
            "alarm-roll",
            "C",
            {},
            setup=series_rows("{C}", 1, at(1, 9), "FREQ=DAILY", travel=30),
        ),
        Case(
            "roll/본인 길 · 직접 입력 이동이 아닌 묶음(API → 길찾기 없이 근사)",
            "alarm-roll",
            "A",
            {},
            setup=series_rows(A, 1, t1, "FREQ=DAILY")
            + "update public.appointment_participants set travel_source = 'API';\n",
        ),
        Case(
            "roll/본인 길 · 같은 호출 두 번(둘째는 아무것도 안 만듦)",
            "alarm-roll",
            "A",
            {},
            setup=series_rows(A, 1, t1, "FREQ=DAILY"),
            then=[("alarm-roll", "A", {})],
        ),
        Case("roll/본인 길 · 검증(series_id가 uuid 아님, 400)", "alarm-roll", "A", {"series_id": "abc"}),
        Case("roll/본인 길 · 인증 없음(401)", "alarm-roll", None, {}),
        Case(
            "roll/배치 · 서비스 키(사용자 둘의 묶음)",
            "alarm-roll",
            None,
            None,
            service=True,
            setup=series_rows(A, 1, t1, "FREQ=DAILY") + series_rows(B, 60, at(1, 15), weekly),
        ),
        Case("roll/배치 · 서비스 키(할 일이 없음)", "alarm-roll", None, None, service=True),
        Case(
            "roll/배치 · 서비스 키(일정 없는 알람 + 약속)",
            "alarm-roll",
            None,
            None,
            service=True,
            setup=series_rows(A, 1, t1, "FREQ=DAILY", count=2) + standalone_series(B, 700, t1, "FREQ=DAILY"),
        ),
        Case(
            "roll/배치 · 서비스 키 두 번",
            "alarm-roll",
            None,
            None,
            service=True,
            setup=series_rows(A, 1, t1, "FREQ=DAILY"),
            then=[("alarm-roll", None, None)],
        ),
    ]


def single(user: str = A, when: str | None = None, appt: int = 1, **kw: object) -> str:
    when = when or at(3, 10)
    return (
        fx.appointment(appt, user, when)
        + fx.participant(appt, appt, user)
        + fx.full_set(100 + appt * 10, appt, appt, user, when, **kw)
    )  # type: ignore[arg-type]


def edit_cases() -> list[Case]:
    t3 = at(3, 10)
    sid = fx.uid(1)
    series = series_rows(A, 1, at(2, 9), "FREQ=DAILY", count=4)
    group_setup = (
        fx.group(50, A, [(51, A, "JOINED"), (52, B, "JOINED")])
        + fx.appointment(3, A, t3, group=fx.uid(50), title="모임")
        + fx.participant(31, 3, A)
        + fx.full_set(330, 3, 31, A, t3)
        + fx.participant(32, 3, B, origin="B집", olat=37.56, olng=126.99, est=30)
        + fx.full_set(340, 3, 32, B, t3, travel=30)
    )
    same_day = (
        single(A, at(3, 9), 1)
        + fx.appointment(2, A, at(3, 14), title="오후")
        + fx.participant(2, 2, A)
        + fx.full_set(200, 2, 2, A, at(3, 14), kinds=("PREPARE", "DEPARTURE", "ARRIVE"), prepare=15)
    )

    def upd(patch: dict, appt: int = 1, scope: str = "this", **extra: object) -> dict:
        return {"action": "UPDATE", "appointment_id": fx.uid(appt), "scope": scope, "patch": patch, **extra}

    def dele(appt: int = 1, scope: str = "this") -> dict:
        return {"action": "DELETE", "appointment_id": fx.uid(appt), "scope": scope}

    return [
        Case(
            "edit/수정 · 제목만(알람 그대로)",
            "appointment-edit",
            "A",
            upd({"title": "새 제목"}),
            setup=single(),
        ),
        Case(
            "edit/수정 · 중요도만(강도 다시 계산)",
            "appointment-edit",
            "A",
            upd({"importance": "HIGH"}),
            setup=single(),
        ),
        Case(
            "edit/수정 · 시각 변경(알람 다시 계산)",
            "appointment-edit",
            "A",
            upd({"appointment_at": at(3, 11, 30)}),
            setup=single(),
        ),
        Case(
            "edit/수정 · 시각을 다른 날로(기상 판정 다시)",
            "appointment-edit",
            "A",
            upd({"appointment_at": at(5, 9)}),
            setup=single(),
        ),
        Case(
            "edit/수정 · 장소 변경(좌표 있음, 직접 이동 유지)",
            "appointment-edit",
            "A",
            upd({"destination": {"name": "건대", "lat": 37.5404, "lng": 127.0693}}),
            setup=single(),
        ),
        Case(
            "edit/수정 · 장소 변경(좌표 없음)",
            "appointment-edit",
            "A",
            upd({"destination": {"name": "이름만"}}),
            setup=single(),
        ),
        Case(
            "edit/수정 · 시각 + 장소 + 제목",
            "appointment-edit",
            "A",
            upd(
                {
                    "title": "복합",
                    "appointment_at": at(3, 15),
                    "destination": {"name": "건대", "lat": 37.5404, "lng": 127.0693},
                }
            ),
            setup=single(),
        ),
        Case(
            "edit/수정 · 시각을 지난 때로(422)",
            "appointment-edit",
            "A",
            upd({"appointment_at": fx.from_now(20)}),
            setup=single(),
        ),
        Case(
            "edit/수정 · 같은 값(바뀐 게 없음)",
            "appointment-edit",
            "A",
            upd({"title": "수업"}),
            setup=single(),
        ),
        Case(
            "edit/수정 · 직접 고른 시각이 있던 알람",
            "appointment-edit",
            "A",
            upd({"appointment_at": at(3, 11)}),
            setup=single(time_source="MANUAL"),
        ),
        Case(
            "edit/수정 · 직접 고른 강도가 있던 알람",
            "appointment-edit",
            "A",
            upd({"importance": "HIGH"}),
            setup=single(
                intensity_source="MANUAL",
                intensity="SOFT",
                voice="CALM",
                sound="theme_calm",
                vibration="NONE",
            ),
        ),
        Case(
            "edit/수정 · 반복 규칙 붙이기(한 번짜리 → 반복)",
            "appointment-edit",
            "A",
            upd({"recurrence_rule": "FREQ=DAILY"}),
            setup=single(),
        ),
        Case(
            "edit/수정 · 반복 규칙 바꾸기",
            "appointment-edit",
            "A",
            upd({"recurrence_rule": "FREQ=WEEKLY;BYDAY=MO"}, scope="following"),
            setup=series,
        ),
        Case(
            "edit/수정 · 같은 날 앞 일정이 생김(7.1.3 다시 판정)",
            "appointment-edit",
            "A",
            upd({"appointment_at": at(3, 8)}, appt=2),
            setup=same_day,
        ),
        Case(
            "edit/수정 · 같은 날 앞 일정을 뒤로 미룸",
            "appointment-edit",
            "A",
            upd({"appointment_at": at(3, 16)}, appt=1),
            setup=same_day,
        ),
        Case(
            "edit/수정 · 반복 · 이번만",
            "appointment-edit",
            "A",
            upd({"title": "이번만 제목", "appointment_at": at(3, 10)}, appt=2, scope="this"),
            setup=series,
        ),
        Case(
            "edit/수정 · 반복 · 이후 모두",
            "appointment-edit",
            "A",
            upd({"title": "이후 제목", "appointment_at": at(3, 10, 30)}, appt=2, scope="following"),
            setup=series,
        ),
        Case(
            "edit/수정 · 반복 · 이후 모두 · 첫 회차",
            "appointment-edit",
            "A",
            upd({"importance": "HIGH"}, appt=1, scope="following"),
            setup=series,
        ),
        Case(
            "edit/수정 · 한 번짜리에 scope following(같은 결과)",
            "appointment-edit",
            "A",
            upd({"title": "x"}, scope="following"),
            setup=single(),
        ),
        Case(
            "edit/수정 · 공동 약속 · 만든 사람이 시각 변경(참여자 재계산)",
            "appointment-edit",
            "A",
            upd({"appointment_at": at(3, 11)}, appt=3),
            setup=group_setup,
        ),
        Case(
            "edit/수정 · 공동 약속 · confirmed=true",
            "appointment-edit",
            "A",
            upd({"appointment_at": at(3, 11)}, appt=3, confirmed=True),
            setup=group_setup,
        ),
        Case(
            "edit/수정 · 공동 약속 · 참여자가 시각 변경(거절)",
            "appointment-edit",
            "B",
            upd({"appointment_at": at(3, 11)}, appt=3),
            setup=group_setup,
        ),
        Case(
            "edit/수정 · 공동 약속 · 참여자가 제목 변경",
            "appointment-edit",
            "B",
            upd({"title": "참여자 제목"}, appt=3),
            setup=group_setup,
        ),
        Case(
            "edit/수정 · 남의 약속(404)",
            "appointment-edit",
            "A",
            upd({"title": "x"}, appt=2),
            setup=single("{B}", appt=2),
        ),
        Case("edit/수정 · 없는 id(404)", "appointment-edit", "A", upd({"title": "x"}, appt=99)),
        Case("edit/수정 · patch가 비어 있음(400)", "appointment-edit", "A", upd({}), setup=single()),
        Case(
            "edit/수정 · patch 없음(400)",
            "appointment-edit",
            "A",
            {"action": "UPDATE", "appointment_id": sid},
            setup=single(),
        ),
        Case(
            "edit/수정 · 반복 규칙 MONTHLY(400)",
            "appointment-edit",
            "A",
            upd({"recurrence_rule": "FREQ=MONTHLY"}),
            setup=single(),
        ),
        Case("edit/수정 · 제목이 빈 글자(400)", "appointment-edit", "A", upd({"title": ""}), setup=single()),
        Case("edit/삭제 · 한 번짜리", "appointment-edit", "A", dele(), setup=single()),
        Case(
            "edit/삭제 · 같은 날 앞 일정을 지움(7.1.3 기상 다시)",
            "appointment-edit",
            "A",
            dele(1),
            setup=same_day,
        ),
        Case(
            "edit/삭제 · 반복 · 이번만(CANCELED로 표시)",
            "appointment-edit",
            "A",
            dele(2, "this"),
            setup=series,
        ),
        Case("edit/삭제 · 반복 · 이후 모두", "appointment-edit", "A", dele(2, "following"), setup=series),
        Case(
            "edit/삭제 · 반복 · 첫 회차 이후 모두(묶음 전체)",
            "appointment-edit",
            "A",
            dele(1, "following"),
            setup=series,
        ),
        Case("edit/삭제 · 공동 약속 · 만든 사람", "appointment-edit", "A", dele(3), setup=group_setup),
        Case("edit/삭제 · 공동 약속 · 참여자(거절)", "appointment-edit", "B", dele(3), setup=group_setup),
        Case("edit/삭제 · 남의 약속(404)", "appointment-edit", "A", dele(2), setup=single("{B}", appt=2)),
        Case("edit/삭제 · 없는 id(404)", "appointment-edit", "A", dele(99)),
        Case(
            "edit/삭제 · scope 잘못(400)",
            "appointment-edit",
            "A",
            {"action": "DELETE", "appointment_id": sid, "scope": "all"},
            setup=single(),
        ),
        Case(
            "edit/삭제 후 복원 · 이번만 지운 회차",
            "appointment-edit",
            "A",
            {"action": "RESTORE", "appointment_id": fx.uid(2)},
            setup=series_rows(A, 1, at(2, 9), "FREQ=DAILY", count=4, canceled=(1,)),
        ),
        Case(
            "edit/복원 · 지운 적 없는 회차",
            "appointment-edit",
            "A",
            {"action": "RESTORE", "appointment_id": fx.uid(2)},
            setup=series,
        ),
        Case(
            "edit/복원 · 한 번짜리",
            "appointment-edit",
            "A",
            {"action": "RESTORE", "appointment_id": sid},
            setup=single(),
        ),
        Case(
            "edit/복원 · 남의 약속(404)",
            "appointment-edit",
            "A",
            {"action": "RESTORE", "appointment_id": fx.uid(2)},
            setup=single("{B}", appt=2),
        ),
        Case(
            "edit/나가기 · 공동 약속 참여자",
            "appointment-edit",
            "B",
            {"action": "LEAVE", "appointment_id": fx.uid(3)},
            setup=group_setup,
        ),
        Case(
            "edit/나가기 · 만든 사람(거절)",
            "appointment-edit",
            "A",
            {"action": "LEAVE", "appointment_id": fx.uid(3)},
            setup=group_setup,
        ),
        Case(
            "edit/나가기 · 개인 약속(거절)",
            "appointment-edit",
            "A",
            {"action": "LEAVE", "appointment_id": sid},
            setup=single(),
        ),
        Case(
            "edit/나가기 · 모임 밖 사람(404)",
            "appointment-edit",
            "C",
            {"action": "LEAVE", "appointment_id": fx.uid(3)},
            setup=group_setup,
        ),
        Case(
            "edit/검증 · action 잘못(400)",
            "appointment-edit",
            "A",
            {"action": "MOVE", "appointment_id": sid},
            setup=single(),
        ),
        Case("edit/검증 · appointment_id 없음(400)", "appointment-edit", "A", {"action": "DELETE"}),
        Case(
            "edit/검증 · appointment_id가 uuid 아님(400)",
            "appointment-edit",
            "A",
            {"action": "DELETE", "appointment_id": "abc"},
        ),
        Case("edit/인증 · 토큰 없음(401)", "appointment-edit", None, dele(), setup=single()),
    ]

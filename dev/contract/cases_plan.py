"""alarm-suggest · alarm-commit 경우 — 같은 요청 · 같은 시작 상태를 Edge와 서버에 넣어 견줌"""

from __future__ import annotations

import fx
from fx import A, B, at, from_now
from harness import Case

SEJONG = {"name": "세종대", "lat": 37.5509, "lng": 127.0736}
NO_COORDS = {"name": "이름만 있는 곳"}
MANUAL40 = {"est_travel_minutes": 40, "travel_source": "MANUAL"}


def new_appt(
    when: str,
    *,
    dest: dict | None = None,
    importance: str = "NORMAL",
    title: str | None = None,
    **extra: object,
) -> dict:
    appt = {"appointment_at": when, "destination": dest or SEJONG, "importance": importance, **extra}
    if title is not None:
        appt["title"] = title
    return appt


def existing_single(
    when: str, *, user: str = A, appt: int = 1, source: str = "MANUAL", est: int | None = 40, **kw: object
) -> str:
    """내 약속 하나 + 참여 조건 + 알람 네 개 (직접 입력한 이동 40분이 저장돼 있음)"""
    return (
        fx.appointment(appt, user, when)
        + fx.participant(appt, appt, user, source=source, est=est)
        + fx.full_set(100 + appt * 10, appt, appt, user, when, travel=est or 40, **kw)  # type: ignore[arg-type]
    )


def history_samples(user: str = A, count: int = 3, base: int = 200) -> str:
    """같은 경로(출발 · 도착 300m 안)의 지난 실측 이동 기록 — CACHED(내 기록 중앙값) 후보"""
    out = ""
    for i in range(count):
        when = at(-(7 + i), 10)
        out += fx.appointment(base + i, user, when, title=f"지난 수업 {i}")
        dep = fx.minus(when, 50 + i * 2)
        arr = fx.minus(when, 10)
        out += fx.participant(
            base + i, base + i, user, departed=dep, arrived=arr, actual=40 + i * 2, late=False
        )
    return out


def prepare_history(
    user: str = A, values: tuple[int, ...] = (35, 40, 45, 50, 55, 60, 38, 42, 47, 52, 58, 44), base: int = 300
) -> str:
    return "".join(fx.daily_record(base + i, user, i + 1, v) for i, v in enumerate(values))


def suggest_cases() -> list[Case]:
    t3 = at(3, 10)
    cases: list[Case] = [
        Case(
            "suggest/새 약속 · 직접 입력 이동",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3), "participant": MANUAL40},
        ),
        Case(
            "suggest/새 약속 · 좌표 없음 · 직접 입력 없음(200 manual_required)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3, dest=NO_COORDS)},
        ),
        Case(
            "suggest/새 약속 · 좌표 있고 키 없음(근사 ESTIMATE)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3)},
        ),
        Case(
            "suggest/새 약속 · 출발지를 좌표로 지정",
            "alarm-suggest",
            "A",
            {
                "appointment": new_appt(t3),
                "participant": {
                    "origin": {"name": "카페", "lat": 37.5601, "lng": 126.9822},
                    "transport_type": "WALK",
                },
            },
        ),
        Case(
            "suggest/새 약속 · 출발지 이름만(집 별칭)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3), "participant": {"origin": {"name": "집"}}},
        ),
        Case(
            "suggest/새 약속 · 중요도 HIGH + 축별 강도",
            "alarm-suggest",
            "A",
            {
                "appointment": new_appt(t3, importance="HIGH"),
                "participant": MANUAL40,
                "levels": {"sound": 0, "voice": 2, "vibration": None},
            },
        ),
        Case(
            "suggest/새 약속 · 내 실측 3건(CACHED 중앙값)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3), "participant": {"transport_type": "TRANSIT"}},
            setup=history_samples(),
        ),
        Case(
            "suggest/새 약속 · 준비 기록 12건(개인 P80)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3), "participant": MANUAL40},
            setup=prepare_history(),
        ),
        Case(
            "suggest/새 약속 · 준비 기록 3건(배우는 중)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3), "participant": MANUAL40},
            setup=prepare_history(values=(40, 55, 48)),
        ),
        Case(
            "suggest/새 약속 · 사용자 B(잠귀 무거움 · 여유 · 준비 40 · LOOSE)",
            "alarm-suggest",
            "B",
            {"appointment": new_appt(t3), "participant": MANUAL40},
            setup=prepare_history("{B}", base=400),
        ),
        Case(
            "suggest/새 약속 · 사용자 C(뉴욕 시간대)",
            "alarm-suggest",
            "C",
            {
                "appointment": new_appt(at(3, 9), dest={"name": "뉴욕", "lat": 40.7, "lng": -74.0}),
                "participant": {
                    "est_travel_minutes": 30,
                    "travel_source": "MANUAL",
                    "origin": {"name": "집", "lat": 40.71, "lng": -74.01},
                },
            },
        ),
        Case(
            "suggest/새 약속 · 저녁 일정(기상 없음)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(at(3, 20)), "participant": MANUAL40},
        ),
        Case(
            "suggest/새 약속 · 이른 아침(새벽 1시)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(at(3, 1)), "participant": MANUAL40},
        ),
        Case(
            "suggest/새 약속 · 같은 날 앞 일정에 기상이 있음",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(at(3, 14)), "participant": MANUAL40},
            setup=existing_single(at(3, 9)),
        ),
        Case(
            "suggest/새 약속 · 같은 날 앞 일정의 기상이 꺼져 있음",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(at(3, 14)), "participant": MANUAL40},
            setup=existing_single(at(3, 9), enabled=False),
        ),
        Case(
            "suggest/새 약속 · kinds 일부 · enabled",
            "alarm-suggest",
            "A",
            {
                "appointment": new_appt(t3),
                "participant": MANUAL40,
                "kinds": ["WAKE", "DEPARTURE"],
                "enabled": {"WAKE": False},
            },
        ),
        Case(
            "suggest/새 약속 · kinds 빈 목록",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3), "participant": MANUAL40, "kinds": []},
        ),
        Case(
            "suggest/새 약속 · 직접 고른 시각(manual_times)",
            "alarm-suggest",
            "A",
            {
                "appointment": new_appt(t3),
                "participant": MANUAL40,
                "manual_times": {"WAKE": at(3, 7), "DEPARTURE": at(3, 9, 5)},
            },
        ),
        Case(
            "suggest/새 약속 · 직접 시각 순서 어긋남(200 check.ok=false)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3), "participant": MANUAL40, "manual_times": {"WAKE": at(3, 9, 30)}},
        ),
        Case(
            "suggest/새 약속 · 준비 · 준비 전 활동 직접 지정",
            "alarm-suggest",
            "A",
            {
                "appointment": new_appt(t3),
                "participant": {
                    **MANUAL40,
                    "prepare_minutes": 25,
                    "before_prepare_minutes": 5,
                    "buffer_minutes": 0,
                },
            },
        ),
        Case(
            "suggest/새 약속 · with_wake 강제",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(at(3, 20)), "participant": {**MANUAL40, "with_wake": True}},
        ),
        Case(
            "suggest/새 약속 · 출발 시각이 이미 지남(422)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(from_now(20)), "participant": MANUAL40},
        ),
        Case(
            "suggest/새 약속 · 기상 · 준비만 지남(꺼짐 PASSED)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(from_now(90)), "participant": MANUAL40},
        ),
        Case(
            "suggest/기존 약속 · 저장된 직접 이동 이어받기",
            "alarm-suggest",
            "A",
            {"appointment_id": fx.uid(1)},
            setup=existing_single(t3),
        ),
        Case(
            "suggest/기존 약속 · 저장된 직접 시각 · 강도 유지",
            "alarm-suggest",
            "A",
            {"appointment_id": fx.uid(1)},
            setup=existing_single(
                t3,
                time_source="MANUAL",
                intensity_source="MANUAL",
                intensity="SOFT",
                voice="CALM",
                sound="theme_calm",
                vibration="NONE",
            ),
        ),
        Case(
            "suggest/기존 약속 · 이동을 바꿔 다시 계산",
            "alarm-suggest",
            "A",
            {
                "appointment_id": fx.uid(1),
                "participant": {"est_travel_minutes": 70, "travel_source": "MANUAL", "transport_type": "CAR"},
            },
            setup=existing_single(t3),
        ),
        Case(
            "suggest/기존 약속 · 저장된 이동이 API(다시 구함)",
            "alarm-suggest",
            "A",
            {"appointment_id": fx.uid(1)},
            setup=existing_single(t3, source="API"),
        ),
        Case(
            "suggest/기존 약속 · 준비 당김(prepare_advance) 있음",
            "alarm-suggest",
            "A",
            {"appointment_id": fx.uid(1)},
            setup=existing_single(t3).replace("prepare_advance_minutes", "prepare_advance_minutes")
            + "update public.appointment_participants set prepare_advance_minutes = 10;\n",
        ),
        Case(
            "suggest/기존 약속 · 내 것이 아님(404)",
            "alarm-suggest",
            "A",
            {"appointment_id": fx.uid(2)},
            setup=existing_single(t3, user=B, appt=2),
        ),
        Case("suggest/기존 약속 · 없는 id(404)", "alarm-suggest", "A", {"appointment_id": fx.uid(99)}),
        Case(
            "suggest/기존 약속 · 참여 조건 없음",
            "alarm-suggest",
            "A",
            {"appointment_id": fx.uid(1)},
            setup=fx.appointment(1, A, t3),
        ),
        Case(
            "suggest/공동 약속 · 내가 참여자",
            "alarm-suggest",
            "A",
            {"appointment_id": fx.uid(3)},
            setup=fx.group(50, B, [(51, B, "JOINED"), (52, A, "JOINED")])
            + fx.appointment(3, B, t3, group=fx.uid(50))
            + fx.participant(31, 3, B)
            + fx.participant(32, 3, A, source="MANUAL", est=35),
        ),
        Case(
            "suggest/검증 · appointment와 appointment_id 둘 다(400)",
            "alarm-suggest",
            "A",
            {"appointment_id": fx.uid(1), "appointment": new_appt(t3)},
            setup=existing_single(t3),
        ),
        Case("suggest/검증 · 둘 다 없음(400)", "alarm-suggest", "A", {}),
        Case(
            "suggest/검증 · destination 없음(400)",
            "alarm-suggest",
            "A",
            {"appointment": {"appointment_at": t3}},
        ),
        Case(
            "suggest/검증 · 좌표 한쪽만(400)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3, dest={"name": "x", "lat": 37.5})},
        ),
        Case("suggest/검증 · 날짜 모양(400)", "alarm-suggest", "A", {"appointment": new_appt("내일 열 시")}),
        Case(
            "suggest/검증 · levels 범위 밖(400)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3), "participant": MANUAL40, "levels": {"sound": 5}},
        ),
        Case(
            "suggest/검증 · est만 보냄(400)",
            "alarm-suggest",
            "A",
            {"appointment": new_appt(t3), "participant": {"est_travel_minutes": 40}},
        ),
        Case("suggest/검증 · 몸통이 배열(400)", "alarm-suggest", "A", [1, 2]),
        Case(
            "suggest/인증 · 토큰 없음(401)",
            "alarm-suggest",
            None,
            {"appointment": new_appt(t3), "participant": MANUAL40},
        ),
    ]
    return cases


def commit_cases() -> list[Case]:
    t3 = at(3, 10)

    def new(**extra: object) -> dict:
        return {"appointment": new_appt(t3, title="계약 약속"), "participant": MANUAL40, **extra}

    cases: list[Case] = [
        Case("commit/새 약속 · 직접 입력 이동", "alarm-commit", "A", new()),
        Case(
            "commit/새 약속 · 반복 주간(첫 회차만)",
            "alarm-commit",
            "A",
            {
                "appointment": new_appt(t3, title="반복 수업", recurrence_rule="FREQ=WEEKLY;BYDAY=TU,TH"),
                "participant": MANUAL40,
            },
        ),
        Case(
            "commit/새 약속 · 반복 매일",
            "alarm-commit",
            "A",
            {
                "appointment": new_appt(t3, title="매일", recurrence_rule="FREQ=DAILY"),
                "participant": MANUAL40,
            },
        ),
        Case(
            "commit/새 약속 · kinds 일부 + enabled 끔",
            "alarm-commit",
            "A",
            new(kinds=["PREPARE", "DEPARTURE"], enabled={"PREPARE": False}),
        ),
        Case("commit/새 약속 · kinds 빈 목록(알람 없는 일정)", "alarm-commit", "A", new(kinds=[])),
        Case(
            "commit/새 약속 · 직접 시각 + 고친 값 기록(edits)",
            "alarm-commit",
            "A",
            new(
                manual_times={"WAKE": at(3, 7, 15)},
                edits=[
                    {
                        "screen": "H05",
                        "field": "scheduled_at",
                        "alarm_type": "WAKE",
                        "auto_value": at(3, 8, 10),
                        "edited_value": at(3, 7, 15),
                    },
                    {"screen": "H03", "field": "transport", "auto_value": "TRANSIT", "edited_value": "WALK"},
                ],
            ),
        ),
        Case(
            "commit/새 약속 · 축별 강도 · 중요도 HIGH",
            "alarm-commit",
            "A",
            {
                "appointment": new_appt(t3, title="중요", importance="HIGH"),
                "participant": MANUAL40,
                "levels": {"sound": 2, "voice": 0, "vibration": 1},
            },
        ),
        Case(
            "commit/새 약속 · 저녁 일정(기상 없음)",
            "alarm-commit",
            "A",
            {"appointment": new_appt(at(3, 20), title="저녁"), "participant": MANUAL40},
        ),
        Case(
            "commit/새 약속 · 같은 날 앞 일정이 있음",
            "alarm-commit",
            "A",
            {"appointment": new_appt(at(3, 14), title="오후"), "participant": MANUAL40},
            setup=existing_single(at(3, 9)),
        ),
        Case(
            "commit/새 약속 · 사용자 B",
            "alarm-commit",
            "B",
            {"appointment": new_appt(t3, title="B 약속"), "participant": MANUAL40},
        ),
        Case(
            "commit/새 약속 · 사용자 C(뉴욕)",
            "alarm-commit",
            "C",
            {
                "appointment": new_appt(
                    at(3, 9), title="C 약속", dest={"name": "뉴욕", "lat": 40.7, "lng": -74.0}
                ),
                "participant": {"est_travel_minutes": 30, "travel_source": "MANUAL"},
            },
        ),
        Case(
            "commit/새 약속 · 공동 약속(모임 소속)",
            "alarm-commit",
            "A",
            {"appointment": new_appt(t3, title="모임", group_id=fx.uid(50)), "participant": MANUAL40},
            setup=fx.group(50, A, [(51, A, "JOINED"), (52, B, "JOINED")]),
        ),
        Case(
            "commit/새 약속 · 모임에 속하지 않음(group_id)",
            "alarm-commit",
            "A",
            {"appointment": new_appt(t3, title="모임", group_id=fx.uid(50)), "participant": MANUAL40},
            setup=fx.group(50, B, [(51, B, "JOINED")]),
        ),
        Case(
            "commit/새 약속 · 목적지 반경(radius_m)",
            "alarm-commit",
            "A",
            {
                "appointment": {**new_appt(t3, title="반경"), "destination": {**SEJONG, "radius_m": 250}},
                "participant": MANUAL40,
            },
        ),
        Case(
            "commit/새 약속 · 좌표 없음 · 직접 이동 없음(502)",
            "alarm-commit",
            "A",
            {"appointment": new_appt(t3, dest=NO_COORDS, title="좌표 없음")},
        ),
        Case(
            "commit/새 약속 · 좌표 있고 키 없음(근사라 502 fallback)",
            "alarm-commit",
            "A",
            {"appointment": new_appt(t3, title="근사")},
        ),
        Case(
            "commit/새 약속 · 출발 시각 지남(422)",
            "alarm-commit",
            "A",
            {"appointment": new_appt(from_now(20), title="늦음"), "participant": MANUAL40},
        ),
        Case(
            "commit/새 약속 · 직접 시각 순서 어긋남(422 ORDER)",
            "alarm-commit",
            "A",
            new(manual_times={"WAKE": at(3, 9, 30)}),
        ),
        Case(
            "commit/새 약속 · 기상 · 준비만 지남(꺼서 저장)",
            "alarm-commit",
            "A",
            {"appointment": new_appt(from_now(90), title="촉박"), "participant": MANUAL40},
        ),
        Case(
            "commit/검증 · 제목 없음(400)",
            "alarm-commit",
            "A",
            {"appointment": new_appt(t3), "participant": MANUAL40},
        ),
        Case(
            "commit/검증 · 반복 규칙 MONTHLY(400)",
            "alarm-commit",
            "A",
            {
                "appointment": new_appt(t3, title="월", recurrence_rule="FREQ=MONTHLY"),
                "participant": MANUAL40,
            },
        ),
        Case(
            "commit/검증 · 둘 다(appointment · appointment_id)(400)",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "appointment": new_appt(t3, title="x")},
            setup=existing_single(t3),
        ),
        Case(
            "commit/검증 · edits 화면 이름(400)",
            "alarm-commit",
            "A",
            new(edits=[{"screen": "ZZ", "field": "x"}]),
        ),
        Case("commit/인증 · 토큰 없음(401)", "alarm-commit", None, new()),
        Case(
            "commit/기존 약속 · 같은 요청 다시(기존 알람 갱신)",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "participant": MANUAL40},
            setup=existing_single(t3),
        ),
        Case(
            "commit/기존 약속 · 직접 이동 이어받기",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1)},
            setup=existing_single(t3),
        ),
        Case(
            "commit/기존 약속 · 이동을 바꿈",
            "alarm-commit",
            "A",
            {
                "appointment_id": fx.uid(1),
                "participant": {
                    "est_travel_minutes": 75,
                    "travel_source": "MANUAL",
                    "transport_type": "CAR",
                    "buffer_minutes": 5,
                },
            },
            setup=existing_single(t3),
        ),
        Case(
            "commit/기존 약속 · kinds로 일부만(replace_existing)",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "participant": MANUAL40, "kinds": ["DEPARTURE"]},
            setup=existing_single(t3),
        ),
        Case(
            "commit/기존 약속 · kinds 일부 + replace_existing=false",
            "alarm-commit",
            "A",
            {
                "appointment_id": fx.uid(1),
                "participant": MANUAL40,
                "kinds": ["DEPARTURE"],
                "replace_existing": False,
            },
            setup=existing_single(t3),
        ),
        Case(
            "commit/기존 약속 · kinds 빈 목록(알람 지움)",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "participant": MANUAL40, "kinds": []},
            setup=existing_single(t3),
        ),
        Case(
            "commit/기존 약속 · 직접 시각 → null로 되돌림",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "participant": MANUAL40, "manual_times": {"WAKE": None}},
            setup=existing_single(t3, time_source="MANUAL"),
        ),
        Case(
            "commit/기존 약속 · 저장된 직접 시각 유지",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "participant": MANUAL40},
            setup=existing_single(t3, time_source="MANUAL"),
        ),
        Case(
            "commit/기존 약속 · 저장된 직접 강도 유지 · 축 바꾸기",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "participant": MANUAL40, "levels": {"sound": 2}},
            setup=existing_single(
                t3,
                intensity_source="MANUAL",
                intensity="SOFT",
                voice="CALM",
                sound="theme_calm",
                vibration="NONE",
            ),
        ),
        Case(
            "commit/기존 약속 · enabled 끄기(H02에서 끈 상태)",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "participant": MANUAL40, "enabled": {"WAKE": False}},
            setup=existing_single(t3),
        ),
        Case(
            "commit/기존 약속 · 알람이 꺼져 있던 것 유지",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "participant": MANUAL40},
            setup=existing_single(t3, enabled=False),
        ),
        Case(
            "commit/기존 약속 · 친구가 옮긴 시각(original_scheduled_at)",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "participant": MANUAL40},
            setup=existing_single(t3).replace(
                "'AUTO', 'AUTO', null, null, null, null, null);",
                "'AUTO', 'AUTO', null, null, null, null, null);",
            )
            + "update public.alarms set original_scheduled_at = scheduled_at + interval '10 minutes', original_time_source = 'AUTO' where alarm_type = 'WAKE';\n",
        ),
        Case(
            "commit/기존 약속 · 내 것이 아님(404)",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(2), "participant": MANUAL40},
            setup=existing_single(t3, user=B, appt=2),
        ),
        Case(
            "commit/기존 약속 · 공동 약속에서 내 몫 저장",
            "alarm-commit",
            "A",
            {
                "appointment_id": fx.uid(3),
                "participant": {"est_travel_minutes": 25, "travel_source": "MANUAL"},
            },
            setup=fx.group(50, B, [(51, B, "JOINED"), (52, A, "JOINED")])
            + fx.appointment(3, B, t3, group=fx.uid(50))
            + fx.participant(31, 3, B),
        ),
        Case(
            "commit/기존 약속 · 참여 조건이 없던 약속",
            "alarm-commit",
            "A",
            {"appointment_id": fx.uid(1), "participant": MANUAL40},
            setup=fx.appointment(1, A, t3),
        ),
        Case(
            "commit/일정 없는 알람 · 새로 만듦",
            "alarm-commit",
            "A",
            {"type": "STANDALONE", "alarm": {"scheduled_at": at(2, 7)}},
        ),
        Case(
            "commit/일정 없는 알람 · 반복 + 강도",
            "alarm-commit",
            "A",
            {
                "type": "STANDALONE",
                "alarm": {
                    "scheduled_at": at(2, 7),
                    "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,WE",
                    "levels": {"sound": 2, "voice": 1},
                },
            },
        ),
        Case(
            "commit/일정 없는 알람 · 꺼진 채로",
            "alarm-commit",
            "A",
            {"type": "STANDALONE", "alarm": {"scheduled_at": at(2, 7), "is_enabled": False}},
        ),
        Case(
            "commit/일정 없는 알람 · alarm_id로 고침",
            "alarm-commit",
            "A",
            {
                "type": "STANDALONE",
                "alarm": {"alarm_id": fx.uid(700), "scheduled_at": at(2, 8), "levels": {"voice": 2}},
            },
            setup=fx.alarm(700, None, A, "WAKE", at(2, 7)),
        ),
        Case(
            "commit/일정 없는 알람 · 남의 alarm_id",
            "alarm-commit",
            "A",
            {"type": "STANDALONE", "alarm": {"alarm_id": fx.uid(701), "scheduled_at": at(2, 8)}},
            setup=fx.alarm(701, None, B, "WAKE", at(2, 7)),
        ),
        Case(
            "commit/일정 없는 알람 · 지난 시각(422)",
            "alarm-commit",
            "A",
            {"type": "STANDALONE", "alarm": {"scheduled_at": from_now(-30)}},
        ),
        Case(
            "commit/일정 없는 알람 · 약속의 알람 id(고치려는 대상이 아님)",
            "alarm-commit",
            "A",
            {"type": "STANDALONE", "alarm": {"alarm_id": fx.uid(110), "scheduled_at": at(2, 8)}},
            setup=existing_single(t3),
        ),
        Case(
            "commit/일정 없는 알람 · 검증(scheduled_at 없음, 400)",
            "alarm-commit",
            "A",
            {"type": "STANDALONE", "alarm": {}},
        ),
    ]
    return cases

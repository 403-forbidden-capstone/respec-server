"""푸시 · 예약 작업 경우 (완결점 3-1 · 3-5) — 가짜 Expo(fake_expo.py)가 받은 "전송 계획"(받는 사람 토큰 · 제목 · 본문 · 채널 · 데이터)까지 견줌

  wake_cases()      wake-check — 본인 미확인 · 친구 '깨워 볼까요?' · 후속 '일어났어요'. Edge는 HTTP(서비스 키), 서버는 예약 작업 본문을 지금 시각으로 한 번(server_job.py)
  edit_push_cases() appointment-edit 공동 약속 푸시 — 바뀜(CHANGED) · 취소(CANCELED) · 알림 끈 참여자의 조용한 푸시 · 확인 필요(needsConfirm)
  suggest_gen_cases() suggestion-generate — 후보 · 만든 수 · 건너뛴 수, 두 번째 실행
현재 시각은 못 바꿈(Edge는 자기 시계) — 시작 상태를 지금 기준 상대 시각으로 만들고 30분 · 10분 경계에서 충분히 떨어진 값만 씀"""

from __future__ import annotations

from datetime import datetime

import fx
from cases_roll_edit import series_rows
from fx import A, B, C, at, from_now
from harness import Case

G = fx.uid(50)  # 모임


# ---------- 공통 시작 상태 ----------
def token(user: str, name: str) -> str:
    return (
        f"insert into public.push_tokens (user_id, token) values ('{user}', 'ExponentPushToken[{name}]');\n"
    )


def wake_alarm(n: int, user: str, minutes_ago: float, **kw: object) -> str:
    """일정 없는 기상 알람 — scheduled_at = 지금 − minutes_ago분"""
    return fx.alarm(n, None, user, "WAKE", from_now(-minutes_ago), **kw)  # type: ignore[arg-type]


def event(
    alarm_n: int,
    user: str,
    etype: str,
    minutes_ago: float,
    *,
    snooze: int | None = None,
    source: str | None = None,
) -> str:
    src = f"'{source}'" if source else "null"
    return (
        "insert into public.alarm_events (alarm_id, user_id, event_type, event_at, snooze_minutes, input_source) values "
        f"('{fx.uid(alarm_n)}', '{user}', '{etype}', '{from_now(-minutes_ago)}', {snooze if snooze is not None else 'null'}, {src});\n"
    )


def later_today_minutes() -> int:
    """오늘(한국) 안에 들어가는 '지금보다 뒤' 분 (기본 180분, 자정이 가까우면 줄임)"""
    now = datetime.now(fx.KST)
    return max(10, min(180, 23 * 60 + 50 - (now.hour * 60 + now.minute)))


def nudge_group(
    *,
    opt_in: bool = True,
    off: tuple[str, ...] = (),
    members: tuple[tuple[int, str], ...] = ((52, B), (53, C)),
    appointment: bool = True,
) -> str:
    """A(자는 사람)가 속한 모임 — 멤버는 모두 JOINED. A의 기상 공유 · 받는 사람의 알림 끔을 고름. 오늘 늦게 모임 약속 하나"""
    out = fx.group(50, A, [(51, A, "JOINED"), *[(n, u, "JOINED") for n, u in members]])
    out += f"update public.group_members set wake_share_opt_in = {'true' if opt_in else 'false'} where group_id = '{G}' and user_id = '{A}';\n"
    for user in off:
        out += f"update public.group_members set notifications_off = true where group_id = '{G}' and user_id = '{user}';\n"
    if appointment:
        out += fx.appointment(3, A, from_now(later_today_minutes()), group=G, title="모임")
    return out


# ---------- wake-check ----------
def wake(name: str, setup: str, then: list[tuple[str, str | None, object | None]] | None = None) -> Case:
    return Case(f"wake/{name}", "wake-check", None, None, setup=setup, service=True, then=then or [])


def wake_cases() -> list[Case]:
    again = ("wake-check", None, None)
    a_missed = wake_alarm(11, A, 45)
    confirmed_now = "$sql"
    return [
        # 본인 알림 (기상 미확인 → 본인에게 '기상 확인이 안 됐어요')
        wake("본인 · 기기 1대", a_missed + token(A, "a1")),
        wake("본인 · 기기 2대", a_missed + token(A, "a1") + token(A, "a2")),
        wake("본인 · 토큰 없음", a_missed),
        wake(
            "본인 · 토큰 모양이 틀림(Expo 토큰 아님)",
            a_missed
            + "insert into public.push_tokens (user_id, token) values ('{A}', 'not-an-expo-token');\n",
        ),
        wake(
            "본인 · 한 사람의 알람 둘이 같이 미확인(푸시는 한 번)",
            wake_alarm(11, A, 45) + wake_alarm(12, A, 120) + token(A, "a1"),
        ),
        wake(
            "본인 · 두 사람(A · B)",
            wake_alarm(11, A, 45) + wake_alarm(12, B, 90) + token(A, "a1") + token(B, "b1"),
        ),
        wake("본인 · 아직 30분 안 지남(20분)", wake_alarm(11, A, 20) + token(A, "a1")),
        wake("본인 · 24시간 넘음(25시간 전)", wake_alarm(11, A, 25 * 60) + token(A, "a1")),
        wake("본인 · 꺼진 알람", wake_alarm(11, A, 45, enabled=False) + token(A, "a1")),
        wake(
            "본인 · 기상 확인이 이미 있음",
            a_missed + event(11, A, "WAKE_CONFIRMED", 40, source="BUTTON") + token(A, "a1"),
        ),
        wake(
            "본인 · 미루기 뒤 다시 울린 지 30분 지남",
            a_missed + event(11, A, "SNOOZED", 45, snooze=10) + token(A, "a1"),
        ),
        wake(
            "본인 · 미루기가 방금 끝남(아직 30분 안 지남)",
            a_missed + event(11, A, "SNOOZED", 10, snooze=5) + token(A, "a1"),
        ),
        wake(
            "본인 · 일정 있는 알람",
            fx.appointment(2, A, from_now(60), title="수업")
            + fx.participant(2, 2, A)
            + fx.alarm(11, 2, A, "WAKE", from_now(-45))
            + token(A, "a1"),
        ),
        wake(
            "본인 · 만료 토큰(DeviceNotRegistered)이면 토큰을 지움",
            a_missed + token(A, "Gone-a") + token(A, "a2"),
        ),
        wake("본인 · 두 번 돌림(두 번째는 아무것도 안 보냄)", a_missed + token(A, "a1"), then=[again]),
        wake("할 일이 없음", ""),
        # 친구 알림 (미확인 10분 뒤 그날 그룹 약속의 그룹원에게 '깨워 볼까요?' · 늦게 일어나면 후속 '일어났어요')
        wake("친구 · B에게 깨워 볼까요?", a_missed + nudge_group(members=((52, B),)) + token(B, "b1")),
        wake(
            "친구 · 본인과 친구에게 같이",
            a_missed + nudge_group(members=((52, B),)) + token(A, "a1") + token(B, "b1"),
        ),
        wake("친구 · 받는 사람 둘(B · C)", a_missed + nudge_group() + token(B, "b1") + token(C, "c1")),
        wake(
            "친구 · 받는 사람 기기 둘",
            a_missed + nudge_group(members=((52, B),)) + token(B, "b1") + token(B, "b2"),
        ),
        wake("친구 · 받는 사람 토큰 없음(대상 1 · 나간 것 0)", a_missed + nudge_group(members=((52, B),))),
        wake("친구 · B가 알림 끔", a_missed + nudge_group(members=((52, B),), off=(B,)) + token(B, "b1")),
        wake(
            "친구 · A가 기상 공유 안 함",
            a_missed + nudge_group(members=((52, B),), opt_in=False) + token(B, "b1"),
        ),
        wake(
            "친구 · A가 깨우기 받기를 끔",
            a_missed
            + nudge_group(members=((52, B),))
            + "update public.profiles set wake_ping_enabled = false where user_id = '{A}';\n"
            + token(B, "b1"),
        ),
        wake(
            "친구 · 그날 모임 약속이 없음",
            a_missed + nudge_group(members=((52, B),), appointment=False) + token(B, "b1"),
        ),
        wake(
            "친구 · 미확인 10분이 안 지남(미확인 5분)",
            wake_alarm(11, A, 35) + nudge_group(members=((52, B),)) + token(B, "b1"),
        ),
        wake("친구 · 받는 사람 만료 토큰", a_missed + nudge_group(members=((52, B),)) + token(B, "Gone-b")),
        wake(
            "친구 · 두 번 돌림(두 번째는 안 보냄)",
            a_missed + nudge_group(members=((52, B),)) + token(B, "b1"),
            then=[again],
        ),
        wake(
            "친구 · 늦게 일어나면 후속 '일어났어요'",
            a_missed + nudge_group(members=((52, B),)) + token(B, "b1"),
            then=[(confirmed_now, None, event(11, A, "WAKE_CONFIRMED", 0, source="BUTTON")), again, again],
        ),
        wake(
            "친구 · 직접 확인(MANUAL)은 후속을 안 보냄",
            a_missed + nudge_group(members=((52, B),)) + token(B, "b1"),
            then=[(confirmed_now, None, event(11, A, "WAKE_CONFIRMED", 0, source="MANUAL")), again],
        ),
    ]


# ---------- appointment-edit 공동 약속 푸시 ----------
def edit_group_setup(
    *, with_c: bool = False, tokens: tuple[tuple[str, str], ...] = (), off: tuple[str, ...] = ()
) -> str:
    t3 = at(3, 10)
    members = [(51, A, "JOINED"), (52, B, "JOINED")] + ([(53, C, "JOINED")] if with_c else [])
    out = (
        fx.group(50, A, members)
        + fx.appointment(3, A, t3, group=G, title="모임")
        + fx.participant(31, 3, A)
        + fx.full_set(330, 3, 31, A, t3)
        + fx.participant(32, 3, B, origin="B집", olat=37.56, olng=126.99, est=30)
        + fx.full_set(340, 3, 32, B, t3, travel=30)
    )
    if with_c:
        out += fx.participant(33, 3, C, origin="C집", olat=37.55, olng=127.0, est=35) + fx.full_set(
            350, 3, 33, C, t3, travel=35
        )
    for user in off:
        out += f"update public.group_members set notifications_off = true where group_id = '{G}' and user_id = '{user}';\n"
    for user, name in tokens:
        out += token(user, name)
    return out


def edit_push_cases() -> list[Case]:
    def upd(user: str, patch: dict, **extra: object) -> dict:
        return {"action": "UPDATE", "appointment_id": fx.uid(3), "scope": "this", "patch": patch, **extra}

    def dele() -> dict:
        return {"action": "DELETE", "appointment_id": fx.uid(3), "scope": "this"}

    tok = (("A", "a1"), ("B", "b1"))
    tok_all = (("A", "a1"), ("B", "b1"), ("B", "b2"), ("C", "c1"))

    def tokens(pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        """사람 이름("A")을 시작 상태 SQL의 자리표시({A})로 바꿈"""
        return tuple(({"A": A, "B": B, "C": C}[u], n) for u, n in pairs)

    later = {"appointment_at": at(3, 11)}  # 60분 늦춤 → 받는 사람이 확인할 때까지 보류(needsConfirm)
    earlier = {"appointment_at": at(3, 9)}  # 앞당김 → 바로 반영
    return [
        Case(
            "push/수정 · 시각을 늦춤(확인 먼저 — 저장 안 됨 · 푸시 없음)",
            "appointment-edit",
            "A",
            upd("A", later),
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/수정 · 시각을 앞당김(확인 필요 없음)",
            "appointment-edit",
            "A",
            upd("A", earlier),
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/수정 · 늦춤(확인함) · B에게 needsConfirm",
            "appointment-edit",
            "A",
            upd("A", later, confirmed=True),
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/수정 · 제목만",
            "appointment-edit",
            "A",
            upd("A", {"title": "새 모임 이름"}),
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/수정 · 장소만",
            "appointment-edit",
            "A",
            upd("A", {"destination": {"name": "건대", "lat": 37.5404, "lng": 127.0693}}),
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/수정 · 시각(늦춤, 확인함) + 제목 + 장소",
            "appointment-edit",
            "A",
            upd(
                "A",
                {
                    "title": "복합",
                    "appointment_at": at(3, 12),
                    "destination": {"name": "건대", "lat": 37.5404, "lng": 127.0693},
                },
                confirmed=True,
            ),
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/수정 · 같은 값(바뀐 게 없음 → 푸시 없음)",
            "appointment-edit",
            "A",
            upd("A", {"title": "모임"}),
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/수정 · 늦춤(확인함) · 받는 사람 기기 둘 + C까지",
            "appointment-edit",
            "A",
            upd("A", later, confirmed=True),
            setup=edit_group_setup(with_c=True, tokens=tokens(tok_all)),
        ),
        Case(
            "push/수정 · 늦춤(확인함) · B가 알림 끔(조용한 데이터 푸시)",
            "appointment-edit",
            "A",
            upd("A", later, confirmed=True),
            setup=edit_group_setup(off=(B,), tokens=tokens(tok)),
        ),
        Case(
            "push/수정 · B 알림 끔 · C는 켬",
            "appointment-edit",
            "A",
            upd("A", earlier),
            setup=edit_group_setup(with_c=True, off=(B,), tokens=tokens(tok_all)),
        ),
        Case(
            "push/수정 · 늦춤(확인함) · 받는 사람 토큰 없음",
            "appointment-edit",
            "A",
            upd("A", later, confirmed=True),
            setup=edit_group_setup(tokens=tokens((("A", "a1"),))),
        ),
        Case(
            "push/수정 · 늦춤(확인함) · 받는 사람 만료 토큰",
            "appointment-edit",
            "A",
            upd("A", later, confirmed=True),
            setup=edit_group_setup(tokens=tokens((("B", "Gone-b"), ("B", "b2")))),
        ),
        Case(
            "push/수정 · 늦춤(확인함) · 토큰 모양이 틀림",
            "appointment-edit",
            "A",
            upd("A", later, confirmed=True),
            setup=edit_group_setup()
            + "insert into public.push_tokens (user_id, token) values ('{B}', 'bad-token');\n",
        ),
        Case(
            "push/수정 · 참여자 B가 시각 변경(거절 → 푸시 없음)",
            "appointment-edit",
            "B",
            upd("B", later),
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/삭제 · 만든 사람(취소 푸시)",
            "appointment-edit",
            "A",
            dele(),
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/삭제 · B가 알림 끔",
            "appointment-edit",
            "A",
            dele(),
            setup=edit_group_setup(off=(B,), tokens=tokens(tok)),
        ),
        Case(
            "push/삭제 · 받는 사람 둘 + C",
            "appointment-edit",
            "A",
            dele(),
            setup=edit_group_setup(with_c=True, tokens=tokens(tok_all)),
        ),
        Case(
            "push/삭제 · 참여자 B(거절 → 푸시 없음)",
            "appointment-edit",
            "B",
            dele(),
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/나가기 · 참여자 B(푸시 여부)",
            "appointment-edit",
            "B",
            {"action": "LEAVE", "appointment_id": fx.uid(3)},
            setup=edit_group_setup(tokens=tokens(tok)),
        ),
        Case(
            "push/개인 약속 수정(푸시 없음)",
            "appointment-edit",
            "A",
            {"action": "UPDATE", "appointment_id": fx.uid(1), "scope": "this", "patch": later},
            setup=fx.appointment(1, A, at(3, 10))
            + fx.participant(1, 1, A)
            + fx.full_set(110, 1, 1, A, at(3, 10))
            + token(A, "a1"),
        ),
    ]


# ---------- suggestion-generate ----------
def history(
    user: str,
    first_n: int,
    residuals: list[int],
    *,
    future: str | None = None,
    advance: int = 0,
    **future_kw: object,
) -> str:
    """반복 묶음 하나 — 과거 회차(-len일 전부터 매일 10시)마다 실제 준비가 계획보다 residual분 길었던 기록 + 앞으로 36시간 안의 회차 하나.
    준비 알람 = 약속 − 90분, 출발 알람 = 약속 − 50분 (full_set 기본값: 이동 40 · 여유 10 · 준비 60 · 준비 전 활동 20)"""
    count = len(residuals)
    first = at(-(count + 1), 10)
    out = series_rows(user, first_n, first, "FREQ=DAILY", count=count)
    for i, residual in enumerate(residuals):
        n = first_n + i
        when = fx.minus(first, -24 * 60 * i)  # i일 뒤 같은 시각 (series_rows와 같은 간격)
        started = fx.minus(when, 90)
        departed = fx.minus(when, 50 - residual)
        out += f"update public.appointment_participants set prepare_started_at = '{started}', prepare_started_source = 'PREPARE_ALARM', departed_at = '{departed}', departed_source = 'BUTTON' where participant_id = '{fx.uid(n)}';\n"
    n = first_n + count
    when = future or at(1, 10)
    out += fx.appointment(n, user, when, rule="FREQ=DAILY", series=first_n)
    out += fx.participant(n, n, user, advance=advance)
    out += fx.full_set(1000 + n * 10, n, n, user, when, **future_kw)  # type: ignore[arg-type]
    return out


def suggest_gen_cases() -> list[Case]:
    def gen(name: str, setup: str, then: list[tuple[str, str | None, object | None]] | None = None) -> Case:
        return Case(
            f"suggest-gen/{name}",
            "suggestion-generate",
            None,
            None,
            setup=setup,
            service=True,
            then=then or [],
        )

    again = ("suggestion-generate", None, None)
    base = history(A, 1, [15, 15, 15]) + history(B, 60, [8, 8, 8]) + history(C, 70, [15, 15])
    return [
        gen("후보 둘(A · B) · 빠지는 하나(C: 기록 2건)", base),
        gen("후보 둘 · 두 번째 실행은 만든 것 0", base, then=[again]),
        gen("한 사람 한 묶음 · 넘침 15분", history(A, 1, [15, 15, 15])),
        gen("넘침이 평균 5분 미만(4분) → 후보지만 제안 없음", history(A, 1, [4, 4, 4])),
        gen("넘침이 딱 5분", history(A, 1, [5, 5, 5])),
        gen("최근 3번 중 하나가 넘치지 않음", history(A, 1, [15, 15, -10])),
        gen("기록 5건(최근 10건 중 평균)", history(A, 1, [6, 9, 12, 15, 18])),
        gen("기록 2건뿐 → 후보 아님", history(A, 1, [15, 15])),
        gen("넘침이 2분(3분 이내라 기록에서 빠짐)", history(A, 1, [2, 2, 2])),
        gen("준비 알람을 직접 고름 → 후보 아님", history(A, 1, [15, 15, 15], time_source="MANUAL")),
        gen("당김이 115분(남은 5분만)", history(A, 1, [15, 15, 15], advance=115)),
        gen("당김이 이미 120분(더 못 당김)", history(A, 1, [15, 15, 15], advance=120)),
        gen("앞으로 36시간 밖(+3일) → 후보 아님", history(A, 1, [15, 15, 15], future=at(3, 10))),
        gen("한 사람 묶음 둘", history(A, 1, [15, 15, 15]) + history(A, 40, [10, 10, 10], future=at(1, 14))),
        gen(
            "7일 안에 이미 제안이 있음(무시함)",
            history(A, 1, [15, 15, 15])
            + f"insert into public.alarm_suggestions (user_id, target_alarm_id, suggestion_type, current_value, suggested_value, status, responded_at, created_at) values ('{A}', '{fx.uid(1000 + 4 * 10 + 1)}', 'PREPARE_ADVANCE', '0', '5', 'IGNORED', '{fx.from_now(-1440)}', '{fx.from_now(-2880)}');\n",
        ),
        gen(
            "대기 중 제안이 이미 있음",
            history(A, 1, [15, 15, 15])
            + f"insert into public.alarm_suggestions (user_id, target_alarm_id, suggestion_type, current_value, suggested_value, status) values ('{A}', '{fx.uid(1000 + 4 * 10 + 1)}', 'PREPARE_ADVANCE', '0', '10', 'PENDING');\n",
        ),
        gen(
            "앞으로 회차가 이번만 지움(CANCELED)",
            history(A, 1, [15, 15, 15])
            + "update public.appointments set exception_kind = 'CANCELED' where appointment_id = '"
            + fx.uid(4)
            + "';\n",
        ),
        gen("할 일이 없음", ""),
    ]

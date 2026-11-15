"""시작 상태 SQL을 만드는 도우미 — 고정 uuid로 약속 · 참여 조건 · 알람 · 기록 행을 직접 넣음 (Edge · 서버 어느 쪽도 거치지 않음)"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
A, B, C = "{A}", "{B}", "{C}"  # harness.reset이 시험 계정 id로 바꿈


def at(days: int, hh: int, mm: int = 0) -> str:
    """지금 기준 한국 날짜 days일 뒤 hh:mm (오프셋 +09:00 ISO). 같은 값을 요청 · 시작 상태에 같이 쓰려고 호출 시점의 시계로 만듦"""
    base = datetime.now(KST).replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=days)
    return base.isoformat()


def from_now(minutes: int) -> str:
    """지금 + minutes분을 초 단위로 자른 ISO (지금 기준 경계 시험용)"""
    return (datetime.now(KST).replace(microsecond=0) + timedelta(minutes=minutes)).isoformat()


def uid(n: int) -> str:
    """고정 uuid — 시작 상태 행끼리 서로 참조하려고 씀"""
    return f"00000000-0000-4000-8000-{n:012x}"


def _q(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def kst_date(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(KST).date().isoformat()


def appointment(
    n: int,
    creator: str | None,
    when: str,
    *,
    title: str = "수업",
    dest: str = "세종대",
    lat: float | None = 37.5509,
    lng: float | None = 127.0736,
    importance: str = "NORMAL",
    rule: str | None = None,
    series: int | None = None,
    occurrence: str | None = None,
    group: str | None = None,
    exception: str | None = None,
    radius: int | None = None,
) -> str:
    return (
        "insert into public.appointments (appointment_id, creator_id, group_id, title, appointment_at, dest_name, dest_lat, dest_lng, importance, recurrence_rule, series_id, occurrence_on, exception_kind, dest_radius_m) values "
        f"({_q(uid(n))}, {_q(creator)}, {_q(group)}, {_q(title)}, {_q(when)}, {_q(dest)}, {_q(lat)}, {_q(lng)}, {_q(importance)}, {_q(rule)}, {_q(uid(series if series is not None else n))}, {_q(occurrence or kst_date(when))}, {_q(exception)}, {_q(radius)});\n"
    )


def participant(
    n: int,
    appt: int,
    user: str,
    *,
    origin: str | None = "집",
    olat: float | None = 37.5547,
    olng: float | None = 126.9707,
    transport: str = "TRANSIT",
    est: int | None = 40,
    source: str = "MANUAL",
    prepare: int = 60,
    buffer: int = 10,
    advance: int = 0,
    departed: str | None = None,
    arrived: str | None = None,
    actual: int | None = None,
    late: bool | None = None,
) -> str:
    return (
        "insert into public.appointment_participants (participant_id, appointment_id, user_id, origin_name, origin_lat, origin_lng, transport_type, est_travel_minutes, travel_source, prepare_minutes, buffer_minutes, prepare_advance_minutes, departed_at, arrived_at, actual_travel_minutes, is_late) values "
        f"({_q(uid(n))}, {_q(uid(appt))}, {_q(user)}, {_q(origin)}, {_q(olat)}, {_q(olng)}, {_q(transport)}, {_q(est)}, {_q(source)}, {prepare}, {buffer}, {advance}, {_q(departed)}, {_q(arrived)}, {_q(actual)}, {_q(late)});\n"
    )


def alarm(
    n: int,
    part: int | None,
    user: str,
    kind: str,
    when: str,
    *,
    enabled: bool = True,
    time_source: str = "AUTO",
    intensity: str = "NORMAL",
    voice: str = "NEUTRAL",
    sound: str | None = "theme_default",
    vibration: str | None = None,
    intensity_source: str = "AUTO",
    rule: str | None = None,
    series: int | None = None,
    occurrence: str | None = None,
    original_at: str | None = None,
    original_source: str | None = None,
) -> str:
    return (
        "insert into public.alarms (alarm_id, participant_id, user_id, alarm_type, scheduled_at, intensity, voice_tone, sound_key, vibration, is_enabled, time_source, intensity_source, recurrence_rule, series_id, occurrence_on, original_scheduled_at, original_time_source) values "
        f"({_q(uid(n))}, {_q(uid(part) if part is not None else None)}, {_q(user)}, {_q(kind)}, {_q(when)}, {_q(intensity)}, {_q(voice)}, {_q(sound)}, {_q(vibration)}, {_q(enabled)}, {_q(time_source)}, {_q(intensity_source)}, {_q(rule)}, {_q(uid(series) if series is not None else None)}, {_q(occurrence)}, {_q(original_at)}, {_q(original_source)});\n"
    )


def minus(when: str, minutes: float) -> str:
    return (datetime.fromisoformat(when) - timedelta(minutes=minutes)).isoformat()


def full_set(
    base_n: int,
    appt: int,
    part: int,
    user: str,
    when: str,
    *,
    travel: int = 40,
    buffer: int = 10,
    prepare: int = 60,
    before_prepare: int = 20,
    kinds: tuple[str, ...] = ("WAKE", "PREPARE", "DEPARTURE", "ARRIVE"),
    enabled: bool = True,
    **alarm_kwargs: object,
) -> str:
    """알람 계산식(출발 = 약속 − 여유 − 이동, 기상 = 출발 − 준비, 준비 알람 = 기상 + 준비 전 활동, 도착 = 약속 − 여유)대로 알람 네 개를 넣음"""
    depart = minus(when, buffer + travel)
    wake = minus(depart, prepare)
    times = {
        "WAKE": wake,
        "PREPARE": (datetime.fromisoformat(wake) + timedelta(minutes=before_prepare)).isoformat(),
        "DEPARTURE": depart,
        "ARRIVE": minus(when, buffer),
    }
    out = ""
    for i, kind in enumerate(("WAKE", "PREPARE", "DEPARTURE", "ARRIVE")):
        if kind in kinds:
            out += alarm(base_n + i, part, user, kind, times[kind], enabled=enabled, **alarm_kwargs)  # type: ignore[arg-type]
    return out


def daily_record(n: int, user: str, days_ago: int, prepare: int) -> str:
    d = (datetime.now(KST) - timedelta(days=days_ago)).date().isoformat()
    return f"insert into public.daily_records (record_id, user_id, record_date, actual_prepare_minutes) values ({_q(uid(n))}, {_q(user)}, {_q(d)}, {prepare});\n"


def group(n: int, owner: str, members: list[tuple[int, str, str]], name: str = "계약 모임") -> str:
    out = f"insert into public.groups (group_id, group_name, owner_id) values ({_q(uid(n))}, {_q(name)}, {_q(owner)});\n"
    for gm, user, status in members:
        if user == owner:  # 모임을 만들면 DB가 만든 사람을 멤버로 자동으로 넣음
            continue
        out += f"insert into public.group_members (group_member_id, group_id, user_id, invite_status, joined_at) values ({_q(uid(gm))}, {_q(uid(n))}, {_q(user)}, {_q(status)}, now());\n"
    return out

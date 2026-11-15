"""본 레포 supabase/functions/_shared/roll-replay.test.ts의 서버판 (기준 b140f70) — 하루 재생 시험 2 (설계 메모 L1_PR3_설계메모.md 7절)

반복 일정을 저장한 뒤 며칠 동안 서버(alarm-roll)가 어떻게 도는지를 가짜 시계 · 가짜 카카오(분 고정, 부른 수 셈) · 가짜 DB(FakeDb)로 차례로 재생해 단계마다 확인
  ① 월 10:00 저장 → 본인 길 roll → 회차 수 · 카카오 호출 수  ② 화 아침 기록(해제 · 출발 · 도착) → 배치가 창을 하루 민 새 회차
  ③ 수: 앱을 안 열어도 서버에 수요일 알람이 이미 있음 · RATE_LIMIT → 대기 → 다음 날 0시 뒤 만들어짐  ④ 목: 배치 두 번 = 변화 없음 · 요일 · 새벽 1시 약속
달력: 2026-10-26 월 · 27 화 · 28 수 · 29 목 · 30 금 · 11-03 화 · 04 수 · 05 목 · 10 화 · 11 수 · 12 목
서버에서 안 보이는 단계(폰이 알람을 거는 모습 · 앱 화면)는 이 시험 밖. 도구는 test_roll.py의 것을 필요한 만큼 복사함
"""

import asyncio
import functools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from app.core.read import Reader
from app.kakao import KakaoError
from app.shared.commit import CommitCtx, commit_standalone, persist_appointment
from app.shared.js import from_ms, ms, parse_iso, to_iso
from app.shared.plan import PlanContext, build_plan
from app.shared.requests import read_commit_request
from app.shared.roll import RollDeps, run_roll
from app.shared.tz import local_date_key
from tests.fake_db import FakeDb

A = "u-a"
B = "u-b"


def async_test(fn: Callable[[], Any]) -> Callable[[], None]:
    """async def 시험을 pytest-asyncio 없이 돌림"""

    @functools.wraps(fn)
    def wrapper() -> None:
        asyncio.run(fn())

    return wrapper


def kst(local: str) -> datetime:  # '2026-10-26T10:00'
    return parse_iso(f"{local}:00+09:00")


def iso(local: str) -> str:
    return f"{local}:00+09:00"


def hm(value: Any) -> str:
    dt = parse_iso(value)
    m = (dt.hour * 60 + dt.minute + 9 * 60) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def ymd(value: Any) -> str:
    return local_date_key(parse_iso(value), "Asia/Seoul")


def weekday(date: str) -> str:
    # JS getUTCDay: 일 0 … 토 6 (파이썬 weekday는 월 0 … 일 6)
    return "월화수목금토일"[datetime.strptime(date, "%Y-%m-%d").weekday()]


def _get(obj: Any, name: str) -> Any:
    """결과 객체가 dict든 dataclass든 읽음 (다른 묶음의 반환 모양을 가정하지 않으려고)"""
    return obj[name] if isinstance(obj, dict) else getattr(obj, name)


HOME = {"lat": 37.5547, "lng": 126.9707}
SCHOOL = {"lat": 37.55, "lng": 127.07}

# ---- 도구 (test_roll.py에서 복사) ----


@dataclass
class Env:
    db: FakeDb
    logs: list[str] = field(default_factory=list)
    warns: list[str] = field(default_factory=list)


def setup(now: datetime, users: list[str]) -> Env:
    db = FakeDb().with_roll_rpc().with_commit_rpc()
    db.now = now
    for user_id in users:
        db.rows("profiles").append(
            {
                "user_id": user_id,
                "timezone": "Asia/Seoul",
                "default_buffer_minutes": 10,
                "default_transport": "TRANSIT",
                "home_name": "집",
                "home_lat": HOME["lat"],
                "home_lng": HOME["lng"],
            }
        )
    return Env(db)


# 가짜 카카오 — 분 고정, 부른 수를 셈. fail을 정하면 부를 때마다 그 실패
def fake_kakao(minutes: int = 50) -> SimpleNamespace:
    state = SimpleNamespace(calls=0, minutes=minutes, fail=None)

    async def lookup(**_: Any) -> dict[str, Any]:
        state.calls += 1
        if state.fail:
            raise KakaoError(state.fail)
        return {"minutes": state.minutes, "partial": False}

    return SimpleNamespace(state=state, lookup=lookup)


def roll(env: Env, path: str, now: datetime, lookup: Any, user_id: str = A):
    env.db.now = now
    deps = RollDeps(
        path=path,  # type: ignore[arg-type]
        now=now,
        read=env.db,
        write=env.db,
        lookup=lookup,
        log=env.logs.append,
        warn=env.warns.append,
    )
    return run_roll(deps, user_id=user_id) if path == "own" else run_roll(deps)


def slot(**over: Any) -> dict[str, Any]:
    return {
        "appointment": {
            "title": "세미나",
            "appointment_at": iso("2026-10-27T09:00"),
            "destination": {"name": "세종대학교", **SCHOOL},
            "importance": "NORMAL",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU,TH",
        },
        "participant": {
            "origin": {"name": "집", **HOME},
            "transport_type": "TRANSIT",
            "est_travel_minutes": 50,
            "travel_source": "API",
            "buffer_minutes": 10,
            "before_prepare_minutes": 30,
        },
        **over,
    }


# alarm-commit이 첫 회차를 저장한 것처럼 (요청 읽기 → 계산 → 저장). 돌려주는 값 = 묶음 번호
async def seed(env: Env, user: str, body: dict[str, Any], now: datetime) -> str:
    request = read_commit_request(Reader.root(body))
    if _get(request, "type") != "APPOINTMENT":
        raise RuntimeError("약속 요청이어야 해요")
    db = env.db
    built = await build_plan(PlanContext(user_id=user, db=db, now=now), _get(request, "draft"))
    saved = await persist_appointment(
        CommitCtx(user_id=user, user_db=db, service=db, now=now),
        plan=_get(built, "plan"),
        inputs=_get(built, "inputs"),
        created=_get(request, "appointment"),
        draft=_get(request, "draft"),
        replace_existing=_get(request, "replace_existing"),
        edits=[],
        time_zone=_get(built, "time_zone"),
    )
    appointment_id = _get(saved, "appointment_id")
    return next(a for a in env.db.rows("appointments") if a["appointment_id"] == appointment_id)["series_id"]


# 일정 없는 반복 알람 첫 회차 저장
async def seed_standalone(env: Env, user: str, *, at: str, rule: str, now: datetime) -> str:
    db = env.db
    saved = await commit_standalone(
        CommitCtx(user_id=user, user_db=db, service=db, now=now),
        alarm={"scheduledAt": parse_iso(at), "recurrenceRule": rule},
        edits=[],
        time_zone="Asia/Seoul",
    )
    alarm_id = _get(saved, "alarm_id")
    return next(a for a in env.db.rows("alarms") if a["alarm_id"] == alarm_id)["series_id"]


def occurrence(db: FakeDb, series: str, date: str) -> dict[str, Any] | None:
    return next(
        (a for a in db.rows("appointments") if a["series_id"] == series and a["occurrence_on"] == date), None
    )


def dates_of(db: FakeDb, series: str) -> list[str]:
    return sorted(a["occurrence_on"] for a in db.rows("appointments") if a["series_id"] == series)


def standalone_dates(db: FakeDb, series: str) -> list[str]:
    return sorted(a["occurrence_on"] for a in db.rows("alarms") if a.get("series_id") == series)


def participant_of(db: FakeDb, appointment_id: Any) -> dict[str, Any]:
    return next(p for p in db.rows("appointment_participants") if p["appointment_id"] == appointment_id)


def alarms_of(db: FakeDb, appointment_id: Any) -> list[dict[str, Any]]:
    participant = participant_of(db, appointment_id)
    return sorted(
        (a for a in db.rows("alarms") if a.get("participant_id") == participant["participant_id"]),
        key=lambda a: ms(parse_iso(a["scheduled_at"])),
    )


def lines(db: FakeDb, appointment_id: Any) -> list[str]:
    return [
        f"{a['alarm_type']} {hm(a['scheduled_at'])} {'on' if a['is_enabled'] else 'off'}"
        for a in alarms_of(db, appointment_id)
    ]


def attempts(db: FakeDb) -> list[str]:
    return sorted(
        f"{r['occurrence_on']} {r['tries']} {r['last_error']} "
        + ("skipped" if r["skipped"] else ymd(r["next_try_at"]) + " " + hm(r["next_try_at"]))
        for r in db.rows("roll_attempts")
    )


# 시험용 간단 행 (alarm-commit을 거치지 않고) — 사용자 B의 일정 한 줄. ats마다 회차 한 줄 (첫 줄의 id가 묶음 번호)
_raw_seq = 0


def raw_series(db: FakeDb, user: str, *, ats: list[str], rule: str | None, title: str | None = None) -> str:
    global _raw_seq
    series = ""
    for when in ats:
        _raw_seq += 1
        appointment_id = f"ap-raw-{_raw_seq}"
        series = series or appointment_id
        at = parse_iso(when)
        db.rows("appointments").append(
            {
                "appointment_id": appointment_id,
                "creator_id": user,
                "group_id": None,
                "title": title or f"일정 {_raw_seq}",
                "appointment_at": when,
                "dest_name": "세종대학교",
                "dest_lat": SCHOOL["lat"],
                "dest_lng": SCHOOL["lng"],
                "importance": "NORMAL",
                "recurrence_rule": rule,
                "memo": None,
                "dest_radius_m": None,
                "series_id": series,
                "occurrence_on": local_date_key(at, "Asia/Seoul"),
            }
        )
        participant_id = f"pt-raw-{_raw_seq}"
        db.rows("appointment_participants").append(
            {
                "participant_id": participant_id,
                "appointment_id": appointment_id,
                "user_id": user,
                "origin_name": "집",
                "origin_lat": HOME["lat"],
                "origin_lng": HOME["lng"],
                "transport_type": "TRANSIT",
                "est_travel_minutes": 50,
                "travel_source": "API",
                "prepare_minutes": 50,
                "buffer_minutes": 10,
            }
        )
        for alarm_type, minutes in (("WAKE", -110), ("PREPARE", -80), ("DEPARTURE", -60), ("ARRIVE", -10)):
            db.rows("alarms").append(
                {
                    "alarm_id": f"al-raw-{_raw_seq}-{alarm_type}",
                    "user_id": user,
                    "participant_id": participant_id,
                    "routine_id": None,
                    "alarm_type": alarm_type,
                    "scheduled_at": to_iso(from_ms(ms(at) + minutes * 60_000)),
                    "intensity": "NORMAL",
                    "voice_tone": "NEUTRAL",
                    "sound_key": None,
                    "is_enabled": True,
                    "snooze_limit": 3,
                    "time_source": "AUTO",
                    "intensity_source": "AUTO",
                    "recurrence_rule": None,
                }
            )
    return series


# ---- 재생용 보조 ----

S2_FIRST_14 = [
    "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02",
    "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09",
]  # fmt: skip

TABLES = ("appointments", "appointment_participants", "alarms")
PRIMARY_KEY = {
    "appointments": "appointment_id",
    "appointment_participants": "participant_id",
    "alarms": "alarm_id",
}


def snapshot(db: FakeDb) -> dict[str, dict[str, str]]:
    """지금 행 전부의 모양을 떠 둠 — 나중에 "처음 있던 행이 그대로인가"를 봄"""
    return {t: {r[PRIMARY_KEY[t]]: json.dumps(r, default=str) for r in db.rows(t)} for t in TABLES}


def assert_unchanged(
    db: FakeDb, before: dict[str, dict[str, str]], except_: dict[str, set[str]] | None = None, label: str = ""
) -> None:
    """before에 있던 행 중 except_(표 이름 → 행 id) 말고는 모두 그대로인지"""
    except_ = except_ or {}
    for table in TABLES:
        now = {r[PRIMARY_KEY[table]]: json.dumps(r, default=str) for r in db.rows(table)}
        for row_id, text in before[table].items():
            if row_id in except_.get(table, set()):
                continue
            assert now.get(row_id) == text, f"{label} {table} {row_id} 행이 바뀌었어요"


@async_test
async def test_day_replay_2():
    """하루 재생 2: 반복 일정 저장 → 본인 길 → 화 기록 · 배치 → 수 앱 안 염 · RATE_LIMIT 재시도 → 목 배치 두 번"""
    mon = kst("2026-10-26T10:00")
    env = setup(mon, [A, B])
    kakao = fake_kakao(50)

    # ===== ① 월 10:00 저장 =====
    # S1 매주 화 · 목 09:00 대중교통 약속 · S3 매주 수 새벽 1:00 약속(UTC로는 전날) · S2 일정 없는 매일 07:30 기상 — 모두 첫 회차 한 줄씩
    s1 = await seed(env, A, slot(), mon)
    s3 = await seed(
        env,
        A,
        slot(
            appointment={
                **slot()["appointment"],
                "title": "야간 스터디",
                "appointment_at": iso("2026-10-28T01:00"),
                "recurrence_rule": "FREQ=WEEKLY;BYDAY=WE",
            }
        ),
        mon,
    )
    s2 = await seed_standalone(env, A, at="2026-10-27T07:30:00+09:00", rule="FREQ=DAILY", now=mon)
    # 사용자 B의 일정 한 줄 — A의 화요일 약속과 같은 날 · 같은 시각, 반복 규칙이 없어 roll이 다룰 일 없음. 끝까지 그대로여야 함
    b_series = raw_series(env.db, B, ats=[iso("2026-10-27T09:00")], rule=None, title="B의 수업")

    def b_rows() -> str:
        return json.dumps(
            [
                [r for r in env.db.rows("appointments") if r["creator_id"] == B],
                [r for r in env.db.rows("appointment_participants") if r["user_id"] == B],
                [r for r in env.db.rows("alarms") if r["user_id"] == B],
            ],
            ensure_ascii=False,
            default=str,
        )

    b_json = b_rows()
    assert b_series and "B의 수업" in b_json
    assert [dates_of(env.db, s1), dates_of(env.db, s3), standalone_dates(env.db, s2)] == [
        ["2026-10-27"],
        ["2026-10-28"],
        ["2026-10-27"],
    ]
    s3_first = occurrence(env.db, s3, "2026-10-28")
    assert (
        s3_first["appointment_at"] == "2026-10-27T16:00:00.000Z"
    )  # 새벽 1시 약속은 UTC로 전날 — 회차 날짜는 한국 날짜

    # 본인 길 roll: 앞으로 14일(~11/9 10:00) 안 회차를 만듦
    own = await roll(env, "own", mon, kakao.lookup)
    assert dates_of(env.db, s1) == [
        "2026-10-27",
        "2026-10-29",
        "2026-11-03",
        "2026-11-05",
    ]  # 화 · 목 4회차 (11/10 화 9시는 창 밖)
    assert dates_of(env.db, s3) == ["2026-10-28", "2026-11-04"]  # 수 2회차 (11/11 새벽 1시는 창 밖)
    assert standalone_dates(env.db, s2) == S2_FIRST_14  # 10/27 ~ 11/9 14회차 (11/10 7:30은 창 밖)
    # 카카오는 길찾기가 필요한 약속 회차 수만큼 (S1 3 + S3 1), 일정 없는 알람 S2는 길찾기 없음
    assert len(own["created"]) == 3 + 1 + 13
    assert own["kakao_calls"] == 4
    assert kakao.state.calls == 4
    assert [own["pending"], own["moved_alarm_ids"], own["canceled_alarm_ids"]] == [[], [], []]
    assert all(c["appointment_id"] is None for c in own["created"] if c["series_id"] == s2)
    # 새 회차의 알람은 첫 회차와 같은 모양으로 — 출발 = 약속 − 여유 10 − 이동 50
    tue2 = occurrence(env.db, s1, "2026-11-03")
    assert participant_of(env.db, tue2["appointment_id"])["est_travel_minutes"] == 50
    assert [x.split(" ")[0] for x in lines(env.db, tue2["appointment_id"])] == [
        "WAKE",
        "PREPARE",
        "DEPARTURE",
        "ARRIVE",
    ]
    assert lines(env.db, tue2["appointment_id"])[2] == "DEPARTURE 08:00 on"
    assert len(env.db.rows("appointments")) == 4 + 2 + 1
    assert len(env.db.rows("appointment_participants")) == 4 + 2 + 1
    assert len(env.db.rows("alarms")) == (4 + 2) * 4 + 4 + 14  # 약속 알람 4개씩 + B 4개 + S2 14개
    assert [json.loads(line) for line in env.logs] == [
        {"fn": "alarm-roll", "path": "own", "kakao_calls": 4, "created": 17, "pending": 0}
    ]
    assert b_rows() == b_json

    # ===== ② 화 10/27: 아침 해제 · 출발 · 도착 기록 =====
    # 해제(기상 알람 두 개: 약속 S1의 기상 · 일정 없는 S2의 화요일 알람), 출발 8:10, 도착 8:55 — 앱이 쓰는 칸을 가짜로 채움
    tue1 = occurrence(env.db, s1, "2026-10-27")
    tue1_participant = participant_of(env.db, tue1["appointment_id"])
    tue1_wake = next(a for a in alarms_of(env.db, tue1["appointment_id"]) if a["alarm_type"] == "WAKE")
    s2_tue = next(
        a
        for a in env.db.rows("alarms")
        if a.get("series_id") == s2 and a.get("occurrence_on") == "2026-10-27"
    )
    for i, alarm in enumerate([tue1_wake, s2_tue]):
        env.db.rows("alarm_events").append(
            {
                "event_id": i + 1,
                "alarm_id": alarm["alarm_id"],
                "user_id": A,
                "event_type": "DISMISSED",
                "event_at": iso("2026-10-27T07:31"),
                "sequence_no": 1,
            }
        )
    tue1_participant.update({"departed_at": iso("2026-10-27T08:10"), "arrived_at": iso("2026-10-27T08:55")})
    recorded = snapshot(env.db)
    events_before = json.dumps(env.db.rows("alarm_events"), default=str)

    # 같은 날 오전 10시 배치: 창이 하루 밀려 (10/27 10:00, 11/10 10:00) — 11/10 화 9시 약속과 7:30 알람이 새로 들어옴
    tuesday = await roll(env, "batch", kst("2026-10-27T10:00"), kakao.lookup)

    def label(c: dict[str, Any]) -> str:
        return "S1" if c["series_id"] == s1 else "S2" if c["series_id"] == s2 else "?"

    assert sorted([label(c), c["occurrence_on"]] for c in tuesday["created"]) == [
        ["S1", "2026-11-10"],
        ["S2", "2026-11-10"],
    ]
    # 길찾기는 S1 새 회차 하나 (배치 길은 꾸러미를 안 돌려줌)
    assert [tuesday["kakao_calls"], tuesday["pending"], tuesday["ring_packages"]] == [1, [], []]
    assert dates_of(env.db, s1) == ["2026-10-27", "2026-10-29", "2026-11-03", "2026-11-05", "2026-11-10"]
    assert dates_of(env.db, s3) == ["2026-10-28", "2026-11-04"]  # 11/11 새벽 1시는 아직 창 밖
    assert standalone_dates(env.db, s2) == [*S2_FIRST_14, "2026-11-10"]
    # 기록은 지난 회차 참여 행에 그대로, 다른 회차 · 다른 사람 행은 안 건드림
    assert [tue1_participant["departed_at"], tue1_participant["arrived_at"]] == [
        iso("2026-10-27T08:10"),
        iso("2026-10-27T08:55"),
    ]
    assert [
        p["participant_id"]
        for p in env.db.rows("appointment_participants")
        if p.get("departed_at") is not None or p.get("arrived_at") is not None
    ] == [tue1_participant["participant_id"]]
    assert_unchanged(env.db, recorded, {}, "②")  # 배치 앞에 있던 모든 행(기록 포함)이 그대로, 새 행만 늘어남
    assert json.dumps(env.db.rows("alarm_events"), default=str) == events_before
    assert (
        participant_of(env.db, occurrence(env.db, s1, "2026-11-10")["appointment_id"]).get("departed_at")
        is None
    )  # 새 회차는 기록 없음
    assert b_rows() == b_json

    # ===== ③ 수 10/28: 앱을 안 열었다고 보고 배치만 =====
    # 수요일 07:30 알람(S2)은 ①에서 미리 만든 행 — 폰이 앱을 안 열었어도 서버에는 이미 있음
    wed_alarm = next(
        a
        for a in env.db.rows("alarms")
        if a.get("series_id") == s2 and a.get("occurrence_on") == "2026-10-28"
    )
    assert [
        wed_alarm["user_id"],
        wed_alarm["alarm_type"],
        hm(wed_alarm["scheduled_at"]),
        ymd(wed_alarm["scheduled_at"]),
        wed_alarm["is_enabled"],
    ] == [
        A,
        "WAKE",
        "07:30",
        "2026-10-28",
        True,
    ]
    wed_before = snapshot(env.db)

    # 아침 8시 배치: 창 끝이 11/11 8:00 — S3 11/11 새벽 1시 회차(길찾기 필요)와 S2 11/11 7:30 알람이 들어옴. 카카오가 RATE_LIMIT를 한 번 터뜨림
    kakao.state.fail = "RATE_LIMIT"
    wed_run = await roll(env, "batch", kst("2026-10-28T08:00"), kakao.lookup)
    assert [["S2" if c["series_id"] == s2 else "?", c["occurrence_on"]] for c in wed_run["created"]] == [
        ["S2", "2026-11-11"]
    ]  # 길찾기 안 쓰는 알람은 계속 만들어짐
    assert wed_run["kakao_calls"] == 1  # 실패한 한 번 뒤로 그 실행은 길찾기를 멈춤
    assert wed_run["pending"] == [{"series_id": s3, "occurrence_on": "2026-11-11", "reason": "ROUTE_RETRY"}]
    assert occurrence(env.db, s3, "2026-11-11") is None
    assert attempts(env.db) == ["2026-11-11 1 RATE_LIMIT 2026-10-29 00:00"]  # 다음 날 0시 뒤 다시
    assert_unchanged(env.db, wed_before, {}, "③ RATE_LIMIT")

    # 같은 날 밤 11:50: 아직 기다리는 중 — 만들 것도 길찾기도 없음
    kakao.state.fail = None
    calls_before = kakao.state.calls
    wait = await roll(env, "batch", kst("2026-10-28T23:50"), kakao.lookup)
    assert [wait["created"], wait["kakao_calls"], kakao.state.calls - calls_before] == [[], 0, 0]
    assert wait["pending"] == []
    assert attempts(env.db) == ["2026-11-11 1 RATE_LIMIT 2026-10-29 00:00"]

    # 10/29 0시 5분 배치: 기다림이 끝나 S3 11/11 회차가 만들어짐 (기록은 지워짐)
    retry = await roll(env, "batch", kst("2026-10-29T00:05"), kakao.lookup)
    assert [["S3" if c["series_id"] == s3 else "?", c["occurrence_on"]] for c in retry["created"]] == [
        ["S3", "2026-11-11"]
    ]
    assert [retry["kakao_calls"], retry["pending"]] == [1, []]
    assert attempts(env.db) == []
    assert dates_of(env.db, s3) == ["2026-10-28", "2026-11-04", "2026-11-11"]
    assert b_rows() == b_json

    # ===== ④ 목 10/29 =====
    # 아침 9:30 배치: 창 끝이 11/12 9:30 — S1 11/12 목 9시(길찾기 1번)와 S2 11/12 7:30
    thu_first = await roll(env, "batch", kst("2026-10-29T09:30"), kakao.lookup)
    assert sorted([label(c), c["occurrence_on"]] for c in thu_first["created"]) == [
        ["S1", "2026-11-12"],
        ["S2", "2026-11-12"],
    ]
    assert thu_first["kakao_calls"] == 1

    # 이어서 배치를 한 번 더: 변화 없음 — 만든 것 0 · 길찾기 0 · 어떤 행도 안 바뀜
    settled = snapshot(env.db)
    calls = kakao.state.calls
    thu_second = await roll(env, "batch", kst("2026-10-29T09:30"), kakao.lookup)
    assert [
        thu_second["created"],
        thu_second["pending"],
        thu_second["moved_alarm_ids"],
        thu_second["canceled_alarm_ids"],
        thu_second["kakao_calls"],
    ] == [[], [], [], [], 0]
    assert kakao.state.calls == calls
    assert_unchanged(env.db, settled, {}, "④")
    assert [len(env.db.rows(t)) for t in TABLES] == [len(settled[t]) for t in TABLES]

    # S1은 화 · 목에만, 수 · 금 회차 없음. 날짜는 겹치지 않음
    s1_dates = dates_of(env.db, s1)
    assert s1_dates == ["2026-10-27", "2026-10-29", "2026-11-03", "2026-11-05", "2026-11-10", "2026-11-12"]
    assert [weekday(d) for d in s1_dates] == ["화", "목", "화", "목", "화", "목"]
    assert len(set(s1_dates)) == len(s1_dates)
    assert (
        len(
            [
                a
                for a in env.db.rows("appointments")
                if a["series_id"] == s1 and weekday(a["occurrence_on"]) in ("수", "금")
            ]
        )
        == 0
    )
    assert all(
        hm(a["appointment_at"]) == "09:00" and ymd(a["appointment_at"]) == a["occurrence_on"]
        for a in env.db.rows("appointments")
        if a["series_id"] == s1
    )

    # 일정 없는 알람(S2)의 같은 날 7:30 기상이 있어도 S1 회차마다 기상 · 준비 · 출발 · 도착이 모두 생김
    for date in s1_dates:
        assert lines(env.db, occurrence(env.db, s1, date)["appointment_id"]) == [
            "WAKE 07:10 on",
            "PREPARE 07:40 on",
            "DEPARTURE 08:00 on",
            "ARRIVE 08:50 on",
        ], date

    # S3 새벽 1시 약속: 회차는 수요일, 기상(화요일 밤 23시대)은 전날 밤 — 알람 시각 날짜 ≠ 약속 날짜
    s3_dates = dates_of(env.db, s3)
    assert [weekday(d) for d in s3_dates] == ["수", "수", "수"]
    for date, eve in (
        ("2026-10-28", "2026-10-27"),
        ("2026-11-04", "2026-11-03"),
        ("2026-11-11", "2026-11-10"),
    ):
        row = occurrence(env.db, s3, date)
        assert hm(row["appointment_at"]) == "01:00"
        wake = next(a for a in alarms_of(env.db, row["appointment_id"]) if a["alarm_type"] == "WAKE")
        assert weekday(ymd(wake["scheduled_at"])) == "화"
        assert [ymd(wake["scheduled_at"]), hm(wake["scheduled_at"])[:2]] == [eve, "23"]
        assert ymd(wake["scheduled_at"]) != row["occurrence_on"]
        assert ymd(row["appointment_at"]) == row["occurrence_on"]

    # 마무리: 카카오는 새 약속 회차(S1 5 + S3 2)마다 한 번, 거기에 RATE_LIMIT로 실패한 한 번
    assert kakao.state.calls == 5 + 2 + 1
    # A의 행은 모두 A 몫, B의 일정 한 줄은 끝까지 그대로
    assert len([a for a in env.db.rows("appointments") if a["creator_id"] == A]) == 6 + 3
    assert len([p for p in env.db.rows("appointment_participants") if p["user_id"] == A]) == 6 + 3
    assert len([a for a in env.db.rows("alarms") if a["user_id"] == A]) == 9 * 4 + 17
    assert len([a for a in env.db.rows("appointments") if a["creator_id"] == B]) == 1
    assert b_rows() == b_json
    assert env.warns == []

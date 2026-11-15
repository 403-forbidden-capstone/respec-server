"""본 레포 supabase/functions/_shared/roll.test.ts의 서버판 (기준 b140f70) — alarm-roll 본문(roll.py)

가짜 DB(tests/fake_db.py FakeDb) + 가짜 카카오(분 고정, 부른 수 셈)로. 달력: 2026-10-26 월 · 27 화 · 28 수 · 29 목 · 30 금 · 11-02 월 · 03 화 · 04 수 · 05 목
요청 · 컨텍스트 객체(commit · edit)는 TS 키 이름 그대로의 dict로 넘김.
"""

import asyncio
import copy
import functools
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.read import Reader
from app.kakao import KakaoError
from app.shared.calc import RING_KINDS, Occurrence, prepare_source_label, prepare_stats_of
from app.shared.commit import CommitCtx, commit_standalone, persist_appointment
from app.shared.edit_run import EditCtx, delete_appointment, update_appointment
from app.shared.js import from_ms, ms, parse_iso, to_iso
from app.shared.plan import PlanContext, build_plan
from app.shared.requests import read_commit_request
from app.shared.roll import (
    FALLBACK_BEFORE_FIRST_ALARM_HOURS,
    RollBase,
    RollDeps,
    RollPlanCtx,
    first_alarm_at,
    inherited_kinds,
    plan_occurrence,
    read_roll_request,
    retry_at,
    routine_step_starts,
    run_roll,
    shift_manual_times,
)
from app.shared.route import TravelChoice
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


HOME = {"lat": 37.5547, "lng": 126.9707}
SCHOOL = {"lat": 37.55, "lng": 127.07}


def _get(obj: Any, name: str) -> Any:
    """결과 객체가 dict든 dataclass든 읽음 (다른 묶음의 반환 모양을 가정하지 않으려고)"""
    return obj[name] if isinstance(obj, dict) else getattr(obj, name)


# ---- 도구 ----


@dataclass
class Env:
    db: FakeDb
    logs: list[str] = field(default_factory=list)
    warns: list[str] = field(default_factory=list)


def setup(now: datetime, users: list[str] | None = None) -> Env:
    db = FakeDb().with_roll_rpc().with_commit_rpc()
    db.now = now
    for user_id in users if users is not None else [A]:
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


# 가짜 카카오 — 분 고정, 부른 수를 셈. script에 실패 이름이나 분을 줄 세우면 부를 때마다 하나씩 꺼내 씀 (다 쓰면 fail 또는 minutes)
def fake_kakao(minutes: int = 50) -> SimpleNamespace:
    state = SimpleNamespace(calls=0, minutes=minutes, partial=False, fail=None, script=[])

    async def lookup(**_: Any) -> dict[str, Any]:
        state.calls += 1
        nxt = state.script.pop(0) if state.script else state.fail
        if isinstance(nxt, str):
            raise KakaoError(nxt)
        return {"minutes": nxt if nxt is not None else state.minutes, "partial": state.partial}

    return SimpleNamespace(state=state, lookup=lookup)


def roll(
    env: Env,
    path: str,
    now: datetime,
    *,
    user_id: str | None = None,
    series_id: str | None = None,
    lookup: Any = None,
    limits: dict[str, Any] | None = None,
):
    env.db.now = now
    deps = RollDeps(
        path=path,  # type: ignore[arg-type]
        now=now,
        read=env.db,
        write=env.db,
        lookup=lookup,
        limits=limits,
        log=env.logs.append,
        warn=env.warns.append,
    )
    if path == "own":
        return run_roll(deps, user_id=user_id or A, series_id=series_id)
    return run_roll(deps)


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


def occurrence(db: FakeDb, series: str, date: str) -> dict[str, Any] | None:
    return next(
        (a for a in db.rows("appointments") if a["series_id"] == series and a["occurrence_on"] == date), None
    )


def dates_of(db: FakeDb, series: str) -> list[str]:
    return sorted(a["occurrence_on"] for a in db.rows("appointments") if a["series_id"] == series)


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


# 시험용 간단 행 (alarm-commit을 거치지 않고) — 많이 만들 때
# ats마다 회차 한 줄 (첫 줄의 id가 묶음 번호)
_raw_seq = 0


def raw_series(db: FakeDb, user: str, *, ats: list[str], rule: str, title: str | None = None) -> str:
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


# 같은 경로를 실제로 다닌 기록 (출발 · 도착이 찍힌 참여 한 줄)
_sample_seq = 0


def add_sample(db: FakeDb, user: str, minutes: int, *, actual: int | None = None) -> None:
    global _sample_seq
    _sample_seq += 1
    appointment_id = f"ap-sample-{_sample_seq}"
    departed = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=_sample_seq)
    db.rows("appointments").append(
        {
            "appointment_id": appointment_id,
            "creator_id": user,
            "group_id": None,
            "title": "지난 일정",
            "appointment_at": to_iso(departed),
            "dest_name": "세종대학교",
            "dest_lat": SCHOOL["lat"],
            "dest_lng": SCHOOL["lng"],
            "importance": "NORMAL",
            "recurrence_rule": None,
            "series_id": appointment_id,
            "occurrence_on": local_date_key(departed, "Asia/Seoul"),
        }
    )
    db.rows("appointment_participants").append(
        {
            "participant_id": f"pt-sample-{_sample_seq}",
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
            "departed_at": to_iso(departed),
            "arrived_at": to_iso(from_ms(ms(departed) + minutes * 60_000)),
            "actual_travel_minutes": actual,
        }
    )


# ===========================================================================
# 두 길 · 응답 모양 · 같은 요청 두 번
# ===========================================================================

MON_10 = kst("2026-10-26T10:00")


@async_test
async def test_own_path_creates_14_day_occurrences_with_ring_packages():
    """본인 길: 앞으로 14일 안 회차를 회차마다 길찾기 1번씩 만들고, 응답에 폰에 걸 꾸러미 · 호출 수를 담음"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)  # 첫 회차 화 10/27 (알람 4개)
    kakao = fake_kakao(35)
    res = await roll(env, "own", MON_10, lookup=kakao.lookup)

    # 화 · 목, 가까운 순. 11/10 화는 창(11/9 10시) 밖
    assert [c["occurrence_on"] for c in res["created"]] == ["2026-10-29", "2026-11-03", "2026-11-05"]
    assert all(c["series_id"] == series and c["appointment_id"] for c in res["created"])
    assert res["kakao_calls"] == 3
    assert kakao.state.calls == 3
    assert [res["pending"], res["moved_alarm_ids"], res["canceled_alarm_ids"]] == [[], [], []]
    assert dates_of(env.db, series) == ["2026-10-27", "2026-10-29", "2026-11-03", "2026-11-05"]

    thu = occurrence(env.db, series, "2026-10-29")
    assert hm(thu["appointment_at"]) == "09:00"
    assert thu["series_id"] == series
    assert thu["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=TU,TH"  # 모든 회차가 같은 규칙
    p = participant_of(env.db, thu["appointment_id"])
    assert (
        p["user_id"],
        p["est_travel_minutes"],
        p["travel_source"],
        p["transport_type"],
        p["buffer_minutes"],
    ) == (
        A,
        35,
        "API",
        "TRANSIT",
        10,
    )
    # 출발 = 9:00 − 여유 10 − 이동 35
    assert lines(env.db, thu["appointment_id"]) == [
        "WAKE 07:25 on",
        "PREPARE 07:55 on",
        "DEPARTURE 08:15 on",
        "ARRIVE 08:50 on",
    ]

    # 꾸러미: 새 회차 3개 × 켜진 알람 4개, 시각 순. alarm-commit과 같은 조립 함수
    assert len(res["ring_packages"]) == 12
    at_millis = [p["atMillis"] for p in res["ring_packages"]]
    assert at_millis == sorted(at_millis)
    first = res["ring_packages"][0]
    assert [first["type"], first["title"], first["subtitle"], first["alarmId"]] == [
        "WAKE",
        "세미나",
        "오전 9:00 · 세종대학교",
        alarms_of(env.db, thu["appointment_id"])[0]["alarm_id"],
    ]
    assert first["departAt"] == ms(kst("2026-10-29T08:15"))

    assert [json.loads(line) for line in env.logs] == [
        {"fn": "alarm-roll", "path": "own", "kakao_calls": 3, "created": 3, "pending": 0}
    ]
    assert env.warns == []


@async_test
async def test_batch_path_creates_same_without_packages():
    """배치 길: 같은 회차를 만들고 꾸러미는 비움 (개수만 로그)"""
    env = setup(MON_10)
    await seed(env, A, slot(), MON_10)
    kakao = fake_kakao()
    res = await roll(env, "batch", MON_10, lookup=kakao.lookup)
    assert len(res["created"]) == 3
    assert res["ring_packages"] == []
    assert res["kakao_calls"] == 3
    assert [json.loads(line) for line in env.logs] == [
        {"fn": "alarm-roll", "path": "batch", "kakao_calls": 3, "created": 3, "pending": 0}
    ]
    assert A not in env.logs[0]  # 로그에 사용자 id 없음


@async_test
async def test_same_request_twice_is_idempotent():
    """같은 요청을 두 번 보내면 같은 결과: 두 번째는 만들 것도 길찾기도 없음 (본인 · 배치 섞어서)"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    kakao = fake_kakao()
    await roll(env, "own", MON_10, lookup=kakao.lookup)

    def tables() -> str:
        return json.dumps(
            [env.db.rows("appointments"), env.db.rows("appointment_participants"), env.db.rows("alarms")],
            default=str,
        )

    before = tables()
    for path in ("own", "batch", "own"):
        again = await roll(env, path, MON_10, lookup=kakao.lookup)
        assert [again["created"], again["pending"], again["kakao_calls"]] == [[], [], 0], path
    assert tables() == before
    assert kakao.state.calls == 3
    assert len(dates_of(env.db, series)) == 4


@async_test
async def test_concurrent_own_and_batch_create_each_occurrence_once():
    """본인 길 · 배치가 동시에 같은 회차를 보면 회차는 한 번만 (길찾기만 두 번 쓸 수 있음)"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    kakao = fake_kakao()
    own, batch = await asyncio.gather(
        roll(env, "own", MON_10, lookup=kakao.lookup), roll(env, "batch", MON_10, lookup=kakao.lookup)
    )
    assert len(own["created"]) + len(batch["created"]) == 3
    assert dates_of(env.db, series) == ["2026-10-27", "2026-10-29", "2026-11-03", "2026-11-05"]
    assert len(env.db.rows("appointment_participants")) == 4
    assert len(env.db.rows("alarms")) == 16


# ===========================================================================
# 사용자 경계
# ===========================================================================

USER_COLUMN = {
    "appointments": "creator_id",
    "appointment_participants": "user_id",
    "alarms": "user_id",
    "todos": "user_id",
    "roll_attempts": "user_id",
    "routines": "user_id",
    "profiles": "user_id",
    "daily_records": "user_id",
}


class _Watch:
    """쿼리 하나를 감싸서 eq 조건을 기억했다가, await 하는 순간 사용자 조건이 있는지 봄"""

    def __init__(self, query: Any, table: str, users: list[str], violations: list[str]) -> None:
        self._query = query
        self._table = table
        self._users = users
        self._violations = violations
        self._eqs: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._query, name)
        if not callable(value):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            if name == "eq":
                self._eqs[args[0]] = args[1]
            if name in ("insert", "upsert"):
                column = USER_COLUMN.get(self._table)
                rows = args[0] if isinstance(args[0], list) else [args[0]]
                owners = {r.get(column) for r in rows}
                if len(owners) == 1 and column:
                    self._eqs[column] = next(iter(owners))
            out = value(*args, **kwargs)
            return self if out is self._query else out

        return call

    def __await__(self):
        column = USER_COLUMN.get(self._table)
        if not column or self._eqs.get(column) not in self._users:
            self._violations.append(f"{self._table} 읽기 · 쓰기에 {column or '?'} 조건이 없음")
        return self._query.__await__()


class _GuardedDb:
    def __init__(self, db: FakeDb, users: list[str], violations: list[str]) -> None:
        self._db = db
        self._users = users
        self._violations = violations

    def from_(self, table: str) -> _Watch:
        return _Watch(self._db.from_(table), table, self._users, self._violations)

    def rpc(self, name: str, args: dict[str, Any] | None = None) -> Any:
        if (
            name == "roll_insert_occurrence"
            and (args or {})["p_appointment"]["creator_id"] not in self._users
        ):
            self._violations.append("roll_insert_occurrence의 creator_id가 사용자가 아님")
        return self._db.rpc(name, args)


@async_test
async def test_batch_does_not_touch_other_users_rows():
    """배치는 남의 행을 안 건드림 (사용자 둘): 모든 쿼리가 사용자로 좁혀지고, 이어받기 · 기록 · 그날 첫 일정이 사람마다 따로"""
    env = setup(MON_10, [A, B])
    a = await seed(env, A, slot(), MON_10)
    b = await seed(
        env,
        B,
        slot(
            appointment={
                **slot()["appointment"],
                "title": "동아리",
                "destination": {"name": "학생회관", "lat": 37.56, "lng": 127.0},
            }
        ),
        MON_10,
    )
    # B에게만 있는 것: 목요일 7:30 일회성 약속(켜진 기상 있음) · 같은 경로 이동 기록 3건 · 켜진 알람 많이
    await seed(
        env,
        B,
        {
            "appointment": {
                "title": "새벽 운동",
                "appointment_at": iso("2026-10-29T07:30"),
                "destination": {"name": "체육관", "lat": 37.5, "lng": 127.0},
            },
            "participant": slot()["participant"],
        },
        MON_10,
    )
    for _ in range(3):
        add_sample(env.db, B, 20)
    tables = ["profiles", "appointments", "appointment_participants", "alarms"]
    before = {t: copy.deepcopy(env.db.rows(t)) for t in tables}

    violations: list[str] = []
    db = _GuardedDb(env.db, [A, B], violations)
    env.db.now = MON_10
    kakao = fake_kakao()
    kakao.state.fail = "TIMEOUT"  # 길찾기가 안 되는 날 — A 몫 폴백이 B의 기록(CACHED)을 가져다 쓰면 안 됨. 첫 알람이 12시간 밖이라 둘 다 미룸
    res = await run_roll(
        RollDeps(
            path="batch",
            now=MON_10,
            read=db,
            write=db,
            lookup=kakao.lookup,
            limits={"lookups": 50},
            log=lambda _: None,
            warn=env.warns.append,
        )
    )
    assert violations == []
    assert res["created"] == []
    assert len(res["pending"]) == 6  # A 3 + B 3 (B의 새벽 운동은 반복이 아님)
    assert len(attempts(env.db)) == 6

    # 성공하는 날: 사람마다 자기 묶음에만
    kakao.state.fail = None
    env.db.tables["roll_attempts"] = []
    ok = await run_roll(
        RollDeps(
            path="batch",
            now=MON_10,
            read=db,
            write=db,
            lookup=kakao.lookup,
            limits={"lookups": 50},
            log=lambda _: None,
            warn=env.warns.append,
        )
    )
    assert violations == []
    assert len(ok["created"]) == 6
    assert dates_of(env.db, a) == ["2026-10-27", "2026-10-29", "2026-11-03", "2026-11-05"]
    assert dates_of(env.db, b) == ["2026-10-27", "2026-10-29", "2026-11-03", "2026-11-05"]
    for row in env.db.rows("appointments"):
        participant = participant_of(env.db, row["appointment_id"])
        assert participant["user_id"] == row["creator_id"]
        assert all(al["user_id"] == row["creator_id"] for al in alarms_of(env.db, row["appointment_id"]))
    # 이어받기도 자기 것에서: B의 동아리 약속 장소(학생회관)가 A 회차로 새지 않음
    a_nov3, b_nov3 = occurrence(env.db, a, "2026-11-03"), occurrence(env.db, b, "2026-11-03")
    assert [a_nov3["title"], a_nov3["dest_name"], b_nov3["title"], b_nov3["dest_name"]] == [
        "세미나",
        "세종대학교",
        "동아리",
        "학생회관",
    ]
    # 그날 첫 일정은 사람마다: B는 목요일 7:30 약속이 앞서서 9시 회차에 기상이 없고, A는 있음
    a_thu = occurrence(env.db, a, "2026-10-29")["appointment_id"]
    b_thu = occurrence(env.db, b, "2026-10-29")["appointment_id"]
    assert [x.split(" ")[0] for x in lines(env.db, a_thu)] == ["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]
    assert [x.split(" ")[0] for x in lines(env.db, b_thu)] == ["PREPARE", "DEPARTURE", "ARRIVE"]
    # 처음 있던 행은 하나도 안 바뀜
    for table in tables:
        for old in before[table]:
            found = next(
                (
                    r
                    for r in env.db.rows(table)
                    if json.dumps(r, sort_keys=True, default=str)
                    == json.dumps(old, sort_keys=True, default=str)
                ),
                None,
            )
            assert found, f"{table}의 기존 행이 바뀌었어요"


@async_test
async def test_fallback_does_not_use_other_users_travel_records():
    """A의 폴백은 B의 이동 기록을 안 씀: 길찾기가 안 되고 첫 알람이 12시간 안이면 A는 ESTIMATE (B만 3건이 있음)"""
    now = kst("2026-10-28T20:00")
    env = setup(MON_10, [A, B])
    await seed(env, A, slot(), MON_10)
    await seed(env, B, slot(), MON_10)
    for _ in range(3):
        add_sample(env.db, B, 20)
    kakao = fake_kakao()
    kakao.state.fail = "TIMEOUT"
    res = await roll(env, "batch", now, lookup=kakao.lookup, limits={"lookups": 50})

    def source_of(user: str) -> list[Any]:
        return [
            participant_of(env.db, x["appointment_id"])["travel_source"]
            for x in env.db.rows("appointments")
            if x["creator_id"] == user and x["occurrence_on"] == "2026-10-29"
        ]

    assert source_of(A) == ["ESTIMATE"]  # 목 7:10 기상 — 지금(수 20시)부터 11시간 10분
    assert source_of(B) == ["CACHED"]
    assert len(res["created"]) == 2


@async_test
async def test_own_path_ignores_other_users_ids_in_body():
    """본인 길: 요청 본문에 남의 id를 넣어도 내 행만 생김 (creator_id · user_id는 검증한 로그인 사용자에서만)"""
    env = setup(MON_10, [A, B])
    a = await seed(env, A, slot(), MON_10)
    b = await seed(env, B, slot(), MON_10)
    # 본문은 series_id만 읽음 — 남의 id를 담은 칸은 무시
    assert read_roll_request(
        Reader.root({"series_id": b, "creator_id": B, "user_id": B, "appointment": {"creator_id": B}})
    ) == {"seriesId": b}
    assert read_roll_request(Reader.root({})) == {}
    broken = Reader.root({"series_id": "not-a-uuid"})
    read_roll_request(broken)
    with pytest.raises(Exception, match="입력값"):
        broken.finish()

    kakao = fake_kakao()
    res = await roll(
        env, "own", MON_10, user_id=A, series_id=b, lookup=kakao.lookup
    )  # 남의 묶음을 먼저 하라고 해도
    assert len(res["created"]) == 3
    assert dates_of(env.db, b) == ["2026-10-27"]  # B 묶음은 그대로
    assert dates_of(env.db, a) == ["2026-10-27", "2026-10-29", "2026-11-03", "2026-11-05"]
    creators = [
        c["args"]["p_appointment"]["creator_id"]
        for c in env.db.rpc_calls
        if c["name"] == "roll_insert_occurrence"
    ]
    assert creators == [A, A, A]
    assert all(x["creator_id"] == A for x in env.db.rows("appointments") if x["series_id"] == a)


# ===========================================================================
# 이어받기 (설계 메모 2절 표)
# ===========================================================================


@async_test
async def test_inheritance_table():
    """이어받기: 만들 종류 · 끈 종류 · 직접 시각(같은 시:분) · 직접 강도 · 출발지 · 수단 · 여유 · 직접 이동시간 · 준비물 · 메모 · 반경"""
    env = setup(MON_10)
    body = slot(
        participant={
            "origin": {"name": "기숙사", "lat": 37.5, "lng": 127.0},
            "transport_type": "SUBWAY",
            "est_travel_minutes": 40,
            "travel_source": "MANUAL",
            "buffer_minutes": 15,
            "before_prepare_minutes": 30,
        },
        kinds=["WAKE", "DEPARTURE"],  # 준비 알람은 안 만듦
        enabled={"WAKE": False},  # 기상은 꺼 둠
        manual_times={"WAKE": iso("2026-10-27T07:30")},  # 직접 고른 기상 시각
        levels={"vibration": 2},  # 직접 고른 강도
    )
    series = await seed(env, A, body, MON_10)
    base = env.db.rows("appointments")[0]
    base.update({"memo": "교재 지참", "dest_radius_m": 200, "importance": "HIGH"})
    env.db.rows("todos").extend(
        [
            {
                "todo_id": "t1",
                "user_id": A,
                "appointment_id": base["appointment_id"],
                "title": "노트북",
                "todo_type": "ITEM",
                "is_done": True,
            },
            {
                "todo_id": "t2",
                "user_id": A,
                "appointment_id": base["appointment_id"],
                "title": "충전기",
                "todo_type": "ITEM",
                "is_done": False,
            },
            {
                "todo_id": "t3",
                "user_id": A,
                "appointment_id": base["appointment_id"],
                "title": "과제 내기",
                "todo_type": "TASK",
                "is_done": False,
            },  # 준비물이 아님
        ]
    )
    assert lines(env.db, base["appointment_id"]) == [
        "WAKE 07:30 off",
        "DEPARTURE 08:05 on",
        "ARRIVE 08:45 on",
    ]

    kakao = fake_kakao(99)
    res = await roll(env, "own", MON_10, lookup=kakao.lookup)
    assert res["kakao_calls"] == 0  # 직접 이동시간은 길찾기를 안 부름
    thu = occurrence(env.db, series, "2026-10-29")
    p = participant_of(env.db, thu["appointment_id"])
    assert [
        p["origin_name"],
        p["origin_lat"],
        p["transport_type"],
        p["buffer_minutes"],
        p["est_travel_minutes"],
        p["travel_source"],
    ] == [
        "기숙사",
        37.5,
        "SUBWAY",
        15,
        40,
        "MANUAL",
    ]
    # 같은 종류 · 같은 시:분 · 끈 채
    assert lines(env.db, thu["appointment_id"]) == ["WAKE 07:30 off", "DEPARTURE 08:05 on", "ARRIVE 08:45 on"]
    wake = alarms_of(env.db, thu["appointment_id"])[0]
    assert [wake["time_source"], wake["intensity_source"], wake["intensity"]] == [
        "MANUAL",
        "MANUAL",
        "STRONG",
    ]
    assert all(
        a["intensity"] == "STRONG" and a["intensity_source"] == "MANUAL"
        for a in alarms_of(env.db, thu["appointment_id"])
    )
    assert [
        thu["title"],
        thu["dest_name"],
        thu["importance"],
        thu["memo"],
        thu["dest_radius_m"],
        thu["recurrence_rule"],
        thu["group_id"],
    ] == [
        "세미나",
        "세종대학교",
        "HIGH",
        "교재 지참",
        200,
        "FREQ=WEEKLY;BYDAY=TU,TH",
        None,
    ]
    items = [t for t in env.db.rows("todos") if t["appointment_id"] == thu["appointment_id"]]
    assert [[t["title"], t["todo_type"], t["is_done"], t["due_date"]] for t in items] == [
        ["노트북", "ITEM", False, "2026-10-29"],
        ["충전기", "ITEM", False, "2026-10-29"],
    ]
    # 켜진 알람만 꾸러미로 (끈 기상은 폰에 안 걸림)
    assert [x for x in res["ring_packages"] if x["atMillis"] == ms(kst("2026-10-29T07:30"))] == []


@async_test
async def test_friend_applied_time_is_not_inherited_by_next_occurrence():
    """10-22(반복): 기준 회차 알람에 친구가 제안해 주인이 적용한 시각이 있어도 새 회차에 이어받지 않음 — 주인이 직접 골랐던 값이면 그 값, 자동이었으면 새 계산"""

    async def run(original_source: str) -> dict[str, Any]:
        env = setup(MON_10)
        body = slot(
            participant={
                "origin": {"name": "기숙사", "lat": 37.5, "lng": 127.0},
                "transport_type": "SUBWAY",
                "est_travel_minutes": 40,
                "travel_source": "MANUAL",
                "buffer_minutes": 15,
                "before_prepare_minutes": 30,
            },
            kinds=["WAKE", "DEPARTURE"],
            manual_times={"WAKE": iso("2026-10-27T07:30")},
        )
        series = await seed(env, A, body, MON_10)
        base = env.db.rows("appointments")[0]
        wake = next(a for a in alarms_of(env.db, base["appointment_id"]) if a["alarm_type"] == "WAKE")
        wake["original_scheduled_at"] = wake["scheduled_at"]  # 07:30에서 친구가 07:20으로 바꿈
        wake["original_time_source"] = original_source
        wake["scheduled_at"] = to_iso(from_ms(ms(parse_iso(wake["scheduled_at"])) - 10 * 60_000))
        wake["time_source"] = "MANUAL"
        await roll(env, "own", MON_10, lookup=fake_kakao(99).lookup)
        return next(
            a
            for a in alarms_of(env.db, occurrence(env.db, series, "2026-10-29")["appointment_id"])
            if a["alarm_type"] == "WAKE"
        )

    owner = await run("MANUAL")
    assert [hm(owner["scheduled_at"]), owner["time_source"], owner.get("original_scheduled_at")] == [
        "07:30",
        "MANUAL",
        None,
    ]  # 친구 값 07:20이 아니라 주인의 07:30
    auto = await run("AUTO")
    assert hm(auto["scheduled_at"]) != "07:20"
    assert auto["time_source"] != "MANUAL"  # 새 계산이라 직접 고른 값으로 안 굳음
    assert auto.get("original_scheduled_at") is None


@async_test
async def test_api_travel_and_prepare_are_recomputed_per_occurrence():
    """이어받기: 길찾기로 구한 이동시간(API)은 안 이어받고 회차마다 새로 구함, 준비시간도 새로 계산 (공통 P80 + 개인 보정)"""
    env = setup(MON_10)
    for d in range(1, 21):
        env.db.rows("daily_records").append(
            {
                "record_id": f"r{d}",
                "user_id": A,
                "record_date": f"2026-10-{d:02d}",
                "actual_prepare_minutes": 90,
            }
        )  # 내 기상~출발 기록 20건 (P80 90)
    series = await seed(env, A, slot(), MON_10)
    base_prepare = participant_of(env.db, env.db.rows("appointments")[0]["appointment_id"])["prepare_minutes"]
    kakao = fake_kakao()
    kakao.state.script = [35, 70, 55]
    await roll(env, "own", MON_10, lookup=kakao.lookup)
    minutes = [
        participant_of(env.db, occurrence(env.db, series, d)["appointment_id"])
        for d in ("2026-10-29", "2026-11-03", "2026-11-05")
    ]
    assert [[p["est_travel_minutes"], p["travel_source"]] for p in minutes] == [
        [35, "API"],
        [70, "API"],
        [55, "API"],
    ]
    assert all(p["prepare_minutes"] > 0 for p in minutes)
    assert minutes[0]["prepare_minutes"] != 50  # 내 기록(20건, 아침 P80 > 공통)으로 보정
    assert (
        base_prepare == minutes[0]["prepare_minutes"]
    )  # 같은 통계라 첫 회차와도 같음 — 회차마다 같은 식으로 새로 계산


@async_test
async def test_first_event_of_day_is_judged_by_occurrence_date():
    """그날 첫 일정은 회차 날짜 기준: 기준 회차에 기상이 없던 게 그날 다른 기상에 밀려서였으면 새 회차가 첫 일정일 때 기상을 만듦, 사용자가 안 고른 거면 없는 채로"""
    # 화요일에는 7:30 약속(기상 있음)이 앞서서 9시 약속에 기상이 없고, 목요일에는 앞선 약속이 없음
    env = setup(MON_10)
    await seed(
        env,
        A,
        {
            "appointment": {
                "title": "새벽 운동",
                "appointment_at": iso("2026-10-27T07:30"),
                "destination": {"name": "체육관", "lat": 37.5, "lng": 127.0},
            },
            "participant": slot()["participant"],
        },
        MON_10,
    )
    series = await seed(env, A, slot(), MON_10)
    base = next(x for x in env.db.rows("appointments") if x["series_id"] == series)
    assert [x.split(" ")[0] for x in lines(env.db, base["appointment_id"])] == [
        "PREPARE",
        "DEPARTURE",
        "ARRIVE",
    ]
    kakao = fake_kakao()
    await roll(env, "own", MON_10, lookup=kakao.lookup)
    # 목: 첫 일정
    assert [
        x.split(" ")[0] for x in lines(env.db, occurrence(env.db, series, "2026-10-29")["appointment_id"])
    ] == ["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]
    assert [
        x.split(" ")[0] for x in lines(env.db, occurrence(env.db, series, "2026-11-03")["appointment_id"])
    ] == ["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]

    # 사용자가 기상을 안 고른 경우 (첫 일정인데 kinds에서 뺌) — 새 회차도 기상 없음
    env2 = setup(MON_10)
    s2 = await seed(env2, A, slot(kinds=["PREPARE", "DEPARTURE"]), MON_10)
    await roll(env2, "own", MON_10, lookup=fake_kakao().lookup)
    assert [
        x.split(" ")[0] for x in lines(env2.db, occurrence(env2.db, s2, "2026-10-29")["appointment_id"])
    ] == ["PREPARE", "DEPARTURE", "ARRIVE"]

    # 알람 없는 일정(kinds = [])은 새 회차도 알람 없음
    env3 = setup(MON_10)
    s3 = await seed(env3, A, slot(kinds=[]), MON_10)
    await roll(env3, "own", MON_10, lookup=fake_kakao().lookup)
    assert len(alarms_of(env3.db, occurrence(env3.db, s3, "2026-10-29")["appointment_id"])) == 0
    assert len(dates_of(env3.db, s3)) == 4


# ===========================================================================
# 길찾기 실패 · 재시도 · 폴백
# ===========================================================================


@async_test
async def test_route_failure_retries_after_1_3_6_hours():
    """길찾기 실패(TIMEOUT · HTTP · NO_ROUTE): 그 회차는 안 만들고 1 · 3 · 6시간 뒤 다시 — 기다리는 동안은 길찾기도 안 부름"""
    for kind in ("TIMEOUT", "HTTP", "NO_ROUTE"):
        env = setup(MON_10)
        series = await seed(env, A, slot(), MON_10)
        kakao = fake_kakao()
        kakao.state.fail = kind
        first = await roll(env, "own", MON_10, lookup=kakao.lookup)
        assert [len(first["created"]), first["kakao_calls"]] == [0, 3], kind
        assert [[p["occurrence_on"], p["reason"]] for p in first["pending"]] == [
            ["2026-10-29", "ROUTE_RETRY"],
            ["2026-11-03", "ROUTE_RETRY"],
            ["2026-11-05", "ROUTE_RETRY"],
        ]
        assert attempts(env.db) == [
            f"2026-10-29 1 {kind} 2026-10-26 11:00",
            f"2026-11-03 1 {kind} 2026-10-26 11:00",
            f"2026-11-05 1 {kind} 2026-10-26 11:00",
        ]
        assert len(dates_of(env.db, series)) == 1

        # 기다리는 동안: 본인 길도 배치도 길찾기를 안 부름 (배치는 roll_due_series가 대기 묶음을 안 줌)
        for path in ("own", "batch"):
            waiting = await roll(env, path, kst("2026-10-26T10:30"), lookup=kakao.lookup)
            assert [waiting["created"], waiting["pending"], waiting["kakao_calls"]] == [[], [], 0], (
                f"{kind} {path}"
            )
        # 1시간 뒤 또 실패 → 3시간 뒤, 그다음 6시간 뒤, 그 뒤로도 6시간
        waits: list[str] = []
        at = kst("2026-10-26T11:00")
        for _ in range(4):
            await roll(env, "batch", at, lookup=kakao.lookup, limits={"lookups": 50})
            waits.append(attempts(env.db)[0])
            at = parse_iso(env.db.rows("roll_attempts")[0]["next_try_at"])
        assert waits == [
            f"2026-10-29 2 {kind} 2026-10-26 14:00",
            f"2026-10-29 3 {kind} 2026-10-26 20:00",
            f"2026-10-29 4 {kind} 2026-10-27 02:00",
            f"2026-10-29 5 {kind} 2026-10-27 08:00",
        ]
        assert [(ms(retry_at(MON_10, n - 1)) - ms(MON_10)) / 3_600_000 for n in (1, 2, 3, 4, 5, 6)] == [
            1,
            3,
            6,
            6,
            6,
            6,
        ]

        # 길찾기가 되돌아오면 만들고 기록을 지움
        kakao.state.fail = None
        done = await roll(env, "own", from_ms(ms(at) + 1000), lookup=kakao.lookup)
        assert len(done["created"]) == 3, kind
        assert attempts(env.db) == [], kind
        assert len(dates_of(env.db, series)) == 4


@async_test
async def test_throw_after_lookup_records_retry():
    """길찾기를 쓴 뒤 저장(roll_insert_occurrence)이 던지면 재시도 기록을 남겨서, 5분 뒤 실행이 같은 회차로 카카오를 또 안 부름"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    original = env.db.rpc_handlers["roll_insert_occurrence"]
    thrown = {"done": False}

    def flaky(args: dict[str, Any]) -> Any:
        if not thrown["done"]:
            thrown["done"] = True
            raise RuntimeError("시험용 저장 실패 (세종대학교 37.55,127.07)")
        return original(args)

    env.db.rpc_handlers["roll_insert_occurrence"] = flaky
    kakao = fake_kakao()

    # 첫 실행: 가까운 회차(10-29)의 저장이 한 번 던짐 → 길찾기는 이미 썼으니 재시도 기록(tries 1, 1시간 뒤), 나머지 두 회차는 만듦
    first = await roll(env, "own", MON_10, lookup=kakao.lookup)
    assert [len(first["created"]), first["kakao_calls"], kakao.state.calls] == [2, 3, 3]
    assert attempts(env.db) == ["2026-10-29 1 INTERNAL 2026-10-26 11:00"]
    assert "세종" not in json.dumps(env.db.rows("roll_attempts"), ensure_ascii=False), (
        "좌표 · 장소명은 기록에 안 남음"
    )
    assert occurrence(env.db, series, "2026-10-29") is None

    # 5분 뒤: 대기 중이라 카카오 0번 (본인 길 · 배치 모두)
    for path in ("own", "batch"):
        again = await roll(env, path, kst("2026-10-26T10:05"), lookup=kakao.lookup)
        assert [again["created"], again["pending"], again["kakao_calls"]] == [[], [], 0], path
    assert kakao.state.calls == 3

    # 1시간 뒤엔 다시 시도해서 만들고 기록을 지움
    later = await roll(env, "own", kst("2026-10-26T11:00"), lookup=kakao.lookup)
    assert [len(later["created"]), later["kakao_calls"]] == [1, 1]
    assert attempts(env.db) == []


@async_test
async def test_pure_calculation_error_without_lookup_is_skipped_without_record():
    """길찾기를 안 쓴 채 던지는 순수 계산 오류는 기존대로: 재시도 기록 없이 건너뜀 + 로그"""
    env = setup(MON_10)
    await seed(
        env,
        A,
        slot(participant={**slot()["participant"], "travel_source": "MANUAL", "est_travel_minutes": 50}),
        MON_10,
    )

    def always_fail(args: dict[str, Any]) -> Any:
        raise RuntimeError("시험용 저장 실패")

    env.db.rpc_handlers["roll_insert_occurrence"] = always_fail
    kakao = fake_kakao()
    run = await roll(env, "own", MON_10, lookup=kakao.lookup)
    assert [len(run["created"]), kakao.state.calls] == [0, 0]
    assert attempts(env.db) == []
    assert any("series_skipped" in w and "process" in w for w in env.warns)


@async_test
async def test_rate_limit_and_auth_stop_lookups_until_next_midnight():
    """RATE_LIMIT · AUTH: 이번 실행의 길찾기를 멈추고(첫 실패 1번만 씀) 다음 날 0시 뒤 다시"""
    for kind in ("RATE_LIMIT", "AUTH"):
        env = setup(MON_10)
        series = await seed(env, A, slot(), MON_10)
        kakao = fake_kakao()
        kakao.state.fail = kind
        res = await roll(env, "own", MON_10, lookup=kakao.lookup)
        assert [len(res["created"]), res["kakao_calls"], kakao.state.calls] == [0, 1, 1], kind
        assert len(res["pending"]) == 3
        assert all(p["reason"] == "ROUTE_RETRY" for p in res["pending"])
        assert attempts(env.db) == [
            f"2026-10-29 1 {kind} 2026-10-27 00:00",
            f"2026-11-03 0 {kind} 2026-10-27 00:00",
            f"2026-11-05 0 {kind} 2026-10-27 00:00",
        ]

        kakao.state.fail = None
        same = await roll(env, "batch", kst("2026-10-26T23:50"), lookup=kakao.lookup)
        assert [same["created"], same["kakao_calls"]] == [[], 0]
        nxt = await roll(env, "batch", kst("2026-10-27T00:05"), lookup=kakao.lookup)
        assert len(nxt["created"]) == 3, kind
        assert len(dates_of(env.db, series)) == 4
        assert attempts(env.db) == []


@async_test
async def test_missing_lookup_behaves_like_auth():
    """길찾기 키가 없으면(lookup 없음) AUTH와 같게: 호출 0, 다음 날 0시 뒤 다시"""
    env = setup(MON_10)
    await seed(env, A, slot(), MON_10)
    res = await roll(env, "own", MON_10)
    assert [len(res["created"]), res["kakao_calls"], len(res["pending"])] == [0, 0, 3]
    assert attempts(env.db) == [
        "2026-10-29 0 AUTH 2026-10-27 00:00",
        "2026-11-03 0 AUTH 2026-10-27 00:00",
        "2026-11-05 0 AUTH 2026-10-27 00:00",
    ]


@async_test
async def test_fallback_order_within_12_hours():
    """첫 알람 12시간 전까지 길찾기가 안 되면 폴백 순서: 같은 경로 내 실측 3건 이상 중앙값(CACHED) → 직선 근사(ESTIMATE). 12시간 밖인 회차는 계속 미룸"""
    assert FALLBACK_BEFORE_FIRST_ALARM_HOURS == 12
    now = kst("2026-10-28T20:00")  # 목 10/29 9시 약속의 기상(7:10)까지 11시간 10분
    # ① 실측 3건 → CACHED (이동 분은 actual_travel_minutes가 있으면 그 값, 없으면 도착 − 출발)
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    add_sample(env.db, A, 40)
    add_sample(env.db, A, 60, actual=30)  # 도착 − 출발은 60분이지만 실측 분 30
    add_sample(env.db, A, 50)
    kakao = fake_kakao()
    kakao.state.fail = "TIMEOUT"
    res = await roll(env, "own", now, lookup=kakao.lookup)
    thu = participant_of(env.db, occurrence(env.db, series, "2026-10-29")["appointment_id"])
    assert [thu["travel_source"], thu["est_travel_minutes"]] == [
        "CACHED",
        40,
    ]  # 중앙값(30, 40, 50) — 질문 · 확인 없이 바로 저장
    assert [c["occurrence_on"] for c in res["created"]] == ["2026-10-29"]
    assert lines(env.db, occurrence(env.db, series, "2026-10-29")["appointment_id"]) == [
        "WAKE 07:20 on",
        "PREPARE 07:50 on",
        "DEPARTURE 08:10 on",
        "ARRIVE 08:50 on",
    ]
    assert [p["occurrence_on"] for p in res["pending"]] == [
        "2026-11-03",
        "2026-11-05",
        "2026-11-10",
    ]  # 12시간 밖 — 폴백 없이 재시도
    assert attempts(env.db) == [
        "2026-11-03 1 TIMEOUT 2026-10-28 21:00",
        "2026-11-05 1 TIMEOUT 2026-10-28 21:00",
        "2026-11-10 1 TIMEOUT 2026-10-28 21:00",
    ]
    assert res["kakao_calls"] == 4

    # ② 실측이 2건뿐이면 → 직선 근사(ESTIMATE)
    env2 = setup(MON_10)
    s2 = await seed(env2, A, slot(), MON_10)
    add_sample(env2.db, A, 40)
    add_sample(env2.db, A, 50)
    await roll(env2, "own", now, lookup=kakao.lookup)
    estimated = participant_of(env2.db, occurrence(env2.db, s2, "2026-10-29")["appointment_id"])
    assert estimated["travel_source"] == "ESTIMATE"
    assert estimated["est_travel_minutes"] > 0

    # ③ 길찾기를 멈춘 경우(RATE_LIMIT)에도 12시간 안이면 같은 폴백
    env3 = setup(MON_10)
    s3 = await seed(env3, A, slot(), MON_10)
    limited = fake_kakao()
    limited.state.fail = "RATE_LIMIT"
    r3 = await roll(env3, "batch", now, lookup=limited.lookup)
    assert (
        participant_of(env3.db, occurrence(env3.db, s3, "2026-10-29")["appointment_id"])["travel_source"]
        == "ESTIMATE"
    )
    assert [len(r3["created"]), r3["kakao_calls"], len(r3["pending"])] == [1, 1, 3]

    # ④ 좌표가 없어서 근사도 못 내면 폴백 없이 미룸 (출발이 지나면 그 날짜는 건너뜀)
    env4 = setup(MON_10)
    s4 = await seed(env4, A, slot(), MON_10)
    env4.db.rows("appointment_participants")[0].update(
        {"origin_lat": None, "origin_lng": None, "origin_name": "어딘가"}
    )
    env4.db.rows("profiles")[0].update({"home_lat": None, "home_lng": None})
    r4 = await roll(env4, "own", now, lookup=kakao.lookup)
    assert [len(r4["created"]), r4["kakao_calls"], len(dates_of(env4.db, s4))] == [0, 0, 1]
    assert [p["reason"] for p in r4["pending"]] == ["ROUTE_RETRY"] * 4
    assert "NO_COORDS" in attempts(env4.db)[0]


@async_test
async def test_fallback_minutes_are_not_inherited():
    """폴백으로 만든 회차를 기준으로 다음 회차를 만들 때는 그 분(CACHED · ESTIMATE)을 안 이어받고 새로 구함"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    env.db.rows("appointment_participants")[0].update({"travel_source": "ESTIMATE", "est_travel_minutes": 12})
    kakao = fake_kakao(48)
    await roll(env, "own", MON_10, lookup=kakao.lookup)
    p = participant_of(env.db, occurrence(env.db, series, "2026-10-29")["appointment_id"])
    assert [p["travel_source"], p["est_travel_minutes"]] == ["API", 48]


# ===========================================================================
# 하루 몫 · 알람 상한 · 실행 한도
# ===========================================================================


@async_test
async def test_daily_budget_defers_until_next_midnight():
    """하루 몫: 오늘 roll이 만든 회차 + 길찾기를 쓰고도 못 만든 날짜가 한도에 닿으면 DAILY_BUDGET으로 미루고 다음 날 0시 뒤 다시"""
    env = setup(MON_10)
    series = await seed(
        env, A, slot(appointment={**slot()["appointment"], "recurrence_rule": "FREQ=DAILY"}), MON_10
    )
    kakao = fake_kakao()
    kakao.state.script = [35, 35]
    first = await roll(env, "batch", MON_10, lookup=kakao.lookup, limits={"dailyLookups": 2, "lookups": 50})
    assert [len(first["created"]), first["kakao_calls"]] == [2, 2]
    assert len(first["pending"]) > 0 and all(p["reason"] == "DAILY_BUDGET" for p in first["pending"])
    assert len(first["pending"]) == 11  # 매일 반복: 첫 회차 + 2 + 11 = 14
    assert all(
        "DAILY_BUDGET 2026-10-27 00:00" in a and " 0 DAILY_BUDGET" in a for a in attempts(env.db)
    )  # 길찾기를 안 썼으니 tries 그대로
    assert len(dates_of(env.db, series)) == 3

    # 같은 날 더 불러도 늘지 않음 (남은 날짜는 대기 중이라 배치는 묶음을 안 받음)
    same = await roll(env, "own", kst("2026-10-26T12:00"), lookup=kakao.lookup, limits={"dailyLookups": 2})
    assert [same["created"], same["kakao_calls"]] == [[], 0]
    # 다음 날 0시 뒤: 몫이 새로 시작
    nxt = await roll(
        env, "batch", kst("2026-10-27T00:05"), lookup=kakao.lookup, limits={"dailyLookups": 5, "lookups": 50}
    )
    assert [len(nxt["created"]), nxt["kakao_calls"]] == [5, 5]


@async_test
async def test_daily_budget_counts_failed_dates_too():
    """하루 몫 셈: 실패해서 못 만든 날짜도 센다 (오늘 만든 2 + 오늘 실패 1 = 3)"""
    env = setup(MON_10)
    await seed(env, A, slot(appointment={**slot()["appointment"], "recurrence_rule": "FREQ=DAILY"}), MON_10)
    kakao = fake_kakao()
    kakao.state.script = ["TIMEOUT", 35, 35]
    first = await roll(env, "own", MON_10, lookup=kakao.lookup, limits={"lookups": 3, "dailyLookups": 3})
    assert [len(first["created"]), first["kakao_calls"], len(attempts(env.db))] == [2, 3, 1]
    # 1시간 뒤(같은 날) 재시도: 오늘 쓴 길찾기 3번(만든 2 + 실패 1) = 한도 → 길찾기 없이 DAILY_BUDGET
    retry = await roll(
        env, "own", kst("2026-10-26T11:01"), lookup=kakao.lookup, limits={"lookups": 3, "dailyLookups": 3}
    )
    assert retry["kakao_calls"] == 0
    assert len(retry["created"]) == 0
    assert all(p["reason"] == "DAILY_BUDGET" for p in retry["pending"])


@async_test
async def test_run_lookup_limit_leaves_run_limit_pending():
    """실행당 길찾기 한도: 본인 길 4회차, 넘는 회차는 RUN_LIMIT로 남겨 다음 실행(배치)이 만듦"""
    env = setup(MON_10)
    series = await seed(
        env, A, slot(appointment={**slot()["appointment"], "recurrence_rule": "FREQ=DAILY"}), MON_10
    )
    kakao = fake_kakao()
    res = await roll(env, "own", MON_10, lookup=kakao.lookup)
    assert [len(res["created"]), res["kakao_calls"]] == [4, 4]
    assert [c["occurrence_on"] for c in res["created"]] == [
        "2026-10-28",
        "2026-10-29",
        "2026-10-30",
        "2026-10-31",
    ]  # 가까운 회차부터
    assert len(res["pending"]) == 9
    assert all(p["reason"] == "RUN_LIMIT" for p in res["pending"])
    assert attempts(env.db) == []  # 기록 없이 다음 실행으로
    batch = await roll(env, "batch", MON_10, lookup=kakao.lookup)
    assert batch["kakao_calls"] == (20 if 9 > 20 else 9)
    assert len(dates_of(env.db, series)) == 14


@async_test
async def test_requested_series_goes_first():
    """요청의 series_id인 묶음 먼저: 한도가 모자라면 요청한 묶음의 가까운 회차부터"""
    env = setup(MON_10)
    near = await seed(
        env,
        A,
        slot(appointment={**slot()["appointment"], "title": "매일", "recurrence_rule": "FREQ=DAILY"}),
        MON_10,
    )
    wanted = await seed(
        env,
        A,
        slot(
            appointment={**slot()["appointment"], "title": "화목", "appointment_at": iso("2026-10-27T13:00")}
        ),
        MON_10,
    )
    res = await roll(env, "own", MON_10, lookup=fake_kakao().lookup, series_id=wanted)
    assert res["kakao_calls"] == 4
    assert [["화목" if c["series_id"] == wanted else "매일", c["occurrence_on"]] for c in res["created"]] == [
        ["화목", "2026-10-29"],
        ["화목", "2026-11-03"],
        ["화목", "2026-11-05"],
        ["매일", "2026-10-28"],
    ]
    assert near


@async_test
async def test_alarm_cap_stops_creation_without_lookup():
    """앞으로 켜진 알람이 상한(400)이면 더 안 만듦: ALARM_CAP, 길찾기도 안 씀"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    for i in range(400):
        env.db.rows("alarms").append(
            {
                "alarm_id": f"bulk-{i}",
                "user_id": A,
                "participant_id": "pt-bulk",
                "alarm_type": "WAKE",
                "scheduled_at": to_iso(kst("2026-12-01T07:00")),
                "is_enabled": True,
            }
        )
    kakao = fake_kakao()
    res = await roll(env, "own", MON_10, lookup=kakao.lookup)
    assert [len(res["created"]), res["kakao_calls"], len(dates_of(env.db, series))] == [0, 0, 1]
    assert [p["reason"] for p in res["pending"]] == ["ALARM_CAP", "ALARM_CAP", "ALARM_CAP"]
    assert attempts(env.db) == []
    # 꺼진 알람이나 지난 알람은 안 셈
    del env.db.rows("alarms")[len(env.db.rows("alarms")) - 400 :]
    for i in range(400):
        env.db.rows("alarms").append(
            {
                "alarm_id": f"off-{i}",
                "user_id": A,
                "alarm_type": "WAKE",
                "scheduled_at": to_iso(kst("2026-12-01T07:00")),
                "is_enabled": False,
            }
        )
    ok = await roll(env, "own", MON_10, lookup=kakao.lookup)
    assert len(ok["created"]) == 3


@async_test
async def test_alarm_cap_grows_with_created_alarms():
    """상한은 만든 만큼 늘려 가며 적용: 상한 6 · 지금 4개 → 첫 회차(+4)까지 만들고 나머지는 ALARM_CAP"""
    env = setup(MON_10)
    await seed(env, A, slot(), MON_10)
    kakao = fake_kakao()
    res = await roll(env, "own", MON_10, lookup=kakao.lookup, limits={"alarmCap": 6})
    assert [c["occurrence_on"] for c in res["created"]] == ["2026-10-29"]
    assert [[p["occurrence_on"], p["reason"]] for p in res["pending"]] == [
        ["2026-11-03", "ALARM_CAP"],
        ["2026-11-05", "ALARM_CAP"],
    ]
    assert res["kakao_calls"] == 1


@async_test
async def test_batch_series_limit_and_time_limit():
    """배치: 일이 있는 묶음 50개까지만, 40초가 지나면 남은 회차는 다음 실행으로"""
    env = setup(MON_10, [f"u-{i}" for i in range(55)])
    for i in range(55):
        # 마지막 회차 11/2 → 11/9 9시가 새 회차 하나
        raw_series(env.db, f"u-{i}", ats=["2026-11-02T09:00:00+09:00"], rule="FREQ=WEEKLY;BYDAY=MO")
    kakao = fake_kakao()
    first = await roll(env, "batch", MON_10, lookup=kakao.lookup, limits={"lookups": 1000})
    assert len(first["created"]) == 50
    second = await roll(env, "batch", MON_10, lookup=kakao.lookup, limits={"lookups": 1000})
    assert len(second["created"]) == 5

    # 시간 제한: 첫 회차 뒤에 40초가 지난 것으로
    env2 = setup(MON_10, ["u-a", "u-b", "u-c"])
    for u in ("u-a", "u-b", "u-c"):
        raw_series(env2.db, u, ats=["2026-11-02T09:00:00+09:00"], rule="FREQ=WEEKLY;BYDAY=MO")
    t = {"v": 0}
    slow = fake_kakao()

    async def slow_lookup(**kw: Any) -> dict[str, Any]:
        t["v"] += 41_000
        return await slow.lookup(**kw)

    res = await roll(env2, "batch", MON_10, lookup=slow_lookup, limits={"lookups": 100})
    assert len(res["created"]) >= 1
    assert len(res["created"]) + len(res["pending"]) == 3
    assert all(p["reason"] == "RUN_LIMIT" for p in res["pending"])


# ===========================================================================
# 한 묶음이 나머지를 멈추지 않음 · 못 읽는 규칙은 정상 건너뜀 · 할 일 없는 묶음은 싸게
# ===========================================================================


@async_test
async def test_series_with_invalid_value_is_skipped_others_continue():
    """잘못된 값(읽을 수 없는 시각)을 가진 묶음은 건너뛰고 로그 한 줄(이유 글자만), 나머지 묶음은 계속"""
    env = setup(MON_10, [A, B])
    bad = raw_series(env.db, B, ats=["2026-11-08T09:00:00+09:00"], rule="FREQ=DAILY", title="비밀 일정")
    next(a for a in env.db.rows("appointments") if a["appointment_id"] == bad)["appointment_at"] = (
        "not-a-date"
    )
    good = await seed(env, A, slot(), MON_10)
    original = env.db.rpc_handlers["roll_due_series"]
    env.db.rpc_handlers["roll_due_series"] = lambda args: [
        {"user_id": B, "series_id": bad, "kind": "APPOINTMENT", "template_id": bad},
        *original(args),
    ]
    res = await roll(env, "batch", MON_10, lookup=fake_kakao().lookup)
    assert len(res["created"]) == 3
    assert len(dates_of(env.db, good)) == 4
    assert [json.loads(w) for w in env.warns] == [
        {"fn": "alarm-roll", "event": "series_skipped", "where": "collect", "reason": "INVALID_VALUE"}
    ]
    assert "비밀" not in env.warns[0] and B not in env.warns[0] and bad not in env.warns[0]


@async_test
async def test_invalid_routine_steps_skip_only_that_routine():
    """잘못된 루틴 단계 값도 그 루틴만 건너뜀"""
    env = setup(kst("2026-10-26T22:00"))
    env.db.rows("routines").extend(
        [
            {
                "routine_id": "r-bad",
                "user_id": A,
                "title": "이상한 루틴",
                "repeat_days": "MON~FRI",
                "is_active": True,
                "steps": [{"order": 1, "title": "x", "offset": 1e308, "minutes": 5, "is_alarm": True}],
            },
            {
                "routine_id": "r-ok",
                "user_id": A,
                "title": "저녁 루틴",
                "repeat_days": "MON~FRI",
                "is_active": True,
                "steps": [
                    {"order": 1, "title": "y", "offset": 21 * 60 + 30, "minutes": 15, "is_alarm": True}
                ],
            },
        ]
    )
    env.db.rows("alarms").extend(
        [
            {
                "alarm_id": "al-bad",
                "user_id": A,
                "routine_id": "r-bad",
                "step_order": 1,
                "alarm_type": "ROUTINE",
                "scheduled_at": to_iso(kst("2026-10-26T21:30")),
                "intensity": "NORMAL",
                "sound_key": None,
                "snooze_limit": 3,
                "is_enabled": True,
            },
            {
                "alarm_id": "al-ok",
                "user_id": A,
                "routine_id": "r-ok",
                "step_order": 1,
                "alarm_type": "ROUTINE",
                "scheduled_at": to_iso(kst("2026-10-26T21:30")),
                "intensity": "NORMAL",
                "sound_key": None,
                "snooze_limit": 3,
                "is_enabled": True,
            },
        ]
    )
    for path in ("own", "batch"):
        env.warns.clear()
        next(a for a in env.db.rows("alarms") if a["alarm_id"] == "al-ok")["scheduled_at"] = to_iso(
            kst("2026-10-26T21:30")
        )
        res = await roll(env, path, kst("2026-10-26T22:00"))
        assert res["moved_alarm_ids"] == ["al-ok"], path
        assert [json.loads(w)["reason"] for w in env.warns] == ["INVALID_VALUE"], path


@async_test
async def test_unsupported_rules_are_skipped_quietly():
    """두 모양(FREQ=DAILY · FREQ=WEEKLY;BYDAY=…) 밖의 규칙(옛 MONTHLY · INTERVAL)은 오류가 아니라 정상 건너뜀: 경고 · 대기 · 길찾기 없음"""
    env = setup(MON_10)
    kakao = fake_kakao()
    for rule in ("FREQ=MONTHLY;BYMONTHDAY=3", "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH", "FREQ=YEARLY", "garbage"):
        raw_series(env.db, A, ats=["2026-10-29T09:00:00+09:00"], rule=rule)
        env.db.rows("alarms").append(
            {
                "alarm_id": f"s-{rule}",
                "user_id": A,
                "participant_id": None,
                "routine_id": None,
                "series_id": f"s-{rule}",
                "occurrence_on": "2026-10-28",
                "alarm_type": "WAKE",
                "scheduled_at": iso("2026-10-28T07:30"),
                "is_enabled": True,
                "recurrence_rule": rule,
            }
        )
    for path in ("own", "batch"):
        res = await roll(env, path, MON_10, lookup=kakao.lookup)
        assert [res["created"], res["pending"], res["kakao_calls"]] == [[], [], 0], path
    assert env.warns == []
    assert len(env.db.rows("appointments")) == 4


@async_test
async def test_empty_series_do_not_starve_busy_series():
    """할 일 없는 묶음(마지막 회차가 +14일 안인 주간 묶음)이 앞줄을 채워도 일이 있는 묶음이 안 굶고, 빈 묶음은 기준 회차 한 줄만 읽고 넘김"""
    now = kst("2026-10-26T08:00")  # 창 끝 11/9 8시
    users = [f"e-{i}" for i in range(60)]
    env = setup(now, [*users, A])
    # 빈 묶음 60개: 주간 월요일 — 10/26 · 11/2 9시가 있고 다음 11/9 9시는 창 밖이라 만들 게 없음. roll_due_series는 마지막 회차가 +14일 안이라 이들을 줌 (due_at이 더 이르니 앞줄)
    for u in users:
        raw_series(
            env.db,
            u,
            ats=["2026-10-26T09:00:00+09:00", "2026-11-02T09:00:00+09:00"],
            rule="FREQ=WEEKLY;BYDAY=MO",
        )
    # 일이 있는 묶음 (뒷줄): 매일 7시, 10/27 ~ 11/8이 있고 11/9 7시가 새 회차
    days = [to_iso(from_ms(ms(kst("2026-10-27T07:00")) + i * 86_400_000)) for i in range(13)]
    raw_series(env.db, A, ats=days, rule="FREQ=DAILY")
    due = [
        r["user_id"] for r in env.db.rpc_handlers["roll_due_series"]({"p_now": to_iso(now), "p_limit": 50})
    ]
    assert A not in due  # 한도 50만 읽으면 이 묶음은 굶었을 것
    assert len(due) == 50

    calls0 = len(env.db.calls)
    kakao = fake_kakao()
    res = await roll(env, "batch", now, lookup=kakao.lookup)
    assert [c["occurrence_on"] for c in res["created"]] == ["2026-11-09"]
    assert res["kakao_calls"] == 1
    used = env.db.calls[calls0:]
    assert len([c for c in used if c == "insert appointments"]) == 0
    assert (
        len([c for c in used if c == "select appointment_participants"]) <= 3
    )  # 빈 묶음은 참여 조건 · 알람 · 준비물을 읽지 않음
    assert len([c for c in used if c == "select todos"]) == 1
    assert (
        len([c for c in used if c == "select roll_attempts"]) == 2
    )  # 재시도 기록 읽기 + 하루 몫 셈 — 둘 다 만들 회차가 남은 사람(A)만
    assert len([c for c in used if c == "select profiles"]) == 61  # 사람마다 한 번
    assert (
        len([c for c in used if c == "select appointments"]) <= 2 * 61 + 1
    )  # 빈 묶음 = 사람별 기준 회차 읽기 + 이미 있는 날짜 읽기 한 번씩 (묶음마다가 아님)
    assert env.warns == []


# ===========================================================================
# 날짜 · 창 · 예외
# ===========================================================================


@async_test
async def test_past_days_not_filled_and_window_end_excluded():
    """서버가 며칠 못 돌았어도 지난 날은 안 메움 · 창 끝(지금 + 14일)은 포함 안 함"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    later = kst("2026-11-02T10:00")
    res = await roll(env, "batch", later, lookup=fake_kakao().lookup)
    # 10/29 목은 이미 지난 날
    assert [c["occurrence_on"] for c in res["created"]] == [
        "2026-11-03",
        "2026-11-05",
        "2026-11-10",
        "2026-11-12",
    ]
    assert dates_of(env.db, series)[:2] == ["2026-10-27", "2026-11-03"]
    edge = setup(kst("2026-11-03T09:00"))
    s2 = await seed(edge, A, slot(), MON_10)
    await roll(edge, "batch", kst("2026-11-03T09:00"), lookup=fake_kakao().lookup)
    assert "2026-11-17" not in dates_of(edge.db, s2)  # 11/17 9시 = 정확히 지금 + 14일
    assert "2026-11-12" in dates_of(edge.db, s2)


@async_test
async def test_moved_row_does_not_duplicate_date():
    """기준 회차의 날짜 칸(occurrence_on)과 약속 시각의 날짜가 어긋나도(시각을 옮긴 행) 같은 날짜를 두 번 만들지 않음"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    # 11/5 목 회차를 만들어 둔 뒤 그 행의 시각만 11/4 수 23:00으로 옮김 — occurrence_on은 11/5 그대로
    kakao = fake_kakao()
    await roll(env, "own", MON_10, lookup=kakao.lookup)
    occurrence(env.db, series, "2026-11-05").update({"appointment_at": iso("2026-11-04T23:00")})
    calls = kakao.state.calls
    res = await roll(env, "own", kst("2026-11-02T10:00"), lookup=kakao.lookup)
    # 11/5는 이미 있으니 건너뜀 (약속 시각의 날짜인 11/4부터 이어 세지 않음)
    assert [c["occurrence_on"] for c in res["created"]] == ["2026-11-10", "2026-11-12"]
    assert kakao.state.calls - calls == 2
    dates = dates_of(env.db, series)
    assert len(set(dates)) == len(dates)


@async_test
async def test_one_am_appointment_uses_local_date_and_previous_night_wake():
    """새벽 1시 약속: 회차 날짜는 내 시간대(한국) 날짜, 기상은 전날 밤"""
    env = setup(MON_10)
    series = await seed(
        env,
        A,
        slot(
            appointment={
                **slot()["appointment"],
                "appointment_at": iso("2026-10-28T01:00"),
                "recurrence_rule": "FREQ=WEEKLY;BYDAY=WE",
            }
        ),
        MON_10,
    )
    res = await roll(env, "own", MON_10, lookup=fake_kakao().lookup)
    assert [c["occurrence_on"] for c in res["created"]] == ["2026-11-04"]
    row = occurrence(env.db, series, "2026-11-04")
    assert row["appointment_at"] == "2026-11-03T16:00:00.000Z"  # UTC로는 11/3
    assert lines(env.db, row["appointment_id"]) == [
        "WAKE 23:10 on",
        "PREPARE 23:40 on",
        "DEPARTURE 00:00 on",
        "ARRIVE 00:50 on",
    ]
    assert ymd(alarms_of(env.db, row["appointment_id"])[0]["scheduled_at"]) == "2026-11-03"


@async_test
async def test_past_alarms_disabled_and_past_departure_skipped():
    """기상 · 준비 시각이 이미 지났으면 그 알람만 끄고 만듦, 출발까지 지났으면 그 날짜는 건너뛰고 기록(다시 안 만듦)"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    kakao = fake_kakao()
    res = await roll(
        env, "own", kst("2026-10-29T07:20"), lookup=kakao.lookup
    )  # 목 7:20 — 기상(7:10)은 지났고 준비(7:40)는 아직
    thu = occurrence(env.db, series, "2026-10-29")
    assert lines(env.db, thu["appointment_id"]) == [
        "WAKE 07:10 off",
        "PREPARE 07:40 on",
        "DEPARTURE 08:00 on",
        "ARRIVE 08:50 on",
    ]
    assert not any(
        p["type"] == "WAKE" and p["atMillis"] == ms(kst("2026-10-29T07:10")) for p in res["ring_packages"]
    )  # 꺼진 알람은 폰에 안 걸림

    env2 = setup(MON_10)
    s2 = await seed(env2, A, slot(), MON_10)
    late = await roll(env2, "own", kst("2026-10-29T08:30"), lookup=kakao.lookup)  # 출발(8:00)이 지남
    assert [c["occurrence_on"] for c in late["created"]] == ["2026-11-03", "2026-11-05", "2026-11-10"]
    assert occurrence(env2.db, s2, "2026-10-29") is None
    assert attempts(env2.db) == ["2026-10-29 1 DEPARTURE_PASSED skipped"]
    calls = kakao.state.calls
    again = await roll(env2, "batch", kst("2026-10-29T08:35"), lookup=kakao.lookup)
    assert [again["created"], again["kakao_calls"]] == [[], 0]  # 건너뛴 날짜는 다시 안 봄
    assert kakao.state.calls == calls


@async_test
async def test_manual_wake_dropped_when_order_breaks():
    """직접 고른 기상 시각이 새 회차 이동시간에서 출발보다 늦어지면 직접 시각을 버리고 자동 시각으로 (순서 어긋남)"""
    env = setup(MON_10)
    series = await seed(env, A, slot(manual_times={"WAKE": iso("2026-10-27T07:30")}), MON_10)
    assert alarms_of(env.db, env.db.rows("appointments")[0]["appointment_id"])[0]["time_source"] == "MANUAL"
    kakao = fake_kakao()
    kakao.state.script = [120]  # 이동 120분 → 출발 6:50이 직접 기상 7:30보다 앞
    await roll(env, "own", MON_10, lookup=kakao.lookup)
    thu = occurrence(env.db, series, "2026-10-29")
    assert lines(env.db, thu["appointment_id"]) == [
        "WAKE 06:00 on",
        "PREPARE 06:30 on",
        "DEPARTURE 06:50 on",
        "ARRIVE 08:50 on",
    ]
    assert alarms_of(env.db, thu["appointment_id"])[0]["time_source"] == "AUTO"
    normal = occurrence(env.db, series, "2026-11-03")
    assert lines(env.db, normal["appointment_id"])[0] == "WAKE 07:30 on"  # 이동이 보통이면 직접 시각 그대로


@async_test
async def test_group_appointments_are_not_rolled():
    """공동 약속(group_id 있음)은 반복이어도 회차를 안 만듦 — 본인 길 · 배치 모두"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    env.db.rows("appointments")[0]["group_id"] = "g1"
    for path in ("own", "batch"):
        res = await roll(env, path, MON_10, lookup=fake_kakao().lookup)
        assert [res["created"], res["pending"], res["kakao_calls"]] == [[], [], 0], path
    assert dates_of(env.db, series) == ["2026-10-27"]


# ===========================================================================
# 일정 없는 반복 알람 · 루틴
# ===========================================================================


async def seed_standalone(
    env: Env, user: str, *, at: str, rule: str, enabled: bool | None = None, now: datetime
) -> str:
    db = env.db
    saved = await commit_standalone(
        CommitCtx(user_id=user, user_db=db, service=db, now=now),
        alarm={"scheduledAt": parse_iso(at), "recurrenceRule": rule, "isEnabled": enabled},
        edits=[],
        time_zone="Asia/Seoul",
    )
    alarm_id = _get(saved, "alarm_id")
    return next(a for a in env.db.rows("alarms") if a["alarm_id"] == alarm_id)["series_id"]


@async_test
async def test_standalone_repeating_alarm():
    """일정 없는 반복 알람: 회차마다 alarms 새 행(같은 묶음 · 날짜 칸), 길찾기 없음, 두 번 불러도 한 번만 · 꺼 둔 알람은 꺼진 채 이어받음"""
    env = setup(MON_10)
    series = await seed_standalone(env, A, at="2026-10-27T07:30:00+09:00", rule="FREQ=DAILY", now=MON_10)
    kakao = fake_kakao()
    res = await roll(env, "own", MON_10, lookup=kakao.lookup)
    assert len(res["created"]) == 13  # 10/28 ~ 11/9 (7:30, 11/9 10:00 앞)
    assert [
        res["created"][0]["occurrence_on"],
        res["created"][12]["occurrence_on"],
        res["created"][0]["appointment_id"],
    ] == [
        "2026-10-28",
        "2026-11-09",
        None,
    ]
    assert res["kakao_calls"] == 0
    rows = [a for a in env.db.rows("alarms") if a.get("series_id") == series]
    assert len(rows) == 14
    assert all(
        a.get("participant_id") is None
        and a.get("routine_id") is None
        and a["alarm_type"] == "WAKE"
        and a["user_id"] == A
        and a["recurrence_rule"] == "FREQ=DAILY"
        for a in rows
    )
    assert len({a["occurrence_on"] for a in rows}) == 14
    assert all(
        hm(a["scheduled_at"]) == "07:30" and ymd(a["scheduled_at"]) == a["occurrence_on"] for a in rows
    )
    assert len(res["ring_packages"]) == 13
    assert [
        res["ring_packages"][0]["title"],
        res["ring_packages"][0]["subtitle"],
        res["ring_packages"][0]["type"],
    ] == ["알람", "오전 7:30", "WAKE"]
    again = await roll(env, "batch", MON_10, lookup=kakao.lookup)
    assert [again["created"], again["pending"]] == [[], []]
    assert len([a for a in env.db.rows("alarms") if a.get("series_id") == series]) == 14

    # 꺼 둔 반복 알람도 회차를 유지 — 새 회차가 꺼진 상태를 이어받음 (L6 안건후보: 꺼 둔 반복 알람도 회차 유지 기본안)
    off = setup(MON_10)
    s2 = await seed_standalone(
        off, A, at="2026-10-27T07:30:00+09:00", rule="FREQ=WEEKLY;BYDAY=TU", enabled=False, now=MON_10
    )
    r2 = await roll(off, "batch", MON_10, lookup=kakao.lookup)
    assert [c["occurrence_on"] for c in r2["created"]] == ["2026-11-03"]
    assert (
        next(
            a
            for a in off.db.rows("alarms")
            if a.get("series_id") == s2 and a.get("occurrence_on") == "2026-11-03"
        )["is_enabled"]
        is False
    )


@async_test
async def test_routine_moves_same_row_to_next_occurrence():
    """루틴: 같은 행의 시각만 다음 발생으로 옮김 (새 행 없음) — 지난 단계 알람만, 켜진 것만, 먼저 옮긴 쪽만"""
    now = kst("2026-10-30T22:00")  # 금 밤 — 'MON~FRI'의 다음은 월
    env = setup(now)
    env.db.rows("routines").append(
        {
            "routine_id": "r1",
            "user_id": A,
            "title": "저녁 루틴",
            "repeat_days": "MON~FRI",
            "is_active": True,
            "steps": [
                {"order": 1, "title": "정리", "offset": 21 * 60 + 30, "minutes": 15, "is_alarm": True},
                {"order": 2, "title": "샤워", "offset": 21 * 60 + 45, "minutes": 20, "is_alarm": True},
                {"order": 3, "title": "독서", "offset": 22 * 60 + 5, "minutes": 30, "is_alarm": False},
            ],
        }
    )

    def row(alarm_id: str, step: int, at: str, enabled: bool = True) -> dict[str, Any]:
        return {
            "alarm_id": alarm_id,
            "user_id": A,
            "routine_id": "r1",
            "step_order": step,
            "participant_id": None,
            "alarm_type": "ROUTINE",
            "scheduled_at": to_iso(kst(at)),
            "intensity": "NORMAL",
            "sound_key": None,
            "snooze_limit": 3,
            "is_enabled": enabled,
        }

    env.db.rows("alarms").extend(
        [
            row("al1", 1, "2026-10-30T21:30"),
            row("al2", 2, "2026-10-30T21:45"),
            row("al3", 3, "2026-10-30T22:30", True),
            row("al4", 2, "2026-10-30T21:45", False),
        ]
    )
    env.db.rows("routines").append(
        {
            "routine_id": "r2",
            "user_id": A,
            "title": "꺼 둔 루틴",
            "repeat_days": "MON~FRI",
            "is_active": False,
            "steps": [{"order": 1, "title": "x", "offset": 60, "minutes": 5, "is_alarm": True}],
        }
    )
    env.db.rows("alarms").append({**row("al5", 1, "2026-10-30T01:00"), "routine_id": "r2"})

    own, batch = await asyncio.gather(
        roll(env, "own", now), roll(env, "batch", now)
    )  # 동시에 와도 먼저 옮긴 쪽만
    assert sorted([*own["moved_alarm_ids"], *batch["moved_alarm_ids"]]) == ["al1", "al2"]

    def at(alarm_id: str) -> str:
        return next(a for a in env.db.rows("alarms") if a["alarm_id"] == alarm_id)["scheduled_at"]

    assert [ymd(at("al1")), hm(at("al1")), ymd(at("al2")), hm(at("al2"))] == [
        "2026-11-02",
        "21:30",
        "2026-11-02",
        "21:45",
    ]  # 월요일, 같은 단계 시각
    assert at("al3") == to_iso(kst("2026-10-30T22:30"))  # 아직 안 지난 알람은 그대로
    assert at("al4") == to_iso(kst("2026-10-30T21:45"))  # 꺼진 알람은 그대로
    assert at("al5") == to_iso(kst("2026-10-30T01:00"))  # 꺼 둔 루틴
    assert len(env.db.rows("alarms")) == 5  # 새 행 없음
    packages = own if own["ring_packages"] else batch
    assert len(own["ring_packages"]) + len(batch["ring_packages"]) == len(
        own["moved_alarm_ids"]
    )  # 본인 길만 꾸러미
    assert packages is own or len(packages["ring_packages"]) == 0
    again = await roll(env, "own", now)
    assert again["moved_alarm_ids"] == []


@async_test
async def test_routine_ring_package_title_is_routine_name():
    """루틴 본인 길 꾸러미: 제목은 루틴 이름, 부제 없음"""
    now = kst("2026-10-27T22:00")
    env = setup(now)
    env.db.rows("routines").append(
        {
            "routine_id": "r1",
            "user_id": A,
            "title": "저녁 루틴",
            "repeat_days": "MON,WED",
            "is_active": True,
            "steps": [{"order": 1, "title": "정리", "offset": 21 * 60 + 30, "minutes": 15, "is_alarm": True}],
        }
    )
    env.db.rows("alarms").append(
        {
            "alarm_id": "al1",
            "user_id": A,
            "routine_id": "r1",
            "step_order": 1,
            "participant_id": None,
            "alarm_type": "ROUTINE",
            "scheduled_at": to_iso(kst("2026-10-27T21:30")),
            "intensity": "NORMAL",
            "sound_key": None,
            "snooze_limit": 3,
            "is_enabled": True,
        }
    )
    res = await roll(env, "own", now)
    assert res["moved_alarm_ids"] == ["al1"]
    assert [
        [p["alarmId"], p["type"], p["title"], p.get("subtitle"), p["atMillis"]] for p in res["ring_packages"]
    ] == [["al1", "ROUTINE", "저녁 루틴", None, ms(kst("2026-10-28T21:30"))]]  # 화 밤 → 수


# ===========================================================================
# 순수 계산
# ===========================================================================


def test_routine_step_starts():
    """routineStepStarts: 앱 parseSteps와 같은 읽기 — order 순, 첫 offset이 시작, minutes → 다음 offset 간격 → 5분을 쌓아 감, 못 읽으면 null"""
    assert routine_step_starts(
        [
            {"order": 1, "offset": 1290, "minutes": 15},
            {"order": 2, "offset": 1305, "minutes": 20},
            {"order": 3, "offset": 1325, "minutes": 30},
        ]
    ) == [1290, 1305, 1325]
    assert routine_step_starts(
        [{"order": 2, "offset": 1305, "minutes": 20}, {"order": 1, "offset": 1290, "minutes": 15}]
    ) == [1290, 1305]  # order 순
    assert routine_step_starts([{"offset": 600}, {"offset": 640}, {"title": "x"}]) == [
        600,
        640,
        645,
    ]  # minutes 없음 → 간격 40, 다음 단계 없음 → 5분 기본
    assert routine_step_starts([{"order": 1, "title": "x", "minutes": 10}, {"order": 2, "minutes": 0}]) == [
        21 * 60 + 30,
        21 * 60 + 40,
    ]  # offset 없음 → 21:30
    assert routine_step_starts([{"order": 1, "offset": 23 * 60, "minutes": 90}, {"order": 2}]) == [
        1380,
        1470,
    ]  # 자정을 넘으면 1440 이상
    assert routine_step_starts([None, 3, {"offset": 100, "minutes": 5}]) == [100]
    for bad in (None, "x", {}, [], [1, "a"]):
        assert routine_step_starts(bad) is None
    assert routine_step_starts(None) is None  # JS undefined


def test_retry_at():
    """retryAt: 실패 횟수 0 · 1 · 2 → 1 · 3 · 6시간, 그 뒤로도 6시간"""
    now = parse_iso("2026-10-26T01:00:00Z")
    assert [(ms(retry_at(now, n)) - ms(now)) / 3_600_000 for n in (0, 1, 2, 3, 9)] == [1, 3, 6, 6, 6]


# planOccurrence — 계산만 (DB 없이)
PROFILE: dict[str, Any] = {
    "timezone": "Asia/Seoul",
    "default_buffer_minutes": 10,
    "default_transport": "TRANSIT",
    "home_name": "집",
    "home_lat": HOME["lat"],
    "home_lng": HOME["lng"],
}


def ctx_at(now: datetime) -> RollPlanCtx:
    return RollPlanCtx(
        user_id=A,
        now=now,
        profile=PROFILE,
        time_zone="Asia/Seoul",
        stats=prepare_stats_of([]),
        late_count4w=0,
    )


BASE_APPOINTMENT: dict[str, Any] = {
    "appointment_id": "ap0",
    "creator_id": A,
    "group_id": None,
    "title": "세미나",
    "appointment_at": to_iso(kst("2026-10-27T09:00")),
    "dest_name": "세종대학교",
    "dest_lat": SCHOOL["lat"],
    "dest_lng": SCHOOL["lng"],
    "importance": "NORMAL",
    "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU,TH",
    "series_id": "ap0",
    "occurrence_on": "2026-10-27",
    "memo": None,
    "dest_radius_m": None,
}
BASE_PARTICIPANT: dict[str, Any] = {
    "participant_id": "pt0",
    "appointment_id": "ap0",
    "user_id": A,
    "origin_name": "집",
    "origin_lat": HOME["lat"],
    "origin_lng": HOME["lng"],
    "transport_type": "TRANSIT",
    "est_travel_minutes": 50,
    "travel_source": "API",
    "prepare_minutes": 50,
    "buffer_minutes": 10,
}


def base_alarm(alarm_type: str, at: str, **over: Any) -> dict[str, Any]:
    return {
        "alarm_id": f"a-{alarm_type}",
        "participant_id": "pt0",
        "user_id": A,
        "alarm_type": alarm_type,
        "scheduled_at": to_iso(kst(at)),
        "intensity": "NORMAL",
        "voice_tone": "NEUTRAL",
        "sound_key": None,
        "is_enabled": True,
        "snooze_limit": 3,
        "time_source": "AUTO",
        "intensity_source": "AUTO",
        "recurrence_rule": None,
        **over,
    }


def full_alarms() -> list[dict[str, Any]]:
    return [
        base_alarm("WAKE", "2026-10-27T07:10"),
        base_alarm("PREPARE", "2026-10-27T07:40"),
        base_alarm("DEPARTURE", "2026-10-27T08:00"),
        base_alarm("ARRIVE", "2026-10-27T08:50"),
    ]


def base_of(alarms: list[dict[str, Any]]) -> RollBase:
    return RollBase(
        appointment=BASE_APPOINTMENT, participant=BASE_PARTICIPANT, alarms=alarms, items=["노트북"]
    )


def api_50() -> Any:
    return TravelChoice(minutes=50, source="API", needs_confirmation=False, note="")


def thu() -> Any:
    return Occurrence(date="2026-10-29", at=kst("2026-10-29T09:00"))


def test_plan_occurrence_pure():
    """planOccurrence: 이동시간만 주면 자동 시각 · 켜진 알람 4개 · 준비물 제목, 참여 칸은 기준 회차에서"""
    planned = plan_occurrence(
        ctx_at(MON_10), base_of(full_alarms()), thu(), travel=api_50(), same_day_wake=False
    )
    assert planned.ok
    assert [
        [a["alarm_type"], hm(a["scheduled_at"]), a["is_enabled"], a["time_source"]] for a in planned.alarms
    ] == [
        ["WAKE", "07:10", True, "AUTO"],
        ["PREPARE", "07:40", True, "AUTO"],
        ["DEPARTURE", "08:00", True, "AUTO"],
        ["ARRIVE", "08:50", True, "AUTO"],
    ]
    assert planned.payload["items"] == [{"title": "노트북", "due_date": "2026-10-29"}]
    assert planned.payload["appointment"]["creator_id"] == A
    assert planned.payload["appointment"]["occurrence_on"] == "2026-10-29"
    assert planned.payload["appointment"]["series_id"] == "ap0"
    assert ms(first_alarm_at(planned.plan)) == ms(kst("2026-10-29T07:10"))


def test_shift_manual_times_keeps_day_offset():
    """shiftManualTimes: 같은 시:분으로, 약속 날짜와 알람 날짜가 다른 알람(전날 밤 기상)은 그 날 수 차이를 그대로"""
    base = kst("2026-10-28T01:00")  # 새벽 1시 약속 (수)
    alarms = [
        base_alarm("WAKE", "2026-10-27T23:30", time_source="MANUAL"),
        base_alarm("PREPARE", "2026-10-27T23:50"),
        base_alarm("DEPARTURE", "2026-10-28T00:10", time_source="MANUAL"),
    ]
    shifted = shift_manual_times(alarms, base, "2026-11-04", "Asia/Seoul")
    assert {k: f"{ymd(to_iso(v))} {hm(to_iso(v))}" for k, v in shifted.items()} == {
        "WAKE": "2026-11-03 23:30",
        "DEPARTURE": "2026-11-04 00:10",
    }


def test_inherited_kinds():
    """inheritedKinds: 기준 회차에 기상이 없어도 그날 다른 기상에 밀려서였을 때만 새 회차가 첫 일정이면 기상을 더함"""
    no_wake = [base_alarm("PREPARE", "2026-10-27T07:40"), base_alarm("DEPARTURE", "2026-10-27T08:00")]

    def kinds(alarms: list[dict[str, Any]], with_wake: bool, base_had_earlier_wake: bool) -> list[str]:
        return inherited_kinds(alarms, with_wake=with_wake, base_had_earlier_wake=base_had_earlier_wake)

    assert kinds(no_wake, True, True) == ["WAKE", "PREPARE", "DEPARTURE"]
    assert kinds(no_wake, True, False) == ["PREPARE", "DEPARTURE"]  # 사용자가 안 고름
    assert kinds(no_wake, False, True) == ["PREPARE", "DEPARTURE"]  # 새 회차도 첫 일정이 아님
    assert kinds([], True, True) == []  # 알람 없는 일정
    assert kinds([base_alarm("WAKE", "2026-10-27T07:10")], True, False) == ["WAKE"]
    assert list(RING_KINDS) == ["WAKE", "PREPARE", "DEPARTURE"]


@async_test
async def test_partial_lookup_is_saved_as_estimate():
    """카카오 partial(앞뒤 도보 구간 빠짐)은 API가 아니라 ESTIMATE로 저장 — 분은 그대로, 재시도 기록 없음"""
    env = setup(MON_10)
    series = await seed(
        env,
        A,
        slot(
            appointment={
                "title": "세미나",
                "appointment_at": iso("2026-10-27T09:00"),
                "destination": {"name": "세종대학교", **SCHOOL},
                "importance": "NORMAL",
                "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
            }
        ),
        MON_10,
    )
    kakao = fake_kakao(40)
    kakao.state.partial = True
    result = await roll(env, "own", MON_10, lookup=kakao.lookup)
    assert len(result["created"]) >= 1
    second = occurrence(env.db, series, "2026-11-03")
    participant = participant_of(env.db, second["appointment_id"])
    assert participant["travel_source"] == "ESTIMATE"
    assert participant["est_travel_minutes"] == 40
    assert len(env.db.rows("roll_attempts")) == 0


# ---- 학습 루프 기초 a (이슈 #266): 반복 다음 회차는 직전 회차 준비값 ±10분 안에서만 (D62 처음 2주 몫) ----


# 회차를 이어서 계산 — 매 회차 직전 회차의 준비값을 기준으로 삼고(plan_occurrence의 기준 참여 조건), 그날까지 쌓인 기록으로 통계를 다시 냄
def simulate(
    days: int, *, onboarding: int, actual: int, records_before: list[int] | None = None
) -> list[dict[str, Any]]:
    records = list(records_before or [])  # 쌓인 순서 — 아래에서 뒤집어 최신순으로 넘김
    previous = onboarding
    out: list[dict[str, Any]] = []
    for day in range(days):
        stats = prepare_stats_of(list(reversed(records)))
        ctx = replace(ctx_at(MON_10), profile={**PROFILE, "default_prepare_minutes": onboarding}, stats=stats)
        base = replace(base_of(full_alarms()), participant={**BASE_PARTICIPANT, "prepare_minutes": previous})
        planned = plan_occurrence(ctx, base, thu(), travel=api_50(), same_day_wake=False)
        assert planned.ok
        previous = planned.plan.prepare["minutes"]
        out.append(
            {
                "day": day,
                "prepare": previous,
                "source": planned.plan.prepare["source"],
                "n": planned.plan.prepare["sampleCount"],
                "label": prepare_source_label(planned.plan.prepare["sampleCount"]),
            }
        )
        records.append(actual)
    return out


def steps(rows: list[dict[str, Any]], first: int) -> list[int]:
    return [abs(r["prepare"] - (first if i == 0 else rows[i - 1]["prepare"])) for i, r in enumerate(rows)]


def test_prepare_step_limit_for_slow_learner():
    """[5절] 매일 실제 85분 (온보딩 70분): 매 회차 직전 값에서 ±10분 넘게 안 움직이고, 5건부터 '내 기록 기반 5건'"""
    rows = simulate(12, onboarding=70, actual=85)
    assert rows[0]["prepare"] == 70  # 기록 0건 = 첫 값 그대로
    assert [rows[0]["source"], rows[0]["label"]] == ["DEFAULT", "배우는 중 0/5"]
    assert all(d <= 10 for d in steps(rows, 70)), f"하루 폭: {steps(rows, 70)}"
    assert [rows[4]["label"], rows[5]["label"], rows[5]["source"]] == [
        "배우는 중 4/5",
        "내 기록 기반 5건",
        "PERSONAL",
    ]
    assert all(i == 0 or r["prepare"] >= rows[i - 1]["prepare"] for i, r in enumerate(rows)), (
        "85분 쪽으로만 천천히 당겨짐"
    )
    assert 70 < rows[11]["prepare"] <= 85


def test_prepare_step_limit_actually_binds():
    """상한이 실제로 걸리는 경우: 기록 30건(120분)이 한꺼번에 있는 사람이 첫 값 50에서 시작하면 회차마다 10분씩만 올라 목표(103)에 닿음"""
    rows = simulate(7, onboarding=50, records_before=[120] * 30, actual=120)
    assert [r["prepare"] for r in rows] == [60, 70, 80, 90, 100, 103, 103]  # 상한이 없으면 첫 회차부터 103
    assert all(d <= 10 for d in steps(rows, 50))
    assert rows[0]["label"] == "내 기록 기반 30건"


def test_no_prepare_cap_when_base_has_no_wake():
    """기준 회차에 기상 알람이 없으면 상한을 걸지 않고, 새 회차가 기상 없는 일정(15분)이면 그대로 15분"""
    no_wake = [base_alarm("PREPARE", "2026-10-27T07:40"), base_alarm("DEPARTURE", "2026-10-27T08:00")]
    stats = prepare_stats_of([120] * 30)
    ctx = replace(ctx_at(MON_10), stats=stats)
    base = replace(base_of(no_wake), participant={**BASE_PARTICIPANT, "prepare_minutes": 15})
    # 새 회차에 기상이 생기는 경우(기준은 기상 없는 회차, 그날 다른 기상에 밀렸던 것) — 비교 대상이 다른 종류(15분)라 상한을 안 걸어 기록 보정이 그대로 반영됨
    with_wake = plan_occurrence(
        ctx, base, thu(), travel=api_50(), same_day_wake=False, base_had_earlier_wake=True
    )
    assert with_wake.ok and with_wake.plan.with_wake and with_wake.plan.prepare["minutes"] > 25
    # 새 회차가 그날 두 번째 일정이면 15분
    second = plan_occurrence(ctx, base_of(full_alarms()), thu(), travel=api_50(), same_day_wake=True)
    assert second.ok and not second.plan.with_wake and second.plan.prepare["minutes"] == 15


# ===========================================================================
# 일정 수정 · 삭제(appointment-edit, PR 4b)와 맞물림 — 예외 회차(exception_kind)는 기준 회차에서 빠지고 날짜 자리는 그대로
# ===========================================================================


def edit_ctx(env: Env, **over: Any) -> EditCtx:
    return EditCtx(user_id=A, user_db=env.db, service=env.db, now=MON_10, **over)


THU_1105 = kst("2026-11-05T10:30")
AFTER_WEEK = kst(
    "2026-11-02T10:00"
)  # 이때의 창 = 11/16 10:00 앞까지 → 11/10 화 · 11/12 목 09:00이 새로 들어옴


@async_test
async def test_edited_occurrence_is_not_the_base():
    """이번만 고친 회차는 기준 회차에서 빠짐: 다음 회차는 이번만 고치기 전의 시각 · 제목으로 만들어짐 (본인 길 · 배치 모두), 고친 회차의 날짜 자리는 그대로라 다시 안 만듦"""
    for path in ("own", "batch"):
        env = setup(MON_10)
        series = await seed(env, A, slot(), MON_10)
        await roll(env, "own", MON_10, lookup=fake_kakao().lookup)  # 10/29 · 11/03 · 11/05
        last = occurrence(env.db, series, "2026-11-05")
        edited = await update_appointment(
            edit_ctx(env),
            {
                "action": "UPDATE",
                "appointment_id": last["appointment_id"],
                "scope": "this",
                "confirmed": True,
                "patch": {"at": THU_1105, "title": "특강"},
            },
        )
        assert edited["saved"]
        assert last["exception_kind"] == "EDITED"

        kakao = fake_kakao()
        res = await roll(env, path, AFTER_WEEK, lookup=kakao.lookup)
        assert [c["occurrence_on"] for c in res["created"]] == ["2026-11-10", "2026-11-12"], path
        assert [
            hm(occurrence(env.db, series, c["occurrence_on"])["appointment_at"]) for c in res["created"]
        ] == ["09:00", "09:00"], path  # 10:30이 아님
        assert [occurrence(env.db, series, c["occurrence_on"])["title"] for c in res["created"]] == [
            "세미나",
            "세미나",
        ], path  # 특강이 아님
        assert [hm(last["appointment_at"]), last["title"], last["occurrence_on"]] == [
            "10:30",
            "특강",
            "2026-11-05",
        ], path  # 고친 회차는 그대로
        assert dates_of(env.db, series) == [
            "2026-10-27",
            "2026-10-29",
            "2026-11-03",
            "2026-11-05",
            "2026-11-10",
            "2026-11-12",
        ], path


@async_test
async def test_canceled_date_is_not_recreated():
    """이번만 지운 날짜는 roll이 다시 안 만듦 (행을 두므로) — 본인 길 · 배치 모두 길찾기도 0번. 행을 진짜 지우면 다시 만들어짐(그래서 행을 두는 것)"""
    env = setup(MON_10)
    series = await seed(env, A, slot(), MON_10)
    await roll(env, "own", MON_10, lookup=fake_kakao().lookup)
    tue = occurrence(env.db, series, "2026-11-03")
    res = await delete_appointment(
        edit_ctx(env), {"action": "DELETE", "appointment_id": tue["appointment_id"], "scope": "this"}
    )
    assert res["mode"] == "CANCEL_THIS"
    assert len(res["canceled_alarm_ids"]) == 4

    kakao = fake_kakao()
    for path in ("own", "batch", "own"):
        assert (await roll(env, path, MON_10, lookup=kakao.lookup))["created"] == [], path
    assert kakao.state.calls == 0
    assert dates_of(env.db, series) == ["2026-10-27", "2026-10-29", "2026-11-03", "2026-11-05"]

    # 대조: 행을 진짜 지우면 roll이 그 날짜를 다시 만듦
    env.db.tables["appointments"] = [
        a for a in env.db.rows("appointments") if a["appointment_id"] != tue["appointment_id"]
    ]
    again = await roll(env, "own", MON_10, lookup=kakao.lookup)
    assert [c["occurrence_on"] for c in again["created"]] == ["2026-11-03"]


@async_test
async def test_series_with_only_exception_rows():
    """예외 회차뿐인 묶음: 이번만 고친 회차만 남아 있으면 그 회차가 기준이 되어 묶음이 멈추지 않음, 이번만 지운 회차뿐이면 다음 회차를 안 만듦"""
    for path in ("own", "batch"):
        edited = setup(MON_10)
        series = await seed(edited, A, slot(), MON_10)
        first = occurrence(edited.db, series, "2026-10-27")
        await update_appointment(
            edit_ctx(edited),
            {
                "action": "UPDATE",
                "appointment_id": first["appointment_id"],
                "scope": "this",
                "confirmed": True,
                "patch": {"at": kst("2026-10-27T09:30")},
            },
        )
        res = await roll(edited, path, MON_10, lookup=fake_kakao().lookup)
        assert [c["occurrence_on"] for c in res["created"]] == ["2026-10-29", "2026-11-03", "2026-11-05"], (
            path
        )
        # 예외뿐이라 그 회차의 시각을 이어받음 (작성자 판단)
        assert [
            hm(occurrence(edited.db, series, c["occurrence_on"])["appointment_at"]) for c in res["created"]
        ] == ["09:30", "09:30", "09:30"], path

        canceled = setup(MON_10)
        series2 = await seed(canceled, A, slot(), MON_10)
        await delete_appointment(
            edit_ctx(canceled),
            {
                "action": "DELETE",
                "appointment_id": occurrence(canceled.db, series2, "2026-10-27")["appointment_id"],
                "scope": "this",
            },
        )
        kakao = fake_kakao()
        assert (await roll(canceled, path, MON_10, lookup=kakao.lookup))["created"] == [], path
        assert kakao.state.calls == 0, path


@async_test
async def test_evening_repeat_has_no_wake():
    """#320: 저녁 반복(18:00)은 기상 없이 준비 · 출발 · 도착만 만들어짐 (아침 반복의 기상은 위 기존 시험들이 확인)"""
    env = setup(MON_10)
    evening = await seed(
        env,
        A,
        slot(
            appointment={
                "title": "저녁 스터디",
                "appointment_at": iso("2026-10-27T18:00"),
                "destination": {"name": "세종대학교", **SCHOOL},
                "importance": "NORMAL",
                "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU,TH",
            }
        ),
        MON_10,
    )
    base = next(x for x in env.db.rows("appointments") if x["series_id"] == evening)
    assert [x.split(" ")[0] for x in lines(env.db, base["appointment_id"])] == [
        "PREPARE",
        "DEPARTURE",
        "ARRIVE",
    ]
    kakao = fake_kakao()
    await roll(env, "own", MON_10, lookup=kakao.lookup)
    for d in ("2026-10-29", "2026-11-03", "2026-11-05"):
        assert [x.split(" ")[0] for x in lines(env.db, occurrence(env.db, evening, d)["appointment_id"])] == [
            "PREPARE",
            "DEPARTURE",
            "ARRIVE",
        ], d

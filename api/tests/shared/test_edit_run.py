"""appointment-edit 본문(edit_run) — 본 레포 supabase/functions/_shared/edit-run.test.ts의 서버판.
가짜 DB(tests/fake_db.py) + 가짜 카카오(분 고정, 부른 수 셈)로.
달력: 2026-10-26 월(지금 10:00) · 27 화 · 29 목 · 11-03 화 · 11-05 목 · 11-10 화 · 11-12 목. 모두 한국 시각
부른 사람의 RLS를 흉내 내는 껍데기(AsUser)로 읽고 쓰게 해서, 남의 약속이 안 보이는 것까지 같이 확인함. service가 닿은 표도 지켜봄(service_tables)

"""

import asyncio
import functools
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core.errors import AppError
from app.core.read import Reader
from app.kakao import KakaoError
from app.shared.commit import CommitCtx, persist_appointment
from app.shared.edit_run import (
    SYNC_CONCURRENCY,
    EditCtx,
    delete_appointment,
    leave_shared_appointment,
    restore_appointment,
    run_edit,
    update_appointment,
)
from app.shared.js import from_ms, ms, parse_iso, to_iso
from app.shared.notify import NotifyOptions
from app.shared.plan import ALARM_COLUMNS, PlanContext, build_plan
from app.shared.requests import read_commit_request
from app.shared.roll import RollDeps, run_roll
from app.shared.supabase import Result
from app.shared.tz import local_date_key, local_day_range
from tests.fake_db import FakeDb

A = "u-a"
B = "u-b"


def async_test(fn):
    """async 시험 함수를 asyncio.run으로 돌림 (pytest 플러그인 없이)"""

    @functools.wraps(fn)
    def wrapper():
        asyncio.run(fn())

    return wrapper


def kst(local: str) -> datetime:
    return datetime.fromisoformat(f"{local}:00+09:00").astimezone(UTC)


def iso(local: str) -> str:
    return f"{local}:00+09:00"


def hm(value: Any) -> str:
    d = parse_iso(value)
    m = (d.hour * 60 + d.minute + 9 * 60) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def ymd(value: Any) -> str:
    return local_date_key(parse_iso(value), "Asia/Seoul")


def _f(obj: Any, name: str) -> Any:
    return obj[name] if isinstance(obj, Mapping) else getattr(obj, name)


def day_start_ms(now: datetime) -> int:
    r = local_day_range(now, "Asia/Seoul")
    return ms(r[0] if isinstance(r, tuple | list) else _f(r, "start"))


HOME = {"lat": 37.5547, "lng": 126.9707}
SCHOOL = {"lat": 37.55, "lng": 127.07}
MON_10 = kst("2026-10-26T10:00")

# ---- 도구 ----


@dataclass
class Env:
    db: FakeDb
    service_tables: set[str] = field(default_factory=set)


def profile_row(user_id: str, **over: Any) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "timezone": "Asia/Seoul",
        "default_buffer_minutes": 10,
        "default_transport": "TRANSIT",
        "home_name": "집",
        "home_lat": HOME["lat"],
        "home_lng": HOME["lng"],
        **over,
    }


def setup(users: list[str] | None = None) -> Env:
    db = FakeDb().with_roll_rpc().with_commit_rpc()
    db.now = MON_10
    for user_id in users or [A]:
        db.rows("profiles").append(profile_row(user_id))
    return Env(db=db)


class AsUser:
    """부른 사람의 RLS 흉내 — 약속은 내가 만들었거나 내가 참여 중인 그룹의 것만, 참여 조건 · 알람 · 할 일은 내 것만 보임"""

    def __init__(self, db: FakeDb, uid: str) -> None:
        self.db = db
        self.uid = uid

    def from_(self, table: str):
        q = self.db.from_(table)
        if table == "appointments":
            groups = {
                m["group_id"]
                for m in self.db.rows("group_members")
                if m["user_id"] == self.uid and m["invite_status"] == "JOINED"
            }
            visible = [
                a["appointment_id"]
                for a in self.db.rows("appointments")
                if a["creator_id"] == self.uid or (a["group_id"] is not None and a["group_id"] in groups)
            ]
            return q.in_("appointment_id", visible)
        if table in ("appointment_participants", "alarms", "todos"):
            return q.eq("user_id", self.uid)
        return q

    def rpc(self, name: str, args: dict | None = None):
        return self.db.rpc(name, args)


class ServiceDb:
    """service 클라이언트 — 닿은 표를 기록"""

    def __init__(self, env: Env) -> None:
        self.env = env

    def from_(self, table: str):
        self.env.service_tables.add(table)
        return self.env.db.from_(table)

    def rpc(self, name: str, args: dict | None = None):
        return self.env.db.rpc(name, args)


@dataclass
class KakaoState:
    calls: int = 0
    minutes: int = 35
    partial: bool = False
    fail: str | None = None


def fake_kakao(minutes: int = 35):
    state = KakaoState(minutes=minutes)

    async def lookup(args: Any = None, /, **kwargs: Any) -> dict[str, Any]:
        # route(TravelDeps.kakao)는 dict 하나로, roll(TravelLookup)은 origin · destination · transport 키워드로 부름
        state.calls += 1
        if state.fail:
            raise KakaoError(state.fail)
        return {"minutes": state.minutes, "partial": state.partial}

    return SimpleNamespace(state=state, lookup=lookup)


def ctx_for(
    env: Env, user: str, *, now: datetime | None = None, kakao: Callable | None = None, push: Any = None
) -> EditCtx:
    return EditCtx(
        user_id=user,
        user_db=AsUser(env.db, user),
        service=ServiceDb(env),
        now=now or MON_10,
        kakao=kakao,
        push=push,
    )


def upd(id_: str, patch: dict, scope: str = "this", confirmed: bool = False) -> dict:
    return {"action": "UPDATE", "appointment_id": id_, "scope": scope, "patch": patch, "confirmed": confirmed}


def del_(id_: str, scope: str = "this") -> dict:
    return {"action": "DELETE", "appointment_id": id_, "scope": scope}


def restore(id_: str) -> dict:
    return {"action": "RESTORE", "appointment_id": id_}


def leave(id_: str) -> dict:
    return {"action": "LEAVE", "appointment_id": id_}


async def roll(
    env: Env, path: str, *, user: str | None = None, now: datetime | None = None, lookup: Any = None
):
    now = now or MON_10
    env.db.now = now
    deps = RollDeps(
        path=path,
        now=now,
        read=env.db,
        write=env.db,
        lookup=lookup,
        log=lambda *a: None,
        warn=lambda *a: None,
    )
    return await run_roll(deps, user_id=(user or A) if path == "own" else None)


def created_of(rolled: Any) -> list[Any]:
    return _f(rolled, "created")


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


async def seed(env: Env, user: str, body: dict, now: datetime = MON_10) -> str:
    """alarm-commit이 약속을 저장한 것처럼 (요청 읽기 → 계산 → 저장). 돌려주는 값 = 약속 id"""
    request = read_commit_request(Reader.root(body))
    if _f(request, "type") != "APPOINTMENT":
        raise RuntimeError("약속 요청이어야 해요")
    db = env.db
    built = await build_plan(PlanContext(user_id=user, db=db, now=now), _f(request, "draft"))
    saved = await persist_appointment(
        CommitCtx(user_id=user, user_db=db, service=db, now=now),
        plan=_f(built, "plan"),
        inputs=_f(built, "inputs"),
        created=_f(request, "appointment"),
        draft=_f(request, "draft"),
        replace_existing=_f(request, "replace_existing"),
        edits=[],
        time_zone=_f(built, "time_zone"),
    )
    return saved["appointment_id"]


async def single(
    env: Env, user: str, local: str, *, title: str | None = None, kinds: list[str] | None = None
) -> str:
    """한 번짜리 약속 (반복 없음)"""
    return await seed(
        env,
        user,
        {
            "appointment": {
                "title": title or "약속",
                "appointment_at": iso(local),
                "destination": {"name": "세종대학교", **SCHOOL},
                "importance": "NORMAL",
            },
            "participant": slot()["participant"],
            **({"kinds": kinds} if kinds else {}),
        },
    )


_raw_seq = 0


def raw_series(
    db: FakeDb,
    user: str,
    *,
    ats: list[str],
    rule: str | None,
    title: str | None = None,
    group: str | None = None,
) -> list[str]:
    """시험용 간단 행 — ats마다 회차 한 줄 (첫 줄의 id가 묶음 번호). 알람 4개: 기상 −110 · 준비 −80 · 출발 −60 · 도착 −10분"""
    global _raw_seq
    made: list[str] = []
    series = ""
    for local in ats:
        _raw_seq += 1
        appointment_id = f"ap-raw-{_raw_seq}"
        series = series or appointment_id
        made.append(appointment_id)
        at = parse_iso(iso(local))
        db.rows("appointments").append(
            {
                "appointment_id": appointment_id,
                "creator_id": user,
                "group_id": group,
                "title": title or "수업",
                "appointment_at": to_iso(at),
                "dest_name": "세종대학교",
                "dest_lat": SCHOOL["lat"],
                "dest_lng": SCHOOL["lng"],
                "importance": "NORMAL",
                "recurrence_rule": rule,
                "memo": None,
                "dest_radius_m": None,
                "series_id": series,
                "occurrence_on": local_date_key(at, "Asia/Seoul"),
                "exception_kind": None,
            }
        )
        add_participant(db, user, appointment_id, at)
    return made


def add_participant(db: FakeDb, user: str, appointment_id: str, at: datetime) -> None:
    global _raw_seq
    _raw_seq += 1
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
            "departed_at": None,
            "arrived_at": None,
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


def row(db: FakeDb, id_: Any) -> dict[str, Any] | None:
    return next((a for a in db.rows("appointments") if a["appointment_id"] == id_), None)


def by_date(db: FakeDb, series: str, date: str) -> dict[str, Any] | None:
    return next(
        (a for a in db.rows("appointments") if a["series_id"] == series and a["occurrence_on"] == date), None
    )


def dates(db: FakeDb, series: str) -> list[str]:
    return sorted(a["occurrence_on"] for a in db.rows("appointments") if a["series_id"] == series)


def participant_of(db: FakeDb, appointment_id: Any, user: str = A) -> dict[str, Any] | None:
    return next(
        (
            p
            for p in db.rows("appointment_participants")
            if p["appointment_id"] == appointment_id and p["user_id"] == user
        ),
        None,
    )


def alarms_of(db: FakeDb, appointment_id: Any, user: str = A) -> list[dict[str, Any]]:
    p = participant_of(db, appointment_id, user)
    found = [a for a in db.rows("alarms") if p and a["participant_id"] == p["participant_id"]]
    return sorted(found, key=lambda a: ms(parse_iso(a["scheduled_at"])))


def lines(db: FakeDb, appointment_id: Any, user: str = A) -> list[str]:
    return [
        f"{a['alarm_type']} {hm(a['scheduled_at'])} {'on' if a['is_enabled'] else 'off'}"
        for a in alarms_of(db, appointment_id, user)
    ]


def kinds(db: FakeDb, appointment_id: Any) -> str:
    return ",".join(sorted(a["alarm_type"] for a in alarms_of(db, appointment_id)))


def alarm_ids(db: FakeDb, appointment_id: Any, user: str = A) -> list[str]:
    return [a["alarm_id"] for a in alarms_of(db, appointment_id, user)]


def snap(db: FakeDb) -> str:
    tables = ["appointments", "appointment_participants", "alarms", "todos", "roll_attempts"]
    return json.dumps([db.rows(t) for t in tables], default=str)


async def expect_http(run: Callable, code: str, reason: str | None = None) -> None:
    with pytest.raises(AppError) as info:
        await run()
    assert info.value.code == code
    if reason:
        assert (info.value.details or {}).get("reason") == reason


# 월요일(지금) 기준으로 시드 + roll로 만든 반복 묶음 — 화·목 09:00 세미나, 회차 10/27 · 10/29 · 11/03 · 11/05 (첫 회차 이동 50분, roll이 만든 회차는 35분)
async def seeded_series(env: Env) -> dict[str, Any]:
    first = await seed(env, A, slot())
    series = row(env.db, first)["series_id"]
    await roll(env, "own", lookup=fake_kakao(35).lookup)
    assert dates(env.db, series) == ["2026-10-27", "2026-10-29", "2026-11-03", "2026-11-05"]
    return {
        "series": series,
        "thu": by_date(env.db, series, "2026-10-29"),
        "tue": by_date(env.db, series, "2026-11-03"),
        "thu2": by_date(env.db, series, "2026-11-05"),
    }


# ===========================================================================
# 이번만 고치기
# ===========================================================================


@async_test
async def test_이번만_고치기_그_회차_행만_고침():
    """이번만 고치기: 그 회차 행만 고침 — 날짜 칸 · 묶음은 그대로, 이번만 고침 표시, 알람 시각 다시 계산(알람 id 그대로), 다른 회차는 그대로"""
    env = setup()
    s = await seeded_series(env)
    thu, tue, thu2 = s["thu"], s["tue"], s["thu2"]

    def others():
        return json.dumps(
            [
                row(env.db, tue["appointment_id"]),
                row(env.db, thu2["appointment_id"]),
                alarms_of(env.db, tue["appointment_id"]),
                alarms_of(env.db, thu2["appointment_id"]),
            ],
            default=str,
        )

    before_others = others()
    ids_before = alarm_ids(env.db, thu["appointment_id"])
    assert lines(env.db, thu["appointment_id"]) == [
        "WAKE 07:25 on",
        "PREPARE 07:55 on",
        "DEPARTURE 08:15 on",
        "ARRIVE 08:50 on",
    ]

    res = await update_appointment(
        ctx_for(env, A),
        upd(thu["appointment_id"], {"at": kst("2026-10-29T10:00"), "title": "발표"}, "this", True),
    )
    assert [res["saved"], res["needs_confirm"]] == [True, False]
    assert [[c["kind"], hm(c["from"]), hm(c["to"])] for c in res["changes"]] == [
        ["WAKE", "07:25", "08:25"],
        ["PREPARE", "07:55", "08:55"],
        ["DEPARTURE", "08:15", "09:15"],
    ]

    after = row(env.db, thu["appointment_id"])
    assert [
        hm(after["appointment_at"]),
        after["title"],
        after["occurrence_on"],
        after["series_id"],
        after["exception_kind"],
        after["recurrence_rule"],
    ] == ["10:00", "발표", "2026-10-29", s["series"], "EDITED", "FREQ=WEEKLY;BYDAY=TU,TH"]
    assert lines(env.db, thu["appointment_id"]) == [
        "WAKE 08:25 on",
        "PREPARE 08:55 on",
        "DEPARTURE 09:15 on",
        "ARRIVE 09:50 on",
    ]
    assert sorted(alarm_ids(env.db, thu["appointment_id"])) == sorted(
        ids_before
    )  # 알람은 지우고 다시 만들지 않고 제자리에서 고침
    assert res["canceled_alarm_ids"] == []
    assert res["deleted_appointment_ids"] == []
    assert len(res["ring_packages"]) == 4
    first = res["ring_packages"][0]
    assert [first["type"], first["title"], first["subtitle"], first["atMillis"]] == [
        "WAKE",
        "발표",
        "오전 10:00 · 세종대학교",
        ms(kst("2026-10-29T08:25")),
    ]
    assert others() == before_others
    assert res["warnings"] == []


@async_test
async def test_같은_요청을_두_번_보내면_같은_결과():
    """같은 요청을 두 번 보내면 같은 결과: 두 번째는 바뀐 것이 없어 아무것도 안 쓰고 폰에 걸 것도 없음"""
    env = setup()
    s = await seeded_series(env)
    request = upd(s["thu"]["appointment_id"], {"at": kst("2026-10-29T10:00")}, "this", True)
    await update_appointment(ctx_for(env, A), request)
    saved = snap(env.db)
    again = await update_appointment(ctx_for(env, A), request)
    assert snap(env.db) == saved
    assert [
        again["saved"],
        again["needs_confirm"],
        again["changes"],
        again["ring_packages"],
        again["canceled_alarm_ids"],
    ] == [
        True,
        False,
        [],
        [],
        [],
    ]
    # 같은 요청을 한 번 더 보내도 roll이 같은 날을 다시 만들지 않음
    kakao = fake_kakao()
    assert created_of(await roll(env, "own", lookup=kakao.lookup)) == []
    assert kakao.state.calls == 0


@async_test
async def test_확인_경계_d67():
    """확인 경계(D67): 5분 이상 늦춰지면 저장하지 않고 needs_confirm — 확인 뒤 confirmed로 다시 보내면 저장. 앞당김 · 4분 늦춤은 바로 저장"""
    env = setup()
    s = await seeded_series(env)
    thu, tue, thu2 = s["thu"], s["tue"], s["thu2"]
    before = snap(env.db)

    ask = await update_appointment(
        ctx_for(env, A), upd(thu["appointment_id"], {"at": kst("2026-10-29T09:05")})
    )
    assert [ask["saved"], ask["needs_confirm"]] == [False, True]
    assert [[c["kind"], hm(c["from"]), hm(c["to"])] for c in ask["changes"]] == [
        ["WAKE", "07:25", "07:30"],
        ["PREPARE", "07:55", "08:00"],
        ["DEPARTURE", "08:15", "08:20"],
    ]
    assert [ask["ring_packages"], ask["canceled_alarm_ids"]] == [[], []]
    assert snap(env.db) == before  # 아무것도 안 씀

    confirmed = await update_appointment(
        ctx_for(env, A), upd(thu["appointment_id"], {"at": kst("2026-10-29T09:05")}, "this", True)
    )
    assert [confirmed["saved"], confirmed["needs_confirm"]] == [True, False]
    assert hm(row(env.db, thu["appointment_id"])["appointment_at"]) == "09:05"

    earlier = await update_appointment(
        ctx_for(env, A), upd(tue["appointment_id"], {"at": kst("2026-11-03T08:30")})
    )
    assert [earlier["saved"], earlier["needs_confirm"], len(earlier["changes"])] == [True, False, 3]
    assert lines(env.db, tue["appointment_id"]) == [
        "WAKE 06:55 on",
        "PREPARE 07:25 on",
        "DEPARTURE 07:45 on",
        "ARRIVE 08:20 on",
    ]

    small = await update_appointment(
        ctx_for(env, A), upd(thu2["appointment_id"], {"at": kst("2026-11-05T09:04")})
    )
    assert [small["saved"], small["needs_confirm"]] == [True, False]
    assert hm(row(env.db, thu2["appointment_id"])["appointment_at"]) == "09:04"


@async_test
async def test_제목만_고치면_알람은_그대로_꾸러미만_줌():
    """제목만 고치면 알람은 그대로, 폰의 알람 제목만 바뀌게 꾸러미를 줌 (이번만 고침 표시는 붙음)"""
    env = setup()
    s = await seeded_series(env)
    thu = s["thu"]
    alarms_before = json.dumps(alarms_of(env.db, thu["appointment_id"]), default=str)
    res = await update_appointment(ctx_for(env, A), upd(thu["appointment_id"], {"title": "  특강 "}))
    assert [res["saved"], res["needs_confirm"], res["changes"], res["canceled_alarm_ids"]] == [
        True,
        False,
        [],
        [],
    ]
    assert json.dumps(alarms_of(env.db, thu["appointment_id"]), default=str) == alarms_before
    saved_row = row(env.db, thu["appointment_id"])
    assert [saved_row["title"], saved_row["exception_kind"]] == ["특강", "EDITED"]
    assert [[p["type"], p["title"]] for p in res["ring_packages"]] == [
        ["WAKE", "특강"],
        ["PREPARE", "특강"],
        ["DEPARTURE", "특강"],
        ["ARRIVE", "특강"],
    ]


@async_test
async def test_직접_고른_알람_시각_manual은_보존():
    """직접 고른 알람 시각(MANUAL)은 보존: 같은 날 시각을 옮기면 그대로, 날짜가 바뀌면 날짜만 따라감 (회차 날짜 칸은 그대로)"""
    env = setup()
    s = await seeded_series(env)
    thu, thu2 = s["thu"], s["thu2"]
    # 10/29: 기상을 07:00으로 직접 고름
    wake0 = next(a for a in alarms_of(env.db, thu["appointment_id"]) if a["alarm_type"] == "WAKE")
    wake0["scheduled_at"] = iso("2026-10-29T07:00")
    wake0["time_source"] = "MANUAL"
    await update_appointment(
        ctx_for(env, A), upd(thu["appointment_id"], {"at": kst("2026-10-29T09:30")}, "this", True)
    )
    assert lines(env.db, thu["appointment_id"])[0] == "WAKE 07:00 on"
    assert alarms_of(env.db, thu["appointment_id"])[0]["time_source"] == "MANUAL"
    assert (
        next(x for x in lines(env.db, thu["appointment_id"]) if x.startswith("DEPARTURE"))
        == "DEPARTURE 08:45 on"
    )

    # 11/05 → 11/06 (금): 기상 직접 값이 하루 뒤 같은 시각으로
    wake = next(a for a in alarms_of(env.db, thu2["appointment_id"]) if a["alarm_type"] == "WAKE")
    wake["scheduled_at"] = iso("2026-11-05T07:00")
    wake["time_source"] = "MANUAL"
    await update_appointment(
        ctx_for(env, A), upd(thu2["appointment_id"], {"at": kst("2026-11-06T09:00")}, "this", True)
    )
    moved = row(env.db, thu2["appointment_id"])
    assert [ymd(moved["appointment_at"]), moved["occurrence_on"]] == ["2026-11-06", "2026-11-05"]
    assert lines(env.db, thu2["appointment_id"])[0] == "WAKE 07:00 on"
    assert ymd(alarms_of(env.db, thu2["appointment_id"])[0]["scheduled_at"]) == "2026-11-06"


@async_test
async def test_출발_시각까지_지난_시각으로는_못_옮김():
    """출발 시각까지 지난 시각으로는 못 옮김 (UNPROCESSABLE) — 아무것도 안 씀"""
    env = setup()
    s = await seeded_series(env)
    before = snap(env.db)
    await expect_http(
        lambda: update_appointment(
            ctx_for(env, A), upd(s["thu"]["appointment_id"], {"at": kst("2026-10-26T10:30")}, "this", True)
        ),
        "UNPROCESSABLE",
    )
    assert snap(env.db) == before


# ===========================================================================
# 장소 · 이동시간
# ===========================================================================


@async_test
async def test_시각만_바꾸면_이동시간은_저장된_값_장소를_바꾸면_길찾기를_새로():
    """시각만 바꾸면 이동시간은 저장된 값 그대로(길찾기 0번), 장소를 바꾸면 길찾기를 새로 구해 알람이 다시 정해짐"""
    env = setup()
    s = await seeded_series(env)
    thu, tue = s["thu"], s["tue"]
    kakao = fake_kakao(20)
    await update_appointment(
        ctx_for(env, A, kakao=kakao.lookup),
        upd(thu["appointment_id"], {"at": kst("2026-10-29T10:00")}, "this", True),
    )
    assert kakao.state.calls == 0
    assert participant_of(env.db, thu["appointment_id"])["est_travel_minutes"] == 35

    res = await update_appointment(
        ctx_for(env, A, kakao=kakao.lookup),
        upd(
            tue["appointment_id"],
            {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}},
            "this",
            True,
        ),
    )
    assert kakao.state.calls == 1
    part = participant_of(env.db, tue["appointment_id"])
    assert [part["est_travel_minutes"], part["travel_source"]] == [20, "API"]
    moved = row(env.db, tue["appointment_id"])
    assert [moved["dest_name"], moved["dest_lat"]] == ["강남역", 37.4979]
    assert [[c["kind"], hm(c["from"]), hm(c["to"])] for c in res["changes"]] == [
        ["WAKE", "07:25", "07:40"],
        ["PREPARE", "07:55", "08:10"],
        ["DEPARTURE", "08:15", "08:30"],
    ]  # 이동 35 → 20분이라 출발이 15분 늦춰짐
    assert res["ring_packages"][0]["subtitle"] == "오전 9:00 · 강남역"


@async_test
async def test_직접_입력한_이동시간_manual은_장소를_바꿔도_그대로():
    """직접 입력한 이동시간(MANUAL)은 장소를 바꿔도 그대로 (D42 C) — 길찾기 0번"""
    env = setup()
    s = await seeded_series(env)
    thu = s["thu"]
    participant_of(env.db, thu["appointment_id"]).update(
        {"est_travel_minutes": 40, "travel_source": "MANUAL"}
    )
    kakao = fake_kakao(5)
    await update_appointment(
        ctx_for(env, A, kakao=kakao.lookup),
        upd(
            thu["appointment_id"],
            {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}},
            "this",
            True,
        ),
    )
    assert kakao.state.calls == 0
    part = participant_of(env.db, thu["appointment_id"])
    assert [part["est_travel_minutes"], part["travel_source"]] == [40, "MANUAL"]


@async_test
async def test_길찾기가_안_되면_근사값으로_저장하고_경고():
    """길찾기가 안 되면 근사값(ESTIMATE)으로 저장하고 경고를 같이 줌, 좌표 없는 장소는 이동시간을 못 구해 ROUTE_UNAVAILABLE (아무것도 안 씀)"""
    env = setup()
    s = await seeded_series(env)
    thu, tue = s["thu"], s["tue"]
    down = fake_kakao()
    down.state.fail = "TIMEOUT"
    res = await update_appointment(
        ctx_for(env, A, kakao=down.lookup),
        upd(
            thu["appointment_id"],
            {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}},
            "this",
            True,
        ),
    )
    assert participant_of(env.db, thu["appointment_id"])["travel_source"] == "ESTIMATE"
    assert any(w["code"] == "TRAVEL_NOT_EXACT" for w in res["warnings"])

    before = snap(env.db)
    await expect_http(
        lambda: update_appointment(
            ctx_for(env, A), upd(tue["appointment_id"], {"destination": {"name": "도서관"}}, "this", True)
        ),
        "ROUTE_UNAVAILABLE",
    )
    assert snap(env.db) == before


# ===========================================================================
# 이후 모두 고치기
# ===========================================================================


@async_test
async def test_이후_모두_고치기_뒤_회차를_지우고_roll이_다시_만듦():
    """이후 모두 고치기: 이 회차를 고치고(표시 지움) 뒤 회차를 지움 → roll이 새 기준으로 다시 만듦. 지운 알람은 canceled_alarm_ids"""
    env = setup()
    ids = raw_series(
        env.db,
        A,
        ats=["2026-10-27T09:00", "2026-10-29T09:00", "2026-11-03T09:00", "2026-11-05T09:00"],
        rule="FREQ=WEEKLY;BYDAY=TU,TH",
    )
    series = row(env.db, ids[0])["series_id"]
    gone = [*alarm_ids(env.db, ids[2]), *alarm_ids(env.db, ids[3])]
    for occurrence_on in ("2026-11-10", "2026-10-28"):
        env.db.rows("roll_attempts").append(
            {
                "user_id": A,
                "series_id": series,
                "occurrence_on": occurrence_on,
                "tries": 1,
                "next_try_at": iso("2026-10-26T11:00"),
                "skipped": True,
                "last_error": "X",
            }
        )

    res = await update_appointment(
        ctx_for(env, A), upd(ids[1], {"at": kst("2026-10-29T08:30"), "title": "세미나2"}, "following", True)
    )
    assert sorted(res["deleted_appointment_ids"]) == sorted([ids[2], ids[3]])
    assert sorted(res["canceled_alarm_ids"]) == sorted(gone)
    assert dates(env.db, series) == ["2026-10-27", "2026-10-29"]
    target = row(env.db, ids[1])
    assert [
        hm(target["appointment_at"]),
        target["title"],
        target["exception_kind"],
        target["occurrence_on"],
    ] == [
        "08:30",
        "세미나2",
        None,
        "2026-10-29",
    ]
    assert next(x for x in lines(env.db, ids[1]) if x.startswith("DEPARTURE")) == "DEPARTURE 07:30 on"
    # 뒤 날짜의 재시도 기록만 정리됨 (이 날짜 앞의 기록은 그대로) — service가 닿은 표는 roll_attempts · 참여 기록 확인뿐
    assert [r["occurrence_on"] for r in env.db.rows("roll_attempts")] == ["2026-10-28"]
    assert list(env.service_tables) == ["roll_attempts"]

    # roll이 새 기준(이 회차의 08:30)으로 다시 만듦
    kakao = fake_kakao(35)
    rolled = created_of(await roll(env, "own", lookup=kakao.lookup))
    assert [_f(c, "occurrence_on") for c in rolled] == ["2026-11-03", "2026-11-05"]
    assert [hm(row(env.db, _f(c, "appointment_id"))["appointment_at"]) for c in rolled] == ["08:30", "08:30"]
    assert [row(env.db, _f(c, "appointment_id"))["title"] for c in rolled] == ["세미나2", "세미나2"]


@async_test
async def test_이후_모두_고치기_예외_회차와_기록_있는_회차는_남김():
    """이후 모두 고치기: 이번만 고친 회차와 출발 · 도착 기록이 있는 회차는 남김 (이번만 고친 값이 덮이지 않음)"""
    env = setup()
    ids = raw_series(
        env.db,
        A,
        ats=[
            "2026-10-29T09:00",
            "2026-11-03T09:00",
            "2026-11-05T09:00",
            "2026-11-10T09:00",
            "2026-11-12T09:00",
        ],
        rule="FREQ=WEEKLY;BYDAY=TU,TH",
    )
    row(env.db, ids[2])["exception_kind"] = "EDITED"  # 11/05는 이번만 고침
    participant_of(env.db, ids[3])["departed_at"] = iso("2026-10-26T09:00")  # 11/10은 출발 기록이 있음
    res = await update_appointment(ctx_for(env, A), upd(ids[0], {"importance": "HIGH"}, "following", True))
    assert sorted(res["deleted_appointment_ids"]) == sorted([ids[1], ids[4]])
    assert sorted(a["appointment_id"] for a in env.db.rows("appointments")) == sorted(
        [ids[0], ids[2], ids[3]]
    )
    assert row(env.db, ids[2])["exception_kind"] == "EDITED"


@async_test
async def test_이후_모두_고치기에서_반복_요일을_바꾸거나_끔():
    """이후 모두 고치기에서 반복 요일을 바꾸면 이 회차에 새 규칙, 반복을 끄면(null) 묶음 모든 행의 규칙이 빠져 roll이 다시 안 만듦"""
    env = setup()
    ids = raw_series(
        env.db,
        A,
        ats=["2026-10-27T09:00", "2026-10-29T09:00", "2026-11-03T09:00", "2026-11-05T09:00"],
        rule="FREQ=WEEKLY;BYDAY=TU,TH",
    )
    series = row(env.db, ids[0])["series_id"]

    await update_appointment(
        ctx_for(env, A), upd(ids[1], {"recurrence_rule": "FREQ=WEEKLY;BYDAY=TH"}, "following", True)
    )
    assert [[a["occurrence_on"], a["recurrence_rule"]] for a in env.db.rows("appointments")] == [
        ["2026-10-27", "FREQ=WEEKLY;BYDAY=TU,TH"],
        ["2026-10-29", "FREQ=WEEKLY;BYDAY=TH"],
    ]  # 뒤 회차는 지워짐
    kakao = fake_kakao()
    assert [_f(c, "occurrence_on") for c in created_of(await roll(env, "own", lookup=kakao.lookup))] == [
        "2026-11-05"
    ]  # 목요일만 (11/03 화 없음)

    off = await update_appointment(ctx_for(env, A), upd(ids[1], {"recurrence_rule": None}, "following", True))
    assert off["saved"]
    assert [
        [a["occurrence_on"], a["recurrence_rule"]]
        for a in env.db.rows("appointments")
        if a["series_id"] == series
    ] == [
        ["2026-10-27", None],
        ["2026-10-29", None],
    ]
    none = fake_kakao()
    assert created_of(await roll(env, "own", lookup=none.lookup)) == []
    assert created_of(await roll(env, "batch", lookup=none.lookup)) == []
    assert none.state.calls == 0


@async_test
async def test_한_번짜리에_반복을_켜면_이_행에_규칙이_들어가고_roll이_다음_회차를_만듦():
    """한 번짜리에 반복을 켜면 이 행에 규칙이 들어가고 roll이 다음 회차를 만듦 (범위와 상관없이, 이번만 고침 표시 없음)"""
    env = setup()
    id_ = await single(env, A, "2026-10-27T09:00", title="스터디")
    res = await update_appointment(
        ctx_for(env, A), upd(id_, {"recurrence_rule": "FREQ=WEEKLY;BYDAY=TU"}, "this", True)
    )
    assert res["saved"]
    saved_row = row(env.db, id_)
    assert [saved_row["recurrence_rule"], saved_row["exception_kind"]] == ["FREQ=WEEKLY;BYDAY=TU", None]
    kakao = fake_kakao(30)
    rolled = created_of(await roll(env, "own", lookup=kakao.lookup))
    assert [_f(c, "occurrence_on") for c in rolled] == ["2026-11-03"]


# ===========================================================================
# 한 번짜리 고치기 · 지우기
# ===========================================================================


@async_test
async def test_한_번짜리_고치기_예외_표시_없이_그_행과_알람을_고침():
    """한 번짜리 고치기: 예외 표시 없이 그 행과 알람을 고침 (범위는 안 봄)"""
    env = setup()
    id_ = await single(env, A, "2026-10-29T09:00")
    for scope in ("this", "following"):
        res = await update_appointment(
            ctx_for(env, A),
            upd(id_, {"at": kst("2026-10-29T10:00" if scope == "this" else "2026-10-29T11:00")}, scope, True),
        )
        assert res["saved"]
    saved_row = row(env.db, id_)
    assert [hm(saved_row["appointment_at"]), saved_row["exception_kind"]] == ["11:00", None]
    assert next(x for x in lines(env.db, id_) if x.startswith("DEPARTURE")) == "DEPARTURE 10:00 on"


@async_test
async def test_한_번짜리_지우기_약속_참여_알람이_사라짐():
    """한 번짜리 지우기: 약속 + 참여 + 알람이 사라지고 폰에서 지울 알람을 돌려줌 — 같은 요청을 두 번 보내면 두 번째는 없는 행이라 성공(아무 일 없음)"""
    env = setup()
    id_ = await single(env, A, "2026-10-29T09:00")
    ids = alarm_ids(env.db, id_)
    assert len(ids) == 4
    res = await delete_appointment(ctx_for(env, A), del_(id_))
    assert [res["deleted"], res["mode"], res["deleted_appointment_ids"]] == [True, "ROW", [id_]]
    assert sorted(res["canceled_alarm_ids"]) == sorted(ids)
    assert [
        len(env.db.rows("appointments")),
        len(env.db.rows("appointment_participants")),
        len(env.db.rows("alarms")),
    ] == [0, 0, 0]

    again = await delete_appointment(ctx_for(env, A), del_(id_))
    assert [again["deleted"], again["mode"], again["canceled_alarm_ids"], again["ring_packages"]] == [
        False,
        "NONE",
        [],
        [],
    ]


# ===========================================================================
# 이번만 지우기 · 되돌리기
# ===========================================================================


@async_test
async def test_이번만_지우기_행은_두고_알람만_지움():
    """이번만 지우기: 행은 두고 알람만 지움 + 지운 회차 표시 → roll이 그 날짜를 다시 안 만듦 (본인 · 배치 모두), 같은 요청 두 번 = 같은 결과"""
    env = setup()
    ids = raw_series(
        env.db,
        A,
        ats=["2026-10-27T09:00", "2026-10-29T09:00", "2026-11-03T09:00", "2026-11-05T09:00"],
        rule="FREQ=WEEKLY;BYDAY=TU,TH",
    )
    series = row(env.db, ids[0])["series_id"]
    gone = alarm_ids(env.db, ids[2])

    def others():
        return json.dumps(
            [
                row(env.db, ids[0]),
                row(env.db, ids[1]),
                row(env.db, ids[3]),
                alarms_of(env.db, ids[1]),
                alarms_of(env.db, ids[3]),
            ],
            default=str,
        )

    before_others = others()

    res = await delete_appointment(ctx_for(env, A), del_(ids[2], "this"))
    assert [res["deleted"], res["mode"], res["deleted_appointment_ids"]] == [True, "CANCEL_THIS", []]
    assert sorted(res["canceled_alarm_ids"]) == sorted(gone)
    assert [
        row(env.db, ids[2])["exception_kind"],
        len(alarms_of(env.db, ids[2])),
        bool(participant_of(env.db, ids[2])),
    ] == [
        "CANCELED",
        0,
        True,
    ]
    assert others() == before_others
    assert dates(env.db, series) == ["2026-10-27", "2026-10-29", "2026-11-03", "2026-11-05"]

    saved = snap(env.db)
    again = await delete_appointment(ctx_for(env, A), del_(ids[2], "this"))
    assert [again["deleted"], again["mode"], again["canceled_alarm_ids"], again["ring_packages"]] == [
        True,
        "CANCEL_THIS",
        [],
        [],
    ]
    assert snap(env.db) == saved

    kakao = fake_kakao()
    assert created_of(await roll(env, "own", lookup=kakao.lookup)) == []
    assert created_of(await roll(env, "batch", lookup=kakao.lookup)) == []
    assert kakao.state.calls == 0


@async_test
async def test_되돌리기_이번만_지운_회차는_알람을_다시_만들어_되살림():
    """되돌리기: 이번만 지운 회차는 알람을 자동 값으로 다시 만들어 되살림(이번만 고침 표시), 지운 적 없으면 아무것도 안 함 — 같은 요청 두 번 = 같은 결과"""
    env = setup()
    ids = raw_series(env.db, A, ats=["2026-10-29T09:00", "2026-11-03T09:00"], rule="FREQ=WEEKLY;BYDAY=TU,TH")
    await delete_appointment(ctx_for(env, A), del_(ids[1], "this"))

    res = await restore_appointment(ctx_for(env, A), restore(ids[1]))
    assert [res["restored"], res.get("reason")] == [True, None]
    assert kinds(env.db, ids[1]) == "ARRIVE,DEPARTURE,PREPARE,WAKE"
    assert [
        next(x for x in lines(env.db, ids[1]) if x.startswith("DEPARTURE")),
        next(x for x in lines(env.db, ids[1]) if x.startswith("ARRIVE")),
    ] == ["DEPARTURE 08:00 on", "ARRIVE 08:50 on"]
    assert row(env.db, ids[1])["exception_kind"] == "EDITED"
    assert [p["type"] for p in res["ring_packages"]] == ["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]
    assert sorted(p["alarmId"] for p in res["ring_packages"]) == sorted(alarm_ids(env.db, ids[1]))

    saved = snap(env.db)
    again = await restore_appointment(ctx_for(env, A), restore(ids[1]))
    assert [again["restored"], again.get("reason"), again["ring_packages"]] == [False, "NOT_DELETED", []]
    assert snap(env.db) == saved
    assert (await restore_appointment(ctx_for(env, A), restore(ids[0])))[
        "restored"
    ] is False  # 지운 적 없는 일정
    assert (await restore_appointment(ctx_for(env, A), restore("nope")))["reason"] == "NOT_FOUND"


@async_test
async def test_되돌리기는_이미_행이_지워진_경우는_못_함():
    """되돌리기는 이미 행이 지워진 경우(한 번짜리 · 이후 모두 지우기)는 못 함 — restored false, 아무것도 안 만듦"""
    env = setup()
    id_ = await single(env, A, "2026-10-29T09:00")
    await delete_appointment(ctx_for(env, A), del_(id_))
    res = await restore_appointment(ctx_for(env, A), restore(id_))
    assert [res["restored"], res.get("reason")] == [False, "NOT_FOUND"]
    assert len(env.db.rows("appointments")) == 0


@async_test
async def test_지운_canceled_회차는_고칠_수_없음():
    """지운(CANCELED) 회차는 고칠 수 없음 — 되돌리기가 먼저 (NOT_FOUND)"""
    env = setup()
    ids = raw_series(env.db, A, ats=["2026-10-29T09:00", "2026-11-03T09:00"], rule="FREQ=WEEKLY;BYDAY=TU,TH")
    await delete_appointment(ctx_for(env, A), del_(ids[1], "this"))
    await expect_http(
        lambda: update_appointment(ctx_for(env, A), upd(ids[1], {"title": "고침"}, "this", True)), "NOT_FOUND"
    )


# ===========================================================================
# 이후 모두 지우기 · 반복 끔
# ===========================================================================


@async_test
async def test_이후_모두_지우기_이_회차와_뒤_회차를_지우고_규칙을_비움():
    """이후 모두 지우기: 이 회차와 뒤 회차를 지우고 남은 행의 규칙을 비움(roll 대상에서 빠짐) — 지난 행 · 기록은 남음, 같은 요청 두 번 = 같은 결과"""
    env = setup()
    ids = raw_series(
        env.db,
        A,
        ats=[
            "2026-10-22T09:00",
            "2026-10-27T09:00",
            "2026-10-29T09:00",
            "2026-11-03T09:00",
            "2026-11-05T09:00",
        ],
        rule="FREQ=WEEKLY;BYDAY=TU,TH",
    )
    series = row(env.db, ids[0])["series_id"]
    participant_of(env.db, ids[0]).update(
        {"departed_at": iso("2026-10-22T08:00"), "arrived_at": iso("2026-10-22T08:50")}
    )  # 지난 회차의 기록
    gone = [x for i in (2, 3, 4) for x in alarm_ids(env.db, ids[i])]
    env.db.rows("roll_attempts").append(
        {
            "user_id": A,
            "series_id": series,
            "occurrence_on": "2026-11-10",
            "tries": 1,
            "next_try_at": iso("2026-10-26T11:00"),
            "skipped": False,
            "last_error": "X",
        }
    )

    res = await delete_appointment(ctx_for(env, A), del_(ids[2], "following"))
    assert [res["deleted"], res["mode"]] == [True, "FROM_HERE"]
    assert sorted(res["deleted_appointment_ids"]) == sorted([ids[2], ids[3], ids[4]])
    assert sorted(res["canceled_alarm_ids"]) == sorted(gone)
    assert dates(env.db, series) == ["2026-10-22", "2026-10-27"]
    assert [a["recurrence_rule"] for a in env.db.rows("appointments")] == [None, None]
    assert [participant_of(env.db, ids[0])["departed_at"], len(alarms_of(env.db, ids[0]))] == [
        iso("2026-10-22T08:00"),
        4,
    ]  # 기록은 그대로
    assert len(env.db.rows("roll_attempts")) == 0

    kakao = fake_kakao()
    assert created_of(await roll(env, "own", lookup=kakao.lookup)) == []
    assert created_of(await roll(env, "batch", lookup=kakao.lookup)) == []
    assert kakao.state.calls == 0

    saved = snap(env.db)
    again = await delete_appointment(ctx_for(env, A), del_(ids[2], "following"))
    assert [again["deleted"], again["mode"], again["canceled_alarm_ids"]] == [False, "NONE", []]
    assert snap(env.db) == saved


@async_test
async def test_이후_모두_지우기_예외_회차도_지우고_기록_있는_뒤_회차는_남김():
    """이후 모두 지우기: 이번만 고친 · 지운 회차도 지움(반복이 끝남), 출발 · 도착 기록이 있는 뒤 회차는 남김"""
    env = setup()
    ids = raw_series(
        env.db,
        A,
        ats=["2026-10-29T09:00", "2026-11-03T09:00", "2026-11-05T09:00", "2026-11-10T09:00"],
        rule="FREQ=WEEKLY;BYDAY=TU,TH",
    )
    row(env.db, ids[1])["exception_kind"] = "EDITED"
    await delete_appointment(ctx_for(env, A), del_(ids[2], "this"))  # 11/05 이번만 지움
    participant_of(env.db, ids[3])["arrived_at"] = iso("2026-11-10T08:50")
    res = await delete_appointment(ctx_for(env, A), del_(ids[0], "following"))
    assert sorted(res["deleted_appointment_ids"]) == sorted([ids[0], ids[1], ids[2]])
    assert [[a["appointment_id"], a["recurrence_rule"]] for a in env.db.rows("appointments")] == [
        [ids[3], None]
    ]


@async_test
async def test_출발_도착_기록이_있는_일정은_못_지움():
    """출발 · 도착 기록이 있는 일정은 못 지움 (HAS_RECORDS) — 아무것도 안 지움"""
    env = setup()
    ids = raw_series(env.db, A, ats=["2026-10-29T09:00", "2026-11-03T09:00"], rule="FREQ=WEEKLY;BYDAY=TU,TH")
    participant_of(env.db, ids[0])["departed_at"] = iso("2026-10-29T08:00")
    before = snap(env.db)
    for scope in ("this", "following"):
        await expect_http(
            lambda scope=scope: delete_appointment(ctx_for(env, A), del_(ids[0], scope)),
            "UNPROCESSABLE",
            "HAS_RECORDS",
        )
    assert snap(env.db) == before


# ===========================================================================
# 7.1.3 — 앞 일정이 바뀌거나 지워지면 같은 날 다음 일정의 기상을 다시 정함
# ===========================================================================


@async_test
async def test_7_1_3_그날_첫_일정을_지우면_둘째_일정에_기상_알람이_붙음():
    """7.1.3: 그날 첫 일정을 지우면 둘째 일정에 기상 알람이 붙음 (준비도 다시 계산), 폰에 걸 꾸러미와 지울 알람을 돌려줌"""
    env = setup()
    first = await single(env, A, "2026-10-29T10:00", title="첫 일정")
    second = await single(env, A, "2026-10-29T13:00", title="둘째 일정")
    assert kinds(env.db, first) == "ARRIVE,DEPARTURE,PREPARE,WAKE"
    assert kinds(env.db, second) == "ARRIVE,DEPARTURE,PREPARE"  # 첫 일정에 밀려 기상 없음
    first_ids = alarm_ids(env.db, first)

    res = await delete_appointment(ctx_for(env, A), del_(first))
    assert kinds(env.db, second) == "ARRIVE,DEPARTURE,PREPARE,WAKE"
    wake = next(a for a in alarms_of(env.db, second) if a["alarm_type"] == "WAKE")
    assert wake["is_enabled"] is True
    assert next(x for x in lines(env.db, second) if x.startswith("DEPARTURE")) == "DEPARTURE 12:00 on"
    assert any(
        p["alarmId"] == wake["alarm_id"] and p["type"] == "WAKE" and p["title"] == "둘째 일정"
        for p in res["ring_packages"]
    )
    assert sorted(res["canceled_alarm_ids"]) == sorted(first_ids)
    assert res["warnings"] == []


@async_test
async def test_7_1_3_일정을_앞으로_옮기면_새_첫_일정에_기상이_붙고_옛_첫_일정의_기상은_떨어짐():
    """7.1.3: 일정을 앞으로 옮기면 새 첫 일정에 기상이 붙고 옛 첫 일정의 기상은 떨어짐 (떨어진 기상은 canceled_alarm_ids)"""
    env = setup()
    a1 = await single(env, A, "2026-10-29T10:00", title="A1")
    a2 = await single(env, A, "2026-10-29T13:00", title="A2")
    a1_wake = next(x for x in alarms_of(env.db, a1) if x["alarm_type"] == "WAKE")["alarm_id"]

    res = await update_appointment(ctx_for(env, A), upd(a2, {"at": kst("2026-10-29T08:00")}, "this", True))
    assert kinds(env.db, a2) == "ARRIVE,DEPARTURE,PREPARE,WAKE"  # 이제 첫 일정
    assert kinds(env.db, a1) == "ARRIVE,DEPARTURE,PREPARE"  # 밀려남
    assert a1_wake in res["canceled_alarm_ids"]
    assert any(p["type"] == "WAKE" and p["title"] == "A2" for p in res["ring_packages"])
    assert not any(p["type"] == "WAKE" and p["title"] == "A1" for p in res["ring_packages"])

    # 다시 뒤로 옮기면 원래대로
    await update_appointment(ctx_for(env, A), upd(a2, {"at": kst("2026-10-29T13:00")}, "this", True))
    assert kinds(env.db, a1) == "ARRIVE,DEPARTURE,PREPARE,WAKE"
    assert kinds(env.db, a2) == "ARRIVE,DEPARTURE,PREPARE"


@async_test
async def test_7_1_3_이미_울린_기상_알람은_앞_일정이_생겨도_떼지_않음():
    """7.1.3: 이미 울린 기상 알람은 앞 일정이 생겨도 떼지 않음 (울린 기록 = 학습 · 미루기 근거), 아직 안 울린 것은 떼짐"""
    env = setup()
    a1 = await single(env, A, "2026-10-29T13:00", title="A1")
    a2 = await single(env, A, "2026-10-29T20:00", title="A2")
    a1_wake = next(x for x in alarms_of(env.db, a1) if x["alarm_type"] == "WAKE")
    # 시험 전제: 기상은 낮 12시 전(12시 이후면 저녁 기상이라 아예 안 생김, #320)이고 11시 반 전에 울림
    assert ms(parse_iso(a1_wake["scheduled_at"])) < ms(kst("2026-10-29T11:30"))

    # 아직 안 울렸을 때(지금 = 월요일) — A2를 A1 앞으로 옮기면 A1의 기상은 떼짐
    early = await update_appointment(ctx_for(env, A), upd(a2, {"at": kst("2026-10-29T12:45")}, "this", True))
    assert a1_wake["alarm_id"] in early["canceled_alarm_ids"]
    assert kinds(env.db, a1) == "ARRIVE,DEPARTURE,PREPARE"
    await update_appointment(
        ctx_for(env, A), upd(a2, {"at": kst("2026-10-29T20:00")}, "this", True)
    )  # 원래대로 (A1이 다시 기상을 받음)
    assert kinds(env.db, a1) == "ARRIVE,DEPARTURE,PREPARE,WAKE"
    wake_again = next(x for x in alarms_of(env.db, a1) if x["alarm_type"] == "WAKE")

    # 이미 울린 뒤(지금 = 11:30) — 같은 변경에도 A1의 기상 알람은 그대로 남음
    late = await update_appointment(
        ctx_for(env, A, now=kst("2026-10-29T11:30")), upd(a2, {"at": kst("2026-10-29T12:45")}, "this", True)
    )
    assert late["saved"] is True
    assert kinds(env.db, a1) == "ARRIVE,DEPARTURE,PREPARE,WAKE"
    assert wake_again["alarm_id"] not in late["canceled_alarm_ids"]
    assert next(x for x in alarms_of(env.db, a1) if x["alarm_type"] == "WAKE")["is_enabled"] is True


@async_test
async def test_7_1_3_셋째_일정은_밀려_있던_대로():
    """7.1.3: 셋째 일정은 밀려 있던 대로 — 둘째가 기상을 받으면 셋째는 그대로, 일부러 기상을 안 고른 일정은 되살리지 않음"""
    env = setup()
    a1 = await single(env, A, "2026-10-29T10:00")
    a2 = await single(env, A, "2026-10-29T13:00")
    a3 = await single(env, A, "2026-10-29T17:00", kinds=["DEPARTURE"])
    a3_before = json.dumps(alarms_of(env.db, a3), default=str)
    await delete_appointment(ctx_for(env, A), del_(a1))
    assert kinds(env.db, a2) == "ARRIVE,DEPARTURE,PREPARE,WAKE"
    assert json.dumps(alarms_of(env.db, a3), default=str) == a3_before


@async_test
async def test_7_1_3_반복_일정의_이번만_지우기_되돌리기도_같은_날_기상을_다시_정함():
    """7.1.3: 반복 일정의 이번만 지우기 · 되돌리기도 같은 날 다른 일정의 기상을 다시 정함"""
    env = setup()
    ids = raw_series(env.db, A, ats=["2026-10-29T09:00", "2026-11-03T09:00"], rule="FREQ=WEEKLY;BYDAY=TU,TH")
    other = await single(env, A, "2026-10-29T13:00", title="오후 약속")
    assert kinds(env.db, other) == "ARRIVE,DEPARTURE,PREPARE"
    await delete_appointment(ctx_for(env, A), del_(ids[0], "this"))
    assert kinds(env.db, other) == "ARRIVE,DEPARTURE,PREPARE,WAKE"
    res = await restore_appointment(ctx_for(env, A), restore(ids[0]))
    assert kinds(env.db, other) == "ARRIVE,DEPARTURE,PREPARE"  # 되살린 회차가 다시 첫 일정
    assert kinds(env.db, ids[0]) == "ARRIVE,DEPARTURE,PREPARE,WAKE"
    assert len(res["canceled_alarm_ids"]) == 1


@async_test
async def test_7_1_3_다시_계산이_안_되면_그_일정은_그대로_두고_경고만():
    """7.1.3: 다시 계산이 안 되면(저장된 이동시간이 없음) 그 일정은 그대로 두고 경고만 — 요청은 성공"""
    env = setup()
    first = await single(env, A, "2026-10-29T10:00")
    second = await single(env, A, "2026-10-29T13:00")
    participant_of(env.db, second)["est_travel_minutes"] = None
    before = json.dumps(alarms_of(env.db, second), default=str)
    res = await delete_appointment(ctx_for(env, A), del_(first))
    assert res["deleted"] is True
    assert json.dumps(alarms_of(env.db, second), default=str) == before
    assert [w["code"] for w in res["warnings"]] == ["WAKE_REEVAL_SKIPPED"]


# ===========================================================================
# 보안 · 공동 약속
# ===========================================================================


@async_test
async def test_남의_개인_약속은_못_고치고_못_지움():
    """남의 개인 약속은 못 고치고 못 지움: 고치기 NOT_FOUND, 지우기 · 되돌리기 · 빠지기는 없는 행처럼 성공(아무 일 없음) — 데이터는 그대로"""
    env = setup([A, B])
    id_ = await single(env, A, "2026-10-29T09:00")
    ids = raw_series(env.db, A, ats=["2026-10-29T15:00", "2026-11-03T15:00"], rule="FREQ=WEEKLY;BYDAY=TU,TH")
    before = snap(env.db)
    b = ctx_for(env, B)
    await expect_http(
        lambda: update_appointment(b, upd(id_, {"title": "가로채기"}, "this", True)), "NOT_FOUND"
    )
    await expect_http(
        lambda: update_appointment(b, upd(ids[0], {"title": "가로채기"}, "following", True)), "NOT_FOUND"
    )
    assert [
        (await delete_appointment(b, del_(id_)))["deleted"],
        (await delete_appointment(b, del_(ids[0], "following")))["mode"],
    ] == [
        False,
        "NONE",
    ]
    assert (await restore_appointment(b, restore(id_)))["restored"] is False
    assert (await leave_shared_appointment(b, leave(id_)))["left"] is False
    assert snap(env.db) == before


@async_test
async def test_공동_약속_만든_사람만_고치기_지우기():
    """공동 약속: 만든 사람만 고치기 · 지우기 (참여만 한 사람은 FORBIDDEN), 반복은 아직 못 고침, 제목 · 중요도만 고치면 다른 참여자의 알람은 그대로, 지우면 약속 전체가 사라짐"""
    env = setup([A, B])
    env.db.rows("group_members").extend(
        [
            {"group_id": "g1", "user_id": A, "invite_status": "JOINED"},
            {"group_id": "g1", "user_id": B, "invite_status": "JOINED"},
        ]
    )
    with_group_push(env)
    [id_] = raw_series(env.db, A, ats=["2026-10-29T09:00"], rule=None, title="팀플", group="g1")
    add_participant(env.db, B, id_, parse_iso(iso("2026-10-29T09:00")))
    b_alarms = alarm_ids(env.db, id_, B)
    before = snap(env.db)

    b = ctx_for(env, B)
    await expect_http(
        lambda: update_appointment(b, upd(id_, {"title": "가로채기"}, "this", True)), "FORBIDDEN"
    )
    await expect_http(lambda: delete_appointment(b, del_(id_)), "FORBIDDEN")
    assert snap(env.db) == before

    a = ctx_for(env, A)
    await expect_http(
        lambda: update_appointment(a, upd(id_, {"recurrence_rule": "FREQ=DAILY"}, "this", True)),
        "UNPROCESSABLE",
        "GROUP_RECURRENCE_LATER",
    )
    titled = await update_appointment(
        a, upd(id_, {"title": "팀플 (장소 변경)", "importance": "HIGH"}, "this", True)
    )
    assert titled["saved"]
    assert [row(env.db, id_)["title"], row(env.db, id_)["exception_kind"]] == ["팀플 (장소 변경)", None]
    assert json.dumps([x["alarm_id"] for x in alarms_of(env.db, id_, B)]) == json.dumps(
        b_alarms
    )  # 제목만 고치면 다른 참여자의 알람은 그대로

    participant_of(env.db, id_, B)["arrived_at"] = iso(
        "2026-10-29T08:50"
    )  # 다른 참여자의 도착 기록도 지킴 (service로 확인)
    await expect_http(lambda: delete_appointment(a, del_(id_)), "UNPROCESSABLE", "HAS_RECORDS")
    participant_of(env.db, id_, B)["arrived_at"] = None
    assert list(env.service_tables) == ["appointment_participants"]

    gone = await delete_appointment(a, del_(id_))
    assert [gone["deleted"], gone["mode"]] == [True, "ROW"]
    assert (
        len(env.db.rows("appointments"))
        + len(env.db.rows("appointment_participants"))
        + len(env.db.rows("alarms"))
        == 0
    )
    assert (
        any(x in b_alarms for x in gone["canceled_alarm_ids"]) is False
    )  # 내 알람만 돌려줌 (B의 폰은 B의 다음 맞추기에서 정리)


# ---- 공동 약속 (친구 9장): 만든 사람이 시각 · 장소를 고치면 다른 참여자의 알람도 다시 계산하고 알림, 지우면 취소 알림 ----

GROUP = "5a5a5a5a-5a5a-4a5a-8a5a-5a5a5a5a5a5a"
T_B = "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"
FAR = {"lat": 37.4, "lng": 127.1}


def with_group_push(env: Env) -> None:
    """DB 함수 plan_group_push 흉내 — 보낸 사람이 그 약속을 만든 사람(비었으면 참여자)이고 아직 안 지난 약속일 때만 OK, 받는 사람 = 약속 참여자 − 보낸 사람"""

    def plan_group_push(args: dict) -> dict:
        ap = next(
            (a for a in env.db.rows("appointments") if a["appointment_id"] == args["p_appointment_id"]), None
        )
        sender = args["p_sender_id"]
        participants = [
            p["user_id"]
            for p in env.db.rows("appointment_participants")
            if p["appointment_id"] == args["p_appointment_id"]
        ]
        mine = ap and (ap["creator_id"] == sender or (ap["creator_id"] is None and sender in participants))
        # 지난 약속 = 하루 경계(내 시간대 오늘 0시 이전)
        if not ap or not mine or ms(parse_iso(ap["appointment_at"])) < day_start_ms(env.db.now):
            return {"status": "FORBIDDEN"}
        members = [
            m
            for m in env.db.rows("group_members")
            if m["group_id"] == args["p_group_id"] and m["invite_status"] == "JOINED"
        ]
        joined = {m["user_id"] for m in members if not m.get("notifications_off")}
        muted = {m["user_id"] for m in members if m.get("notifications_off")}
        return {
            "status": "OK",
            "group_name": "캡스톤팀",
            "appointment_title": ap["title"],
            "recipients": [u for u in participants if u != sender and u in joined],
            "silent_recipients": [
                u for u in participants if u != sender and u in muted
            ],  # 알림을 끈 멤버(차단 쌍도 같은 쪽)에게는 조용한 데이터 푸시
        }

    env.db.rpc_handlers["plan_group_push"] = plan_group_push


def push_spy(fail: bool = False):
    sent: list[dict] = []

    async def fetch_impl(
        url: str, *, method: str = "POST", headers: dict, body: str, timeout: float
    ) -> httpx.Response:
        if fail:
            raise RuntimeError("network down")
        body = json.loads(body)
        sent.extend(body)
        return httpx.Response(
            200,
            json={"data": [{"status": "ok", "id": f"ticket-{len(sent)}-{i}"} for i, _ in enumerate(body)]},
        )

    async def sleep(_ms: int) -> None:
        return None

    return SimpleNamespace(sent=sent, push=NotifyOptions(fetch_impl=fetch_impl, sleep=sleep))


P_A = slot()["participant"]
P_B = {
    "origin": {"name": "먼 집", **FAR},
    "transport_type": "TRANSIT",
    "est_travel_minutes": 70,
    "travel_source": "API",
    "buffer_minutes": 10,
    "before_prepare_minutes": 30,
}


async def group_case(users: list[str] | None = None, local: str = "2026-10-29T09:00"):
    """그룹 + 공동 약속(만든 사람 A, 참여자 B 이상). 알람은 alarm-commit이 저장한 것과 같은 계산으로 만듦"""
    users = users or [A, B]
    env = setup(users)
    env.db.rows("groups").append({"group_id": GROUP, "group_name": "캡스톤팀"})
    for u in users:
        env.db.rows("group_members").append({"group_id": GROUP, "user_id": u, "invite_status": "JOINED"})
    with_group_push(env)
    id_ = await seed(
        env,
        A,
        {
            "appointment": {
                "title": "팀플",
                "appointment_at": iso(local),
                "destination": {"name": "세종대학교", **SCHOOL},
                "importance": "NORMAL",
                "group_id": GROUP,
            },
            "participant": P_A,
        },
    )
    for u in users[1:]:
        participant = (
            P_B
            if u == B
            else {
                **P_B,
                "origin": {"name": f"{u} 집", "lat": FAR["lat"] - users.index(u) / 100, "lng": FAR["lng"]},
            }
        )
        await seed(env, u, {"appointment_id": id_, "participant": participant})
        token = (
            T_B
            if u == B
            else f"ExponentPushToken[{''.join(c for c in u if c.isalnum()).ljust(22, 'x')[:22]}]"
        )
        env.db.rows("push_tokens").append({"user_id": u, "token": token})
    return env, id_


def times(db: FakeDb, id_: str, user: str) -> list[int]:
    return [ms(parse_iso(a["scheduled_at"])) for a in alarms_of(db, id_, user)]


def mins(n: int) -> int:
    return n * 60_000


@async_test
async def test_9_10_9_15_만든_사람이_시각을_10분_앞당기면_참여자의_알람이_바로_10분_빨라짐():
    """9-10 · 9-15: 만든 사람이 시각을 10분 앞당기면 참여자의 알람이 바로 10분 빨라지고, 참여자에게만 변경 알림(만든 사람은 제외, 알림 데이터에 change · needsConfirm)"""
    env, id_ = await group_case()
    kk = fake_kakao()
    spy = push_spy()
    b_before = times(env.db, id_, B)
    res = await update_appointment(
        ctx_for(env, A, kakao=kk.lookup, push=spy.push),
        upd(id_, {"at": kst("2026-10-29T08:50")}, "this", True),
    )
    assert times(env.db, id_, B) == [t - mins(10) for t in b_before]
    assert res["participants"] == {"updated": 1, "held": 0, "skipped": 0}
    assert kk.state.calls == 0  # 시각만 바꾸면 길찾기를 새로 안 부름
    assert [
        [
            m["to"],
            m["title"],
            m["body"],
            m["data"]["kind"],
            m["data"]["change"],
            m["data"]["needsConfirm"],
            m["data"]["recipientUserId"],
            m["data"]["appointmentId"],
        ]
        for m in spy.sent
    ] == [[T_B, "캡스톤팀", "‘팀플’ 약속이 바뀌었어요", "GROUP_APPT_CHANGED", "CHANGED", False, B, id_]]
    assert res["notified"] == 1
    assert ymd(row(env.db, id_)["appointment_at"]) == "2026-10-29"
    assert hm(row(env.db, id_)["appointment_at"]) == "08:50"


@async_test
async def test_9_11_9_12_참여자의_알람이_5분_이상_늦춰지면_확인할_때까지_보류():
    """9-11 · 9-12: 참여자의 알람이 5분 이상 늦춰지면 그 사람이 확인할 때까지 보류(알람 그대로, 알림에 needsConfirm), 5분 미만은 바로 반영 — 만든 사람 자신의 알람은 확인 시트 규칙 그대로"""
    late6 = await group_case()
    b_before = times(late6[0].db, late6[1], B)
    spy6 = push_spy()
    res6 = await update_appointment(
        ctx_for(late6[0], A, kakao=fake_kakao().lookup, push=spy6.push),
        upd(late6[1], {"at": kst("2026-10-29T09:06")}, "this", True),
    )
    assert times(late6[0].db, late6[1], B) == b_before  # 보류: B 알람은 아직 그대로
    assert res6["participants"] == {"updated": 0, "held": 1, "skipped": 0}
    assert [m["data"]["needsConfirm"] for m in spy6.sent] == [True]
    assert hm(row(late6[0].db, late6[1])["appointment_at"]) == "09:06"  # 약속 자체는 바뀜

    late3 = await group_case()
    b_before3 = times(late3[0].db, late3[1], B)
    spy3 = push_spy()
    res3 = await update_appointment(
        ctx_for(late3[0], A, kakao=fake_kakao().lookup, push=spy3.push),
        upd(late3[1], {"at": kst("2026-10-29T09:03")}, "this"),
    )
    assert times(late3[0].db, late3[1], B) == [t + mins(3) for t in b_before3]
    assert [res3["saved"], res3["participants"], [m["data"]["needsConfirm"] for m in spy3.sent]] == [
        True,
        {"updated": 1, "held": 0, "skipped": 0},
        [False],
    ]

    # 만든 사람 자신은 10분 늦추면 확인이 먼저 — 확인 전에는 아무것도 안 바뀌고 알림도 없음
    mine = await group_case()
    spy_m = push_spy()
    ask = await update_appointment(
        ctx_for(mine[0], A, kakao=fake_kakao().lookup, push=spy_m.push),
        upd(mine[1], {"at": kst("2026-10-29T09:10")}, "this"),
    )
    assert [
        ask["saved"],
        ask["needs_confirm"],
        hm(row(mine[0].db, mine[1])["appointment_at"]),
        len(spy_m.sent),
    ] == [False, True, "09:00", 0]


@async_test
async def test_9_13_9_14_장소만_바꾸면_참여자마다_자기_출발지로_길찾기를_다시_부름():
    """9-13 · 9-14: 장소만 바꾸면 참여자마다 자기 출발지로 길찾기를 다시 부르고(서로 다른 새 알람 시각), 변경 알림이 감"""
    env, id_ = await group_case()
    calls = 0

    async def counted(args: Any) -> dict:
        nonlocal calls
        calls += 1
        origin = _f(args, "origin")
        # A(집) 30분, B(먼 집) 90분 — B는 이동이 길어져 알람이 앞당겨짐
        return {"minutes": 30 if _f(origin, "lat") > 37.5 else 90, "partial": False}

    spy = push_spy()
    res = await update_appointment(
        ctx_for(env, A, kakao=counted, push=spy.push),
        upd(id_, {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}}, "this", True),
    )
    assert calls == 2  # A와 B 각자 한 번

    def wake(user: str) -> int:
        return ms(
            parse_iso(
                next(a for a in alarms_of(env.db, id_, user) if a["alarm_type"] == "WAKE")["scheduled_at"]
            )
        )

    def lead(user: str) -> int:
        p = participant_of(env.db, id_, user)
        return p["est_travel_minutes"] + p["buffer_minutes"] + p["prepare_minutes"]

    assert wake(A) - wake(B) == mins(lead(B) - lead(A))  # 각자 이동 · 여유 · 준비만큼 약속 시각에서 거꾸로
    assert participant_of(env.db, id_, A)["est_travel_minutes"] == 30
    assert participant_of(env.db, id_, B)["est_travel_minutes"] == 90
    assert wake(B) < wake(A)
    assert res["participants"] == {"updated": 1, "held": 0, "skipped": 0}
    assert [[m["data"]["kind"], m["data"]["change"]] for m in spy.sent] == [["GROUP_APPT_CHANGED", "CHANGED"]]


@async_test
async def test_9_16_참여자의_이미_울린_알람은_안_건드림():
    """9-16: 참여자의 이미 울린 알람은 안 건드리고 아직 안 울린 알람만 다시 맞춤, 알람이 모두 울렸으면 그 사람은 건너뜀"""
    env, id_ = await group_case()
    wake_b = next(a for a in alarms_of(env.db, id_, B) if a["alarm_type"] == "WAKE")
    rung_at = to_iso(from_ms(ms(MON_10) - mins(30)))
    wake_b["scheduled_at"] = rung_at  # 울린 알람
    others = [
        ms(parse_iso(a["scheduled_at"])) for a in alarms_of(env.db, id_, B) if a["alarm_type"] != "WAKE"
    ]
    res = await update_appointment(
        ctx_for(env, A, kakao=fake_kakao().lookup, push=push_spy().push),
        upd(id_, {"at": kst("2026-10-29T08:50")}, "this", True),
    )
    assert wake_b["scheduled_at"] == rung_at
    now_others = [
        ms(parse_iso(a["scheduled_at"])) for a in alarms_of(env.db, id_, B) if a["alarm_type"] != "WAKE"
    ]
    assert all(t < others[i] for i, t in enumerate(now_others))  # 아직 안 울린 알람은 앞당겨짐
    assert res["participants"] == {"updated": 1, "held": 0, "skipped": 0}

    all_env, all_id = await group_case()
    for a in alarms_of(all_env.db, all_id, B):
        a["scheduled_at"] = rung_at
    before = times(all_env.db, all_id, B)
    res2 = await update_appointment(
        ctx_for(all_env, A, kakao=fake_kakao().lookup, push=push_spy().push),
        upd(all_id, {"at": kst("2026-10-29T08:50")}, "this", True),
    )
    assert times(all_env.db, all_id, B) == before
    assert res2["participants"] == {"updated": 0, "held": 0, "skipped": 1}


@async_test
async def test_9_17_참여자의_이동시간을_구하지_못하면_기존_알람을_그대로_두고_경고만():
    """9-17: 참여자의 이동시간을 구하지 못하면(길찾기 실패 → 근사값뿐) 그 사람의 기존 알람을 그대로 두고 경고만 — 이름 없이, 다른 참여자는 정상, 알림은 감"""
    env, id_ = await group_case([A, B, "u-c"])

    async def lookup(args: Any) -> dict:
        if _f(_f(args, "origin"), "lat") < 37.5:
            raise KakaoError("QUOTA")  # 먼 집에서 오는 사람들만 실패
        return {"minutes": 30, "partial": False}

    spy = push_spy()
    b_before = times(env.db, id_, B)
    res = await update_appointment(
        ctx_for(env, A, kakao=lookup, push=spy.push),
        upd(id_, {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}}, "this", True),
    )
    assert times(env.db, id_, B) == b_before
    assert res["participants"] == {"updated": 0, "held": 0, "skipped": 2}
    assert len([w for w in res["warnings"] if w["code"] == "PARTICIPANT_ROUTE_FAILED"]) == 2
    assert B not in json.dumps(res["warnings"])
    assert len(spy.sent) == 2  # 알림은 모두에게


@async_test
async def test_동기화_대상은_그룹에_아직_참여_중인_참여자만():
    """동기화 대상: 그룹에 아직 참여 중인 참여자만 — 나간 사람에게 남은 참여 조건은 다시 계산하지도 알리지도 않음"""
    env, id_ = await group_case()
    next(m for m in env.db.rows("group_members") if m["user_id"] == B)["invite_status"] = "LEFT"
    before = times(env.db, id_, B)
    spy = push_spy()
    res = await update_appointment(
        ctx_for(env, A, kakao=fake_kakao().lookup, push=spy.push),
        upd(id_, {"at": kst("2026-10-29T08:50")}, "this", True),
    )
    assert times(env.db, id_, B) == before
    assert res["participants"] == {"updated": 0, "held": 0, "skipped": 0}
    assert len(spy.sent) == 0


@async_test
async def test_9_39_제목_중요도만_고치면_참여자_알림도_다시_계산도_없음():
    """9-39: 제목 · 중요도만 고치면 참여자 알림도 다시 계산도 없음"""
    env, id_ = await group_case()
    spy = push_spy()
    kk = fake_kakao()
    b_before = json.dumps(alarms_of(env.db, id_, B), default=str)
    res = await update_appointment(
        ctx_for(env, A, kakao=kk.lookup, push=spy.push), upd(id_, {"title": "팀플 (제목만)"}, "this", True)
    )
    assert [len(spy.sent), res.get("participants"), res.get("notified"), kk.state.calls] == [0, None, None, 0]
    assert json.dumps(alarms_of(env.db, id_, B), default=str) == b_before


@async_test
async def test_9_20_만든_사람이_지우면_참여자에게_취소_알림():
    """9-20: 만든 사람이 지우면 참여자의 참여 · 알람이 사라지고 참여자에게 취소 알림(change CANCELED) — 알림이 안 가도 지우기는 끝남, 지난 약속을 지울 땐 알림 없음"""
    env, id_ = await group_case()
    spy = push_spy()
    res = await delete_appointment(ctx_for(env, A, push=spy.push), del_(id_))
    assert [res["deleted"], res["mode"], res.get("notified")] == [True, "ROW", 1]
    assert [
        [m["to"], m["title"], m["body"], m["data"]["change"], m["data"]["appointmentId"]] for m in spy.sent
    ] == [[T_B, "캡스톤팀", "‘팀플’ 약속이 취소됐어요", "CANCELED", id_]]
    assert (
        len(env.db.rows("appointments"))
        + len(env.db.rows("appointment_participants"))
        + len(env.db.rows("alarms"))
        == 0
    )

    broken_env, broken_id = await group_case()
    down = push_spy(fail=True)
    quiet_res = await delete_appointment(ctx_for(broken_env, A, push=down.push), del_(broken_id))
    assert [
        quiet_res["deleted"],
        any(w["code"] == "PUSH_NOT_SENT" for w in quiet_res["warnings"]),
        len(broken_env.db.rows("appointments")),
    ] == [
        True,
        True,
        0,
    ]

    past_env, past_id = await group_case()
    spy_past = push_spy()
    past_res = await delete_appointment(
        ctx_for(past_env, A, now=kst("2026-10-30T10:00"), push=spy_past.push), del_(past_id)
    )
    assert [past_res["deleted"], past_res.get("notified"), len(spy_past.sent)] == [True, None, 0]

    # 같은 날 이미 지난 약속(하루 경계)은 아직 '안 지난' 약속이라 취소 알림이 감
    same_env, same_id = await group_case()
    spy_same = push_spy()
    same_res = await delete_appointment(
        ctx_for(same_env, A, now=kst("2026-10-29T09:30"), push=spy_same.push), del_(same_id)
    )
    assert [same_res["deleted"], same_res.get("notified"), len(spy_same.sent)] == [True, 1, 1]


@async_test
async def test_조용한_푸시_알림을_끈_참여자에게는_데이터_푸시만():
    """조용한 푸시: 알림을 끈 참여자에게는 보이는 알림 없이 데이터 푸시만(알람 다시 맞추기), needsConfirm도 실림 — 보이는 알림 수(notified)에는 안 더함"""
    env, id_ = await group_case([A, B, "u-mute"])
    next(m for m in env.db.rows("group_members") if m["user_id"] == "u-mute")["notifications_off"] = True
    spy = push_spy()
    res = await update_appointment(
        ctx_for(env, A, kakao=fake_kakao().lookup, push=spy.push),
        upd(id_, {"at": kst("2026-10-29T09:06")}, "this", True),
    )  # 6분 늦춤 = 보류(needsConfirm)
    assert res["notified"] == 1
    by_user = {m["data"]["recipientUserId"]: m for m in spy.sent}
    assert by_user[B]["title"] == "캡스톤팀"
    assert by_user["u-mute"].get("title") is None or by_user["u-mute"]["title"] == ""  # 문구 없는 데이터 푸시
    assert by_user["u-mute"]["data"]["silent"] is True
    assert by_user["u-mute"]["data"]["needsConfirm"] is True
    assert by_user[B]["data"]["needsConfirm"] is True


@async_test
async def test_9_32_지난_날의_공동_약속은_고칠_수_없음():
    """9-32: 지난 날의 공동 약속은 고칠 수 없고(PAST_APPOINTMENT), 지난 날로 옮길 수도 없고, 새로 참여할 수도 없음 — 앞으로 약속, 오늘 이미 지난 약속은 오늘 안에는 됨 (하루 경계: 내 폰 시간대 오늘 0시)"""
    env, id_ = await group_case()
    late = kst("2026-10-30T09:30")  # 약속 다음 날
    same_day = kst("2026-10-29T09:30")  # 약속과 같은 날, 시각은 이미 지남
    await expect_http(
        lambda: update_appointment(
            ctx_for(env, A, now=late, kakao=fake_kakao().lookup),
            upd(id_, {"title": "지난 약속 고치기"}, "this", True),
        ),
        "UNPROCESSABLE",
        "PAST_APPOINTMENT",
    )
    await update_appointment(
        ctx_for(env, A, now=same_day, kakao=fake_kakao().lookup),
        upd(id_, {"title": "오늘 이미 지난 약속 고치기"}, "this", True),
    )  # 오류 없이 고쳐짐
    assert row(env.db, id_)["title"] == "오늘 이미 지난 약속 고치기"
    await expect_http(
        lambda: update_appointment(
            ctx_for(env, A, kakao=fake_kakao().lookup),
            upd(id_, {"at": kst("2026-10-25T23:00")}, "this", True),
        ),
        "UNPROCESSABLE",
        "PAST_APPOINTMENT",
    )  # 기준 시각(10/26 월요일)의 어제로 옮기기

    join_env, join_id = await group_case([A, B])
    c = "u-late"
    join_env.db.rows("profiles").append(profile_row(c))
    join_env.db.rows("group_members").append({"group_id": GROUP, "user_id": c, "invite_status": "JOINED"})
    await expect_http(
        lambda: seed(join_env, c, {"appointment_id": join_id, "participant": P_A}, kst("2026-10-30T09:30")),
        "UNPROCESSABLE",
        "PAST_APPOINTMENT",
    )
    assert participant_of(join_env.db, join_id, c) is None
    await seed(join_env, c, {"appointment_id": join_id, "participant": P_A})  # 아직 안 지났을 때는 참여됨
    assert participant_of(join_env.db, join_id, c)

    # 하루 경계: 같은 날 이미 지난 약속에는 참여할 수 있고, 시간대는 그 사람 것 (UTC 사람에게는 서울 기준으로는 지난 날이어도 아직 오늘)
    same_join_env, same_join_id = await group_case([A, B])
    for u, tz in (("u-same", "Asia/Seoul"), ("u-utc", "UTC")):
        same_join_env.db.rows("profiles").append(profile_row(u, timezone=tz))
        same_join_env.db.rows("group_members").append(
            {"group_id": GROUP, "user_id": u, "invite_status": "JOINED"}
        )
    # 같은 날 이미 지난 약속: '지난 약속' 검사는 통과하고 그다음 단계(출발이 지났다)에서 멈춤 — 지난 날이면 PAST_APPOINTMENT에서 멈췄을 것
    await expect_http(
        lambda: seed(
            same_join_env,
            "u-same",
            {"appointment_id": same_join_id, "participant": P_A},
            kst("2026-10-29T09:30"),
        ),
        "UNPROCESSABLE",
        "DEPARTURE_PASSED",
    )  # 약속 09:00, 지금 09:30 — 같은 날
    await expect_http(
        lambda: seed(
            same_join_env,
            "u-utc",
            {"appointment_id": same_join_id, "participant": P_A},
            parse_iso("2026-10-29T16:00:00Z"),
        ),
        "UNPROCESSABLE",
        "DEPARTURE_PASSED",
    )  # 서울로는 10/30 01:00이지만 UTC 사람에게는 아직 10/29


@async_test
async def test_9_32_고치기_하루_경계():
    """9-32(고치기 하루 경계): 오늘 안으로 옮기기는 지난 약속 오류가 아님(출발이 지났다는 다른 오류), 시간대는 부른 사람 것(UTC 사람은 서울로는 지난 날이어도 오늘 약속을 고침)"""
    env, id_ = await group_case()
    await expect_http(
        lambda: update_appointment(
            ctx_for(env, A, kakao=fake_kakao().lookup),
            upd(id_, {"at": kst("2026-10-26T08:00")}, "this", True),
        ),
        "UNPROCESSABLE",
        "DEPARTURE_PASSED",
    )  # 기준 시각(10/26 10:00)의 오늘 이른 시각
    utc_env, utc_id = await group_case()
    next(p for p in utc_env.db.rows("profiles") if p["user_id"] == A)["timezone"] = "UTC"
    await update_appointment(
        ctx_for(utc_env, A, now=parse_iso("2026-10-29T16:00:00Z"), kakao=fake_kakao().lookup),
        upd(utc_id, {"title": "UTC 사람의 오늘 약속"}, "this", True),
    )
    assert row(utc_env.db, utc_id)["title"] == "UTC 사람의 오늘 약속"
    seoul_env, seoul_id = await group_case()
    await expect_http(
        lambda: update_appointment(
            ctx_for(seoul_env, A, now=parse_iso("2026-10-29T16:00:00Z"), kakao=fake_kakao().lookup),
            upd(seoul_id, {"title": "서울 사람"}, "this", True),
        ),
        "UNPROCESSABLE",
        "PAST_APPOINTMENT",
    )


@async_test
async def test_9_38_만든_사람이_없는_공동_약속은_참여한_사람이_고침():
    """9-38: 만든 사람이 없는(탈퇴한) 공동 약속은 그 약속에 참여한 사람이 고침(만든 사람 칸이 비어 있어도) — 참여 안 한 그룹원은 FORBIDDEN, 알림은 고친 사람 제외 다른 참여자에게 (지우기는 X-APPT-01)"""
    env, id_ = await group_case([A, B, "u-c"])
    row(env.db, id_)["creator_id"] = None
    spy = push_spy()
    a_before = times(env.db, id_, A)
    res = await update_appointment(
        ctx_for(env, B, kakao=fake_kakao().lookup, push=spy.push),
        upd(id_, {"at": kst("2026-10-29T08:50")}, "this", True),
    )
    assert hm(row(env.db, id_)["appointment_at"]) == "08:50"
    assert times(env.db, id_, A) == [t - mins(10) for t in a_before]
    assert res["participants"] == {"updated": 2, "held": 0, "skipped": 0}
    assert all(m["data"]["recipientUserId"] != B for m in spy.sent)
    env.db.rows("group_members").append({"group_id": GROUP, "user_id": "u-d", "invite_status": "JOINED"})
    env.db.rows("profiles").append(profile_row("u-d"))
    await expect_http(
        lambda: update_appointment(ctx_for(env, "u-d"), upd(id_, {"title": "참여 안 한 사람"}, "this", True)),
        "FORBIDDEN",
    )


@async_test
async def test_x_appt_01_만든_사람이_없는_공동_약속은_참여한_사람이_지움():
    """X-APPT-01: 만든 사람이 없는(탈퇴한) 공동 약속은 그 약속에 참여한 사람이 지움 — 참여 안 한 그룹원은 FORBIDDEN, 지우면 약속 · 참여 · 알람이 사라지고 지운 사람 빼고 취소 알림, 만든 사람이 있는 약속은 참여자가 못 지움"""
    env, id_ = await group_case([A, B, "u-c"])
    row(env.db, id_)["creator_id"] = None
    env.db.rows("group_members").append({"group_id": GROUP, "user_id": "u-d", "invite_status": "JOINED"})
    await expect_http(
        lambda: delete_appointment(ctx_for(env, "u-d"), del_(id_)), "FORBIDDEN"
    )  # 참여 안 한 그룹원
    assert len(env.db.rows("appointments")) == 1
    spy = push_spy()
    res = await delete_appointment(ctx_for(env, B, push=spy.push), del_(id_))  # 참여자 B가 지움
    assert [res["deleted"], res["mode"], res["deleted_appointment_ids"]] == [True, "ROW", [id_]]
    assert (
        len(env.db.rows("appointments"))
        + len(env.db.rows("appointment_participants"))
        + len(env.db.rows("alarms"))
        == 0
    )
    assert res["notified"] == 1  # 알림 받는 폰이 있는 다른 참여자 u-c (A는 시험 설정에 폰이 없음)
    assert [[m["data"]["change"], m["data"]["recipientUserId"]] for m in spy.sent] == [["CANCELED", "u-c"]]
    alive_env, alive_id = await group_case([A, B])  # 만든 사람이 있는 약속은 참여자가 못 지움 (9-18 · 9-31)
    await expect_http(lambda: delete_appointment(ctx_for(alive_env, B), del_(alive_id)), "FORBIDDEN")
    assert len(alive_env.db.rows("appointments")) == 1


@async_test
async def test_x_appt_03_장소도_바뀜_길찾기가_실패하면_알람을_같이_앞당김():
    """X-APPT-03(장소도 바뀜): 약속을 30분 앞당기며 장소도 바꿨는데 참여자의 길찾기가 실패하면 그 사람의 알람을 지난번 간격 그대로 30분 같이 앞당기고 확인 알림(needsConfirm) — 경고는 SHIFTED, 길찾기가 된 사람은 새 계산"""
    env, id_ = await group_case([A, B, "u-c"])

    async def lookup(args: Any) -> dict:
        if abs(_f(_f(args, "origin"), "lat") - FAR["lat"]) < 0.001:
            raise KakaoError("QUOTA")  # B(먼 집)만 실패
        return {"minutes": 50, "partial": False}

    b_before = times(env.db, id_, B)
    spy = push_spy()
    res = await update_appointment(
        ctx_for(env, A, kakao=lookup, push=spy.push),
        upd(
            id_,
            {
                "at": kst("2026-10-29T08:30"),
                "destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276},
            },
            "this",
            True,
        ),
    )
    assert times(env.db, id_, B) == [t - mins(30) for t in b_before]  # 새 약속 뒤에 남지 않고 같이 앞당겨짐
    assert res["participants"] == {"updated": 2, "held": 0, "skipped": 0}  # B(옮김) + u-c(새 계산)
    failed = [w for w in res["warnings"] if w["code"] == "PARTICIPANT_ROUTE_FAILED"]
    assert [w.get("detail") for w in failed] == ["SHIFTED"]
    assert B not in json.dumps(res["warnings"])
    by_user = {m["data"]["recipientUserId"]: m["data"]["needsConfirm"] for m in spy.sent}
    assert by_user[B] is True  # 폰이 확인 화면을 띄움


@async_test
async def test_x_appt_03_늦춤_장소만이면_알람을_옮기지_않음():
    """X-APPT-03(늦춤 · 장소만): 길찾기가 실패한 참여자라도 약속을 5분 이상 늦추면 알람을 옮기지 않고(확인이 먼저) 기존대로 두며, 시각 없이 장소만 바뀐 경우도 알람 그대로 + 경고"""

    async def lookup(args: Any) -> dict:
        if abs(_f(_f(args, "origin"), "lat") - FAR["lat"]) < 0.001:
            raise KakaoError("QUOTA")
        return {"minutes": 30, "partial": False}

    later_env, later_id = await group_case([A, B])
    b_later = times(later_env.db, later_id, B)
    res_later = await update_appointment(
        ctx_for(later_env, A, kakao=lookup, push=push_spy().push),
        upd(
            later_id,
            {
                "at": kst("2026-10-29T09:30"),
                "destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276},
            },
            "this",
            True,
        ),
    )
    assert times(later_env.db, later_id, B) == b_later
    assert res_later["participants"] == {"updated": 0, "held": 0, "skipped": 1}
    assert [w.get("detail") for w in res_later["warnings"] if w["code"] == "PARTICIPANT_ROUTE_FAILED"] == [
        None
    ]
    place_env, place_id = await group_case([A, B])
    b_place = times(place_env.db, place_id, B)
    res_place = await update_appointment(
        ctx_for(place_env, A, kakao=lookup, push=push_spy().push),
        upd(place_id, {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}}, "this", True),
    )
    assert times(place_env.db, place_id, B) == b_place
    assert res_place["participants"] == {"updated": 0, "held": 0, "skipped": 1}


@async_test
async def test_x_appt_03_저장된_이동시간_없음():
    """X-APPT-03(저장된 이동시간 없음): 시각만 앞당겼는데 참여자의 이동시간도 출발지도 없어 계산할 수 없어도 그 사람의 알람은 새 약속 뒤에 남지 않고 같이 앞당겨지며, 앞당긴 시각이 이미 지났으면 그 알람은 꺼짐"""
    env, id_ = await group_case([A, B])
    p = participant_of(env.db, id_, B)
    p["est_travel_minutes"] = None
    p["origin_lat"] = None
    p["origin_lng"] = None
    prof = next(x for x in env.db.rows("profiles") if x["user_id"] == B)
    prof["home_lat"] = None
    prof["home_lng"] = None
    before = [ms(parse_iso(x["scheduled_at"])) for x in alarms_of(env.db, id_, B)]
    spy = push_spy()
    res = await update_appointment(
        ctx_for(env, A, kakao=fake_kakao().lookup, push=spy.push),
        upd(id_, {"at": kst("2026-10-29T08:30")}, "this", True),
    )
    assert times(env.db, id_, B) == [t - mins(30) for t in before]
    assert res["participants"] == {"updated": 1, "held": 0, "skipped": 0}
    assert [w.get("detail") for w in res["warnings"] if w["code"] == "PARTICIPANT_ROUTE_FAILED"] == [
        "SHIFTED"
    ]
    assert {m["data"]["recipientUserId"]: m["data"]["needsConfirm"] for m in spy.sent}[B] is True


@async_test
async def test_x_appt_03_옮기는_대상():
    """X-APPT-03(옮기는 대상): 이미 울린 알람은 그대로, 꺼 둔 알람은 꺼 둔 채 같이 옮겨 순서가 안 뒤집히고, 옮긴 시각이 이미 지난 알람만 꺼지고 아직 안 지난 알람은 켜진 채 옮겨짐"""
    env, id_ = await group_case([A, B])
    # 만든 사람은 알람이 없어 자기 계산에 안 걸림
    env.db.tables["appointment_participants"] = [
        x for x in env.db.rows("appointment_participants") if x["user_id"] != A
    ]
    env.db.tables["alarms"] = [x for x in env.db.rows("alarms") if x["user_id"] != A]
    rows = sorted(alarms_of(env.db, id_, B), key=lambda x: ms(parse_iso(x["scheduled_at"])))
    assert len(rows) == 4
    old = [{"id": x["alarm_id"], "at": ms(parse_iso(x["scheduled_at"]))} for x in rows]
    rows[1]["is_enabled"] = False  # 둘째 알람은 꺼 둠
    now = from_ms(old[0]["at"] + mins(1))  # 첫 알람만 울린 뒤
    delta = -(-(old[2]["at"] - ms(now)) // mins(1)) * mins(
        1
    )  # 셋째 알람은 옮기면 now 이전(지남), 넷째는 아직 이후 (올림)
    assert old[3]["at"] - delta > ms(now) and old[2]["at"] - delta <= ms(now)

    async def lookup(args: Any) -> dict:
        if abs(_f(_f(args, "origin"), "lat") - FAR["lat"]) < 0.001:
            raise KakaoError("QUOTA")
        return {"minutes": 30, "partial": False}

    new_at = from_ms(ms(parse_iso(row(env.db, id_)["appointment_at"])) - delta)
    res = await update_appointment(
        ctx_for(env, A, now=now, kakao=lookup, push=push_spy().push),
        upd(
            id_,
            {"at": new_at, "destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}},
            "this",
            True,
        ),
    )
    assert res["participants"] == {"updated": 1, "held": 0, "skipped": 0}

    def by_id(alarm_id: str) -> dict:
        return next(x for x in alarms_of(env.db, id_, B) if x["alarm_id"] == alarm_id)

    state = [
        {"at": ms(parse_iso(by_id(o["id"])["scheduled_at"])), "on": by_id(o["id"])["is_enabled"]} for o in old
    ]
    assert state == [
        {"at": old[0]["at"], "on": True},  # 이미 울린 알람은 그대로
        {"at": old[1]["at"] - delta, "on": False},  # 꺼 둔 알람은 꺼 둔 채 같이 옮김
        {"at": old[2]["at"] - delta, "on": False},  # 옮긴 시각이 이미 지났으면 꺼짐
        {"at": old[3]["at"] - delta, "on": True},  # 아직 안 지났으면 켜진 채 옮겨짐
    ]


@async_test
async def test_x_appt_03_다시_보내기_옮기기는_약속_행을_쓴_뒤():
    """X-APPT-03(다시 보내기): 약속 행 쓰기가 실패한 요청을 다시 보내도 참여자 알람은 한 번만 옮겨짐(옮기기는 약속 행을 쓴 뒤) — 첫 시도는 알람을 안 건드림"""
    env, id_ = await group_case([A, B])

    async def lookup(args: Any) -> dict:
        if abs(_f(_f(args, "origin"), "lat") - FAR["lat"]) < 0.001:
            raise KakaoError("QUOTA")
        return {"minutes": 50, "partial": False}

    b_before = times(env.db, id_, B)
    req = upd(
        id_,
        {"at": kst("2026-10-29T08:30"), "destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}},
        "this",
        True,
    )
    env.db.fail_on.append({"table": "appointments", "op": "update"})
    with pytest.raises(Exception):  # noqa: B017 — 어떤 오류든 (원본: assert.rejects)
        await update_appointment(ctx_for(env, A, kakao=lookup, push=push_spy().push), req)
    assert times(env.db, id_, B) == b_before  # 약속 행이 안 써졌으니 알람도 그대로
    env.db.fail_on.clear()
    await update_appointment(ctx_for(env, A, kakao=lookup, push=push_spy().push), req)
    assert times(env.db, id_, B) == [t - mins(30) for t in b_before]  # 한 번만 옮겨짐


@async_test
async def test_x_appt_01_지난_약속은_참여자가_못_지움():
    """X-APPT-01(지난 약속): 만든 사람이 없는 공동 약속이라도 지난 약속은 참여자가 못 지움 — 정책이 막아 0행이 되는데 성공처럼 보이지 않게 먼저 PAST_APPOINTMENT"""
    env, id_ = await group_case([A, B])
    row(env.db, id_)["creator_id"] = None
    await expect_http(
        lambda: delete_appointment(
            ctx_for(env, B, now=kst("2026-10-30T10:00"), push=push_spy().push), del_(id_)
        ),
        "UNPROCESSABLE",
        "PAST_APPOINTMENT",
    )
    assert len(env.db.rows("appointments")) == 1
    await delete_appointment(
        ctx_for(env, B, push=push_spy().push), del_(id_)
    )  # 안 지난 약속(기준 시각 10/26)은 지움
    assert len(env.db.rows("appointments")) == 0


@async_test
async def test_9_43_10명_그룹에서_장소를_바꾸면_참여자마다_길찾기를_다시_부름():
    """9-43: 10명 그룹에서 장소를 바꾸면 참여자마다 길찾기를 다시 부름(최대 10번), 일부가 실패해도 나머지는 정상이고 실패한 사람은 기존 알람 유지"""
    users = [A, B, *[f"u-p{i + 1}" for i in range(8)]]
    env, id_ = await group_case(users)
    calls = 0

    async def lookup(args: Any) -> dict:
        nonlocal calls
        calls += 1
        if abs(_f(_f(args, "origin"), "lat") - 37.38) < 0.001:
            raise KakaoError("QUOTA")  # 한 사람만 실패
        return {"minutes": 100, "partial": False}  # 이동이 길어져 모두 앞당겨짐

    before = [json.dumps(times(env.db, id_, u)) for u in users]
    res = await update_appointment(
        ctx_for(env, A, kakao=lookup, push=push_spy().push),
        upd(id_, {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}}, "this", True),
    )
    assert calls == 10
    assert res["participants"] == {"updated": 8, "held": 0, "skipped": 1}
    assert len([w for w in res["warnings"] if w["code"] == "PARTICIPANT_ROUTE_FAILED"]) == 1
    unchanged = [u for i, u in enumerate(users) if json.dumps(times(env.db, id_, u)) == before[i]]
    assert unchanged == [users[2]]  # 실패한 사람(출발지 37.38)만 기존 알람 그대로 — users[2]


@async_test
async def test_공동_약속_고치기에서_service는_다른_참여자의_행을_읽고_쓰는_곳에만_닿음():
    """공동 약속 고치기에서 service 클라이언트는 다른 참여자의 행을 읽고 쓰는 곳에만 닿음 — 개인 일정 고치기는 그대로 roll_attempts뿐"""
    env, id_ = await group_case()
    await update_appointment(
        ctx_for(env, A, kakao=fake_kakao().lookup, push=push_spy().push),
        upd(id_, {"at": kst("2026-10-29T08:50")}, "this", True),
    )
    assert all(t in env.service_tables for t in ("appointment_participants", "group_members"))


@async_test
async def test_공동_약속에서_나만_빠지기():
    """공동 약속에서 나만 빠지기: 내 참여 · 알람만 사라지고 약속 · 만든 사람의 참여는 그대로, 만든 사람 · 공동 약속이 아닌 일정은 UNPROCESSABLE, 두 번 보내도 같은 결과"""
    env = setup([A, B])
    env.db.rows("group_members").extend(
        [
            {"group_id": "g1", "user_id": A, "invite_status": "JOINED"},
            {"group_id": "g1", "user_id": B, "invite_status": "JOINED"},
        ]
    )
    [id_] = raw_series(env.db, A, ats=["2026-10-29T09:00"], rule=None, title="팀플", group="g1")
    add_participant(env.db, B, id_, parse_iso(iso("2026-10-29T09:00")))
    own = await single(env, A, "2026-10-30T09:00")
    b_alarms = alarm_ids(env.db, id_, B)
    a_alarms = json.dumps(alarms_of(env.db, id_, A), default=str)

    b = ctx_for(env, B)
    res = await leave_shared_appointment(b, leave(id_))
    assert [res["left"], sorted(res["canceled_alarm_ids"])] == [True, sorted(b_alarms)]
    assert [
        bool(participant_of(env.db, id_, B)),
        len(alarms_of(env.db, id_, B)),
        bool(row(env.db, id_)),
        json.dumps(alarms_of(env.db, id_, A), default=str),
    ] == [False, 0, True, a_alarms]

    saved = snap(env.db)
    again = await leave_shared_appointment(b, leave(id_))
    assert [again["left"], again["canceled_alarm_ids"]] == [True, []]
    assert snap(env.db) == saved

    await expect_http(
        lambda: leave_shared_appointment(ctx_for(env, A), leave(id_)), "UNPROCESSABLE", "CREATOR_CANNOT_LEAVE"
    )
    await expect_http(
        lambda: leave_shared_appointment(ctx_for(env, A), leave(own)), "UNPROCESSABLE", "NOT_SHARED"
    )


@async_test
async def test_service_클라이언트는_필요한_곳에만_닿음():
    """service 클라이언트는 필요한 곳(재시도 기록 정리 · 공동 약속 기록 확인)에만 닿음 — 나머지 읽기 · 쓰기는 모두 부른 사람의 RLS 클라이언트"""
    env = setup()
    s = await seeded_series(env)
    ids = raw_series(env.db, A, ats=["2026-10-29T15:00", "2026-11-03T15:00"], rule="FREQ=WEEKLY;BYDAY=TU,TH")
    single1 = await single(env, A, "2026-10-30T09:00")
    ctx = ctx_for(env, A, kakao=fake_kakao().lookup)
    await update_appointment(ctx, upd(s["thu"]["appointment_id"], {"title": "x"}, "this", True))
    await update_appointment(ctx, upd(ids[0], {"at": kst("2026-10-29T16:00")}, "following", True))
    await delete_appointment(ctx, del_(ids[0], "following"))
    await delete_appointment(ctx, del_(single1))
    await restore_appointment(ctx, restore(s["thu"]["appointment_id"]))
    assert sorted(env.service_tables) == ["roll_attempts"]


# ===========================================================================
# 중간에 실패해도 다시 보내면 이어서 끝남
# ===========================================================================


@async_test
async def test_알람을_쓰다_실패하면_다시_보내면_그대로_끝남():
    """알람을 쓰다 실패하면 약속 행은 안 바뀐 채라 같은 요청을 다시 보내면 그대로 끝남"""
    env = setup()
    s = await seeded_series(env)
    thu = s["thu"]
    request = upd(thu["appointment_id"], {"at": kst("2026-10-29T10:00")}, "this", True)
    env.db.fail_on.append({"table": "alarms", "op": "update"})
    await expect_http(lambda: update_appointment(ctx_for(env, A), request), "INTERNAL")
    saved_row = row(env.db, thu["appointment_id"])
    assert [hm(saved_row["appointment_at"]), saved_row["exception_kind"]] == ["09:00", None]

    env.db.fail_on.clear()
    res = await update_appointment(ctx_for(env, A), request)
    assert res["saved"]
    assert lines(env.db, thu["appointment_id"]) == [
        "WAKE 08:25 on",
        "PREPARE 08:55 on",
        "DEPARTURE 09:15 on",
        "ARRIVE 09:50 on",
    ]
    assert row(env.db, thu["appointment_id"])["exception_kind"] == "EDITED"


@async_test
async def test_이후_모두_고치기_중간에_실패해도_다시_보내면_끝남():
    """이후 모두 고치기 중간에 실패해도(뒤 회차는 이미 지움) 다시 보내면 이 회차가 고쳐지고 끝남"""
    env = setup()
    ids = raw_series(
        env.db,
        A,
        ats=["2026-10-29T09:00", "2026-11-03T09:00", "2026-11-05T09:00"],
        rule="FREQ=WEEKLY;BYDAY=TU,TH",
    )
    request = upd(ids[0], {"at": kst("2026-10-29T08:30")}, "following", True)
    env.db.fail_on.append({"table": "appointments", "op": "update"})
    await expect_http(lambda: update_appointment(ctx_for(env, A), request), "INTERNAL")
    assert hm(row(env.db, ids[0])["appointment_at"]) == "09:00"  # 이 회차는 그대로
    assert [a["appointment_id"] for a in env.db.rows("appointments")] == [ids[0]]  # 뒤 회차는 이미 지움

    env.db.fail_on.clear()
    res = await update_appointment(ctx_for(env, A), request)
    assert res["saved"]
    assert hm(row(env.db, ids[0])["appointment_at"]) == "08:30"
    assert next(x for x in lines(env.db, ids[0]) if x.startswith("DEPARTURE")) == "DEPARTURE 07:30 on"


@async_test
async def test_되돌리기_중간에_실패해도_다시_보내면_이어서_끝남():
    """되돌리기 중간에 실패해도(알람은 만들었는데 표시 쓰기 실패) 다시 보내면 이어서 끝남 — 같은 알람을 또 넣다 CONFLICT가 나지 않음 (로컬 리뷰 반영)"""
    env = setup()
    ids = raw_series(env.db, A, ats=["2026-10-29T09:00", "2026-11-03T09:00"], rule="FREQ=WEEKLY;BYDAY=TU,TH")
    await delete_appointment(ctx_for(env, A), del_(ids[1], "this"))
    env.db.fail_on.append({"table": "appointments", "op": "update"})
    await expect_http(lambda: restore_appointment(ctx_for(env, A), restore(ids[1])), "INTERNAL")
    assert row(env.db, ids[1])["exception_kind"] == "CANCELED"  # 표시는 그대로, 알람은 이미 만들어 둔 상태
    assert kinds(env.db, ids[1]) == "ARRIVE,DEPARTURE,PREPARE,WAKE"

    env.db.fail_on.clear()
    res = await restore_appointment(ctx_for(env, A), restore(ids[1]))
    assert [res["restored"], res.get("reason")] == [True, None]
    assert row(env.db, ids[1])["exception_kind"] == "EDITED"
    assert kinds(env.db, ids[1]) == "ARRIVE,DEPARTURE,PREPARE,WAKE"  # 알람이 두 벌이 되지 않음


@async_test
async def test_run_edit이_action에_맞는_함수를_부름():
    """runEdit이 action에 맞는 함수를 부름"""
    env = setup()
    id_ = await single(env, A, "2026-10-29T09:00")
    ctx = ctx_for(env, A)
    assert (await run_edit(ctx, upd(id_, {"title": "바꿈"})))["saved"] is True
    assert (await run_edit(ctx, restore(id_)))["restored"] is False
    assert (await run_edit(ctx, del_(id_)))["deleted"] is True


# ===========================================================================
# 저녁에만 일정이 있는 날은 기상 알람 없음 (#320) — 계산된 기상이 낮 12시 이후면 준비 · 출발 · 도착만
# ===========================================================================


@async_test
async def test_320_저녁_단일_일정은_기상_없이_준비_출발_도착만():
    """#320: 저녁 단일 일정은 기상 없이 준비 · 출발 · 도착만, 저녁 일정 둘도 둘 다 기상 없음 (첫 일정이 기상 없다고 둘째가 기상을 얻지 않음)"""
    env = setup()
    e1 = await single(env, A, "2026-10-29T17:00")
    e2 = await single(env, A, "2026-10-29T19:00")
    assert kinds(env.db, e1) == "ARRIVE,DEPARTURE,PREPARE"
    assert kinds(env.db, e2) == "ARRIVE,DEPARTURE,PREPARE"
    assert participant_of(env.db, e1)["prepare_minutes"] == 15


@async_test
async def test_320_오전_일정_저녁_일정_오전을_지워도_저녁에_기상을_되살리지_않음():
    """#320: 오전 일정 + 저녁 일정 — 오전은 기상, 저녁은 기상 없음. 오전 일정을 지워도 저녁 일정에 기상을 되살리지 않음"""
    env = setup()
    morning = await single(env, A, "2026-10-29T09:00")
    evening = await single(env, A, "2026-10-29T18:00")
    assert kinds(env.db, morning) == "ARRIVE,DEPARTURE,PREPARE,WAKE"
    assert kinds(env.db, evening) == "ARRIVE,DEPARTURE,PREPARE"
    before = json.dumps(alarms_of(env.db, evening), default=str)
    await delete_appointment(ctx_for(env, A), del_(morning))
    assert json.dumps(alarms_of(env.db, evening), default=str) == before


@async_test
async def test_320_아침_일정을_저녁으로_옮기면_기상이_떨어지고_다시_아침으로_옮기면_붙음():
    """#320: 아침 일정을 저녁으로 옮기면 기상이 떨어지고, 다시 아침으로 옮기면 기상이 붙음"""
    env = setup()
    a = await single(env, A, "2026-10-29T09:00")
    wake = next(x for x in alarms_of(env.db, a) if x["alarm_type"] == "WAKE")
    assert wake
    res = await update_appointment(ctx_for(env, A), upd(a, {"at": kst("2026-10-29T20:00")}, "this", True))
    assert kinds(env.db, a) == "ARRIVE,DEPARTURE,PREPARE"
    assert wake["alarm_id"] in res["canceled_alarm_ids"]
    await update_appointment(ctx_for(env, A), upd(a, {"at": kst("2026-10-29T09:00")}, "this", True))
    assert kinds(env.db, a) == "ARRIVE,DEPARTURE,PREPARE,WAKE"


@async_test
async def test_320_저녁_일정_둘_중_앞_일정을_지워도_다른_저녁_일정에_기상이_붙지_않음():
    """#320: 저녁 일정 둘 중 앞 일정을 지워도 다른 저녁 일정에 기상이 붙지 않음"""
    env = setup()
    e1 = await single(env, A, "2026-10-29T17:00")
    e2 = await single(env, A, "2026-10-29T19:00")
    await delete_appointment(ctx_for(env, A), del_(e1))
    assert kinds(env.db, e2) == "ARRIVE,DEPARTURE,PREPARE"


@async_test
async def test_320_f1_일부러_끈_기상은_장소_중요도만_고쳐도_되살아나지_않음():
    """#320 F1: 13:00 일정에서 사용자가 일부러 끈 기상은 장소 · 중요도만 고쳐도 되살아나지 않음 (계산된 기상이 낮 12시 전이었으므로)"""
    env = setup()
    a = await single(env, A, "2026-10-29T13:00")
    assert any(
        x["alarm_type"] == "WAKE" for x in alarms_of(env.db, a)
    )  # 계산된 기상이 12시 전이라 처음엔 기상이 있음
    # 사용자가 H05에서 기상을 끔 — 기상 행만 지움
    rows = env.db.rows("alarms")
    pid = participant_of(env.db, a)["participant_id"]
    wake_idx = next(i for i, x in enumerate(rows) if x["participant_id"] == pid and x["alarm_type"] == "WAKE")
    rows.pop(wake_idx)
    assert kinds(env.db, a) == "ARRIVE,DEPARTURE,PREPARE"
    await update_appointment(ctx_for(env, A), upd(a, {"importance": "HIGH"}, "this", True))
    assert kinds(env.db, a) == "ARRIVE,DEPARTURE,PREPARE"  # 일부러 끈 기상이 그대로


@async_test
async def test_320_f1_저녁_일정을_아침으로_옮기면_기상이_생김():
    """#320 F1: 저녁 일정(규칙으로 기상 없음)을 아침으로 옮기면 기상이 생김"""
    env = setup()
    a = await single(env, A, "2026-10-29T20:00")
    assert kinds(env.db, a) == "ARRIVE,DEPARTURE,PREPARE"
    await update_appointment(ctx_for(env, A), upd(a, {"at": kst("2026-10-29T09:00")}, "this", True))
    assert kinds(env.db, a) == "ARRIVE,DEPARTURE,PREPARE,WAKE"


# ---- 9장 PR 리뷰 반영 ----


@async_test
async def test_9_18_서버_그룹을_나간_만든_사람은_고치기_지우기가_not_found():
    """9-18(서버): 그룹을 나간(내보내진) 만든 사람은 약속이 보여도 고치기 · 지우기가 NOT_FOUND — 다른 참여자의 알람은 건드리지 않고 알림도 없음 (RLS가 약속 행을 막기 전에 service가 먼저 쓰지 못하게)"""
    env, id_ = await group_case()
    # 나가면 DB가 그 사람의 앞으로 약속 참여 조건 · 알람을 지움(트리거) — 약속 행은 남고 만든 사람 칸도 그대로
    next(m for m in env.db.rows("group_members") if m["user_id"] == A)["invite_status"] = "LEFT"
    env.db.tables["alarms"] = [x for x in env.db.rows("alarms") if x["user_id"] != A]
    env.db.tables["appointment_participants"] = [
        p for p in env.db.rows("appointment_participants") if p["user_id"] != A
    ]
    b_before = times(env.db, id_, B)
    spy = push_spy()
    with pytest.raises(AppError) as e1:
        await update_appointment(
            ctx_for(env, A, kakao=fake_kakao().lookup, push=spy.push),
            upd(id_, {"at": kst("2026-10-29T08:50")}, "this", True),
        )
    assert e1.value.status == 404
    with pytest.raises(AppError) as e2:
        await delete_appointment(ctx_for(env, A, push=spy.push), del_(id_))
    assert e2.value.status == 404
    assert times(env.db, id_, B) == b_before
    assert hm(row(env.db, id_)["appointment_at"]) == "09:00"
    assert spy.sent == []


class _FailedUpdate:
    """update(...).eq(...).eq(...) 를 await하면 DB 오류 결과"""

    def eq(self, *args: Any, **kwargs: Any) -> "_FailedUpdate":
        return self

    def __await__(self):
        async def result() -> Result:
            return Result(None, {"message": "boom", "code": "XX000"}, None)

        return result().__await__()


class _FailingParticipantQuery:
    def __init__(self, q: Any) -> None:
        self._q = q

    def update(self, *args: Any, **kwargs: Any) -> _FailedUpdate:
        return _FailedUpdate()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._q, name)


class _FailingParticipantUpdate(ServiceDb):
    """서비스 클라이언트의 참여 조건 update만 DB 오류로 만든 껍데기 (읽기는 그대로)"""

    def from_(self, table: str):
        q = self.env.db.from_(table)
        return _FailingParticipantQuery(q) if table == "appointment_participants" else q


@async_test
async def test_9_14_오류_참여자를_맞추다_db_오류가_나면_요청이_실패():
    """9-14(오류): 참여자를 맞추다 DB 오류가 나면 "건너뜀"으로 삼키지 않고 요청이 실패(500) — 약속 행은 안 바뀌고 푸시도 없어서, 같은 요청을 다시 보내면 이어서 맞춰짐"""
    env, id_ = await group_case()
    b_before = times(env.db, id_, B)
    spy = push_spy()
    ctx = ctx_for(env, A, kakao=fake_kakao().lookup, push=spy.push)
    ctx.service = _FailingParticipantUpdate(env)
    with pytest.raises(AppError) as info:
        await update_appointment(ctx, upd(id_, {"at": kst("2026-10-29T08:50")}, "this", True))
    assert info.value.status == 500
    assert hm(row(env.db, id_)["appointment_at"]) == "09:00"  # 약속 행은 그대로
    assert times(env.db, id_, B) == b_before
    assert spy.sent == []

    again = await update_appointment(
        ctx_for(env, A, kakao=fake_kakao().lookup, push=spy.push),
        upd(id_, {"at": kst("2026-10-29T08:50")}, "this", True),
    )  # 다시 보냄
    assert again["participants"] == {"updated": 1, "held": 0, "skipped": 0}
    assert times(env.db, id_, B) == [t - mins(10) for t in b_before]
    assert hm(row(env.db, id_)["appointment_at"]) == "08:50"
    assert len(spy.sent) == 1


@async_test
async def test_9_17_알림_알람을_그대로_둔_참여자_폰에도_needs_confirm():
    """9-17(알림): 이동시간을 못 구해 알람을 그대로 둔 참여자 폰에도 needsConfirm: true — 앞당김 · 5분 미만 늦춤으로 바로 맞춘 사람은 false"""
    env, id_ = await group_case([A, B, "u-c"])

    async def lookup(args: Any) -> dict:
        if abs(_f(_f(args, "origin"), "lat") - FAR["lat"]) < 0.001:
            raise KakaoError("QUOTA")  # B(먼 집, 37.4)만 실패 — u-c(37.38)는 이동이 길어져 앞당겨짐
        return {"minutes": 100, "partial": False}

    spy = push_spy()
    res = await update_appointment(
        ctx_for(env, A, kakao=lookup, push=spy.push),
        upd(id_, {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}}, "this", True),
    )
    assert res["participants"] == {"updated": 1, "held": 0, "skipped": 1}
    by_user = {m["data"]["recipientUserId"]: m["data"]["needsConfirm"] for m in spy.sent}
    assert by_user == {B: True, "u-c": False}


@async_test
async def test_9_43_동시_참여자는_4명씩_묶어_한꺼번에_맞춤():
    """9-43(동시): 참여자는 4명씩 묶어 한꺼번에 맞춤 — 길찾기가 동시에 4번을 넘지 않고(앱 제한 시간 안에 끝나게), 둘 이상은 겹쳐 돌며, 결과 요약 · 경고 순서는 그대로"""
    users = [A, B, *[f"u-p{i + 1}" for i in range(8)]]
    env, id_ = await group_case(users)
    inflight = 0
    peak = 0

    async def lookup(args: Any) -> dict:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.005)
        inflight -= 1
        if abs(_f(_f(args, "origin"), "lat") - 37.38) < 0.001:
            raise KakaoError("QUOTA")
        return {"minutes": 100, "partial": False}

    res = await update_appointment(
        ctx_for(env, A, kakao=lookup, push=push_spy().push),
        upd(id_, {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}}, "this", True),
    )
    assert SYNC_CONCURRENCY == 4
    assert 1 < peak <= 4, f"동시 길찾기 최대 {peak}"
    assert res["participants"] == {"updated": 8, "held": 0, "skipped": 1}
    assert len([w for w in res["warnings"] if w["code"] == "PARTICIPANT_ROUTE_FAILED"]) == 1


# ---- 10장: 친구가 제안해 주인이 적용한 알람 시각 (original_scheduled_at이 채워진 MANUAL) ----


async def friend_applied_case():
    """10/29 09:00 약속의 기상을 친구가 07:25 → 07:20으로 바꿔 주인이 적용한 상태"""
    env = setup()
    s = await seeded_series(env)
    thu = s["thu"]
    wake = next(a for a in alarms_of(env.db, thu["appointment_id"]) if a["alarm_type"] == "WAKE")
    original = wake["scheduled_at"]
    wake["scheduled_at"] = to_iso(from_ms(ms(parse_iso(original)) - mins(5)))
    wake["time_source"] = "MANUAL"
    wake["original_scheduled_at"] = original
    wake["original_time_source"] = "AUTO"  # 친구가 바꾸기 전엔 자동 계산이던 알람
    return env, thu, wake, original


async def friend_applied_all():
    """기상 · 준비 · 출발 셋 다 친구가 바꿔 주인이 적용한 상태 (기상 −10 · 준비 +10 · 출발 −10분)"""
    env = setup()
    s = await seeded_series(env)
    thu = s["thu"]
    for type_, delta in (("WAKE", -10), ("PREPARE", 10), ("DEPARTURE", -10)):
        r = next(a for a in alarms_of(env.db, thu["appointment_id"]) if a["alarm_type"] == type_)
        r["original_scheduled_at"] = r["scheduled_at"]
        r["original_time_source"] = "AUTO"
        r["scheduled_at"] = to_iso(from_ms(ms(parse_iso(r["scheduled_at"])) + delta * 60_000))
        r["time_source"] = "MANUAL"
    return env, thu


@async_test
async def test_10_22_약속의_제목만_다시_저장하면_친구가_정한_알람_시각이_그대로():
    """10-22: 약속의 제목만 다시 저장하면 친구가 정한 알람 시각이 그대로(알람을 안 건드림), 중요도만 바꿔도 시각은 그대로"""
    env, thu, wake, _ = await friend_applied_case()
    before = wake["scheduled_at"]
    await update_appointment(ctx_for(env, A), upd(thu["appointment_id"], {"title": "새 제목"}, "this", True))
    assert [wake["scheduled_at"], wake["time_source"], wake["original_scheduled_at"] is not None] == [
        before,
        "MANUAL",
        True,
    ]
    await update_appointment(
        ctx_for(env, A), upd(thu["appointment_id"], {"importance": "HIGH"}, "this", True)
    )
    assert [wake["scheduled_at"], wake["time_source"], wake["original_scheduled_at"] is not None] == [
        before,
        "MANUAL",
        True,
    ]


@async_test
async def test_10_22_약속의_시각을_바꾸면_친구가_정한_알람_시각은_새_계산에_밀림():
    """10-22: 약속의 시각을 바꾸면 친구가 정한 알람 시각은 새 계산에 밀림(주인이 직접 고른 MANUAL과 다름) — 장소를 바꿔도 같음"""
    env, thu, wake, _ = await friend_applied_case()
    await update_appointment(
        ctx_for(env, A), upd(thu["appointment_id"], {"at": kst("2026-10-29T09:30")}, "this", True)
    )
    moved = next(a for a in alarms_of(env.db, thu["appointment_id"]) if a["alarm_type"] == "WAKE")
    assert (
        hm(moved["scheduled_at"]) == "07:55"
    )  # 09:30 약속의 새 계산 (친구 값 07:20은 같은 날이라 MANUAL이면 그대로였을 것)
    assert moved["time_source"] != "MANUAL"

    place_env, place_thu, _, _ = await friend_applied_case()
    await update_appointment(
        ctx_for(place_env, A, kakao=fake_kakao(20).lookup),
        upd(
            place_thu["appointment_id"],
            {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}},
            "this",
            True,
        ),
    )
    after_place = next(
        a for a in alarms_of(place_env.db, place_thu["appointment_id"]) if a["alarm_type"] == "WAKE"
    )
    assert after_place["time_source"] != "MANUAL"
    assert hm(after_place["scheduled_at"]) == "07:40"  # 이동 20분으로 새 계산 (친구 값이 아님)


@async_test
async def test_10_22_친구_값이_셋_다에_있어도_약속_시각을_바꾸면_새_계산():
    """10-22: 친구 값이 기상 · 준비 · 출발 셋 다에 있어도 약속 시각을 바꾸면 친구 값이 없는 상태와 똑같이 새 계산 — 준비 알람 시각이 친구 값 간격에 끌려가지 않음"""
    control = setup()
    c = await seeded_series(control)
    await update_appointment(
        ctx_for(control, A), upd(c["thu"]["appointment_id"], {"at": kst("2026-10-29T09:30")}, "this", True)
    )

    def shape(db: FakeDb, id_: Any) -> list:
        return sorted([a["alarm_type"], hm(a["scheduled_at"]), a["time_source"]] for a in alarms_of(db, id_))

    expected = shape(control.db, c["thu"]["appointment_id"])

    env, thu = await friend_applied_all()
    await update_appointment(
        ctx_for(env, A), upd(thu["appointment_id"], {"at": kst("2026-10-29T09:30")}, "this", True)
    )
    assert shape(env.db, thu["appointment_id"]) == expected


@async_test
async def test_10_22_친구가_바꾸기_전에_주인이_직접_골랐던_알람은_주인의_값이_이어짐():
    """10-22: 친구가 바꾸기 전에 주인이 직접 골랐던 알람(original_time_source = MANUAL)은 약속 시각이 바뀌면 친구 값만 지워지고 주인의 값이 이어짐"""
    env, thu, wake, original = await friend_applied_case()
    wake["original_time_source"] = "MANUAL"
    await update_appointment(
        ctx_for(env, A), upd(thu["appointment_id"], {"at": kst("2026-10-29T09:30")}, "this", True)
    )
    moved = next(a for a in alarms_of(env.db, thu["appointment_id"]) if a["alarm_type"] == "WAKE")
    assert [hm(moved["scheduled_at"]), moved["time_source"]] == [
        hm(original),
        "MANUAL",
    ]  # 친구 값 07:20이 아니라 주인이 고른 07:25


def test_10_22_알람을_읽는_열_목록에_원래_시각_칸이_들어_있음():
    """10-22: 알람을 읽는 열 목록에 원래 시각 · 그때의 time_source 칸이 들어 있음 (가짜 DB는 열 목록을 안 보므로 따로 못 박음)"""
    columns = [x.strip() for x in ALARM_COLUMNS.split(",")]
    assert "original_scheduled_at" in columns and "original_time_source" in columns


@async_test
async def test_10_23_주인이_직접_고른_manual은_약속_시각이_바뀌어도_그대로():
    """10-23: 주인이 직접 고른 MANUAL(원래 시각 기억 없음)은 약속 시각이 바뀌어도 그대로 — 친구 값과 규칙이 다름 (기존 규칙)"""
    env, thu, wake, _ = await friend_applied_case()
    wake["original_scheduled_at"] = None  # 주인이 직접 고친 값이면 서버 가드가 기억을 비움
    own = wake["scheduled_at"]
    await update_appointment(
        ctx_for(env, A), upd(thu["appointment_id"], {"at": kst("2026-10-29T09:30")}, "this", True)
    )
    assert [wake["scheduled_at"], wake["time_source"]] == [own, "MANUAL"]

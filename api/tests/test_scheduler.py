"""scheduler (완결점 1-10) — 모드별 동작 · 하트비트 · 정시 · 안전 쪽 기본값. Supabase는 가짜 네트워크(MockTransport)로 흉내냄"""

import json
import logging
import signal
import threading
import types
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from app import scheduler as scheduler_module
from app.db import Db, DbError
from app.jobs import JOBS, WAKE_SHADOW_LEAD_SECONDS, Job, daily_at, every_minutes
from app.scheduler import Scheduler

from .conftest import SUPABASE_URL, make_settings

SECRET = "sb_secret_test_value"
BOUNDARY = datetime(2026, 11, 15, 3, 5, 0, tzinfo=UTC)  # 5분 정시
LEAD = timedelta(seconds=WAKE_SHADOW_LEAD_SECONDS)


@dataclass
class FakeRest:
    """PostgREST 흉내 — server_routes 읽기 · server_heartbeat upsert(merge-duplicates) · DB 함수"""

    modes: dict[str, str] = field(default_factory=dict)
    routes_status: int = 200
    routes_body: object | None = None  # 값이 있으면 이 본문을 그대로 (깨진 응답 흉내)
    missed_rows: list[dict] = field(default_factory=list)
    heartbeat_status: int = 200
    heartbeat: dict[str, dict] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/rest/v1/server_routes"):
            assert request.url.params["kind"] == "eq.job"  # 함수 줄이 아니라 작업 줄만
            if self.routes_status != 200:
                return httpx.Response(self.routes_status, json={"message": "boom"})
            if self.routes_body is not None:
                return httpx.Response(200, json=self.routes_body)
            return httpx.Response(200, json=[{"name": n, "mode": m} for n, m in self.modes.items()])
        if request.method == "POST" and path.endswith("/rest/v1/server_heartbeat"):
            if self.heartbeat_status != 200:
                return httpx.Response(self.heartbeat_status, json={"message": "boom"})
            assert request.url.params["on_conflict"] == "job_name"
            assert "merge-duplicates" in request.headers["prefer"]
            for row in json.loads(request.content):
                self.heartbeat.setdefault(row["job_name"], {}).update(row)  # 본문에 있는 칸만 고침
            return httpx.Response(201)
        if request.method == "POST" and path.endswith("/rest/v1/rpc/find_missed_wake_alarms"):
            return httpx.Response(200, json=self.missed_rows)
        return httpx.Response(404)

    def calls(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]

    def rpc_bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.calls("POST", "/rpc/find_missed_wake_alarms")]

    def success_writes(self) -> list[dict]:
        return [
            row
            for r in self.calls("POST", "/rest/v1/server_heartbeat")
            for row in json.loads(r.content)
            if "last_success_at" in row
        ]


def make_scheduler(rest: FakeRest, jobs: tuple[Job, ...] = JOBS, clock=None) -> Scheduler:
    db = Db(make_settings(), transport=httpx.MockTransport(rest.handler))
    return Scheduler(db, jobs, clock=clock or (lambda: BOUNDARY))


@pytest.fixture
def rest() -> FakeRest:
    return FakeRest()


@pytest.fixture(autouse=True)
def _log_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="respec.scheduler")


def lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "respec.scheduler"]


# ---------- 정시 계산 ----------


def test_every_five_minutes_matches_pg_cron_boundaries():
    nxt = every_minutes(5)
    assert nxt(datetime(2026, 11, 15, 3, 2, 10, tzinfo=UTC)) == BOUNDARY
    assert nxt(BOUNDARY) == BOUNDARY + timedelta(minutes=5)  # 정시 자체면 다음 정시
    assert nxt(BOUNDARY - timedelta(microseconds=1)) == BOUNDARY
    kst = timezone(timedelta(hours=9))
    assert nxt(datetime(2026, 11, 15, 12, 2, 10, tzinfo=kst)) == BOUNDARY  # 시간대가 달라도 UTC 정시


def test_daily_at_is_0900_utc_which_is_1800_kst():
    nxt = daily_at(9)
    assert nxt(datetime(2026, 11, 15, 8, 59, tzinfo=UTC)) == datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    assert nxt(datetime(2026, 11, 15, 9, 0, tzinfo=UTC)) == datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
    assert nxt(datetime(2026, 11, 15, 23, 0, tzinfo=UTC)) == datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def test_registered_jobs_match_m1_rows():
    # 본 레포 M1이 server_routes(kind = 'job') · server_heartbeat에 미리 넣은 이름과 같아야 함 (이름이 다르면 표 줄을 못 찾아 edge로 봄)
    assert [job.name for job in JOBS] == ["wake-check", "alarm-roll", "suggestion-generate"]


# ---------- 하트비트 (alive_at) ----------


def test_heartbeat_writes_alive_at_for_every_job_regardless_of_mode(rest):
    s = make_scheduler(rest)
    t0 = BOUNDARY - timedelta(minutes=2)
    s.tick(t0)
    assert set(rest.heartbeat) == {"wake-check", "alarm-roll", "suggestion-generate"}
    assert all(row["alive_at"] == t0.isoformat() for row in rest.heartbeat.values())
    assert rest.success_writes() == []  # alive_at만 — last_success_at은 건드리지 않음


def test_heartbeat_is_once_a_minute(rest):
    s = make_scheduler(rest)
    t0 = BOUNDARY - timedelta(minutes=3)
    s.tick(t0)
    s.tick(t0 + timedelta(seconds=59))
    assert len(rest.calls("POST", "/rest/v1/server_heartbeat")) == 1
    s.tick(t0 + timedelta(seconds=60))
    assert len(rest.calls("POST", "/rest/v1/server_heartbeat")) == 2


def test_heartbeat_failure_does_not_crash_and_retries_in_a_minute_not_every_second(rest, caplog):
    rest.heartbeat_status = 500
    s = make_scheduler(rest)
    t0 = BOUNDARY - timedelta(minutes=3)
    s.tick(t0)
    s.tick(t0 + timedelta(seconds=1))
    assert len(rest.calls("POST", "/rest/v1/server_heartbeat")) == 1  # 1초마다 DB를 두드리지 않음
    assert any(
        "heartbeat 쓰기 실패: HTTPStatusError 500" in m for m in lines(caplog)
    )  # 상태 번호만 — 주소 · 본문은 안 남김
    assert all(SUPABASE_URL not in m and "boom" not in m for m in lines(caplog))
    rest.heartbeat_status = 200
    s.tick(t0 + timedelta(seconds=61))
    assert rest.heartbeat["wake-check"]["alive_at"] == (t0 + timedelta(seconds=61)).isoformat()


# ---------- mode별 동작 ----------


def test_edge_mode_does_nothing_even_at_due_time(rest):
    rest.modes = {"wake-check": "edge"}
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))  # 첫 틱 — 정시 기준 잡음
    s.tick(BOUNDARY - LEAD)
    assert rest.rpc_bodies() == [] and rest.success_writes() == []


def test_job_without_row_in_table_counts_as_edge(rest):
    rest.modes = {}  # 줄이 없음
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - LEAD)
    assert rest.rpc_bodies() == []


def test_shadow_counts_before_the_boundary_and_logs_only_the_count(rest, caplog):
    rest.modes = {"wake-check": "shadow"}
    rest.missed_rows = [
        {
            "alarm_id": "aaaaaaaa-1111-2222-3333-444444444444",
            "user_id": "bbbbbbbb-1111-2222-3333-444444444444",
        },
        {
            "alarm_id": "cccccccc-1111-2222-3333-444444444444",
            "user_id": "dddddddd-1111-2222-3333-444444444444",
        },
    ]
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    assert rest.rpc_bodies() == []  # 정시 30초 전이 되기 전엔 안 셈
    s.tick(BOUNDARY - LEAD)
    # 정시를 기준 시각(p_now)으로 셈 — Edge가 정시에 볼 것과 같은 후보. 조회 폭 24시간은 Edge와 같음
    assert rest.rpc_bodies() == [{"p_now": BOUNDARY.isoformat(), "p_lookback_hours": 24}]
    assert any(m == "shadow job=wake-check boundary=2026-11-15T03:05:00Z checked=2" for m in lines(caplog))
    text = caplog.text
    assert "aaaaaaaa" not in text and "bbbbbbbb" not in text  # 알람 · 사용자 번호는 로그에 안 남김
    assert SECRET not in text
    assert rest.success_writes() == []  # shadow는 성공 기록을 안 씀 — pg_cron이 계속 Edge를 부르게


def test_shadow_runs_once_per_boundary_and_again_on_the_next(rest):
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - LEAD)
    s.tick(BOUNDARY - LEAD + timedelta(seconds=1))
    s.tick(BOUNDARY + timedelta(seconds=10))
    assert len(rest.rpc_bodies()) == 1
    nxt = BOUNDARY + timedelta(minutes=5)
    s.tick(nxt - LEAD)
    assert [b["p_now"] for b in rest.rpc_bodies()] == [BOUNDARY.isoformat(), nxt.isoformat()]


def test_scheduler_started_inside_the_lead_window_runs_that_boundary_at_once(rest):
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(seconds=10))  # 시작이 30초 앞당김 구간 안
    assert [b["p_now"] for b in rest.rpc_bodies()] == [BOUNDARY.isoformat()]


def test_late_wakeup_skips_that_boundary(rest, caplog):
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY + timedelta(seconds=61))  # 정시를 61초 넘겨 깨어남 (오래 막혔음)
    assert rest.rpc_bodies() == []
    assert any("회차 놓침 job=wake-check boundary=2026-11-15T03:05:00Z count=1" in m for m in lines(caplog))
    nxt = BOUNDARY + timedelta(minutes=5)
    s.tick(nxt - LEAD)  # 다음 회차는 정상
    assert [b["p_now"] for b in rest.rpc_bodies()] == [nxt.isoformat()]


def test_very_late_wakeup_catches_up_in_one_tick_without_running_old_rounds(rest, caplog):
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY + timedelta(hours=2))  # 2시간 막혔다 깨어남
    assert rest.rpc_bodies() == []  # 오래된 회차는 안 돌림
    # 03:05 ~ 05:05 정시 25개를 한 번에 건너뜀 (앞당김이 있는 작업은 정시가 되면 늦은 것 — 05:05도 지금(05:05:00)엔 이미 늦음)
    assert any("count=25" in m for m in lines(caplog))
    s.tick(BOUNDARY + timedelta(hours=2, seconds=1))  # 다음 정시(05:10) 앞당김 구간 전까지는 아무것도 안 돎
    assert rest.rpc_bodies() == []
    nxt = BOUNDARY + timedelta(hours=2, minutes=5)
    s.tick(nxt - LEAD)
    assert [b["p_now"] for b in rest.rpc_bodies()] == [nxt.isoformat()]


def test_shadow_window_is_exactly_the_30_seconds_before_the_boundary(rest, caplog):
    """shadow는 pg_cron이 Edge를 부르기(정시) 전에만 센다 — 정시 뒤에 세면 Edge가 MISSED를 기록한 뒤라 0 근처가 되어 대조가 안 됨"""
    rest.modes = {"wake-check": "shadow"}
    early = make_scheduler(rest)
    early.tick(BOUNDARY - timedelta(minutes=1))
    early.tick(BOUNDARY - timedelta(seconds=31))
    assert rest.rpc_bodies() == []  # 31초 전: 아직 안 셈
    early.tick(BOUNDARY - timedelta(seconds=30))
    assert len(rest.rpc_bodies()) == 1  # 30초 전: 셈

    for late_by in (0, 1, 10, 59):  # 정시 이후는 몇 초든 건너뜀 (틱이 막혔다 늦게 깨어난 경우)
        rest.requests.clear()
        late = make_scheduler(rest)
        late.tick(BOUNDARY - timedelta(minutes=1))
        late.tick(BOUNDARY + timedelta(seconds=late_by))
        assert rest.rpc_bodies() == [], late_by
    assert any("회차 놓침 job=wake-check" in m for m in lines(caplog))


def test_job_without_lead_may_start_up_to_a_minute_late_but_not_after(rest):
    ran: list[datetime] = []

    def work(db: Db, at: datetime) -> str:
        ran.append(at)
        return "ok"

    jobs = (Job("alarm-roll", every_minutes(5), shadow=work),)  # 앞당김 없음
    rest.modes = {"alarm-roll": "shadow"}
    for late_by, expect in ((59, 1), (60, 0)):
        ran.clear()
        s = make_scheduler(rest, jobs)
        s.tick(BOUNDARY - timedelta(minutes=1))
        s.tick(BOUNDARY + timedelta(seconds=late_by))
        assert len(ran) == expect, late_by


# ---------- 시계가 뒤로 가면 ----------


def test_clock_jumping_back_rewrites_heartbeat_at_once_and_replans_jobs_from_the_new_time(rest, caplog):
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest)
    t0 = BOUNDARY - timedelta(minutes=3)  # 03:02
    s.tick(t0)
    back = t0 - timedelta(hours=1)  # 시계가 1시간 뒤로 감 (02:02)
    s.tick(back)
    # 하트비트: 음수 경과가 계속 "아직 아님"으로 읽혀 1시간 동안 멈추면 안 됨 — 바로 새 시각으로 다시 씀
    assert rest.heartbeat["wake-check"]["alive_at"] == back.isoformat()
    assert any("시계가 뒤로 감" in m for m in lines(caplog))
    # 작업: 03:05로 잡혀 있던 정시가 아니라 새 시각 기준(02:05)으로 다시 계산
    boundary = back.replace(minute=5)
    s.tick(boundary - LEAD)
    assert [b["p_now"] for b in rest.rpc_bodies()] == [boundary.isoformat()]


# ---------- 하트비트 성공 로그 (24시간 간격 확인용) ----------


def test_heartbeat_ok_is_logged_about_every_five_minutes_and_not_on_failure(rest, caplog):
    s = make_scheduler(rest)
    t0 = BOUNDARY - timedelta(minutes=30)
    for sec in range(0, 12 * 60, 60):  # 12분 동안 1분마다 하트비트
        s.tick(t0 + timedelta(seconds=sec))
    oks = [m for m in lines(caplog) if m.startswith("heartbeat ok")]
    assert (
        oks
        == [
            f"heartbeat ok at={(t0 + timedelta(seconds=off)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
            for off in (0, 300, 600)
        ]
    )  # 처음 · 5분 뒤 · 10분 뒤 — 줄 사이 간격이 6분 이하여야 "하트비트 간격 6분 이하"를 로그로 확인할 수 있음

    caplog.clear()
    rest.heartbeat_status = 500
    s2 = make_scheduler(rest)
    s2.tick(t0)
    assert not any(m.startswith("heartbeat ok") for m in lines(caplog))


# ---------- 구현 없는 작업이 server로 정해지면 shadow로 낮춤 ----------


def test_server_mode_without_implementation_is_lowered_to_shadow_and_logs_error(rest, caplog):
    rest.modes = {"wake-check": "server"}  # wake-check는 shadow만 구현돼 있음
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - LEAD)
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("shadow로 낮춤" in m and "job=wake-check" in m for m in errors)
    assert len(rest.rpc_bodies()) == 1  # 낮춘 shadow로 세기는 함 (대조는 계속)
    assert rest.success_writes() == []  # 성공 기록은 안 씀 → pg_cron이 계속 Edge를 부름
    assert any(m.startswith("shadow job=wake-check") for m in lines(caplog))


def test_server_mode_on_job_without_any_handler_logs_error_and_does_nothing(rest, caplog):
    rest.modes = {"alarm-roll": "server", "suggestion-generate": "server"}
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY)  # alarm-roll 정시 (앞당김 없음)
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("job=alarm-roll" in m for m in errors)
    assert rest.success_writes() == [] and rest.rpc_bodies() == []
    daily = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    s.tick(daily - timedelta(minutes=1))
    s.tick(daily)  # 매일 18:00 KST
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("job=suggestion-generate" in m for m in errors)
    assert rest.success_writes() == []


# ---------- 표를 못 읽으면 edge ----------


@pytest.mark.parametrize(
    "setup",
    [
        lambda rest: setattr(rest, "routes_status", 500),
        lambda rest: setattr(rest, "routes_body", {"not": "a list"}),
        lambda rest: setattr(rest, "routes_body", "garbage"),
    ],
    ids=["서버 오류", "목록이 아님", "문자열"],
)
def test_unreadable_table_means_edge(rest, caplog, setup):
    rest.modes = {"wake-check": "shadow"}  # 읽었다면 셌을 설정
    setup(rest)
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - LEAD)
    assert rest.rpc_bodies() == [] and rest.success_writes() == []
    assert any("스위치 표를 못 읽어 edge로 봄" in m for m in lines(caplog))


def test_unknown_mode_value_means_edge(rest, caplog):
    rest.modes = {"wake-check": "canary"}  # 함수용 값 — 작업 줄에는 올 수 없는 값 (표 제약이 막지만 안전하게)
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - LEAD)
    assert rest.rpc_bodies() == []
    assert any("모르는 mode라 edge로 봄" in m for m in lines(caplog))


def test_mode_is_read_at_each_run_so_a_flip_to_edge_takes_effect_next_round(rest):
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - LEAD)
    assert len(rest.rpc_bodies()) == 1
    rest.modes = {"wake-check": "edge"}  # 되돌리기: SQL 한 줄
    nxt = BOUNDARY + timedelta(minutes=5)
    s.tick(nxt - LEAD)
    assert len(rest.rpc_bodies()) == 1  # 다음 회차부터 안 함


# ---------- 작업 실패는 루프를 안 멈춤 ----------


def test_shadow_db_failure_is_logged_and_next_round_still_runs(rest, caplog):
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rpc/find_missed_wake_alarms"):
            return httpx.Response(500, json={"message": "boom"})
        return rest.handler(request)

    s._db = Db(make_settings(), transport=httpx.MockTransport(broken))
    s.tick(BOUNDARY - LEAD)  # 예외가 새어 나오지 않음
    assert any("shadow 실패 job=wake-check" in m for m in lines(caplog))
    nxt = BOUNDARY + timedelta(minutes=5)
    s.tick(nxt - LEAD)  # 다음 회차도 돎 (표 읽기는 정상)
    assert sum("shadow 실패" in m for m in lines(caplog)) == 2


def test_table_row_without_mode_is_ignored_not_a_crash(rest, caplog):
    rest.routes_body = [
        {"name": "wake-check"},
        {"name": "alarm-roll", "mode": "shadow"},
    ]  # wake-check 줄에 mode가 없음
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - LEAD)
    assert rest.rpc_bodies() == []  # mode를 못 읽은 줄은 edge
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)  # KeyError가 새지 않음


def test_read_job_modes_keeps_only_rows_with_string_name_and_mode(rest):
    rest.routes_body = [
        {"name": "a"},  # mode 없음
        {"name": "b", "mode": None},
        {"name": "c", "mode": "shadow"},
        {"mode": "edge"},  # name 없음
        "junk",
    ]
    db = Db(make_settings(), transport=httpx.MockTransport(rest.handler))
    assert db.read_job_modes() == {"c": "shadow"}  # KeyError를 내지 않고 읽을 수 있는 줄만


def test_invalid_server_url_is_a_db_error_not_a_crash(caplog):
    """SUPABASE_URL이 이상해도 틱이 예외로 죽지 않고 DbError로 로그만 남김 (httpx.InvalidURL은 HTTPError가 아님)"""
    db = Db(make_settings(supabase_url="http://[::1"))
    with pytest.raises(DbError):  # 호출하는 쪽이 DbError만 알아도 되게 Db가 직접 바꿔 줌
        db.heartbeat(["wake-check"], BOUNDARY)
    s = Scheduler(db, JOBS, clock=lambda: BOUNDARY)
    s.tick(BOUNDARY - timedelta(minutes=1))  # 예외가 새어 나오지 않음
    assert any("heartbeat 쓰기 실패" in m for m in lines(caplog))


def test_unexpected_exception_while_reading_the_table_means_edge_and_the_round_is_not_lost(rest, caplog):
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    real = s._db.read_job_modes
    s._db.read_job_modes = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # DbError가 아닌 예외
    s.tick(BOUNDARY - LEAD)  # 새어 나오지 않음 — 그 회차는 edge로 봄
    assert rest.rpc_bodies() == []
    assert any("스위치 표를 못 읽어 edge로 봄" in m and "RuntimeError" in m for m in lines(caplog))
    s._db.read_job_modes = real  # 다음 회차는 정상
    nxt = BOUNDARY + timedelta(minutes=5)
    s.tick(nxt - LEAD)
    assert [b["p_now"] for b in rest.rpc_bodies()] == [nxt.isoformat()]


def test_unexpected_exception_in_heartbeat_does_not_skip_that_ticks_jobs(rest, caplog):
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest)
    s._db.heartbeat = lambda names, at: (_ for _ in ()).throw(RuntimeError("boom"))
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - LEAD)  # 같은 틱에서 작업이 돎
    assert len(rest.rpc_bodies()) == 1
    assert any("heartbeat" in m and "RuntimeError" in m for m in lines(caplog))


def test_unexpected_exception_in_job_is_contained_and_only_its_type_is_logged(rest, caplog):
    def buggy(db: Db, boundary: datetime) -> str:
        raise ValueError("user-secret-detail-123")

    jobs = (Job("wake-check", every_minutes(5), shadow=buggy),)
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest, jobs)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY)  # 새어 나오면 여기서 시험이 실패함
    assert any("shadow 예외 job=wake-check" in m and "ValueError" in m for m in lines(caplog))
    assert "user-secret-detail-123" not in caplog.text  # 예외 내용은 안 찍음


# ---------- server 손잡이 (3차에 채움 — 틀만 시험) ----------


def test_server_handler_success_records_last_success_at(rest, caplog):
    ran: list[datetime] = []

    def work(db: Db, boundary: datetime) -> str:
        ran.append(boundary)
        return "done=1"

    jobs = (Job("wake-check", every_minutes(5), server=work),)
    rest.modes = {"wake-check": "server"}
    later = BOUNDARY + timedelta(seconds=7)
    s = make_scheduler(rest, jobs, clock=lambda: later)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY)
    assert ran == [BOUNDARY]
    assert rest.success_writes() == [{"job_name": "wake-check", "last_success_at": later.isoformat()}]
    assert any(m == "server job=wake-check boundary=2026-11-15T03:05:00Z done=1" for m in lines(caplog))


def test_server_handler_gets_the_current_time_not_the_early_boundary(rest):
    """앞당김(정시 30초 전)은 shadow가 Edge보다 먼저 세려는 것 — server가 정시 값을 기준으로 받으면 아직 안 온 후보까지 처리하게 됨(3차 함정)"""
    got: list[datetime] = []
    shadow_got: list[datetime] = []
    jobs = (
        Job(
            "wake-check",
            every_minutes(5),
            lead_seconds=30,
            shadow=lambda db, at: shadow_got.append(at) or "s",
            server=lambda db, at: got.append(at) or "v",
        ),
    )
    now = BOUNDARY - timedelta(seconds=20)
    rest.modes = {"wake-check": "server"}
    s = make_scheduler(rest, jobs)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(now)
    assert got == [now] and shadow_got == []  # server: 지금 시각
    rest.modes = {"wake-check": "shadow"}
    s2 = make_scheduler(rest, jobs)
    s2.tick(BOUNDARY - timedelta(minutes=1))
    s2.tick(now)
    assert shadow_got == [BOUNDARY]  # shadow: Edge가 정시에 볼 후보와 맞추려고 정시


@pytest.mark.parametrize("error", [DbError("ConnectError"), RuntimeError("boom")], ids=["DB 오류", "예외"])
def test_server_handler_failure_records_no_success(rest, error):
    def work(db: Db, boundary: datetime) -> str:
        raise error

    jobs = (Job("wake-check", every_minutes(5), server=work),)
    rest.modes = {"wake-check": "server"}
    s = make_scheduler(rest, jobs)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY)
    assert rest.success_writes() == []  # 실패는 기록 안 함 → pg_cron이 Edge로 이어받음


def test_success_record_failure_does_not_crash(rest, caplog):
    jobs = (Job("wake-check", every_minutes(5), server=lambda db, b: "ok"),)
    rest.modes = {"wake-check": "server"}
    s = make_scheduler(rest, jobs)
    s.tick(BOUNDARY - timedelta(minutes=1))
    rest.heartbeat_status = 500  # 이제부터 server_heartbeat 쓰기가 실패
    s.tick(BOUNDARY)
    assert any("last_success_at 기록 실패" in m for m in lines(caplog))


# ---------- DB 접근 ----------


def test_db_requires_secret_key():
    with pytest.raises(ValueError):
        Db(make_settings(supabase_secret_key=None))


def test_every_request_kind_carries_the_secret_as_apikey_and_never_in_urls(rest):
    """하트비트 · 표 읽기 · DB 함수 · 성공 기록 네 종류를 모두 훑음"""
    rest.modes = {"wake-check": "shadow"}
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - LEAD)
    s._db.record_success("wake-check", BOUNDARY)
    kinds = {(r.method, r.url.path.split("/rest/v1/")[1]) for r in rest.requests}
    assert (
        kinds
        == {
            ("POST", "server_heartbeat"),
            ("GET", "server_routes"),
            ("POST", "rpc/find_missed_wake_alarms"),
        }
    )  # shadow가 부르는 것은 이것뿐 — 다른 표 · 함수를 건드리는 요청이 없음 (record_success도 같은 server_heartbeat)
    assert all(r.headers["apikey"] == SECRET for r in rest.requests)
    assert all(SECRET not in str(r.url) for r in rest.requests)
    assert all(str(r.url).startswith(SUPABASE_URL) for r in rest.requests)


def test_db_sends_legacy_jwt_key_as_bearer_too():
    legacy = "eyJhbGciOiJIUzI1NiJ9.payload.sig"
    rest = FakeRest()
    db = Db(make_settings(supabase_secret_key=legacy), transport=httpx.MockTransport(rest.handler))
    db.heartbeat(["wake-check"], BOUNDARY)
    assert rest.requests[0].headers["authorization"] == f"Bearer {legacy}"


# ---------- main ----------


def test_scheduler_clock_is_timezone_aware_utc():
    now = scheduler_module._utc_now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(
        0
    )  # naive면 alive_at의 isoformat에 오프셋이 없어 어긋남


def test_main_loop_survives_an_exception_in_tick_and_exits_zero_on_signal(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", SECRET)
    handlers: dict[int, object] = {}
    monkeypatch.setattr(
        scheduler_module.signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler)
    )

    class FastEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:  # 1초씩 안 기다림
            return self.is_set()

    monkeypatch.setattr(scheduler_module, "threading", types.SimpleNamespace(Event=FastEvent))
    calls: list[int] = []

    def tick(self, now):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")  # 첫 틱 예외 — 루프가 죽지 않아야 두 번째 틱이 옴
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(Scheduler, "tick", tick)
    assert scheduler_module.main() == 0
    assert len(calls) == 2


def test_main_without_secret_key_exits_one_and_never_starts_the_loop(monkeypatch, caplog):
    monkeypatch.setenv("SUPABASE_URL", "http://x.test")
    caplog.set_level(logging.INFO)

    def never(*args, **kwargs):
        # 키 없이 DB 연결을 만들면 안 됨. 예외로 해 두어야 이 검사가 깨졌을 때 시험이 무한 루프로 멈추지 않고 실패함
        raise AssertionError("키 없이 DB 연결을 만듦")

    monkeypatch.setattr(scheduler_module, "Db", never)
    assert scheduler_module.main() == 1
    assert any("SUPABASE_SECRET_KEY가 없어" in m for m in lines(caplog))

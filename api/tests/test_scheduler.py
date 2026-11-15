"""scheduler (완결점 1-10) — 모드별 동작 · 하트비트 · 정시 · 안전 쪽 기본값. Supabase는 가짜 네트워크(MockTransport)로 흉내냄"""

import json
import logging
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
    assert any("count=24" in m for m in lines(caplog))  # 03:05 ~ 05:00 정시 24개를 한 번에 건너뜀
    s.tick(
        BOUNDARY + timedelta(hours=2, seconds=1)
    )  # 바로 다음 틱: 아직 늦지 않은 05:05 회차(정시 30초 앞)부터 정상
    assert [b["p_now"] for b in rest.rpc_bodies()] == [(BOUNDARY + timedelta(hours=2)).isoformat()]


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


def test_db_sends_secret_as_apikey_header_and_not_in_urls(rest):
    s = make_scheduler(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    assert rest.requests and all(r.headers["apikey"] == SECRET for r in rest.requests)
    assert all(SECRET not in str(r.url) for r in rest.requests)
    assert all(str(r.url).startswith(SUPABASE_URL) for r in rest.requests)


def test_db_sends_legacy_jwt_key_as_bearer_too():
    legacy = "eyJhbGciOiJIUzI1NiJ9.payload.sig"
    rest = FakeRest()
    db = Db(make_settings(supabase_secret_key=legacy), transport=httpx.MockTransport(rest.handler))
    db.heartbeat(["wake-check"], BOUNDARY)
    assert rest.requests[0].headers["authorization"] == f"Bearer {legacy}"


# ---------- main ----------


def test_main_without_secret_key_exits_one_and_never_starts_the_loop(monkeypatch, caplog):
    monkeypatch.setenv("SUPABASE_URL", "http://x.test")
    caplog.set_level(logging.INFO)

    def never(*args, **kwargs):
        # 키 없이 DB 연결을 만들면 안 됨. 예외로 해 두어야 이 검사가 깨졌을 때 시험이 무한 루프로 멈추지 않고 실패함
        raise AssertionError("키 없이 DB 연결을 만듦")

    monkeypatch.setattr(scheduler_module, "Db", never)
    assert scheduler_module.main() == 1
    assert any("SUPABASE_SECRET_KEY가 없어" in m for m in lines(caplog))

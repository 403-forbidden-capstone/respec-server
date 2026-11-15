"""scheduler server 모드 (완결점 3-2 · 3-3 · 3-5) — 세 작업이 실제 본문을 돌리고 last_success_at을 쓰는지, 실패하면 안 쓰는지.
스위치 표 · 하트비트는 test_scheduler.py의 FakeRest, 본문은 jobs.py가 부르는 함수를 가짜로 바꿔 (db, now)를 받았는지 봄.
이벤트 루프 회귀: shared/supabase._http()가 닫힌 루프에 묶인 클라이언트를 쓰던 문제"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import jobs as jobs_module
from app.async_runtime import AsyncRuntime, runtime
from app.core.errors import AppError
from app.db import Db
from app.jobs import JOBS, Job
from app.jobs_roll import kakao_lookup
from app.scheduler import Scheduler
from app.shared import supabase as shared_supabase
from app.shared.supabase import Db as SharedDb

from .conftest import make_settings
from .test_scheduler import BOUNDARY, FakeRest

NOW = BOUNDARY + timedelta(seconds=7)


@pytest.fixture
def rest() -> FakeRest:
    return FakeRest()


@pytest.fixture
def started():
    """스케줄러 프로세스가 시작할 때처럼 runtime을 켬 (Supabase 호출은 MockTransport로 흡수). 쓰고 나면 닫음"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runtime.start(make_settings(), client)
    yield seen
    runtime.close()


@pytest.fixture(autouse=True)
def _log_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="respec.scheduler")


def lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "respec.scheduler"]


def scheduler_for(rest: FakeRest, name: str | None = None) -> Scheduler:
    db = Db(make_settings(), transport=httpx.MockTransport(rest.handler))
    jobs = JOBS if name is None else tuple(j for j in JOBS if j.name == name)
    return Scheduler(db, jobs, clock=lambda: NOW)


def drive(s: Scheduler, name: str) -> None:
    """정시(앞당김이 있는 wake-check는 정시 30초 전)까지 틱 — server는 그때의 지금 시각으로 돎"""
    job = next(j for j in JOBS if j.name == name)
    boundary = job.next_boundary(BOUNDARY - timedelta(seconds=1))
    s.tick(boundary - timedelta(minutes=1))
    s.tick(boundary - timedelta(seconds=job.lead_seconds))


# 작업 이름 → (jobs.py가 부르는 본문 이름, 가짜가 돌려줄 응답, 로그 요약)
CASES = {
    "wake-check": (
        "run_wake_check",
        {
            "checked": 2,
            "recorded": 2,
            "pushed": 1,
            "claimed": 0,
            "nudged": 0,
            "receipts": {"checked": 3, "tokensRemoved": 1, "expired": 0},
        },
        "checked=2 recorded=2 pushed=1 claimed=0 nudged=0 receipts_checked=3 tokens_removed=1 expired=0",
    ),
    "alarm-roll": (
        "run_alarm_roll",
        {
            "created": 4,
            "moved_alarm_ids": ["a", "b"],
            "pending": 1,
            "ring_packages": [],
            "canceled_alarm_ids": [],
            "kakao_calls": 3,
        },
        "created=4 moved_alarm_ids=2 pending=1 canceled_alarm_ids=0 kakao_calls=3",
    ),
    "suggestion-generate": (
        "run_suggestion_generate",
        {"candidates": 5, "created": 2, "skipped": 3},
        "candidates=5 created=2 skipped=3",
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_server_mode_runs_real_handler_and_records_success(name, rest, started, monkeypatch, caplog):
    attr, response, summary = CASES[name]
    got: list[tuple[object, datetime]] = []

    async def fake(db, now, **kwargs):
        got.append((db, now))
        return response

    monkeypatch.setattr(jobs_module, attr, fake)
    rest.modes = {name: "server"}
    drive(scheduler_for(rest, name), name)
    assert len(got) == 1
    db, at = got[0]
    assert isinstance(db, SharedDb)  # create_service_client() — 서비스 키 클라이언트
    assert db.headers["apikey"] == "sb_secret_test_value"
    assert at.tzinfo is not None and at.utcoffset() == timedelta(0)
    assert rest.success_writes() == [{"job_name": name, "last_success_at": NOW.isoformat()}]
    assert any(m.startswith(f"server job={name} ") and m.endswith(summary) for m in lines(caplog))
    assert rest.rpc_bodies() == []  # shadow 길(find_missed_wake_alarms 세기)은 안 탐
    assert started == []  # Edge 호출 길 없음 — 가짜 본문이 막아 둬서 PostgREST에도 요청이 안 감


@pytest.mark.parametrize("name", list(CASES))
def test_server_mode_handler_failure_does_not_record_success(name, rest, started, monkeypatch, caplog):
    attr = CASES[name][0]

    async def boom(db, now, **kwargs):
        raise AppError("INTERNAL", "내부 메시지에는 키 sb_secret_x 같은 게 섞일 수 있음")

    monkeypatch.setattr(jobs_module, attr, boom)
    rest.modes = {name: "server"}
    drive(scheduler_for(rest, name), name)
    assert rest.success_writes() == []  # 기록 안 함 → pg_cron이 Edge를 부름
    text = caplog.text
    assert f"server 실패 job={name}" in text and "code=INTERNAL" in text
    assert "sb_secret_x" not in text  # 오류 종류(코드)만


def test_server_mode_unexpected_exception_logs_kind_only(rest, started, monkeypatch, caplog):
    async def boom(db, now, **kwargs):
        raise KeyError("secret-value")

    monkeypatch.setattr(jobs_module, "run_suggestion_generate", boom)
    rest.modes = {"suggestion-generate": "server"}
    drive(scheduler_for(rest, "suggestion-generate"), "suggestion-generate")
    assert rest.success_writes() == []
    assert "KeyError" in caplog.text and "secret-value" not in caplog.text


def test_wake_check_server_gets_current_time_not_the_boundary(rest, started, monkeypatch):
    got: list[datetime] = []

    async def fake(db, now, **kwargs):
        got.append(now)
        return CASES["wake-check"][1]

    monkeypatch.setattr(jobs_module, "run_wake_check", fake)
    rest.modes = {"wake-check": "server"}
    s = scheduler_for(rest, "wake-check")
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - timedelta(seconds=30))  # 앞당겨 깨어남 — 처리 기준은 정시가 아니라 그 순간
    assert got == [BOUNDARY - timedelta(seconds=30)]


def test_server_mode_end_to_end_through_postgrest(rest):
    """가짜 본문 없이 suggestion-generate 전체 — 서비스 키로 PostgREST(prepare_suggestion_inputs)를 부르고 성공을 기록"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    runtime.start(make_settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        rest.modes = {"suggestion-generate": "server"}
        drive(scheduler_for(rest, "suggestion-generate"), "suggestion-generate")
    finally:
        runtime.close()
    assert [r.url.path for r in seen] == ["/rest/v1/rpc/prepare_suggestion_inputs"]
    assert seen[0].headers["apikey"] == "sb_secret_test_value"
    assert json.loads(seen[0].content)["p_now"].endswith("Z")
    assert rest.success_writes() == [{"job_name": "suggestion-generate", "last_success_at": NOW.isoformat()}]


def test_alarm_roll_lookup_follows_kakao_key(started):
    """Edge: 카카오 키가 있으면 길찾기 lookup, 없으면 undefined(= None)"""

    async def go():
        return kakao_lookup()

    assert runtime.run(go()) is None
    shared_supabase.configure(
        make_settings(kakao_rest_api_key="  kakao-key  "), shared_supabase._state["client"]
    )
    assert callable(runtime.run(go()))


def test_alarm_roll_server_passes_no_lookup_without_key(rest, started, monkeypatch):
    seen: dict[str, object] = {}

    async def fake(db, now, *, lookup=None, **kwargs):
        seen["lookup"] = lookup
        return CASES["alarm-roll"][1]

    monkeypatch.setattr(jobs_module, "run_alarm_roll", fake)
    rest.modes = {"alarm-roll": "server"}
    drive(scheduler_for(rest, "alarm-roll"), "alarm-roll")
    assert seen["lookup"] is None  # 시험 환경엔 KAKAO_REST_API_KEY 없음 → 길찾기 없이
    assert len(rest.success_writes()) == 1


@pytest.mark.parametrize("mode", ["edge", "shadow"])
def test_edge_and_shadow_never_call_server_handlers(mode, rest, started, monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("server 본문이 불림")

    for attr in ("run_wake_check", "run_alarm_roll", "run_suggestion_generate"):
        monkeypatch.setattr(jobs_module, attr, never)
    rest.modes = {name: mode for name in CASES}
    s = scheduler_for(rest)
    s.tick(BOUNDARY - timedelta(minutes=1))
    s.tick(BOUNDARY - timedelta(seconds=30))
    s.tick(BOUNDARY)
    daily = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    s.tick(daily - timedelta(seconds=1))
    s.tick(daily)
    assert rest.success_writes() == []
    assert (len(rest.rpc_bodies()) == 1) == (mode == "shadow")  # shadow는 wake-check 세기만


def test_all_three_jobs_have_server_handlers():
    assert all(isinstance(j, Job) and j.server is not None for j in JOBS)


# ---------- 이벤트 루프 ----------


def test_runtime_reuses_one_loop_and_client_across_jobs():
    """tick마다 asyncio.run()을 새로 열면 안 됨 — 같은 루프 · 같은 클라이언트로 여러 번"""
    rt = AsyncRuntime()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    rt.start(make_settings(), client)
    try:

        async def info():
            return asyncio.get_running_loop(), shared_supabase._http()

        (l1, c1), (l2, c2) = rt.run(info()), rt.run(info())
        assert l1 is l2 and c1 is c2 is client
    finally:
        rt.close()
    assert client.is_closed  # 종료할 때 닫음
    assert shared_supabase._state["client"] is None  # configure(None, None)으로 되돌림


def test_runtime_run_before_start_is_a_clear_error():
    async def noop():
        return 1

    with pytest.raises(RuntimeError, match="시작되지 않음"):
        AsyncRuntime().run(noop())


class _LoopBoundClient(httpx.AsyncClient):
    """옛 실패 재현 — 처음 만든 루프에 묶여, 그 루프가 닫힌 뒤에 쓰면 httpx(anyio)처럼 `Event loop is closed`"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bound_loop = asyncio.get_running_loop()

    async def get(self, *args, **kwargs):
        if self.bound_loop.is_closed():
            raise RuntimeError("Event loop is closed")
        return httpx.Response(200, json=[])


def test_lazy_http_client_survives_multiple_asyncio_runs(monkeypatch):
    """회귀: configure 없이 _http()를 asyncio.run() 여러 번에 걸쳐 쓰면 두 번째에서 `Event loop is closed`로 죽던 문제"""
    monkeypatch.setattr(shared_supabase.httpx, "AsyncClient", _LoopBoundClient)
    shared_supabase.configure(make_settings(), None)
    try:

        async def use():
            client = shared_supabase._http()
            await client.get("http://x.test")
            return client

        first = asyncio.run(use())  # 이 루프는 끝나며 닫힘
        second = asyncio.run(use())  # 옛 코드: 닫힌 루프에 묶인 first를 다시 써서 RuntimeError
        assert first is not second
    finally:
        shared_supabase.configure(None, None)


def test_configured_client_is_never_replaced():
    """configure로 넣은 클라이언트(서버 lifespan · 스케줄러 runtime)는 그대로 씀"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    shared_supabase.configure(make_settings(), client)
    try:

        async def use():
            return shared_supabase._http()

        assert asyncio.run(use()) is client and asyncio.run(use()) is client
    finally:
        shared_supabase.configure(None, None)


def test_http_outside_a_running_loop_is_a_clear_error():
    shared_supabase.configure(make_settings(), None)
    try:
        with pytest.raises(RuntimeError):
            shared_supabase._http()
    finally:
        shared_supabase.configure(None, None)

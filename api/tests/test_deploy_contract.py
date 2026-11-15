"""배포 길(AWS 세션, respec-server PR #2)이 api에 기대는 계약 — /health의 version · HEAD · /health/deep 캐시 · scheduler 시작점"""

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import scheduler
from app.core.config import Settings
from app.main import create_app
from app.routers import health as health_module
from tests.conftest import bearer, make_settings

API_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = API_DIR.parent


# ---------- /health version ----------


def test_health_returns_version_from_app_version_env(make_client):
    client = make_client(app_version="sha-0123abc")
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "version": "sha-0123abc"}


def test_health_version_defaults_to_dev_and_blank_means_dev(client, make_client):
    assert client.get("/health").json()["version"] == "dev"
    assert make_client(app_version="  ").get("/health").json()["version"] == "dev"


def test_app_version_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "sha-deadbee")
    assert Settings(supabase_url="http://x.test").app_version == "sha-deadbee"


def test_head_health_is_200_without_body(client):
    res = client.head("/health")
    assert res.status_code == 200
    assert res.content == b""
    assert res.headers["access-control-allow-origin"] == "*"


def test_health_still_needs_no_token_and_me_still_does(client):
    assert client.get("/health").status_code == 200
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers=bearer("x")).status_code == 401


# ---------- /health/deep 짧은 캐시 ----------


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(health_module, "_now", lambda: now[0])
    return now


def test_deep_result_is_cached_for_a_few_seconds(make_client, fake, clock):
    client = make_client(health_deep_cache_seconds=5)
    assert client.get("/health/deep").status_code == 200
    for _ in range(5):
        assert client.get("/health/deep").status_code == 200
    assert len(fake.rest_requests) == 1  # 6번 불러도 PostgREST는 1번
    clock[0] += 5.1
    assert client.get("/health/deep").status_code == 200
    assert len(fake.rest_requests) == 2  # 시간이 지나면 새로 확인


def test_deep_failure_is_cached_too_and_recovers_after_ttl(make_client, fake, clock):
    client = make_client(health_deep_cache_seconds=5)
    fake.rest_down = True
    assert client.get("/health/deep").status_code == 503
    fake.rest_down = False  # 복구됐지만 캐시 동안은 이전 결과
    assert client.get("/health/deep").status_code == 503
    assert len(fake.rest_requests) == 1  # 장애 중에도 DB를 두드리지 않음
    clock[0] += 5.1
    assert client.get("/health/deep").status_code == 200


def test_deep_cache_can_be_disabled(make_client, fake, clock):
    client = make_client(health_deep_cache_seconds=0)
    client.get("/health/deep")
    client.get("/health/deep")
    assert len(fake.rest_requests) == 2


def test_head_health_deep_matches_get_status_without_body(make_client, fake):
    """감시 서비스가 HEAD로 부르는 경우 — 405가 아니라 GET과 같은 상태 코드, 본문 없음"""
    client = make_client()
    res = client.head("/health/deep")
    assert res.status_code == 200
    assert res.content == b""
    fake.rest_down = True
    assert client.head("/health/deep").status_code == 503


def test_concurrent_deep_requests_share_one_check(fake):
    """동시에 12개가 와도 PostgREST 확인은 한 번 — 락을 빼면 12번 부름 (TestClient가 순차라서 일부러 스레드로)"""

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/server_heartbeat"):
            await asyncio.sleep(0.3)  # 겹치게 만드는 지연
        return fake.handler(request)

    app = create_app(make_settings(health_deep_cache_seconds=5), transport=httpx.MockTransport(slow_handler))
    with TestClient(app) as test_client, ThreadPoolExecutor(12) as pool:
        codes = list(pool.map(lambda _: test_client.get("/health/deep").status_code, range(12)))
    assert set(codes) == {200}
    assert len(fake.rest_requests) == 1


@pytest.mark.parametrize("bad", ["inf", "nan", "-1", "61"])
def test_deep_cache_seconds_rejects_out_of_range_values(bad):
    # inf면 캐시가 영영 안 풀려 감시가 장애를 못 보고, nan · 음수는 캐시가 조용히 꺼짐
    with pytest.raises(ValidationError):
        Settings(supabase_url="http://x.test", health_deep_cache_seconds=bad)


@pytest.mark.parametrize("ok", ["0", "5", "60"])
def test_deep_cache_seconds_accepts_zero_to_sixty(ok):
    assert Settings(
        supabase_url="http://x.test", health_deep_cache_seconds=ok
    ).health_deep_cache_seconds == float(ok)


# ---------- scheduler 시작점: `python -m app.scheduler` ----------


def _scheduler_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in {n.upper() for n in Settings.model_fields}}
    env.update({"PYTHONUTF8": "1", **extra})
    return env


class _Output:
    """자식 프로세스 출력을 줄 단위로 모음 — 고정 시간 sleep 대신 원하는 줄이 나올 때까지 기다리려고"""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.lines: list[str] = []
        self._thread = threading.Thread(target=self._read, args=(proc,), daemon=True)
        self._thread.start()

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            self.lines.append(raw.decode("utf-8", "replace"))

    def wait_for(self, needle: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(needle in line for line in self.lines):
                return True
            time.sleep(0.05)
        return False

    def text(self) -> str:
        self._thread.join(timeout=5)
        return "".join(self.lines)


def _start_scheduler() -> tuple[subprocess.Popen, _Output]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.scheduler"],
        cwd=API_DIR,
        env=_scheduler_env(
            SUPABASE_URL="http://127.0.0.1:9",  # 아무도 안 듣는 주소 — DB 호출은 바로 실패해도 스케줄러는 떠 있어야 함
            SUPABASE_SECRET_KEY="sb_secret_test_value",
            HTTP_TIMEOUT_SECONDS="0.5",
            APP_VERSION="sha-1234567",
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc, _Output(proc)


def test_scheduler_module_starts_and_stays_up():
    proc, output = _start_scheduler()
    try:
        assert output.wait_for("sha-1234567")  # 시작 로그가 나올 때까지 (느린 CI에서도 흔들리지 않게)
        time.sleep(0.5)
        assert proc.poll() is None  # 배포 직후 점검은 "running · 재시작 0"을 봄 — 바로 죽으면 안 됨
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGTERM은 POSIX 신호 — Windows의 terminate()는 강제 종료라 이 경로를 안 탐",
)
def test_scheduler_exits_zero_with_log_on_sigterm():
    """`docker stop`이 보내는 신호 — 핸들러가 없으면 -15로 죽고(deploy.sh의 정상 종료 경로가 깨짐) 로그도 없음"""
    proc, output = _start_scheduler()
    assert output.wait_for("scheduler 시작")
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 0
    assert "scheduler 종료" in output.text()


def test_scheduler_registers_handlers_and_exits_zero_when_signalled(monkeypatch):
    """모든 OS에서 도는 쪽: 두 신호(SIGTERM · SIGINT) 핸들러가 등록되고, 신호를 받으면 0으로 끝남"""
    monkeypatch.setenv("SUPABASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test_value")
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "0.5")
    handlers: dict[int, object] = {}
    monkeypatch.setattr(
        scheduler.signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler)
    )
    waits: list[float | None] = []

    class RecordingEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            waits.append(timeout)
            return super().wait(timeout)

    # scheduler 모듈이 보는 threading만 바꿔 치움 (threading.Event 전체를 바꾸면 Thread 내부의 wait까지 기록됨)
    monkeypatch.setattr(scheduler, "threading", types.SimpleNamespace(Event=RecordingEvent))
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(scheduler.main()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (signal.SIGTERM in handlers and signal.SIGINT in handlers) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert signal.SIGTERM in handlers and signal.SIGINT in handlers
    time.sleep(0.3)
    assert thread.is_alive()  # 신호 전에는 떠 있음
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    thread.join(timeout=5)
    assert not thread.is_alive() and result == [0]
    # 시간 제한 없는 wait()는 Windows에서 Ctrl+C로 안 깨어남 → 짧은 주기로 돌아야 함
    assert waits and all(w is not None and w <= 5 for w in waits)


def test_scheduler_without_settings_exits_nonzero_without_echoing_values():
    secret = "sb_secret_SCHEDULERSECRET1234567890"
    proc = subprocess.run(
        [sys.executable, "-m", "app.scheduler"],
        cwd=API_DIR,
        env=_scheduler_env(SUPABASE_SECRET_KEY=secret),  # SUPABASE_URL이 빠짐
        capture_output=True,
        timeout=20,
    )
    assert proc.returncode != 0
    assert secret.encode() not in proc.stdout + proc.stderr


def test_scheduler_without_secret_key_exits_one_with_reason():
    """키 없이 떠 있기만 하는 스케줄러는 하트비트도 스위치 읽기도 못 하면서 정상처럼 보임 → 바로 죽어서 배포 직후 점검에 드러나게"""
    proc = subprocess.run(
        [sys.executable, "-m", "app.scheduler"],
        cwd=API_DIR,
        env=_scheduler_env(SUPABASE_URL="http://x.test"),
        capture_output=True,
        timeout=20,
    )
    assert proc.returncode == 1
    assert "SUPABASE_SECRET_KEY가 없어".encode() in proc.stdout + proc.stderr


# ---------- 빈 문자열 env는 "없음" (compose가 값이 비면 빈 문자열을 넣어도 기본값이 쓰이게) ----------


def test_empty_env_values_are_treated_as_unset(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://x.test")
    for name in (
        "APP_VERSION",
        "HEALTH_DEEP_CACHE_SECONDS",
        "SUPABASE_SECRET_KEY",
        "LOG_LEVEL",
        "JWKS_CACHE_TTL_SECONDS",
    ):
        monkeypatch.setenv(name, "")
    settings = Settings()
    assert settings.app_version == "dev"
    assert settings.health_deep_cache_seconds == 5.0
    assert settings.supabase_secret_key is None
    assert settings.log_level == "INFO"
    assert settings.jwks_cache_ttl_seconds == 600


def test_empty_required_env_still_fails_without_echoing(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "")  # 빈 값 = 없음 → 필수 설정이 빠진 것
    with pytest.raises(ValidationError):
        Settings()


# ---------- 이미지 · CI 빌드 입력 ----------


def test_dockerignore_applies_to_subfolders():
    """Docker는 패턴을 build context 맨 위 경로 기준으로 봄 — `**/`가 없으면 app/ 아래 .pyc · .env가 이미지에 들어감"""
    lines = [
        line.strip()
        for line in (API_DIR / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for pattern in ("**/__pycache__", "**/*.pyc", "**/.env", "**/.env.*", "**/*.env"):
        assert pattern in lines


def test_image_and_ci_install_with_locked_not_frozen():
    """--frozen은 pyproject.toml과 uv.lock이 어긋나도 통과함 — --locked는 어긋나면 실패"""
    dockerfile = (API_DIR / "Dockerfile").read_text(encoding="utf-8")
    ci = (REPO_DIR / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "uv sync --locked" in dockerfile and "--frozen" not in dockerfile
    assert "uv sync --locked" in ci and "uv sync --frozen" not in ci

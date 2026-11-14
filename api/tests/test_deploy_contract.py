"""배포 길(AWS 세션, respec-server PR #2)이 api에 기대는 계약 — /health의 version · HEAD · /health/deep 캐시 · scheduler 시작점"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core.config import Settings
from app.routers import health as health_module
from tests.conftest import bearer

API_DIR = Path(__file__).resolve().parents[1]


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


# ---------- scheduler 시작점: `python -m app.scheduler` ----------


def _scheduler_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in {n.upper() for n in Settings.model_fields}}
    env.update({"PYTHONUTF8": "1", **extra})
    return env


def test_scheduler_module_starts_and_stays_up():
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.scheduler"],
        cwd=API_DIR,
        env=_scheduler_env(SUPABASE_URL="http://x.test", APP_VERSION="sha-1234567"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(1.5)
        assert proc.poll() is None  # 배포 직후 점검은 "running · 재시작 0"을 봄 — 바로 죽으면 안 됨
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=10)
    assert b"sha-1234567" in out


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
    from pydantic import ValidationError

    monkeypatch.setenv("SUPABASE_URL", "")  # 빈 값 = 없음 → 필수 설정이 빠진 것
    with pytest.raises(ValidationError):
        Settings()

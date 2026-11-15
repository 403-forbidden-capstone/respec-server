"""계약 시험 틀(dev/contract)이 서버 쪽에 넘기는 환경 변수 — 셸에 남은 운영 값이 서버로만 새지 않는지

Docker · 로컬 스택 없이 확인한다 (`clean_env.py`는 허용 목록 + api.env만 넘김).
"""

import importlib.util
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

LOCAL_ENV = "SUPABASE_URL=http://127.0.0.1:28321\nSUPABASE_SECRET_KEY=local-secret\nAPP_VERSION=local\n"
SHELL = {
    "PATH": "/usr/bin",
    "TEMP": "/tmp/x",
    "LC_ALL": "C.UTF-8",
    "KAKAO_REST_API_KEY": "prod-kakao",
    "PREPARE_ML_ENABLED": "1",
    "PREPARE_ML_ALLOWED_USER_IDS": "prod-ids",
    "SUPABASE_URL": "https://prod.example",
    "SUPABASE_SECRET_KEY": "prod-secret",
    "SUPABASE_PUBLISHABLE_KEY": "prod-anon",
    "EXPO_ACCESS_TOKEN": "prod-expo",
}


def _load():
    spec = importlib.util.spec_from_file_location("clean_env", REPO / "dev" / "contract" / "clean_env.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shell_prod_values_are_dropped_and_api_env_wins(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_publishable_from_stack", lambda: "local-anon")
    env_file = tmp_path / "api.env"
    env_file.write_text(LOCAL_ENV, encoding="utf-8")

    env = m.server_env(SHELL, env_file)

    assert env["PATH"] == "/usr/bin" and env["TEMP"] == "/tmp/x" and env["LC_ALL"] == "C.UTF-8"
    for leaked in (
        "KAKAO_REST_API_KEY",
        "PREPARE_ML_ENABLED",
        "PREPARE_ML_ALLOWED_USER_IDS",
        "EXPO_ACCESS_TOKEN",
    ):
        assert leaked not in env
    assert env["SUPABASE_URL"] == "http://127.0.0.1:28321"
    assert env["SUPABASE_SECRET_KEY"] == "local-secret"
    assert env["SUPABASE_PUBLISHABLE_KEY"] == "local-anon"  # 셸의 운영 값이 아니라 로컬 스택 값
    assert "prod" not in " ".join(env.values())


def test_publishable_key_in_api_env_is_used_without_asking_the_stack(tmp_path, monkeypatch):
    m = _load()

    def boom():
        raise AssertionError("api.env에 키가 있으면 스택을 부르지 않음")

    monkeypatch.setattr(m, "_publishable_from_stack", boom)
    env_file = tmp_path / "api.env"
    env_file.write_text(LOCAL_ENV + "SUPABASE_PUBLISHABLE_KEY=from-file\n", encoding="utf-8")

    assert m.server_env(SHELL, env_file)["SUPABASE_PUBLISHABLE_KEY"] == "from-file"


def test_missing_api_env_still_drops_shell_values(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_publishable_from_stack", lambda: None)

    env = m.server_env(SHELL, tmp_path / "없음.env")

    assert set(env) == {"PATH", "TEMP", "LC_ALL"}


def test_scrub_process_env_replaces_the_process_environment(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_publishable_from_stack", lambda: "local-anon")
    monkeypatch.setattr(m, "API_ENV", tmp_path / "api.env")
    (tmp_path / "api.env").write_text(LOCAL_ENV, encoding="utf-8")
    fake_environ = dict(SHELL)
    monkeypatch.setattr(os, "environ", fake_environ)  # 진짜 환경은 안 건드림

    m.scrub_process_env()

    assert "KAKAO_REST_API_KEY" not in fake_environ and "PREPARE_ML_ENABLED" not in fake_environ
    assert fake_environ["SUPABASE_PUBLISHABLE_KEY"] == "local-anon"
    assert fake_environ["SUPABASE_URL"] == "http://127.0.0.1:28321"

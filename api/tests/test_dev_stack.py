"""로컬 개발 환경(dev/ · compose.local.yml · docs/local-dev.md)의 약속을 지키는지 — Docker · npx 없이 가짜 CLI로 확인

파일끼리 포트가 어긋나지 않는지, prepare가 비밀값 파일을 안 복사하는지, 출력에 키 · 토큰이 안 새는지를 본다.
"""

import base64
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
import urllib.request
from argparse import Namespace
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_local_stack():
    spec = importlib.util.spec_from_file_location("local_stack", REPO / "dev" / "local_stack.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ls():
    return _load_local_stack()


def _config() -> dict:
    return tomllib.loads((REPO / "dev" / "stack-config.toml").read_text(encoding="utf-8"))


def _read(*parts: str) -> str:
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


# ---------- 파일끼리의 약속 ----------


def test_script_port_matches_stack_config(ls):
    assert _config()["api"]["port"] == ls.API_PORT


def test_stack_ports_are_unique_and_avoid_reserved_ranges_and_other_stacks(ls):
    config = _config()
    ports = [
        config["api"]["port"],
        config["db"]["port"],
        config["db"]["shadow_port"],
        config["studio"]["port"],
        config["local_smtp"]["port"],
        config["analytics"]["port"],
        config["edge_runtime"]["inspector_port"],
        ls.SERVER_PORT,
    ]
    assert len(set(ports)) == len(ports)
    # Windows가 예약하는 54236~54435 · 54564~54763 구간과 본 레포 로컬 스택(5432x)을 피함
    assert all(port >= 20000 and not 54000 <= port < 55000 for port in ports)
    assert config["project_id"] != "respec-alarm"  # 본 레포 스택과 이름이 달라야 동시에 뜸


def test_stack_uses_es256_signing_keys_file_and_script_asks_cli_for_es256(ls):
    assert _config()["auth"]["signing_keys_path"] == "./signing_keys.json"
    # 스크립트가 실제로 ES256으로 키를 만드는지는 아래 prepare 시험에서 CLI 인자로 확인


def test_stack_config_has_no_hand_copied_functions_list():
    # 손으로 베낀 목록은 본 레포가 바뀔 때마다 어긋남(예: voice-commit · alarm-briefing-device는 false) → prepare가 붙임
    assert not re.search(r"^\[functions\.", _read("dev", "stack-config.toml"), re.MULTILINE)


def test_compose_local_points_at_the_isolated_stack_and_same_ports(ls):
    text = _read("compose.local.yml")
    # 컨테이너 안에서 호스트의 로컬 Supabase로 가는 주소 두 곳(URL · JWKS)이 모두 격리 스택 포트여야 함
    assert text.count(f"http://host.docker.internal:{ls.API_PORT}") == 2
    assert "SUPABASE_JWKS_URL" in text and "/auth/v1/.well-known/jwks.json" in text
    assert re.search(rf'"127\.0\.0\.1:{ls.SERVER_PORT}:8000"', text)  # 서버 호스트 포트도 한 곳에서 묶임
    assert "dev/.stack/api.env" in text
    assert "app.scheduler" in text  # scheduler도 같은 이미지에서 시작 명령만 바꿔 뜸


def test_generated_stack_folder_is_never_committed():
    ignored = _read(".gitignore").splitlines()
    assert "dev/.stack/" in ignored  # 서명 키 · 로컬 키가 들어 있음


def test_local_dev_doc_matches_script_ports_and_cli_version(ls):
    doc = _read("docs", "local-dev.md")
    config = _config()
    for port in (
        ls.API_PORT,
        ls.SERVER_PORT,
        config["db"]["port"],
        config["db"]["shadow_port"],
        config["edge_runtime"]["inspector_port"],
    ):
        assert str(port) in doc
    assert f"--port {ls.SERVER_PORT}" in doc and f"--api-url http://127.0.0.1:{ls.SERVER_PORT}" in doc
    assert f"npx {ls.SUPABASE_CLI}" in doc  # 문서의 명령 버전 = 스크립트가 쓰는 버전


# ---------- prepare ----------


def _fake_cli_that_writes_key(calls: list[tuple[str, ...]], stack: Path):
    def fake(*args: str, capture: bool = False, stdin: str | None = None):
        calls.append(args)
        if args[:2] == ("gen", "signing-key"):
            (stack / "supabase" / "signing_keys.json").write_text(
                json.dumps([{"kty": "EC", "kid": "k1", "alg": "ES256", "d": "PRIVATE"}]), encoding="utf-8"
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return fake


def _make_main_repo(root: Path) -> Path:
    sb = root / "supabase"
    (sb / "migrations").mkdir(parents=True)
    (sb / "migrations" / "001_init.sql").write_text("select 1;", encoding="utf-8")
    (sb / "functions" / "wake-check").mkdir(parents=True)
    (sb / "functions" / "wake-check" / "index.ts").write_text("// fn", encoding="utf-8")
    # 비밀값 파일: 깊이와 이름이 달라도 복사되면 안 됨
    for secret in (".env", ".env.local", ".env.example"):
        (sb / "functions" / secret).write_text("KAKAO_REST_API_KEY=real-secret", encoding="utf-8")
    (sb / "functions" / "wake-check" / ".env").write_text("A=real-secret", encoding="utf-8")
    (sb / "config.toml").write_text(
        'project_id = "respec-alarm"\n[api]\nport = 54321\n'
        "[functions.voice-commit]\nverify_jwt = false\n"
        "[functions.alarm-commit]\nverify_jwt = true\n"
        "[analytics]\nenabled = true\n",
        encoding="utf-8",
    )
    return root


def test_prepare_copies_without_secrets_asks_for_es256_and_appends_main_functions(
    ls, tmp_path, monkeypatch, capsys
):
    stack = tmp_path / "stack"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(ls, "STACK", stack)
    monkeypatch.setattr(ls, "_cli", _fake_cli_that_writes_key(calls, stack))
    main_repo = _make_main_repo(tmp_path / "main")

    assert ls.cmd_prepare(Namespace(main_repo=str(main_repo))) == 0

    copied = stack / "supabase"
    assert (copied / "migrations" / "001_init.sql").is_file()
    assert (copied / "functions" / "wake-check" / "index.ts").is_file()
    assert not list(copied.rglob(".env*"))  # 어느 깊이에도 비밀값 파일이 없음
    # ES256 키를 CLI에 요청함
    assert any(call[:2] == ("gen", "signing-key") and "ES256" in call for call in calls)
    assert "kid" in (copied / "signing_keys.json").read_text(encoding="utf-8")
    # 설정: 격리 스택 설정 + 본 레포의 함수별 verify_jwt(값이 다른 voice-commit 포함), 본 레포의 다른 항목은 안 섞임
    config = tomllib.loads((copied / "config.toml").read_text(encoding="utf-8"))
    assert config["project_id"] == "respec-server-dev"
    assert config["api"]["port"] == ls.API_PORT
    assert config["functions"]["voice-commit"]["verify_jwt"] is False
    assert config["functions"]["alarm-commit"]["verify_jwt"] is True
    assert config["analytics"]["enabled"] is False
    assert "PRIVATE" not in capsys.readouterr().out  # 키 내용은 화면에 안 찍음


def test_prepare_keeps_existing_key_but_recreates_an_empty_one(ls, tmp_path, monkeypatch):
    stack = tmp_path / "stack"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(ls, "STACK", stack)
    monkeypatch.setattr(ls, "_cli", _fake_cli_that_writes_key(calls, stack))
    main_repo = _make_main_repo(tmp_path / "main")

    ls.cmd_prepare(Namespace(main_repo=str(main_repo)))
    first = (stack / "supabase" / "signing_keys.json").read_text(encoding="utf-8")
    ls.cmd_prepare(Namespace(main_repo=str(main_repo)))  # 다시 실행해도 키를 안 바꿈
    assert [c for c in calls if c[:2] == ("gen", "signing-key")] == [c for c in calls[:1]]
    assert (stack / "supabase" / "signing_keys.json").read_text(encoding="utf-8") == first

    (stack / "supabase" / "signing_keys.json").write_text(
        "[]\n", encoding="utf-8"
    )  # 중단돼 빈 목록만 남은 경우
    ls.cmd_prepare(Namespace(main_repo=str(main_repo)))
    assert "kid" in (stack / "supabase" / "signing_keys.json").read_text(encoding="utf-8")


def test_prepare_stops_with_a_clear_message_when_main_repo_is_wrong(ls, tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "STACK", tmp_path / "stack")
    with pytest.raises(SystemExit) as exc:
        ls.cmd_prepare(Namespace(main_repo=str(tmp_path)))
    assert "본 레포" in str(exc.value)


# ---------- env · check: 비밀값은 파일로만, 화면에는 안 ----------


def test_env_writes_secret_to_file_only_never_to_screen(ls, tmp_path, monkeypatch, capsys):
    secret = "sb_secret_FAKEFAKEFAKE1234567890"
    monkeypatch.setattr(ls, "STACK", tmp_path / "stack")
    monkeypatch.setattr(ls, "_status", lambda: {"SECRET_KEY": secret, "API_URL": "http://127.0.0.1:1"})

    assert ls.cmd_env(Namespace()) == 0

    shown = capsys.readouterr()
    assert secret not in shown.out + shown.err
    written = (tmp_path / "stack" / "api.env").read_text(encoding="utf-8")
    assert f"SUPABASE_SECRET_KEY={secret}" in written
    assert f"SUPABASE_JWT_ISSUER=http://127.0.0.1:{ls.API_PORT}/auth/v1" in written
    if sys.platform != "win32":
        assert (tmp_path / "stack" / "api.env").stat().st_mode & 0o077 == 0  # 소유자만 읽음


def test_status_parses_env_style_lines(ls, monkeypatch):
    out = 'API_URL="http://127.0.0.1:28321"\nSECRET_KEY="sb_secret_a"\nnot a pair\n'
    monkeypatch.setattr(ls, "_cli", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""))
    assert ls._status() == {"API_URL": "http://127.0.0.1:28321", "SECRET_KEY": "sb_secret_a"}


def _fake_jwt(payload: dict) -> str:
    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'ES256', 'kid': 'abc'})}.{enc(payload)}.SIGNATUREVALUE"


def test_jwt_summary_shows_only_alg_kid_presence_and_issuer(ls):
    token = _fake_jwt({"iss": "http://127.0.0.1:28321/auth/v1", "sub": "USER-ID-1", "email": "a@b.test"})
    summary = ls._jwt_summary(token)
    assert summary == "alg=ES256 kid=있음 iss=http://127.0.0.1:28321/auth/v1"
    assert "USER-ID-1" not in summary and "a@b.test" not in summary and "SIGNATUREVALUE" not in summary


def test_check_never_prints_token_or_keys_and_exits_zero_when_all_pass(ls, monkeypatch, capsys):
    token = _fake_jwt({"iss": "http://127.0.0.1:28321/auth/v1"})
    anon = "sb_publishable_FAKEANONKEY"
    monkeypatch.setattr(ls, "_status", lambda: {"PUBLISHABLE_KEY": anon})
    monkeypatch.setattr(ls, "_local_user_token", lambda key: (token, "user-1"))

    def fake_request(method, url, *, headers=None, body=None):
        if url.endswith("/me") and method == "GET":
            return (
                (200, '{"id":"user-1"}')
                if headers and "Authorization" in headers and token in headers["Authorization"]
                else (401, "{}")
            )
        if url.endswith("/me") and method == "OPTIONS":
            return 204, ""
        return 200, '{"status":"ok"}'

    monkeypatch.setattr(ls, "_request", fake_request)
    assert ls.cmd_check(Namespace(api_url=f"http://127.0.0.1:{ls.SERVER_PORT}")) == 0
    shown = capsys.readouterr()
    assert token not in shown.out + shown.err and anon not in shown.out + shown.err


def test_check_exits_nonzero_when_the_server_rejects_a_valid_local_token(ls, monkeypatch):
    monkeypatch.setattr(ls, "_status", lambda: {"PUBLISHABLE_KEY": "k"})
    monkeypatch.setattr(ls, "_local_user_token", lambda key: (_fake_jwt({"iss": "x"}), "user-1"))
    monkeypatch.setattr(ls, "_request", lambda *a, **k: (401, "{}"))
    assert ls.cmd_check(Namespace(api_url="http://127.0.0.1:28000")) == 1


@pytest.mark.parametrize(
    "url", ["https://api.respecalarm.com", "http://example.com:28000", "http://127.0.0.1.evil.test:28000"]
)
def test_check_refuses_non_local_api_urls_before_touching_anything(ls, monkeypatch, url):
    # 로컬 시험 토큰을 운영 주소 등으로 보내지 않음 — 거절은 로컬 스택 조회보다 먼저
    def boom():
        raise AssertionError("로컬 스택을 조회하면 안 됨")

    monkeypatch.setattr(ls, "_status", boom)
    with pytest.raises(SystemExit):
        ls.cmd_check(Namespace(api_url=url))


def test_requests_ignore_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    # 비교 대상: 기본 opener는 환경변수 프록시를 읽어 ProxyHandler(프록시 있음)를 가짐
    default = [
        h.proxies
        for h in urllib.request.build_opener().handlers
        if isinstance(h, urllib.request.ProxyHandler)
    ]
    assert default and default[0]
    # 환경변수를 넣은 "뒤에" 모듈을 불러 opener가 만들어지게 함 - 우리 opener는 프록시 핸들러가 없어야
    # (있으면 로컬 요청의 시험 계정 비밀번호 · 토큰이 프록시로 감)
    module = _load_local_stack()
    assert not [h for h in module._OPENER.handlers if isinstance(h, urllib.request.ProxyHandler)]

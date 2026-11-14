"""서버 개발용 로컬 스택 도우미 — 표준 라이브러리만 씀 (자세한 순서는 docs/local-dev.md)

  python dev/local_stack.py prepare --main-repo ../respec-alarm-capstone   # 1) 격리 Supabase 작업 폴더 만들기
  npx supabase@2.119.0 --workdir dev/.stack start                          # 2) 띄우기 (Docker 필요)
  python dev/local_stack.py env                                            # 3) 서버용 env 파일(dev/.stack/api.env) 만들기
  python dev/local_stack.py check --api-url http://127.0.0.1:28000         # 4) 로컬 토큰으로 서버 /me 확인

비밀값 규칙: 이 스크립트는 토큰 · 키 값을 화면에 찍지 않음(상태 코드 · id 일치 여부 · 토큰의 alg/iss만).
만든 파일은 전부 dev/.stack/ 아래이며 깃에 안 들어감. 키가 든 파일은 소유자만 읽게 만듦(Linux · macOS).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STACK = HERE / ".stack"
SUPABASE_CLI = "supabase@2.119.0"  # 본 레포 README와 같은 버전
API_PORT = 28321  # stack-config.toml의 [api] port와 같아야 함 (로컬 Supabase API 게이트웨이)
SERVER_PORT = 28000  # 서버(api)를 띄우는 호스트 포트. compose.local.yml · docs/local-dev.md와 같아야 함
TEST_EMAIL = "server-dev@example.test"
TEST_PASSWORD = "local-dev-only-password"  # 로컬 스택 안에서만 쓰는 시험 계정
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# 프록시 환경변수(HTTP_PROXY 등)가 있어도 로컬 요청은 프록시를 타지 않게 — 시험 계정 비밀번호 · 토큰이 프록시로 가지 않도록
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _cli(*args: str, capture: bool = False, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        sys.exit("npx를 못 찾음 - Node.js가 필요함 (docs/local-dev.md의 준비물 참고)")
    return subprocess.run(
        [npx, "--yes", SUPABASE_CLI, "--workdir", str(STACK), *args],
        text=True,
        encoding="utf-8",
        errors="replace",  # 지역화된 오류 글(cp949 등)이 섞여도 디코딩에서 죽지 않게
        input=stdin,
        capture_output=capture,
        check=False,
    )


def _write_private(path: Path, text: str) -> None:
    """키가 든 파일 - 소유자만 읽게(Linux · macOS). 이미 있던 파일도 권한을 다시 맞춤"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


# ---------- prepare ----------


def _functions_sections(config_text: str) -> str:
    """본 레포 config.toml에서 [functions.*] 항목(함수별 verify_jwt 등)만 뽑음.
    정적 목록으로 두면 본 레포가 바뀔 때마다 어긋나므로(함수가 늘 때 값이 달라짐) prepare가 매번 읽어 붙임"""
    kept: list[str] = []
    keep = False
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            keep = stripped.startswith("[functions.")
        if keep:
            kept.append(line)
    return ("\n".join(kept).strip() + "\n") if kept else ""


def _has_key(path: Path) -> bool:
    return path.is_file() and '"kid"' in path.read_text(encoding="utf-8", errors="replace")


def _git_summary(repo: Path) -> str:
    """복사해 온 본 레포의 브랜치 · 커밋 (증거용). git이 없거나 저장소가 아니면 빈 문자열"""

    def run(*args: str) -> str:
        try:
            done = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""

    head = run("rev-parse", "--abbrev-ref", "HEAD")
    commit = run("rev-parse", "--short", "HEAD")
    if not (head and commit):
        return ""
    dirty = " (supabase/에 커밋 안 된 변경 있음)" if run("status", "--porcelain", "--", "supabase") else ""
    return f"{head} @ {commit}{dirty}"


def cmd_prepare(args: argparse.Namespace) -> int:
    main_repo = Path(args.main_repo).resolve()
    source = main_repo / "supabase"
    for sub in ("migrations", "functions"):
        if not (source / sub).is_dir():
            sys.exit(f"{source / sub} 가 없음 - --main-repo가 본 레포(respec-alarm-capstone) 폴더인지 확인")
    target = STACK / "supabase"
    target.mkdir(parents=True, exist_ok=True)
    for sub in ("migrations", "functions"):
        try:
            if (target / sub).exists():
                shutil.rmtree(target / sub)
            # 본 레포의 functions/.env(비밀값)는 복사하지 않음 - 함수 비밀값은 serve 때 --env-file로 따로 넘김
            shutil.copytree(source / sub, target / sub, ignore=shutil.ignore_patterns(".env", ".env.*"))
        except (OSError, shutil.Error) as err:
            sys.exit(
                f"{target / sub} 를 준비하지 못함 ({err}) - 그 폴더를 쓰는 프로그램(편집기 · 탐색기)을 닫고 다시 실행"
            )

    config = (HERE / "stack-config.toml").read_text(encoding="utf-8")
    main_config = source / "config.toml"
    sections = _functions_sections(main_config.read_text(encoding="utf-8")) if main_config.is_file() else ""
    if not sections:
        print(
            "주의: 본 레포 supabase/config.toml에서 [functions.*]를 못 찾음 - 함수별 verify_jwt가 기본(true)으로 동작함"
        )
    merged = (
        config.rstrip()
        + "\n\n# --- 아래는 본 레포 supabase/config.toml의 [functions.*] (prepare가 매번 붙임) ---\n"
        + sections
    )
    (target / "config.toml").write_text(merged, encoding="utf-8", newline="\n")

    keys = target / "signing_keys.json"
    if not _has_key(keys):
        _write_private(
            keys, "[]\n"
        )  # CLI가 기존 파일을 먼저 읽으므로 빈 목록으로 시작 (중단돼 빈 목록만 남은 경우도 다시 만듦)
        done = _cli("gen", "signing-key", "--algorithm", "ES256", "--yes", capture=True)
        if done.returncode != 0 or not _has_key(keys):
            keys.unlink(missing_ok=True)
            # 개인 키가 stdout에 나올 수 있는 경로는 막아 두고 stderr만 보여 줌
            sys.exit("ES256 서명 키 만들기 실패:\n" + (done.stderr or "")[:2000])
        print("ES256 서명 키 만듦 (dev/.stack/supabase/signing_keys.json - 깃에 안 들어감)")
    n_migrations = len(list((target / "migrations").glob("*.sql")))
    n_functions = merged.count("[functions.")
    where = _git_summary(main_repo)
    print(
        f"준비 완료: 마이그레이션 {n_migrations}개 · 함수 설정 {n_functions}개 · 본 레포 {where or main_repo} -> {STACK}"
    )
    print("다음: npx supabase@2.119.0 --workdir dev/.stack start")
    return 0


# ---------- env / check 공통: 로컬 스택 상태 ----------


def _status() -> dict[str, str]:
    done = _cli("status", "-o", "env", capture=True)
    if done.returncode != 0:
        sys.exit(
            "로컬 스택이 안 떠 있음 - 먼저 `npx supabase@2.119.0 --workdir dev/.stack start`\n"
            + (done.stderr or "")[:500]
        )
    values: dict[str, str] = {}
    for line in (done.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.isidentifier():
            values[key] = value.strip().strip('"')
    return values


def cmd_env(_: argparse.Namespace) -> int:
    status = _status()
    secret = status.get("SECRET_KEY") or status.get("SERVICE_ROLE_KEY")
    if not secret:
        sys.exit("status에서 서버 전용 키를 못 찾음")
    url = f"http://127.0.0.1:{API_PORT}"
    lines = [
        "# dev/local_stack.py env 가 만든 파일 - 로컬 스택 전용 · 깃에 안 들어감",
        f"SUPABASE_URL={url}",
        f"SUPABASE_JWT_ISSUER={url}/auth/v1",
        f"SUPABASE_SECRET_KEY={secret}",
        "APP_VERSION=local",
        "HEALTH_REQUIRE_SCHEDULER=false",
    ]
    out = STACK / "api.env"
    STACK.mkdir(parents=True, exist_ok=True)
    _write_private(out, "\n".join(lines) + "\n")
    print(f"만듦: {out} (값은 안 찍음)")
    return 0


# ---------- check ----------


def _request(method: str, url: str, *, headers: dict[str, str] | None = None, body: object | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=10) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")
    except OSError as err:
        return 0, str(err)


def _local_user_token(anon_key: str) -> tuple[str, str]:
    """로컬 GoTrue에서 시험 계정으로 로그인(없으면 만듦)해 (access_token, user_id)를 돌려줌"""
    base = f"http://127.0.0.1:{API_PORT}/auth/v1"
    headers = {"apikey": anon_key}
    body = {"email": TEST_EMAIL, "password": TEST_PASSWORD}
    status, text = _request("POST", f"{base}/signup", headers=headers, body=body)
    if status not in (200, 201) or "access_token" not in text:
        status, text = _request("POST", f"{base}/token?grant_type=password", headers=headers, body=body)
    if status != 200:
        sys.exit(f"로컬 로그인 실패(status {status}) - 스택이 떠 있는지 확인")
    payload = json.loads(text)
    return payload["access_token"], payload["user"]["id"]


def _jwt_summary(token: str) -> str:
    """헤더 alg · kid 유무와 payload의 iss만 보여 줌(서명 · 다른 값은 안 찍음)"""

    def part(segment: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))

    head, payload, _ = token.split(".")
    return f"alg={part(head).get('alg')} kid={'있음' if part(head).get('kid') else '없음'} iss={part(payload).get('iss')}"


def _require_local_url(url: str) -> str:
    """check는 로컬 시험 토큰을 보냄 - 로컬 주소가 아니면 거절 (실수로 운영 주소를 주는 경우 방지)"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS:
        sys.exit(
            f"--api-url은 로컬 주소(http://127.0.0.1:포트 · http://localhost:포트)만 받음: {parsed.scheme}://{parsed.hostname}"
        )
    return url.rstrip("/")


def cmd_check(args: argparse.Namespace) -> int:
    api = _require_local_url(args.api_url)
    status = _status()
    anon = status.get("PUBLISHABLE_KEY") or status.get("ANON_KEY")
    if not anon:
        sys.exit("status에서 앱용 키를 못 찾음")
    token, user_id = _local_user_token(anon)
    print(f"로컬 토큰: {_jwt_summary(token)}")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")

    results: list[tuple[str, bool, str]] = []

    def expect(name: str, got: int, want: int, extra: str = "") -> None:
        results.append((name, got == want, f"status {got} (기대 {want}){extra}"))

    code, _ = _request("GET", f"{api}/health")
    expect("GET /health", code, 200)
    code, text = _request("GET", f"{api}/health/deep")
    expect("GET /health/deep (서버 키 -> 로컬 DB server_heartbeat 읽기)", code, 200, f" {text.strip()[:120]}")
    code, _ = _request("HEAD", f"{api}/health/deep")
    expect("HEAD /health/deep (감시가 HEAD로 부르는 경우)", code, 200)
    code, text = _request("GET", f"{api}/me", headers={"Authorization": f"Bearer {token}"})
    matched = code == 200 and json.loads(text).get("id") == user_id
    results.append(("GET /me 로컬 토큰 -> 200 · id 일치", matched, f"status {code} · id 일치={matched}"))
    code, _ = _request("GET", f"{api}/me")
    expect("GET /me 헤더 없음 -> 401", code, 401)
    code, _ = _request("GET", f"{api}/me", headers={"Authorization": f"Bearer {tampered}"})
    expect("GET /me 서명 훼손 -> 401", code, 401)
    code, _ = _request("OPTIONS", f"{api}/me")
    expect("OPTIONS /me 토큰 없이 -> 204", code, 204)

    for name, ok, detail in results:
        print(("통과  " if ok else "실패  ") + f"{name}: {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


def main() -> int:
    # 한국어 Windows(cp949)에서 stdout을 파이프로 보내도 한글 · 기호 때문에 죽지 않게
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="격리 Supabase 작업 폴더(dev/.stack) 만들기")
    prepare.add_argument("--main-repo", required=True, help="본 레포(respec-alarm-capstone) 폴더 경로")
    prepare.set_defaults(func=cmd_prepare)
    sub.add_parser("env", help="서버용 env 파일 dev/.stack/api.env 만들기").set_defaults(func=cmd_env)
    check = sub.add_parser("check", help="로컬 토큰으로 서버 /me 등을 확인 (로컬 주소만)")
    check.add_argument("--api-url", default=f"http://127.0.0.1:{SERVER_PORT}")
    check.set_defaults(func=cmd_check)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

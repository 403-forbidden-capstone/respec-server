"""서버 개발용 로컬 스택 도우미 — 표준 라이브러리만 씀 (자세한 순서는 docs/local-dev.md)

  python dev/local_stack.py prepare --main-repo ../respec-alarm-capstone   # 1) 격리 Supabase 작업 폴더 만들기
  npx supabase@2.119.0 --workdir dev/.stack start                          # 2) 띄우기 (Docker 필요)
  python dev/local_stack.py env                                            # 3) 서버용 env 파일(dev/.stack/api.env) 만들기
  python dev/local_stack.py check --api-url http://127.0.0.1:28000         # 4) 로컬 토큰으로 서버 /me 확인

비밀값 규칙: 이 스크립트는 토큰 · 키 값을 화면에 찍지 않음(상태 코드 · id 일치 여부만).
만든 파일은 전부 dev/.stack/ 아래이며 깃에 안 들어감.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STACK = HERE / ".stack"
SUPABASE_CLI = "supabase@2.119.0"  # 본 레포 README와 같은 버전
API_PORT = 28321  # stack-config.toml의 [api] port와 같아야 함
TEST_EMAIL = "server-dev@example.test"
TEST_PASSWORD = "local-dev-only-password"  # 로컬 스택 안에서만 쓰는 시험 계정


def _cli(*args: str, capture: bool = False, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        sys.exit("npx를 못 찾음 — Node.js가 필요함 (docs/local-dev.md의 준비물 참고)")
    return subprocess.run(
        [npx, "--yes", SUPABASE_CLI, "--workdir", str(STACK), *args],
        text=True,
        encoding="utf-8",
        input=stdin,
        capture_output=capture,
        check=False,
    )


# ---------- prepare ----------


def cmd_prepare(args: argparse.Namespace) -> int:
    source = Path(args.main_repo).resolve() / "supabase"
    for sub in ("migrations", "functions"):
        if not (source / sub).is_dir():
            sys.exit(f"{source / sub} 가 없음 — --main-repo가 본 레포(respec-alarm-capstone) 폴더인지 확인")
    target = STACK / "supabase"
    target.mkdir(parents=True, exist_ok=True)
    for sub in ("migrations", "functions"):
        shutil.rmtree(target / sub, ignore_errors=True)
        # 본 레포의 functions/.env(비밀값)는 복사하지 않음 — 함수 비밀값은 serve 때 --env-file로 따로 넘김
        shutil.copytree(source / sub, target / sub, ignore=shutil.ignore_patterns(".env", ".env.*"))
    shutil.copyfile(HERE / "stack-config.toml", target / "config.toml")
    keys = target / "signing_keys.json"
    if not keys.exists():
        keys.write_text("[]\n", encoding="utf-8")  # CLI가 기존 파일을 먼저 읽으므로 빈 목록으로 시작
        done = _cli("gen", "signing-key", "--algorithm", "ES256", "--yes", capture=True)
        if done.returncode != 0 or '"kid"' not in keys.read_text(encoding="utf-8"):
            keys.unlink(missing_ok=True)
            sys.exit("ES256 서명 키 만들기 실패:\n" + (done.stdout or "") + (done.stderr or ""))
        print("ES256 서명 키 만듦 (dev/.stack/supabase/signing_keys.json — 깃에 안 들어감)")
    n_migrations = len(list((target / "migrations").glob("*.sql")))
    print(f"준비 완료: 마이그레이션 {n_migrations}개 · 함수 복사 · 설정 복사 → {STACK}")
    print("다음: npx supabase@2.119.0 --workdir dev/.stack start")
    return 0


# ---------- env / check 공통: 로컬 스택 상태 ----------


def _status() -> dict[str, str]:
    done = _cli("status", "-o", "env", capture=True)
    if done.returncode != 0:
        sys.exit(
            "로컬 스택이 안 떠 있음 — 먼저 `npx supabase@2.119.0 --workdir dev/.stack start`\n"
            + (done.stderr or "")
        )
    values: dict[str, str] = {}
    for line in done.stdout.splitlines():
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
        "# dev/local_stack.py env 가 만든 파일 — 로컬 스택 전용 · 깃에 안 들어감",
        f"SUPABASE_URL={url}",
        f"SUPABASE_JWT_ISSUER={url}/auth/v1",
        f"SUPABASE_SECRET_KEY={secret}",
        "APP_VERSION=local",
        "HEALTH_REQUIRE_SCHEDULER=false",
    ]
    out = STACK / "api.env"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"만듦: {out} (값은 안 찍음)")
    return 0


# ---------- check ----------


def _request(method: str, url: str, *, headers: dict[str, str] | None = None, body: object | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
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
        sys.exit(f"로컬 로그인 실패(status {status}) — 스택이 떠 있는지 확인")
    payload = json.loads(text)
    return payload["access_token"], payload["user"]["id"]


def _jwt_summary(token: str) -> str:
    """헤더 alg · kid와 payload의 iss만 보여 줌(서명 · 값은 안 찍음)"""

    def part(segment: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))

    head, payload, _ = token.split(".")
    return f"alg={part(head).get('alg')} kid={'있음' if part(head).get('kid') else '없음'} iss={part(payload).get('iss')}"


def cmd_check(args: argparse.Namespace) -> int:
    api = args.api_url.rstrip("/")
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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="격리 Supabase 작업 폴더(dev/.stack) 만들기")
    prepare.add_argument("--main-repo", required=True, help="본 레포(respec-alarm-capstone) 폴더 경로")
    prepare.set_defaults(func=cmd_prepare)
    sub.add_parser("env", help="서버용 env 파일 dev/.stack/api.env 만들기").set_defaults(func=cmd_env)
    check = sub.add_parser("check", help="로컬 토큰으로 서버 /me 등을 확인")
    check.add_argument("--api-url", default="http://127.0.0.1:28000")
    check.set_defaults(func=cmd_check)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

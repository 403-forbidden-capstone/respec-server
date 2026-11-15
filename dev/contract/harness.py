"""계약 시험 틀 (로컬 격리 스택 전용) — 같은 시작 상태를 두 번 만들어 Edge(본 레포 코드)와 서버(Python)를 번갈아 부르고
응답 · DB 행을 견줌. 운영 DB · 운영 키는 쓰지 않음(127.0.0.1만, 로컬 스택의 시험 계정만).

  한 경우 = (시작 상태 SQL, 부르는 사람, 함수, 요청) → [Edge 쪽] DB 초기화 → 시작 상태 → 호출 → DB 덤프
                                                  [서버 쪽] 같은 순서 → 정규화한 (상태 · 본문 · DB 덤프)를 견줌
  정규화: request_id · 생성 · 수정 시각(created_at · updated_at · last_calculated_at)을 뺌, 새로 생긴 id(uuid)는 행 내용 순서대로 <새N>으로 바꿈
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import clean_env  # noqa: E402
import local_stack as ls  # noqa: E402

DB_CONTAINER = "supabase_db_respec-server-dev"
EDGE_BASE = f"http://127.0.0.1:{ls.API_PORT}/functions/v1/"
SERVER_BASE = f"http://127.0.0.1:{ls.SERVER_PORT}/fn/"
AUTH_BASE = f"http://127.0.0.1:{ls.API_PORT}/auth/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 시험 계정 — id는 고정(시작 상태 SQL이 이 id로 행을 만듦)
USERS = {
    "A": ("a0000000-0000-4000-8000-00000000000a", "contract-a@example.test"),
    "B": ("b0000000-0000-4000-8000-00000000000b", "contract-b@example.test"),
    "C": ("c0000000-0000-4000-8000-00000000000c", "contract-c@example.test"),
}
PASSWORD = "contract-local-only-password"

DATA_TABLES = [
    "alarm_events",
    "alarm_proposals",
    "alarm_suggestions",
    "alarms",
    "appointment_participants",
    "appointments",
    "conversation_sessions",
    "daily_journals",
    "daily_records",
    "friend_links",
    "friends",
    "group_members",
    "groups",
    "push_sends",
    "push_tickets",
    "push_tokens",
    "rate_limit_events",
    "roll_attempts",
    "routines",
    "sharing_permissions",
    "todos",
    "user_blocks",
    "user_edit_logs",
    "user_places",
    "wake_hides",
    "wake_scripts",
    "ai_calls",
    "prepare_ml_verified_profiles",
]
# 비교할 표와 정렬 열 (id가 아니라 내용으로 정렬해야 Edge · 서버 쪽 순서가 같음)
DUMP_TABLES = [
    "appointments",
    "appointment_participants",
    "alarms",
    "user_edit_logs",
    "roll_attempts",
    "groups",
    "group_members",
    "push_sends",
    "alarm_events",
    "push_tokens",
    "push_tickets",
    "alarm_suggestions",
]
MASK_COLUMNS = {
    "created_at",
    "updated_at",
    "last_calculated_at",
    "joined_at",
    "invited_at",
    "invite_code",
    "invite_code_issued_at",
    "invite_code_expires_at",
    "sent_at",
    "last_seen_at",  # 푸시 보낸 시각 · 토큰 확인 시각 — Edge · 서버가 따로 돌아 몇 초 다름
}
EXPO_BASE = "http://127.0.0.1:28999"  # fake_expo.py (진짜 Expo는 안 부름)
JOB_FUNCS = {
    "wake-check",
    "suggestion-generate",
}  # 서버에는 HTTP 길이 없는 예약 작업 — server_job.py로 본문을 직접 돌림
API_DIR = HERE.parent.parent / "api"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def sql(statement: str) -> str:
    done = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            DB_CONTAINER,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            "-q",
            "-f",
            "-",
        ],
        input=statement,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if done.returncode != 0:
        raise RuntimeError(f"SQL 실패: {done.stderr.strip()[:600]}\n--- {statement[:300]}")
    return done.stdout.strip()


def sql_json(statement: str) -> Any:
    out = sql(statement)
    return json.loads(out) if out else None


def http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: object | None = None,
    timeout: float = 60,
) -> tuple[int, Any, dict[str, str]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=timeout) as res:
            status, raw, hdrs = res.status, res.read().decode("utf-8", "replace"), dict(res.headers)
    except urllib.error.HTTPError as err:
        status, raw, hdrs = err.code, err.read().decode("utf-8", "replace"), dict(err.headers)
    except OSError as err:
        return 0, f"연결 실패: {err}", {}
    try:
        return status, json.loads(raw) if raw else None, hdrs
    except ValueError:
        return status, raw, hdrs


@dataclass
class Env:
    anon: str
    secret: str
    tokens: dict[str, str] = field(default_factory=dict)


def setup_env() -> Env:
    status = ls._status()
    anon = status.get("PUBLISHABLE_KEY") or status.get("ANON_KEY")
    secret = status.get("SECRET_KEY") or status.get("SERVICE_ROLE_KEY")
    if not anon or not secret:
        raise SystemExit("로컬 스택 키를 못 찾음")
    env = Env(anon=anon, secret=secret)
    admin = {"apikey": secret, "Authorization": f"Bearer {secret}"}
    for name, (uid, email) in USERS.items():
        # 있으면 건너뜀(409 · 422), 없으면 고정 id로 만듦
        http(
            "POST",
            f"{AUTH_BASE}/admin/users",
            headers=admin,
            body={"id": uid, "email": email, "password": PASSWORD, "email_confirm": True},
        )
        status_code, body, _ = http(
            "POST",
            f"{AUTH_BASE}/token?grant_type=password",
            headers={"apikey": anon},
            body={"email": email, "password": PASSWORD},
        )
        if status_code != 200:
            raise SystemExit(f"{name} 로그인 실패({status_code}): {str(body)[:200]}")
        if body["user"]["id"] != uid:
            raise SystemExit(f"{name}의 id가 고정 id와 다름 — 로컬 스택에 같은 이메일의 다른 계정이 있음")
        env.tokens[name] = body["access_token"]
    return env


BASE_PROFILES = """
insert into public.profiles (user_id, nickname, timezone, default_buffer_minutes, default_transport, home_name, home_lat, home_lng, default_prepare_minutes, wake_sensitivity, wake_margin, handle)
values
 ('{A}', '계약A', 'Asia/Seoul', 10, 'TRANSIT', '집', 37.5547, 126.9707, 60, 'NORMAL', 'NORMAL', 'contract_a'),
 ('{B}', '계약B', 'Asia/Seoul', 15, 'WALK', '집', 37.5600, 126.9900, 40, 'HEAVY', 'LOOSE', 'contract_b'),
 ('{C}', '계약C', 'America/New_York', 5, 'CAR', null, null, null, 60, 'LIGHT', 'TIGHT', 'contract_c')
on conflict (user_id) do update set nickname = excluded.nickname, timezone = excluded.timezone, default_buffer_minutes = excluded.default_buffer_minutes,
  default_transport = excluded.default_transport, home_name = excluded.home_name, home_lat = excluded.home_lat, home_lng = excluded.home_lng,
  default_prepare_minutes = excluded.default_prepare_minutes, wake_sensitivity = excluded.wake_sensitivity, wake_margin = excluded.wake_margin, handle = excluded.handle,
  wake_ping_enabled = true;
"""


def reset(extra_sql: str = "") -> None:
    """데이터 표를 비우고 시험 계정의 프로필을 고정 값으로 되돌린 뒤 이 경우의 시작 상태를 넣음"""
    ids = {k: v[0] for k, v in USERS.items()}
    parts = [
        f"truncate {', '.join('public.' + t for t in DATA_TABLES)} restart identity cascade;",
        BASE_PROFILES.format(**ids),
    ]
    parts.append("update public.server_routes set mode = 'edge'; delete from public.server_route_users;")
    if extra_sql:
        parts.append(extra_sql.format(**ids))
    sql("\n".join(parts))


def dump() -> dict[str, list[dict[str, Any]]]:
    selects = ", ".join(
        f"'{t}', (select coalesce(json_agg(row_to_json(x)), '[]'::json) from public.{t} x)"
        for t in DUMP_TABLES
    )
    return sql_json(f"select json_build_object({selects});")


def server_job(fn: str) -> tuple[int, Any]:
    """서버 쪽 예약 작업 본문 한 번 (Edge의 HTTP 응답과 같은 모양을 (200, JSON)으로, 실패는 (500, {error}))"""
    python = (
        API_DIR
        / ".venv"
        / ("Scripts/python.exe" if (API_DIR / ".venv" / "Scripts").is_dir() else "bin/python")
    )
    done = subprocess.run(
        [str(python), str(HERE / "server_job.py"), fn],
        cwd=API_DIR,
        env=clean_env.server_env(),  # 셸의 운영 값(카카오 · PREPARE_ML_* 등)은 버리고 api.env만 (clean_env.py)
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if not lines:
        return 0, f"실행기 오류: {done.stderr.strip()[-400:]}"
    out = json.loads(lines[-1])
    return (200, out["result"]) if out["ok"] else (500, {"error": out["error"]})


def expo_reset() -> None:
    http("POST", EXPO_BASE + "/__reset", body={}, timeout=5)


def expo_view() -> dict[str, Any] | None:
    """가짜 Expo가 받은 보내기 요청 — 요청 수와 메시지(받는 토큰 · 문구 · 채널 · 데이터)를 내용순으로 늘어놓음 (Edge · 서버의 보내는 순서가 달라도 같게)"""
    status, log, _ = http("GET", EXPO_BASE + "/__log", timeout=5)
    if status != 200 or not isinstance(log, list):
        return None
    messages = [m for req in log for m in req["messages"]]
    messages.sort(key=lambda m: json.dumps(m, sort_keys=True, ensure_ascii=False))
    return {"requests": len(log), "authorized": any(req["authorized"] for req in log), "messages": messages}


def call(
    side: str,
    fn: str,
    user: str | None,
    body: object | None,
    env: Env,
    *,
    service: bool = False,
    method: str = "POST",
) -> tuple[int, Any]:
    if side == "server" and fn in JOB_FUNCS:
        return server_job(fn)
    if fn in JOB_FUNCS:
        service = True
    if service:
        auth = {"Authorization": f"Bearer {env.secret}", "apikey": env.secret}
    elif user is None:
        auth = {}
    else:
        auth = {"Authorization": f"Bearer {env.tokens[user]}", "apikey": env.anon}
    url = (EDGE_BASE if side == "edge" else SERVER_BASE) + fn
    status, payload, _ = http(method, url, headers=auth, body=body, timeout=90)
    return status, payload


def _scrub(value: Any, fixed: set[str]) -> Any:
    """열 이름으로 시각을 빼고, 값의 uuid 중 고정 id가 아닌 것은 모양만 남김(번호 매기기용 자리표시)"""
    if isinstance(value, dict):
        return {k: _scrub(v, fixed) for k, v in value.items() if k not in MASK_COLUMNS and k != "request_id"}
    if isinstance(value, list):
        return [_scrub(v, fixed) for v in value]
    if isinstance(value, str):
        return UUID_RE.sub(lambda m: m.group(0) if m.group(0) in fixed else "<uuid>", value)
    return value


def normalize(
    status: int, body: Any, rows: dict[str, list[dict[str, Any]]], fixed: set[str], expo: Any = None
) -> dict[str, Any]:
    """새로 생긴 uuid를 (표 순서 · 행 내용 순서 · 열 순서)로 번호 매겨 <새1>, <새2>…로 바꾼 결과를 돌려줌"""
    mapping: dict[str, str] = {}

    def assign(row: dict[str, Any]) -> None:
        for _, v in row.items():
            if isinstance(v, str):
                for u in UUID_RE.findall(v):
                    if u not in fixed and u not in mapping:
                        mapping[u] = f"<새{len(mapping) + 1}>"

    ordered: dict[str, list[dict[str, Any]]] = {}
    for table in DUMP_TABLES:
        table_rows = rows.get(table, [])
        keyed = sorted(
            table_rows, key=lambda r: json.dumps(_scrub(r, fixed), sort_keys=True, ensure_ascii=False)
        )
        for row in keyed:
            assign(row)
        ordered[table] = keyed

    def swap(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: swap(v) for k, v in value.items() if k not in MASK_COLUMNS and k != "request_id"}
        if isinstance(value, list):
            return [swap(v) for v in value]
        if isinstance(value, str):
            return UUID_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), value)
        return value

    # 응답 속 uuid도 같은 번호표로 바꿈 (DB에 없는 새 uuid가 응답에만 있으면 그 응답의 등장 순서로 번호를 이어 붙임)
    text = json.dumps(body, ensure_ascii=False, sort_keys=False)
    for u in UUID_RE.findall(text):
        if u not in fixed and u not in mapping:
            mapping[u] = f"<새{len(mapping) + 1}>"
    result = {"status": status, "body": swap(body), "db": {t: swap(r) for t, r in ordered.items()}}
    if expo is not None:
        result["expo"] = swap(expo)
    return result


def diff(a: Any, b: Any, path: str = "$", out: list[str] | None = None, limit: int = 12) -> list[str]:
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: Edge에 없음 · 서버 {json.dumps(b[k], ensure_ascii=False)[:120]}")
            elif k not in b:
                out.append(f"{path}.{k}: Edge {json.dumps(a[k], ensure_ascii=False)[:120]} · 서버에 없음")
            else:
                diff(a[k], b[k], f"{path}.{k}", out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: 길이 Edge {len(a)} · 서버 {len(b)}")
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            diff(x, y, f"{path}[{i}]", out, limit)
    elif a != b:
        out.append(
            f"{path}: Edge {json.dumps(a, ensure_ascii=False)[:120]} · 서버 {json.dumps(b, ensure_ascii=False)[:120]}"
        )
    return out


@dataclass
class Case:
    name: str
    fn: str
    user: str | None
    body: object | None
    setup: str = ""  # 이 경우의 시작 상태 SQL ({A} {B} {C}는 시험 계정 id)
    service: bool = False
    # 같은 상태에서 이어서 부를 호출들 (함수 이름, 부르는 사람, 요청). 앞 응답은 따로 비교하고 마지막 DB 덤프도 비교함
    then: list[tuple[str, str | None, object | None]] = field(default_factory=list)
    note: str = ""


def run_side(side: str, case: Case, env: Env, fixed: set[str]) -> dict[str, Any]:
    reset(case.setup)
    expo_reset()
    steps: list[tuple[int, Any, Any]] = []

    def step(fn: str, user: str | None, req: object | None, service: bool = False) -> None:
        status, body = call(side, fn, user, req, env, service=service)
        steps.append((status, body, expo_view()))  # 호출마다 그 호출이 보낸 푸시를 따로 거둠
        expo_reset()

    step(case.fn, case.user, case.body, case.service)
    for fn, user, req in case.then:
        if fn == "$sql":  # 호출 사이에 DB 상태를 바꿈 (예: 그 사이 사용자가 기상 확인) — 비교 대상 아님
            sql(str(req).format(**{k: v[0] for k, v in USERS.items()}))
            continue
        step(fn, user, req)
    rows = dump()
    result = normalize(steps[0][0], steps[0][1], rows, fixed, steps[0][2])
    if len(steps) > 1:
        result["then"] = [
            normalize(s, b, {t: [] for t in DUMP_TABLES}, fixed, e) | {"db": None} for s, b, e in steps[1:]
        ]
    return result


def compare(
    case: Case, env: Env, fixed: set[str], *, edge_first: bool
) -> tuple[bool, list[str], dict[str, Any], dict[str, Any]]:
    order = ["edge", "server"] if edge_first else ["server", "edge"]
    results: dict[str, dict[str, Any]] = {}
    for side in order:
        results[side] = run_side(side, case, env, fixed)
    differences = diff(results["edge"], results["server"])
    return not differences, differences, results["edge"], results["server"]


def fixed_ids(extra: set[str] | None = None) -> set[str]:
    return {v[0] for v in USERS.values()} | (extra or set())

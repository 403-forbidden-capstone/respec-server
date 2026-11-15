"""장소 함수 재생 — 같은 입력을 Edge · 서버 양쪽에 보내 응답을 비교하고 지연(p95)을 잼 (완결점 1-12 ③, ②의 요청 수)

  확인하는 것 (전부 맞아야 통과):
  · 요청이 양쪽 30건 이상 (고정 입력 20건 × 반복 횟수 `--rounds`, 기본 3 = 양쪽 60건)
  · 케이스마다 **기대한 상태로 양쪽이 답함**: 정상 입력은 200, 잘못된 입력은 400 — 양쪽이 똑같이 실패(키 만료 · 한도 초과 · 네트워크 오류)해도
    "같은 오류"로 통과하는 것을 막음. 서버 5xx가 한 번이라도 있으면 실패
  · 같은 입력의 응답(상태 + 파싱한 JSON)이 Edge · 서버에서 같음 (값 차이 0)
  · 같은 입력으로 잰 p95가 서버 ≤ Edge — 순서를 번갈아 부르고 첫 바퀴는 데우기(Edge Deno 콜드 스타트)로 뺌(`--no-warmup`으로 끔).
    연결을 매번 새로 맺고 클라이언트 → EC2 · 클라이언트 → Supabase 경로가 달라 서버 처리 시간 자체가 아니라 연결 포함 값임
  통과해도 ② canary 24시간 · 서버 5xx 0(nginx 로그 `$status`) · Edge 자동 대체 0(앱 로그)은 이 스크립트가 재지 않음 — 따로 셈.

값은 환경 변수로만 받고 화면 · 파일에 안 찍음 (토큰 · 키 · 비밀번호):
  REPLAY_SUPABASE_URL  Supabase 프로젝트 주소 (Edge는 이 주소의 /functions/v1)
  REPLAY_ANON_KEY      Supabase 공개 키(anon · publishable) — 로그인 요청의 apikey 헤더
  REPLAY_EMAIL · REPLAY_PASSWORD  점검용 시험 계정 (운영 DB의 시험 계정)
  REPLAY_SERVER_URL    서버 주소 (예: https://api.respecalarm.com)
표준 라이브러리만 씀 (설치할 것 없음). 입력은 모두 공개된 장소(랜드마크)라 개인 정보가 없고, 출력은 케이스 이름 · 상태 · 시간만.
`--print-grep`은 재생 입력(검색어 · 좌표)을 잡는 grep 패턴을 찍음 — 서버 로그에 0건이어야 함(완결점 1-12 ④).
끝나면 종료 코드: 0 = 통과 / 1 = 아니면.
"""

import argparse
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

Case = dict  # {"name": 사람이 읽는 이름, "fn": "place-search" | "place-reverse", "body": 요청 본문, "expect": 기대 상태}

SEOUL = {"lat": 37.5547, "lng": 126.9707}  # 서울역 근처 — 기준 좌표

CASES: list[Case] = [
    # 검색 10건: 기준 좌표 있음 · 없음, limit, "…역"(지하철역 먼저), 결과 없음
    {"name": "검색 세종대", "fn": "place-search", "body": {"query": "세종대", **SEOUL}, "expect": 200},
    {"name": "검색 강남역(역)", "fn": "place-search", "body": {"query": "강남역", **SEOUL}, "expect": 200},
    {
        "name": "검색 스타벅스(여러 지점)",
        "fn": "place-search",
        "body": {"query": "스타벅스", **SEOUL},
        "expect": 200,
    },
    {"name": "검색 시청역(역, 좌표 없음)", "fn": "place-search", "body": {"query": "시청역"}, "expect": 200},
    {"name": "검색 서울역", "fn": "place-search", "body": {"query": "서울역", "limit": 5}, "expect": 200},
    {
        "name": "검색 홍대입구역",
        "fn": "place-search",
        "body": {"query": "홍대입구역", **SEOUL, "limit": 15},
        "expect": 200,
    },
    {
        "name": "검색 경복궁 limit 3",
        "fn": "place-search",
        "body": {"query": "경복궁", "limit": 3},
        "expect": 200,
    },
    {"name": "검색 롯데월드타워", "fn": "place-search", "body": {"query": "롯데월드타워"}, "expect": 200},
    {
        "name": "검색 카카오 판교",
        "fn": "place-search",
        "body": {"query": "카카오 판교", **SEOUL},
        "expect": 200,
    },
    {
        "name": "검색 결과 없음",
        "fn": "place-search",
        "body": {"query": "ㅁㄴㅇㄹㅁㄴㅇㄹ12345"},
        "expect": 200,
    },
    # 좌표 → 주소 7건: 도로명 · 건물 이름 · 지번만 · 바다 · 0,0
    {
        "name": "주소 서울시청",
        "fn": "place-reverse",
        "body": {"lat": 37.5663, "lng": 126.9779},
        "expect": 200,
    },
    {"name": "주소 강남역", "fn": "place-reverse", "body": {"lat": 37.4979, "lng": 127.0276}, "expect": 200},
    {"name": "주소 세종대", "fn": "place-reverse", "body": {"lat": 37.5509, "lng": 127.0736}, "expect": 200},
    {"name": "주소 부산역", "fn": "place-reverse", "body": {"lat": 35.1151, "lng": 129.0422}, "expect": 200},
    {
        "name": "주소 제주공항",
        "fn": "place-reverse",
        "body": {"lat": 33.5113, "lng": 126.4930},
        "expect": 200,
    },
    {
        "name": "주소 바다(주소 없음)",
        "fn": "place-reverse",
        "body": {"lat": 33.0, "lng": 125.0},
        "expect": 200,
    },
    {"name": "주소 0,0", "fn": "place-reverse", "body": {"lat": 0, "lng": 0}, "expect": 200},
    # 잘못된 입력 3건: 오류 본문까지 같아야 함 (입력 검사는 키 · 카카오와 상관없음)
    {"name": "오류 검색어 없음", "fn": "place-search", "body": {}, "expect": 400},
    {"name": "오류 lat만", "fn": "place-search", "body": {"query": "a", "lat": 37.5}, "expect": 400},
    {"name": "오류 좌표 범위", "fn": "place-reverse", "body": {"lat": 91, "lng": 0}, "expect": 400},
]

# 응답 하나: (상태 번호, 파싱한 JSON 본문 또는 글자, 걸린 시간 ms) — 상태 0은 네트워크 오류
Response = tuple[int, object, float]
Post = Callable[[Case], Response]


def percentile(values: list[float], pct: float) -> float:
    """가장 가까운 순위(nearest-rank) 백분위 — 값이 20~60개라 보간하지 않음"""
    if not values:
        raise ValueError("값이 없음")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def _js_str(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def leak_grep_pattern(cases: list[Case] = CASES) -> str:
    """재생 입력의 검색어 · 좌표를 `grep -E`로 잡는 패턴 (서버 로그에 0건이어야 함). 입력 목록 하나만 믿도록 CASES에서 만듦"""
    words: set[str] = set()
    for case in cases:
        body = case["body"]
        if isinstance(body.get("query"), str) and len(body["query"]) >= 2:
            words.add(re.escape(body["query"]))
        for key in ("lat", "lng"):
            value = body.get(key)
            # 0 · 33.0 · 91 · 37.5처럼 짧은 값은 로그의 다른 숫자(상태 · 시간)와 겹쳐 오탐이 되므로 소수점 아래 3자리 이상인 좌표만
            if isinstance(value, int | float) and len(_js_str(value).partition(".")[2]) >= 3:
                words.add(re.escape(_js_str(value)))
    return "|".join(sorted(words) + ["Bearer", "eyJ", "KakaoAK"])


def run(cases: list[Case], rounds: int, post_edge: Post, post_server: Post, *, warmup: bool = False) -> dict:
    """같은 입력을 Edge · 서버에 번갈아 보내고(회차마다 순서를 바꿈) 차이 · 기대와 다른 상태 · 지연을 모아 돌려줌"""
    diffs: list[str] = []
    unexpected: list[str] = []
    status_of: dict[str, tuple[int, int]] = {}  # 차이 · 기대와 다른 상태의 케이스 → (Edge 상태, 서버 상태)
    edge_ms: list[float] = []
    server_ms: list[float] = []
    server_5xx = 0
    sent = 0
    for round_no in range(rounds + (1 if warmup else 0)):
        counted = not (warmup and round_no == 0)
        for case in cases:
            pair = [("edge", post_edge), ("server", post_server)]
            if round_no % 2:
                pair.reverse()
            got: dict[str, Response] = {}
            for side, post in pair:
                got[side] = post(case)
            if not counted:
                continue
            sent += 1
            edge_ms.append(got["edge"][2])
            server_ms.append(got["server"][2])
            statuses = (got["edge"][0], got["server"][0])
            if got["edge"][:2] != got["server"][:2]:
                diffs.append(case["name"])
                status_of[case["name"]] = statuses
            if statuses != (case["expect"], case["expect"]):
                unexpected.append(case["name"])
                status_of[case["name"]] = statuses
            if 500 <= statuses[1] < 600:
                server_5xx += 1
    return {
        "requests_per_side": sent,
        "diffs": diffs,
        "unexpected": unexpected,
        "status_of": status_of,
        "server_5xx": server_5xx,
        "edge_p50": percentile(edge_ms, 50),
        "edge_p95": percentile(edge_ms, 95),
        "server_p50": percentile(server_ms, 50),
        "server_p95": percentile(server_ms, 95),
    }


def verdict(summary: dict) -> bool:
    """요청 30건 이상 · 기대한 상태(정상 200 · 잘못된 입력 400) · 서버 5xx 0 · 값 차이 0 · 서버 p95 ≤ Edge p95"""
    return (
        summary["requests_per_side"] >= 30
        and not summary["unexpected"]
        and not summary["server_5xx"]
        and not summary["diffs"]
        and summary["server_p95"] <= summary["edge_p95"]
    )


# ---------- 네트워크 (표준 라이브러리) ----------


def _http_json(url: str, body: object, headers: dict[str, str]) -> Response:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    # 연결 실패 · 시간 초과 · 연결이 끊김(RemoteDisconnected · ConnectionResetError)은 상태 0
    except (OSError, http.client.HTTPException):
        return 0, "네트워크 오류", (time.perf_counter() - started) * 1000
    elapsed = (time.perf_counter() - started) * 1000
    try:
        return status, json.loads(raw.decode("utf-8")), elapsed
    except ValueError:
        return status, "JSON 아님", elapsed


def login(supabase_url: str, anon_key: str, email: str, password: str) -> str:
    status, body, _ = _http_json(
        f"{supabase_url.rstrip('/')}/auth/v1/token?grant_type=password",
        {"email": email, "password": password},
        {"apikey": anon_key},
    )
    token = body.get("access_token") if isinstance(body, dict) else None
    if status != 200 or not isinstance(token, str):
        raise SystemExit(f"로그인 실패 (상태 {status}) — 환경 변수를 확인하세요")
    return token


def _rounds(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("--rounds는 1 이상이어야 함")
    return value


ENV_NAMES = ("REPLAY_SUPABASE_URL", "REPLAY_ANON_KEY", "REPLAY_EMAIL", "REPLAY_PASSWORD", "REPLAY_SERVER_URL")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="장소 함수 Edge ↔ 서버 재생 비교")
    parser.add_argument(
        "--rounds", type=_rounds, default=3, help="고정 입력 20건을 몇 번 반복할지 (기본 3 = 양쪽 60건)"
    )
    parser.add_argument("--no-warmup", action="store_true", help="첫 한 바퀴를 데우기로 빼지 않고 셈에 넣음")
    parser.add_argument(
        "--print-grep",
        action="store_true",
        help="서버 로그 확인용 grep 패턴만 찍고 끝냄 (환경 변수 필요 없음)",
    )
    args = parser.parse_args(argv)

    if args.print_grep:
        print(leak_grep_pattern())
        return 0

    env = {name: os.environ.get(name, "") for name in ENV_NAMES}
    missing = [name for name, value in env.items() if not value]
    if missing:
        print("환경 변수가 비어 있음:", ", ".join(missing), file=sys.stderr)
        return 1

    token = login(
        env["REPLAY_SUPABASE_URL"], env["REPLAY_ANON_KEY"], env["REPLAY_EMAIL"], env["REPLAY_PASSWORD"]
    )
    edge_headers = {"Authorization": f"Bearer {token}", "apikey": env["REPLAY_ANON_KEY"]}
    server_headers = {"Authorization": f"Bearer {token}"}
    edge_base = env["REPLAY_SUPABASE_URL"].rstrip("/") + "/functions/v1/"
    server_base = env["REPLAY_SERVER_URL"].rstrip("/") + "/fn/"

    def post_edge(case: Case) -> Response:
        return _http_json(edge_base + case["fn"], case["body"], edge_headers)

    def post_server(case: Case) -> Response:
        return _http_json(server_base + case["fn"], case["body"], server_headers)

    summary = run(CASES, args.rounds, post_edge, post_server, warmup=not args.no_warmup)
    print(
        f"요청 {summary['requests_per_side']}건씩 (양쪽)  값 차이 {len(set(summary['diffs']))}건  기대와 다른 상태 {len(set(summary['unexpected']))}건  서버 5xx {summary['server_5xx']}건"
    )
    for name in dict.fromkeys(summary["diffs"]):
        edge_status, server_status = summary["status_of"][name]
        print(f"  값 차이: {name} (Edge {edge_status} · 서버 {server_status})")
    for name in dict.fromkeys(summary["unexpected"]):
        edge_status, server_status = summary["status_of"][name]
        print(f"  기대와 다른 상태: {name} (Edge {edge_status} · 서버 {server_status})")
    print(f"Edge   p50 {summary['edge_p50']:.0f}ms  p95 {summary['edge_p95']:.0f}ms")
    print(f"서버   p50 {summary['server_p50']:.0f}ms  p95 {summary['server_p95']:.0f}ms")
    ok = verdict(summary)
    print(
        "결과:",
        "통과 — 끝 칸 ③과 ②의 요청 수 (canary 24시간 · 서버 5xx 0 · Edge 자동 대체 0은 따로 셈)"
        if ok
        else "통과 못 함",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""장소 함수 재생 — 같은 입력을 Edge · 서버 양쪽에 보내 응답을 비교하고 지연(p95)을 잼 (완결점 1-12 ② ③)

  ② 요청 30건 이상 (고정 입력 20건 × 반복 횟수 `--rounds`, 기본 2 = 양쪽 40건씩)
  ③ 같은 입력 20건의 응답이 Edge · 서버에서 같음(상태 번호 + JSON 본문, 값 차이 0) · 같은 입력으로 잰 p95가 서버 ≤ Edge
     (Edge는 Deno 콜드 스타트가 있어 첫 호출이 느림 — 순서를 번갈아 부르고, 첫 회차를 데우기로 빼고 싶으면 `--warmup`)

값은 환경 변수로만 받고 화면 · 파일에 안 찍음 (토큰 · 키 · 비밀번호):
  REPLAY_SUPABASE_URL  Supabase 프로젝트 주소 (Edge는 이 주소의 /functions/v1)
  REPLAY_ANON_KEY      Supabase 공개 키(anon · publishable) — 로그인 요청의 apikey 헤더
  REPLAY_EMAIL · REPLAY_PASSWORD  점검용 시험 계정 (운영 DB의 시험 계정)
  REPLAY_SERVER_URL    서버 주소 (예: https://api.respecalarm.com)
표준 라이브러리만 씀 (설치할 것 없음). 입력은 모두 공개된 장소(랜드마크)라 개인 정보가 없고, 출력은 케이스 이름 · 상태 · 시간만.
끝나면 종료 코드: 0 = 차이 0 · 서버 p95 ≤ Edge p95 / 1 = 아니면.
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

Case = dict  # {"name": 사람이 읽는 이름, "fn": "place-search" | "place-reverse", "body": 요청 본문}

SEOUL = {"lat": 37.5547, "lng": 126.9707}  # 서울역 근처 — 기준 좌표

CASES: list[Case] = [
    # 검색 10건: 기준 좌표 있음 · 없음, limit, "…역"(지하철역 먼저), 결과 없음
    {"name": "검색 세종대", "fn": "place-search", "body": {"query": "세종대", **SEOUL}},
    {"name": "검색 강남역(역)", "fn": "place-search", "body": {"query": "강남역", **SEOUL}},
    {"name": "검색 스타벅스(여러 지점)", "fn": "place-search", "body": {"query": "스타벅스", **SEOUL}},
    {"name": "검색 시청역(역, 좌표 없음)", "fn": "place-search", "body": {"query": "시청역"}},
    {"name": "검색 서울역", "fn": "place-search", "body": {"query": "서울역", "limit": 5}},
    {"name": "검색 홍대입구역", "fn": "place-search", "body": {"query": "홍대입구역", **SEOUL, "limit": 15}},
    {"name": "검색 경복궁 limit 3", "fn": "place-search", "body": {"query": "경복궁", "limit": 3}},
    {"name": "검색 롯데월드타워", "fn": "place-search", "body": {"query": "롯데월드타워"}},
    {"name": "검색 카카오 판교", "fn": "place-search", "body": {"query": "카카오 판교", **SEOUL}},
    {"name": "검색 결과 없음", "fn": "place-search", "body": {"query": "ㅁㄴㅇㄹㅁㄴㅇㄹ12345"}},
    # 좌표 → 주소 7건: 도로명 · 건물 이름 · 지번만 · 바다 · 0,0
    {"name": "주소 서울시청", "fn": "place-reverse", "body": {"lat": 37.5663, "lng": 126.9779}},
    {"name": "주소 강남역", "fn": "place-reverse", "body": {"lat": 37.4979, "lng": 127.0276}},
    {"name": "주소 세종대", "fn": "place-reverse", "body": {"lat": 37.5509, "lng": 127.0736}},
    {"name": "주소 부산역", "fn": "place-reverse", "body": {"lat": 35.1151, "lng": 129.0422}},
    {"name": "주소 제주공항", "fn": "place-reverse", "body": {"lat": 33.5113, "lng": 126.4930}},
    {"name": "주소 바다(주소 없음)", "fn": "place-reverse", "body": {"lat": 33.0, "lng": 125.0}},
    {"name": "주소 0,0", "fn": "place-reverse", "body": {"lat": 0, "lng": 0}},
    # 잘못된 입력 3건: 오류 본문까지 같아야 함 (입력 검사는 키 · 카카오와 상관없음)
    {"name": "오류 검색어 없음", "fn": "place-search", "body": {}},
    {"name": "오류 lat만", "fn": "place-search", "body": {"query": "a", "lat": 37.5}},
    {"name": "오류 좌표 범위", "fn": "place-reverse", "body": {"lat": 91, "lng": 0}},
]

# 응답 하나: (상태 번호, 파싱한 JSON 본문 또는 글자, 걸린 시간 ms)
Response = tuple[int, object, float]
Post = Callable[[Case], Response]


def percentile(values: list[float], pct: float) -> float:
    """가장 가까운 순위(nearest-rank) 백분위 — 값이 20~40개라 보간하지 않음"""
    if not values:
        raise ValueError("값이 없음")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def run(cases: list[Case], rounds: int, post_edge: Post, post_server: Post, *, warmup: bool = False) -> dict:
    """같은 입력을 Edge · 서버에 번갈아 보내고(회차마다 순서를 바꿈) 차이와 지연을 모아 돌려줌"""
    diffs: list[str] = []
    edge_ms: list[float] = []
    server_ms: list[float] = []
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
            if got["edge"][:2] != got["server"][:2]:
                diffs.append(case["name"])
    return {
        "requests_per_side": sent,
        "diffs": diffs,
        "edge_p50": percentile(edge_ms, 50),
        "edge_p95": percentile(edge_ms, 95),
        "server_p50": percentile(server_ms, 50),
        "server_p95": percentile(server_ms, 95),
    }


def verdict(summary: dict) -> bool:
    """끝 칸 ②(30건 이상) · ③(차이 0 · 서버 p95 ≤ Edge p95)"""
    return (
        summary["requests_per_side"] >= 30
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
    except (urllib.error.URLError, TimeoutError):
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="장소 함수 Edge ↔ 서버 재생 비교")
    parser.add_argument(
        "--rounds", type=int, default=2, help="고정 입력 20건을 몇 번 반복할지 (기본 2 = 양쪽 40건)"
    )
    parser.add_argument("--warmup", action="store_true", help="첫 한 바퀴는 데우기로 빼고 셈에 안 넣음")
    args = parser.parse_args(argv)

    env = {
        name: os.environ.get(name, "")
        for name in (
            "REPLAY_SUPABASE_URL",
            "REPLAY_ANON_KEY",
            "REPLAY_EMAIL",
            "REPLAY_PASSWORD",
            "REPLAY_SERVER_URL",
        )
    }
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

    summary = run(CASES, args.rounds, post_edge, post_server, warmup=args.warmup)
    print(f"요청 {summary['requests_per_side']}건씩 (양쪽)  값 차이 {len(summary['diffs'])}건")
    for name in summary["diffs"]:
        print(f"  차이: {name}")
    print(f"Edge   p50 {summary['edge_p50']:.0f}ms  p95 {summary['edge_p95']:.0f}ms")
    print(f"서버   p50 {summary['server_p50']:.0f}ms  p95 {summary['server_p95']:.0f}ms")
    ok = verdict(summary)
    print("결과:", "통과 (끝 칸 ② ③)" if ok else "통과 못 함")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

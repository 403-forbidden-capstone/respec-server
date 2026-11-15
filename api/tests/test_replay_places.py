"""dev/replay_places.py — 재생 스크립트의 비교 · 백분위 · 판정 (네트워크는 가짜로)"""

import http.client
import importlib.util
import io
import re
import sys
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENV_NAMES = (
    "REPLAY_SUPABASE_URL",
    "REPLAY_ANON_KEY",
    "REPLAY_EMAIL",
    "REPLAY_PASSWORD",
    "REPLAY_SERVER_URL",
)


@pytest.fixture(scope="module")
def replay():
    spec = importlib.util.spec_from_file_location("replay_places", REPO / "dev" / "replay_places.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["replay_places"] = module
    spec.loader.exec_module(module)
    return module


def test_the_fixed_inputs_are_twenty_and_cover_search_reverse_and_invalid(replay):
    cases = replay.CASES
    assert len(cases) == 20 and len({c["name"] for c in cases}) == 20
    assert (
        sum(c["fn"] == "place-search" for c in cases) == 12  # 검색 10 + 잘못된 입력 2
        and sum(c["fn"] == "place-reverse" for c in cases) == 8  # 주소 7 + 잘못된 입력 1
    )
    queries = [c["body"].get("query", "") for c in cases if c["fn"] == "place-search"]
    assert any(q.endswith("역") for q in queries)  # 지하철역 먼저 규칙
    assert any(c["body"].get("limit") for c in cases) and any("lat" in c["body"] for c in cases)
    assert sum(c["expect"] == 400 for c in cases) == 3 and sum(c["expect"] == 200 for c in cases) == 17


def test_percentile_is_nearest_rank(replay):
    values = [float(v) for v in range(1, 21)]
    assert replay.percentile(values, 95) == 19.0  # 20개의 95% = 19번째
    assert replay.percentile(values, 50) == 10.0
    assert replay.percentile([5.0], 95) == 5.0
    assert replay.percentile([3.0, 1.0, 2.0], 100) == 3.0
    with pytest.raises(ValueError):
        replay.percentile([], 95)


def fake_post(ms: float, mutate=lambda case, body: body, status=None):
    """기대한 상태(정상 200 · 잘못된 입력 400)로 답하는 가짜. status를 주면 그 상태로 고정"""

    def post(case):
        return status or case["expect"], mutate(case, {"echo": case["name"]}), ms

    return post


def test_identical_responses_with_faster_server_pass(replay):
    summary = replay.run(replay.CASES, 2, fake_post(100.0), fake_post(40.0))
    assert summary["requests_per_side"] == 40 and summary["diffs"] == [] and summary["unexpected"] == []
    assert summary["server_p95"] == 40.0 and summary["edge_p95"] == 100.0
    assert replay.verdict(summary)


def test_a_single_body_difference_fails_and_names_the_case(replay):
    def tweak(case, body):
        return {**body, "x": 1} if case["name"] == "주소 바다(주소 없음)" else body

    summary = replay.run(replay.CASES, 2, fake_post(100.0), fake_post(40.0, tweak))
    assert summary["diffs"] == ["주소 바다(주소 없음)"] * 2  # 두 바퀴 모두
    assert summary["status_of"]["주소 바다(주소 없음)"] == (200, 200)
    assert not replay.verdict(summary)


def test_a_status_difference_counts_and_server_5xx_is_counted(replay):
    summary = replay.run(replay.CASES, 1, fake_post(10.0), fake_post(10.0, status=502))
    assert summary["diffs"] != [] and summary["server_5xx"] == 20
    assert not replay.verdict(summary)


def test_both_sides_failing_the_same_way_is_not_a_pass(replay):
    """키 만료 · 한도 초과로 양쪽이 똑같이 502여도 '같은 오류'로 통과하면 안 됨"""
    summary = replay.run(replay.CASES, 2, fake_post(40.0, status=502), fake_post(40.0, status=502))
    assert summary["diffs"] == [] and len(summary["unexpected"]) == 40
    assert summary["status_of"]["검색 세종대"] == (502, 502)
    assert not replay.verdict(summary)


def test_a_valid_input_answered_400_or_an_invalid_one_answered_200_is_unexpected(replay):
    def swap(case):
        return 400 if case["expect"] == 200 else 200, {"echo": case["name"]}, 10.0

    summary = replay.run(replay.CASES, 2, swap, swap)
    assert summary["diffs"] == [] and len(summary["unexpected"]) == 40
    assert not replay.verdict(summary)


def test_network_errors_status_0_on_both_sides_fail(replay):
    def down(case):
        return 0, "네트워크 오류", 20_000.0

    summary = replay.run(replay.CASES, 2, down, down)
    assert summary["diffs"] == [] and len(summary["unexpected"]) == 40
    assert not replay.verdict(summary)


def test_slower_server_p95_fails_even_with_no_diffs(replay):
    summary = replay.run(replay.CASES, 2, fake_post(40.0), fake_post(100.0))
    assert summary["diffs"] == [] and summary["unexpected"] == [] and not replay.verdict(summary)


def test_fewer_than_30_requests_fails_the_verdict(replay):
    summary = replay.run(replay.CASES, 1, fake_post(100.0), fake_post(40.0))
    assert summary["requests_per_side"] == 20 and not replay.verdict(summary)  # ② 30건 이상


def test_order_alternates_each_round_and_warmup_round_is_not_counted(replay):
    order: list[str] = []

    def edge(case):
        order.append("edge")
        return 200, {}, 1.0

    def server(case):
        order.append("server")
        return 200, {}, 1.0

    one = replay.CASES[:1]
    summary = replay.run(one, 2, edge, server, warmup=True)
    assert order == [
        "edge",
        "server",
        "server",
        "edge",
        "edge",
        "server",
    ]  # 데우기 · 1회차 · 2회차, 매번 순서가 바뀜
    assert summary["requests_per_side"] == 2  # 데우기는 셈에 안 넣음


def test_a_warmup_failure_does_not_count_but_a_counted_one_does(replay):
    calls = {"n": 0}

    def flaky_edge(case):
        calls["n"] += 1
        return (502 if calls["n"] == 1 else 200), {}, 1.0  # 맨 처음 한 번(데우기)만 실패

    def ok(case):
        return 200, {}, 1.0

    one = replay.CASES[:1]
    assert replay.run(one, 1, flaky_edge, ok, warmup=True)["unexpected"] == []


def fake_response(status: int, payload: bytes):
    return urllib.error.HTTPError("http://x", status, "x", {}, io.BytesIO(payload))


def test_http_json_turns_connection_failures_into_status_0(replay, monkeypatch):
    for failure in (
        ConnectionResetError("reset"),
        http.client.RemoteDisconnected("closed"),
        http.client.IncompleteRead(b"partial"),  # OSError가 아닌 HTTPException
        http.client.BadStatusLine("garbage"),
        TimeoutError("slow"),
        urllib.error.URLError("no route"),
    ):

        def boom(*args, _failure=failure, **kwargs):
            raise _failure

        monkeypatch.setattr(replay.urllib.request, "urlopen", boom)
        status, body, elapsed = replay._http_json("http://x", {}, {})
        assert (status, body) == (0, "네트워크 오류") and elapsed >= 0


def test_http_json_reads_error_bodies_and_flags_non_json(replay, monkeypatch):
    def error_with_json(*args, **kwargs):
        raise fake_response(502, b'{"error":{"code":"PLACE_UNAVAILABLE"}}')

    def error_with_html(*args, **kwargs):
        raise fake_response(502, b"<html>bad gateway</html>")

    monkeypatch.setattr(replay.urllib.request, "urlopen", error_with_json)
    assert replay._http_json("http://x", {}, {})[:2] == (502, {"error": {"code": "PLACE_UNAVAILABLE"}})
    monkeypatch.setattr(replay.urllib.request, "urlopen", error_with_html)
    assert replay._http_json("http://x", {}, {})[:2] == (502, "JSON 아님")


def test_rounds_must_be_at_least_one(replay, capsys):
    for bad in ("0", "-1"):
        with pytest.raises(SystemExit) as info:
            replay.main(["--rounds", bad])
        assert info.value.code == 2
    assert "1 이상" in capsys.readouterr().err


def test_main_refuses_to_run_without_env_and_never_prints_secrets(replay, monkeypatch, capsys):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPLAY_PASSWORD", "SECRET-PASSWORD-VALUE")
    assert replay.main([]) == 1
    captured = capsys.readouterr()
    assert (
        "REPLAY_SUPABASE_URL" in captured.err and "SECRET-PASSWORD-VALUE" not in captured.out + captured.err
    )


def run_main_with_fake_network(replay, monkeypatch, capsys, argv, answer):
    """환경 변수 · 로그인 · 네트워크를 가짜로 채워 main을 끝까지 돌림. 반환: (종료 코드, 출력, 부른 주소 목록)"""
    for name in ENV_NAMES:
        monkeypatch.setenv(name, "https://example.invalid" if name.endswith("URL") else "SECRET-VALUE")
    urls: list[str] = []

    def fake_http(url, body, headers):
        urls.append(url)
        if "/auth/v1/token" in url:
            return 200, {"access_token": "SECRET-TOKEN"}, 1.0
        side = "edge" if "/functions/v1/" in url else "server"
        case = next(c for c in replay.CASES if c["fn"] == url.rsplit("/", 1)[1] and c["body"] == body)
        status, ms = answer(side, case)
        return status, {"status": status}, ms

    monkeypatch.setattr(replay, "_http_json", fake_http)
    code = replay.main(argv)
    out = capsys.readouterr()
    assert "SECRET-TOKEN" not in out.out + out.err and "SECRET-VALUE" not in out.out + out.err
    return code, out.out, urls


def test_main_passes_with_expected_statuses_and_a_faster_server(replay, monkeypatch, capsys):
    def answer(side, case):
        return case["expect"], 100.0 if side == "edge" else 40.0

    code, out, urls = run_main_with_fake_network(replay, monkeypatch, capsys, [], answer)
    assert code == 0 and "요청 60건씩 (양쪽)" in out and "결과: 통과" in out
    assert len(urls) == 1 + 2 * 20 * 4  # 로그인 1 + 양쪽 × 20건 × (3바퀴 + 데우기 1)


def test_main_no_warmup_counts_every_round(replay, monkeypatch, capsys):
    def answer(side, case):
        return case["expect"], 100.0 if side == "edge" else 40.0

    code, out, urls = run_main_with_fake_network(
        replay, monkeypatch, capsys, ["--no-warmup", "--rounds", "2"], answer
    )
    assert code == 0 and "요청 40건씩 (양쪽)" in out and len(urls) == 1 + 2 * 20 * 2


def test_main_prints_statuses_on_failures_and_exits_1(replay, monkeypatch, capsys):
    def answer(side, case):
        if case["name"] == "검색 세종대":
            return (502 if side == "server" else 200), 10.0
        return case["expect"], 10.0

    code, out, _ = run_main_with_fake_network(replay, monkeypatch, capsys, ["--rounds", "2"], answer)
    assert code == 1 and "결과: 통과 못 함" in out
    assert "값 차이: 검색 세종대 (Edge 200 · 서버 502)" in out
    assert "기대와 다른 상태: 검색 세종대 (Edge 200 · 서버 502)" in out
    assert "서버 5xx 2건" in out


def test_main_does_not_pass_when_both_sides_fail_identically(replay, monkeypatch, capsys):
    def answer(side, case):
        return 502, 10.0

    code, out, _ = run_main_with_fake_network(replay, monkeypatch, capsys, [], answer)
    assert code == 1 and "값 차이 0건" in out and "기대와 다른 상태 20건" in out


def test_print_grep_lists_the_replayed_queries_and_precise_coordinates_only(replay, monkeypatch, capsys):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    assert replay.main(["--print-grep"]) == 0  # 환경 변수 없이도 됨
    pattern = capsys.readouterr().out.strip()
    parts = pattern.split("|")
    assert re.escape("롯데월드타워") in parts and re.escape("ㅁㄴㅇㄹㅁㄴㅇㄹ12345") in parts
    assert re.escape("37.5547") in parts
    assert re.escape("126.493") in parts  # 126.4930은 JS 문자열로 126.493
    short = {re.escape(value) for value in ("0", "91", "33", "125", "33.0", "37.5")}
    assert not short & set(parts)  # 짧은 값은 로그의 다른 숫자와 겹쳐 오탐
    assert re.search(pattern, "lat=37.5663 lng=126.9779") and re.search(pattern, "q=서울역")
    assert not re.search(pattern, "request_id=a method=POST path=/fn/place-search status=200 ms=37")
    assert {"Bearer", "eyJ", "KakaoAK"} <= set(parts)  # 토큰 · 키 모양도 같이 잡음

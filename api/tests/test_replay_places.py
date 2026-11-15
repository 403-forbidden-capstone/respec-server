"""dev/replay_places.py — 재생 스크립트의 비교 · 백분위 · 판정 (네트워크는 가짜로)"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


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


def test_percentile_is_nearest_rank(replay):
    values = [float(v) for v in range(1, 21)]
    assert replay.percentile(values, 95) == 19.0  # 20개의 95% = 19번째
    assert replay.percentile(values, 50) == 10.0
    assert replay.percentile([5.0], 95) == 5.0
    assert replay.percentile([3.0, 1.0, 2.0], 100) == 3.0
    with pytest.raises(ValueError):
        replay.percentile([], 95)


def fake_post(ms: float, mutate=lambda case, body: body):
    def post(case):
        return 200, mutate(case, {"echo": case["name"]}), ms

    return post


def test_identical_responses_with_faster_server_pass(replay):
    summary = replay.run(replay.CASES, 2, fake_post(100.0), fake_post(40.0))
    assert summary["requests_per_side"] == 40 and summary["diffs"] == []
    assert summary["server_p95"] == 40.0 and summary["edge_p95"] == 100.0
    assert replay.verdict(summary)


def test_a_single_body_difference_fails_and_names_the_case(replay):
    def tweak(case, body):
        return {**body, "x": 1} if case["name"] == "주소 바다(주소 없음)" else body

    summary = replay.run(replay.CASES, 2, fake_post(100.0), fake_post(40.0, tweak))
    assert summary["diffs"] == ["주소 바다(주소 없음)"] * 2  # 두 바퀴 모두
    assert not replay.verdict(summary)


def test_a_status_difference_counts(replay):
    def post_server(case):
        return 502, {"echo": case["name"]}, 10.0

    assert replay.run(replay.CASES, 1, fake_post(10.0), post_server)["diffs"] != []


def test_slower_server_p95_fails_even_with_no_diffs(replay):
    summary = replay.run(replay.CASES, 2, fake_post(40.0), fake_post(100.0))
    assert summary["diffs"] == [] and not replay.verdict(summary)


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


def test_main_refuses_to_run_without_env_and_never_prints_secrets(replay, monkeypatch, capsys):
    for name in (
        "REPLAY_SUPABASE_URL",
        "REPLAY_ANON_KEY",
        "REPLAY_EMAIL",
        "REPLAY_PASSWORD",
        "REPLAY_SERVER_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPLAY_PASSWORD", "SECRET-PASSWORD-VALUE")
    assert replay.main([]) == 1
    captured = capsys.readouterr()
    assert (
        "REPLAY_SUPABASE_URL" in captured.err and "SECRET-PASSWORD-VALUE" not in captured.out + captured.err
    )

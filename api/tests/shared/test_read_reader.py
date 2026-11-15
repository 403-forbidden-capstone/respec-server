"""read.ts → app/core/read.py에 이번 묶음에서 더한 Reader 메서드(bool · one_of · uuid · date · object · string_list · objects · json · is_null · string pattern · parse_iso) 시험

원본 read.test.ts 중 이 메서드들을 쓰는 경우. (이미 있는 string · number · int · root · read_json 시험은 tests/test_read.py)
"""

from datetime import UTC, datetime

import pytest

from app.core.errors import AppError
from app.core.read import Reader, parse_iso


def test_iso_8601_needs_offset():
    """ISO 8601은 오프셋이 있어야 함 (5장 5.2)"""
    assert parse_iso("2026-10-20T10:00:00+09:00") == datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
    assert parse_iso("2026-10-20T10:00Z") == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    assert parse_iso("2026-10-20T10:00:00") is None  # 오프셋 없음
    assert parse_iso("2026-10-20") is None
    assert parse_iso("2026-13-40T10:00:00Z") is None


def test_issues_collected_then_thrown_once():
    """필드를 읽으면서 문제를 모았다가 한 번에 던짐 (VALIDATION_FAILED + details.issues)"""
    r = Reader.root({"title": 3, "minutes": "x", "transport": "ROCKET", "when": "soon", "nested": {"n": -1}})
    r.string("title", required=True)
    r.int("minutes")
    r.one_of("transport", ("WALK", "CAR"))
    r.date("when")
    nested = r.object("nested")
    assert nested is not None
    nested.int("n", min=0)
    r.string("missing", required=True)
    with pytest.raises(AppError) as exc:
        r.finish()
    assert exc.value.code == "VALIDATION_FAILED"
    assert exc.value.status == 400
    assert [i["path"] for i in exc.value.details["issues"]] == [
        "title",
        "minutes",
        "transport",
        "when",
        "nested.n",
        "missing",
    ]


def test_good_values_trim_null_is_none():
    """맞는 값은 그대로 읽음 · 글자는 앞뒤 공백 제거 · null은 없음"""
    r = Reader.root(
        {
            "title": "  팀 회의 ",
            "n": 5,
            "ok": True,
            "id": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
            "when": "2026-10-20T10:00:00+09:00",
            "skipped": None,
            "kinds": ["WAKE", "WAKE", "DEPARTURE"],
        }
    )
    assert r.string("title", max=100) == "팀 회의"
    assert r.int("n", min=0, max=10) == 5
    assert r.bool("ok") is True
    assert r.uuid("id") == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert r.date("when") == datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
    assert r.string("skipped") is None
    assert r.is_null("skipped") is True
    assert r.string_list("kinds", ("WAKE", "PREPARE", "DEPARTURE")) == ["WAKE", "DEPARTURE"]
    r.finish()


def test_out_of_range_recorded_as_problems():
    """범위를 벗어나면 문제로 기록"""
    r = Reader.root({"a": 600.5, "b": 700, "c": "x" * 101, "kinds": ["WAKE", "ARRIVE"], "items": [1]})
    r.int("a")
    r.int("b", max=600)
    r.string("c", max=100)
    r.string_list("kinds", ("WAKE", "PREPARE", "DEPARTURE"))
    r.objects("items")
    assert len(r.issues) == 5


def test_json_any_value_but_too_big_rejected():
    """user_edit_logs 값처럼 아무 JSON이나 받되 너무 크면 거절"""
    r = Reader.root({"small": {"a": 1}, "big": "x" * 3000})
    assert r.json("small") == {"a": 1}
    assert r.json("big") is None
    assert len(r.issues) == 1


def test_string_pattern_and_bool_type_messages():
    """원본 read.ts의 pattern(형식이 맞지 않아요) · bool(true · false여야 해요) 문구 — 맞춤 확인"""
    r = Reader.root({"rule": "abc", "flag": "yes"})
    import re

    assert r.string("rule", pattern=re.compile(r"^\d+$")) is None
    assert r.bool("flag") is None
    assert r.issues == [
        {"path": "rule", "message": "형식이 맞지 않아요"},
        {"path": "flag", "message": "true · false여야 해요"},
    ]

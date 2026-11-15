"""요청 본문 읽기 — 본 레포 Edge `_shared/read.ts`(Reader · readJson)와 같은 문구 · 순서 · 경계 (JS와 달라지기 쉬운 곳 위주)"""

import json

import pytest

from app.core.errors import AppError
from app.core.read import Reader, clean_text, js_length, js_trim, read_json


def body(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def fails(raw: bytes) -> str:
    with pytest.raises(AppError) as info:
        read_json(raw)
    assert info.value.code == "VALIDATION_FAILED" and info.value.details is None
    return info.value.message


def test_read_json_failure_messages():
    assert fails(b"") == "요청 본문(JSON)이 비어 있어요"
    assert fails(b"  \n\t ") == "요청 본문(JSON)이 비어 있어요"
    assert fails(chr(0xFEFF).encode()) == "요청 본문(JSON)이 비어 있어요"  # BOM만 있어도 JS trim이 지움
    assert fails(b"{not json") == "요청 본문이 JSON 형식이 아니에요"
    assert fails(b"") != fails(b"{")
    assert read_json(b'{"a":1}') == {"a": 1}
    assert read_json(b"[1]") == [1]  # 배열도 JSON — 객체인지는 Reader.root가 따짐


@pytest.mark.parametrize("raw", [b'{"lat": NaN}', b'{"lat": Infinity}', b'{"lat": -Infinity}'])
def test_python_only_json_extensions_are_rejected_like_js(raw):
    assert fails(raw) == "요청 본문이 JSON 형식이 아니에요"


def test_leading_utf8_bom_is_removed_like_fetch_text_but_a_bom_inside_is_not():
    bom = chr(0xFEFF).encode("utf-8")
    assert read_json(bom + b'{"a":1}') == {
        "a": 1
    }  # Fetch 표준 req.text()는 맨 앞 BOM을 지움 → Edge는 정상 처리
    assert fails(bom) == "요청 본문(JSON)이 비어 있어요"
    assert fails(b" " + bom + b"{}") == "요청 본문이 JSON 형식이 아니에요"  # 본문 중간의 BOM은 JSON 문법 오류
    just = bom + b'{"query":"' + b"a" * (100_000 - 12) + b'"}'  # BOM은 글자 수에 안 셈
    read_json(just)


def test_clean_text_replaces_lone_surrogates_with_fffd_and_keeps_pairs():
    lone_high, lone_low, pair = chr(0xD83D), chr(0xDC00), chr(0xD83D) + chr(0xDE00)
    assert clean_text("a" + lone_high) == "a" + chr(0xFFFD)
    assert clean_text(lone_low + "b") == chr(0xFFFD) + "b"
    assert clean_text(pair) == chr(0x1F600) and clean_text("😀한글") == "😀한글"  # 정상 글자는 그대로


def test_body_size_limit_counts_utf16_units():
    just = b'{"query":"' + b"a" * (100_000 - 12) + b'"}'  # 정확히 100,000자
    assert len(just) == 100_000
    read_json(just)
    assert fails(just + b" ") == "요청이 너무 커요"
    # 이모지는 JS length가 2 — 50,000개 = 100,000 단위는 통과, 하나 더면 초과 (파이썬 len은 50,001)
    emoji_ok = ('"' + "😀" * 49_999 + '"').encode()  # 따옴표 2 + 99,998
    read_json(emoji_ok)
    assert fails(('"' + "😀" * 50_000 + '"').encode()) == "요청이 너무 커요"


def test_invalid_utf8_is_replaced_not_crashing():
    assert read_json(b'{"a":"\xff"}') == {"a": "�"}


def test_root_must_be_an_object():
    for value in ([1], "text", 5, None, True):
        with pytest.raises(AppError) as info:
            Reader.root(value)
        assert info.value.message == "요청 본문은 JSON 객체여야 해요"


def issues_of(root: Reader) -> list[dict]:
    with pytest.raises(AppError) as info:
        root.finish()
    assert info.value.message == "입력값을 확인해 주세요"
    return info.value.details["issues"]


def test_required_null_and_missing_are_the_same_problem():
    root = Reader.root({"a": None})
    root.string("a", required=True)
    root.number("b", required=True)
    assert issues_of(root) == [
        {"path": "a", "message": "필수 값이에요"},
        {"path": "b", "message": "필수 값이에요"},
    ]
    ok = Reader.root({})
    assert ok.string("a") is None and ok.number("b") is None
    ok.finish()  # 선택이면 없어도 문제 없음


def test_string_rules():
    r = Reader.root({"s": "  hi  ", "n": 5, "e": "   ", "long": "a" * 101, "emoji": "😀" * 51})
    assert r.string("s", min=1, max=100) == "hi"  # trim한 값
    assert r.string("n") is None
    assert r.string("e", min=1) is None
    assert r.string("long", max=100) is None
    assert r.string("emoji", max=100) is None  # JS length 102
    assert issues_of(r) == [
        {"path": "n", "message": "글자여야 해요"},
        {"path": "e", "message": "1자 이상이어야 해요"},
        {"path": "long", "message": "100자 이하여야 해요"},
        {"path": "emoji", "message": "100자 이하여야 해요"},
    ]


def test_string_length_boundary_is_inclusive():
    r = Reader.root({"a": "😀" * 50, "b": "x" * 100})
    assert r.string("a", max=100) == "😀" * 50 and r.string("b", max=100) == "x" * 100
    r.finish()


def test_js_trim_and_length_helpers():
    assert js_trim(chr(0xA0) + chr(0xFEFF) + chr(0x3000) + " x " + chr(0x2028)) == "x"
    assert js_trim("\x1cx\x1c") == "\x1cx\x1c"  # 파이썬 strip은 지우지만 JS trim은 안 지움
    assert js_length("😀") == 2 and js_length("한글") == 2 and js_length("") == 0
    assert js_length("\ud800") == 1  # 짝 없는 서로게이트도 안 죽음


def test_number_rules_messages_and_bool_is_not_a_number():
    r = Reader.root(
        {"t": True, "s": "37", "hi": 91, "lo": -181, "big": 10**400, "huge": 1e400, "ok": 37.5, "zero": 0}
    )
    assert r.number("t") is None and r.number("s") is None
    assert r.number("hi", min=-90, max=90) is None
    assert r.number("lo", min=-180, max=180) is None
    assert r.number("big") is None and r.number("huge") is None
    assert r.number("ok", min=-90, max=90) == 37.5 and r.number("zero", min=-90, max=90) == 0
    assert issues_of(r) == [
        {"path": "t", "message": "숫자여야 해요"},
        {"path": "s", "message": "숫자여야 해요"},
        {"path": "hi", "message": "90 이하여야 해요"},
        {"path": "lo", "message": "-180 이상이어야 해요"},
        {"path": "big", "message": "숫자여야 해요"},
        {"path": "huge", "message": "숫자여야 해요"},
    ]


def test_int_rules_5_point_0_is_an_integer_but_5_point_5_is_not():
    r = Reader.root({"a": 5.0, "b": 5.5, "c": 0, "d": 16, "e": 15, "f": 1})
    assert r.int("a", min=1, max=15) == 5 and isinstance(r.int("a", min=1, max=15), int)
    assert r.int("b", min=1, max=15) is None
    assert r.int("c", min=1, max=15) is None
    assert r.int("d", min=1, max=15) is None
    assert r.int("e", min=1, max=15) == 15 and r.int("f", min=1, max=15) == 1  # 경계는 포함
    assert issues_of(r) == [
        {"path": "b", "message": "정수여야 해요"},
        {"path": "c", "message": "1 이상이어야 해요"},
        {"path": "d", "message": "15 이하여야 해요"},
    ]


def test_problem_at_and_order_are_kept():
    r = Reader.root({"q": 1})
    r.string("q", required=True)
    r.problem_at("lat", "lat · lng는 같이 있어야 해요")
    assert [i["path"] for i in issues_of(r)] == ["q", "lat"]


def test_issues_never_echo_input_values():
    r = Reader.root({"query": "SECRET-SEARCH", "lat": "37.5509"})
    r.string("query", max=3)
    r.number("lat")
    text = json.dumps(issues_of(r), ensure_ascii=False)
    assert "SECRET-SEARCH" not in text and "37.5509" not in text


# ---- read.ts의 나머지 읽기 함수 (appointment-edit 요청 읽기가 씀) ----


def test_bool_one_of_uuid_date_object_and_is_null():
    import re
    from datetime import UTC, datetime

    from app.core.read import parse_iso

    uid = "11111111-1111-4111-8111-111111111ABC"
    r = Reader.root(
        {
            "flag": True,
            "bad_flag": "true",
            "pick": "A",
            "bad_pick": "Z",
            "id": uid,
            "bad_id": uid + "\n",
            "at": "2026-10-29T10:00:00+09:00",
            "bad_at": "2026-10-29T10:00:00",
            "obj": {"x": 1},
            "arr": [1],
            "nothing": None,
            "rule": "FREQ=DAILY",
            "bad_rule": "daily",
        }
    )
    assert r.bool("flag") is True
    assert r.bool("bad_flag") is None
    assert r.one_of("pick", ["A", "B"]) == "A"
    assert r.one_of("bad_pick", ["A", "B"]) is None
    assert r.uuid("id") == uid.lower()  # 소문자로
    assert r.uuid("bad_id") is None  # 끝 줄바꿈도 거절 (JS `$`와 같게)
    assert r.date("at") == datetime(2026, 10, 29, 1, 0, tzinfo=UTC)
    assert r.date("bad_at") is None
    assert r.object("obj").value == {"x": 1}
    assert r.object("arr") is None
    assert r.is_null("nothing") and not r.is_null("missing") and not r.is_null("flag")
    assert r.string("rule", pattern=re.compile(r"^FREQ=(DAILY|WEEKLY)$")) == "FREQ=DAILY"
    assert r.string("bad_rule", pattern=re.compile(r"^FREQ=(DAILY|WEEKLY)$")) is None
    assert [(i["path"], i["message"]) for i in r.issues] == [
        ("bad_flag", "true · false여야 해요"),
        ("bad_pick", "A · B 중 하나여야 해요"),
        ("bad_id", "UUID 형식이 아니에요"),
        ("bad_at", "시각은 오프셋이 있는 ISO 8601이어야 해요 (예: 2026-10-20T10:00:00+09:00)"),
        ("arr", "객체여야 해요"),
        ("bad_rule", "형식이 맞지 않아요"),
    ]
    assert parse_iso("2026-02-30T10:00:00Z") is None  # 없는 날짜
    assert parse_iso("2026-10-29T10:00Z") == datetime(2026, 10, 29, 10, 0, tzinfo=UTC)  # 초 생략 · Z

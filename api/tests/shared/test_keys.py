"""본 레포 `_shared/keys.test.ts`의 서버판 — 서비스 키 비교 도구"""

from starlette.datastructures import Headers

from app.shared.keys import key_candidates, matches_any_key, parse_key_dictionary, timing_safe_equal


def test_key_dictionary_values_only_broken_is_single_key():
    """키 사전(JSON)에서 값만 꺼냄, 깨진 값은 단일 키로 봄"""
    assert parse_key_dictionary('{"default":"sb_secret_a","other":"sb_secret_b"}') == [
        "sb_secret_a",
        "sb_secret_b",
    ]
    assert parse_key_dictionary(None) == []
    assert parse_key_dictionary("") == []
    assert parse_key_dictionary("sb_secret_plain") == ["sb_secret_plain"]
    assert parse_key_dictionary('{"a":1,"b":""}') == []


def test_key_dictionary_js_edges():
    """JS JSON.parse · typeof 'object' 동작과 맞춘 곳 — 배열은 값 목록, null · 숫자 · 글자는 빈 목록, NaN은 JSON이 아니라 단일 키"""
    assert parse_key_dictionary('["a","",1]') == ["a"]
    assert parse_key_dictionary("null") == []
    assert parse_key_dictionary("123") == []
    assert parse_key_dictionary('"abc"') == []
    assert parse_key_dictionary("NaN") == ["NaN"]


def test_timing_safe_equal():
    """같은 글자만 같다고 봄 (길이가 달라도 거절)"""
    assert timing_safe_equal("abc", "abc") is True
    assert timing_safe_equal("abc", "abd") is False
    assert timing_safe_equal("abc", "abcd") is False
    assert timing_safe_equal("키", "키") is True
    assert timing_safe_equal("키", "크") is False


def test_header_apikey_or_bearer_both_accepted():
    """apikey 머리글 · Authorization Bearer 어느 쪽에 실어도 받음 (배치는 둘 다, 새 키는 apikey만)"""
    keys = ["service-key"]
    assert matches_any_key(Headers({"apikey": "service-key"}), keys) is True
    assert matches_any_key(Headers({"authorization": "Bearer service-key"}), keys) is True
    assert (
        matches_any_key(Headers({"authorization": "Bearer service-key", "apikey": "service-key"}), keys)
        is True
    )
    assert (
        matches_any_key(Headers({"authorization": "Bearer user-token", "apikey": "anon-key"}), keys) is False
    )
    assert matches_any_key(Headers({}), keys) is False


def test_no_server_keys_means_nobody_passes():
    """서버에 키가 하나도 없으면 아무도 통과 못 함 (빈 키로 뚫리지 않음)"""
    assert matches_any_key(Headers({"apikey": ""}), [""]) is False
    assert matches_any_key(Headers({"apikey": "x"}), []) is False
    assert key_candidates(Headers({"authorization": "Basic abc"})) == []

"""`_shared/db.ts`(dbFail · must) — 시험 파일은 원본에 없음. 사용자에게 나가는 문구가 Edge와 같은지만 확인"""

import logging

import pytest

from app.core.errors import AppError
from app.shared.db import db_fail, must
from app.shared.supabase import Result


@pytest.mark.parametrize(
    ("code", "error_code", "message"),
    [
        ("42501", "FORBIDDEN", "이 작업을 할 권한이 없어요"),
        ("23505", "CONFLICT", "이미 같은 내용이 있어요. 새로고침한 뒤 다시 시도해 주세요"),
        ("XX000", "INTERNAL", "데이터를 처리하지 못했어요. 잠시 후 다시 시도해 주세요"),
        (None, "INTERNAL", "데이터를 처리하지 못했어요. 잠시 후 다시 시도해 주세요"),
    ],
)
def test_db_fail_maps_code(code, error_code, message):
    with pytest.raises(AppError) as info:
        db_fail("ctx", {"message": "relation secret_table failed", "code": code})
    assert info.value.code == error_code
    assert info.value.message == message
    assert "secret_table" not in info.value.message


def test_db_fail_logs_code_message_only(caplog):
    with caplog.at_level(logging.ERROR, logger="respec.db"), pytest.raises(AppError):
        db_fail("todos", {"message": "boom", "code": "23505", "details": "Key (a)=(1) exists"})
    assert "[db] todos: 23505 boom" in caplog.text
    # details에는 값(중복 키의 토큰 등)이 들어갈 수 있어 서버 로그엔 안 남김 (PR #10 리뷰)
    assert "Key (a)=(1)" not in caplog.text


def test_must_returns_data_or_raises():
    assert must("x", Result(data=[1, 2])) == [1, 2]
    assert must("x", Result(data=None)) is None
    with pytest.raises(AppError):
        must("x", Result(error={"message": "m", "code": "42501"}))

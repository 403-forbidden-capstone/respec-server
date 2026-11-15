"""본 레포 supabase/functions/_shared/db.ts의 서버판 (기준 b140f70) — DB 오류 → 서버 함수 오류

원문(테이블 · 칼럼 이름)은 서버 로그에만 남기고 사용자에게는 일반 문구 (8장 8.1).
로그에는 코드 · 메시지 · details만 (Edge console.error와 같음).
"""

import logging
import re
from collections.abc import Mapping
from typing import Any, NoReturn

from ..core.errors import AppError

log = logging.getLogger("respec.db")

_QUOTED = re.compile(r'"[^"]*"')


def safe_db_message(message: Any) -> str:
    """PostgREST 오류 문구에서 따옴표 안 값을 가림 — 22P02(`invalid input syntax for type uuid: "<값>"`)처럼 입력값이 들어오는 경우가 있어서.
    문구의 뜻(어떤 오류인지)은 남김"""
    return _QUOTED.sub('"…"', str(message or ""))


def db_fail(context: str, error: Mapping[str, Any]) -> NoReturn:
    code = error.get("code")
    # details에는 값(예: 중복 키의 토큰)이 들어갈 수 있어 서버 로그엔 안 남김 — 코드 · 문구만 (Edge는 details까지 찍었음)
    log.error("[db] %s: %s %s", context, code or "", safe_db_message(error.get("message")))
    if code == "42501":
        raise AppError("FORBIDDEN", "이 작업을 할 권한이 없어요")
    if code == "23505":
        raise AppError("CONFLICT", "이미 같은 내용이 있어요. 새로고침한 뒤 다시 시도해 주세요")
    raise AppError("INTERNAL", "데이터를 처리하지 못했어요. 잠시 후 다시 시도해 주세요")


def must(context: str, result: Any) -> Any:
    """supabase 결과(Result)에서 data만 꺼냄. error가 있으면 던짐"""
    if result.error:
        db_fail(context, result.error)
    return result.data

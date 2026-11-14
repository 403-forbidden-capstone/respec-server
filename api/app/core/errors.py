"""오류 모양 — 본 레포 `supabase/functions/_shared/errors.ts`와 같은 코드 · 상태 번호.

본문: {"error": {"code", "message", "details"?}} (details가 없으면 키 자체를 뺌)
앱(`mobile/src/lib/edge.ts`)이 code로 문구를 고르므로 이름 · 상태를 바꾸면 안 됨.
"""

from typing import Any

# 상태 번호는 TS ERROR_STATUS와 같게 (tests/test_errors.py가 표 전체를 대조)
ERROR_STATUS: dict[str, int] = {
    "VALIDATION_FAILED": 400,
    "AUTH_REQUIRED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "CONFLICT": 409,
    "UNPROCESSABLE": 422,
    "RATE_LIMITED": 429,
    "INTERNAL": 500,
    "ROUTE_UNAVAILABLE": 502,
    "PLACE_UNAVAILABLE": 502,
    "LLM_UNAVAILABLE": 502,
    "VOICE_UNAVAILABLE": 503,
    "NOT_CONFIGURED": 503,
}

# 서버에만 있는 코드 (Edge에는 없음) — 공개 키(JWKS)를 못 받고 캐시도 없어 토큰을 검사할 수 없을 때. 401이면 앱이 로그아웃시키므로 503
SERVER_ONLY_ERROR_STATUS: dict[str, int] = {
    "AUTH_UNAVAILABLE": 503,
}

ALL_ERROR_STATUS = {**ERROR_STATUS, **SERVER_ONLY_ERROR_STATUS}

# 상태 번호 → 대표 코드 (FastAPI · Starlette 기본 오류를 우리 모양으로 바꿀 때)
_CODE_BY_STATUS = {
    400: "VALIDATION_FAILED",
    401: "AUTH_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "UNPROCESSABLE",
    429: "RATE_LIMITED",
}

INTERNAL_MESSAGE = "잠시 후 다시 시도해 주세요"


class AppError(Exception):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        if code not in ALL_ERROR_STATUS:
            raise ValueError(f"알 수 없는 오류 코드: {code}")
        self.code = code
        self.message = message
        self.details = details

    @property
    def status(self) -> int:
        return ALL_ERROR_STATUS[self.code]


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


def code_for_status(status: int) -> str:
    return _CODE_BY_STATUS.get(status, "INTERNAL" if status >= 500 else "VALIDATION_FAILED")

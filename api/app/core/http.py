"""요청 · 응답 틀 — 본 레포 `_shared/http.ts` · `cors.ts`의 서버판.

Edge `serve()`가 하던 일을 맨 바깥 미들웨어 하나가 함:
  · OPTIONS는 바로 답함 (Edge는 200, 서버는 204 — 완결점 1-4)
  · X-Request-Id: 앱이 보낸 값이 짧고 안전한 글자일 때만 그대로 쓰고, 아니면 새로 만듦 (응답 · 로그에 그대로 찍히므로)
  · 모든 응답(오류 · 500 포함)에 CORS · X-Request-Id를 붙임
  · 예상 못 한 오류는 사용자에게 일반 문구. 로그에는 요청 번호 · 예외 종류 · 코드 위치(파일:줄:함수)만 남김
    (예외 메시지에는 좌표 · 검색어 · 키가 섞일 수 있어 원문은 안 남김 — 재현은 요청 번호로)
로그에는 메서드 · 경로(퍼센트 인코딩) · 상태 · 걸린 시간 · 요청 번호만 남김 — 토큰 · 키 · 좌표 · 검색어 · 본문 · 쿼리 문자열은 절대 안 남김
"""

import json
import logging
import os
import re
import time
import traceback
import uuid
from urllib.parse import quote

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import INTERNAL_MESSAGE, AppError, error_body

log = logging.getLogger("respec.http")

# `_shared/cors.ts`와 같은 허용 헤더 · 노출 헤더. 메서드는 서버에 GET(/me · /health)이 있어 GET을 더함
CORS_HEADERS: dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, idempotency-key, x-request-id",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Expose-Headers": "x-request-id",
    "Access-Control-Max-Age": "86400",
}

# `_shared/http.ts` REQUEST_ID_PATTERN과 같음
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_LONE_SURROGATE = re.compile(
    "[" + chr(0xD800) + "-" + chr(0xDFFF) + "]"
)  # 파이썬 str에는 짝 없는 서로게이트가 들어갈 수 있음


def request_id_of(sent: str | None) -> str:
    return sent if sent and REQUEST_ID_PATTERN.fullmatch(sent) else str(uuid.uuid4())


class EdgeJSONResponse(JSONResponse):
    """Edge `jsonResponse`와 같은 Content-Type (charset 포함), 한글을 \\u 로 풀어쓰지 않음"""

    media_type = JSON_CONTENT_TYPE

    def render(self, content: object) -> bytes:
        text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        # JS JSON.stringify는 짝 없는 서로게이트를 \udXXX 이스케이프로 내보냄 — 그대로 두면 UTF-8로 못 만들어 500
        text = _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)
        return text.encode("utf-8")


def error_response(error: AppError) -> EdgeJSONResponse:
    return EdgeJSONResponse(error_body(error.code, error.message, error.details), status_code=error.status)


def request_id_in(request: Request) -> str:
    return request.scope.get("state", {}).get("request_id", "-")


def _crash_location(exc: BaseException) -> str:
    """예외가 난 곳 — 안쪽부터 최대 5칸 `파일:줄:함수`. 예외 메시지 · 변수 값은 안 넣음"""
    frames = traceback.extract_tb(exc.__traceback__)[-5:]
    return " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in reversed(frames)) or "-"


class EdgeCompatMiddleware:
    """맨 바깥 미들웨어 (순수 ASGI) — 위 설명 참고"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_sent = Request(scope).headers.get("x-request-id")
        request_id = request_id_of(raw_sent)
        scope.setdefault("state", {})["request_id"] = request_id
        started_at = time.perf_counter()
        status_holder = {"status": 500}
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                status_holder["status"] = message["status"]
                headers = MutableHeaders(scope=message)
                for key, value in CORS_HEADERS.items():
                    headers.setdefault(key, value)
                headers["X-Request-Id"] = request_id
            await send(message)

        try:
            if scope["method"] == "OPTIONS":
                await Response(status_code=204)(scope, receive, send_with_headers)
            else:
                await self.app(scope, receive, send_with_headers)
        except Exception as exc:  # noqa: BLE001 — 예상 못 한 오류는 여기서 일반 문구로 바꿈
            log.error(
                "unexpected error request_id=%s type=%s at=%s",
                request_id,
                type(exc).__name__,
                _crash_location(exc),
            )
            # 응답이 이미 시작됐으면 더 보낼 수 없음 — 다시 던지면 서버(uvicorn)가 예외 메시지째 로그에 찍으므로 여기서 마무리
            if not started:
                await error_response(AppError("INTERNAL", INTERNAL_MESSAGE))(
                    scope, receive, send_with_headers
                )
        finally:
            log.info(
                "request_id=%s method=%s path=%s status=%s ms=%d",
                request_id,
                scope["method"],
                quote(scope["path"], safe="/"),  # 디코딩된 줄바꿈 등이 가짜 로그 줄이 되지 않게 퍼센트 인코딩
                status_holder["status"],
                (time.perf_counter() - started_at) * 1000,
            )

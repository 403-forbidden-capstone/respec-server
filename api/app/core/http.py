"""요청 · 응답 틀 — 본 레포 `_shared/http.ts` · `cors.ts`의 서버판.

Edge `serve()`가 하던 일을 맨 바깥 미들웨어 하나가 함:
  · OPTIONS는 바로 답함 (Edge는 200, 서버는 204 — 완결점 1-4)
  · X-Request-Id: 앱이 보낸 값이 짧고 안전한 글자일 때만 그대로 쓰고, 아니면 새로 만듦 (응답 · 로그에 그대로 찍히므로)
  · 모든 응답(오류 · 500 포함)에 CORS · X-Request-Id를 붙임
  · 예상 못 한 오류는 원문을 로그에만(요청 번호와 함께) 남기고 사용자에게는 일반 문구
로그에는 메서드 · 경로 · 상태 · 걸린 시간 · 요청 번호만 남김 — 토큰 · 키 · 좌표 · 검색어 · 본문 · 쿼리 문자열은 절대 안 남김
"""

import json
import logging
import re
import time
import uuid

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


def request_id_of(sent: str | None) -> str:
    return sent if sent and REQUEST_ID_PATTERN.fullmatch(sent) else str(uuid.uuid4())


class EdgeJSONResponse(JSONResponse):
    """Edge `jsonResponse`와 같은 Content-Type (charset 포함), 한글을 \\u 로 풀어쓰지 않음"""

    media_type = JSON_CONTENT_TYPE

    def render(self, content: object) -> bytes:
        return json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def error_response(error: AppError) -> EdgeJSONResponse:
    return EdgeJSONResponse(error_body(error.code, error.message, error.details), status_code=error.status)


def request_id_in(request: Request) -> str:
    return request.scope.get("state", {}).get("request_id", "-")


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
        except Exception:  # noqa: BLE001 — 예상 못 한 오류는 여기서 일반 문구로 바꿈
            # 원문은 로그에만, 요청 본문 · 헤더는 안 찍음 (예외 메시지에 값이 섞일 수 있어 스택은 남기되 값 꺼내 쓰지 않음)
            log.exception("unexpected error request_id=%s", request_id)
            if started:
                raise
            await error_response(AppError("INTERNAL", INTERNAL_MESSAGE))(scope, receive, send_with_headers)
        finally:
            log.info(
                "request_id=%s method=%s path=%s status=%s ms=%d",
                request_id,
                scope["method"],
                scope["path"],
                status_holder["status"],
                (time.perf_counter() - started_at) * 1000,
            )

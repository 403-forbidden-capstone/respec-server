"""앱 조립 — `uvicorn app.main:create_app --factory`

설정은 환경 변수(`app/core/config.py`). 시험은 create_app(settings, transport=...)로 가짜 설정 · 가짜 네트워크를 넣음.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core.config import Settings
from .core.errors import AppError, code_for_status, error_body
from .core.http import EdgeCompatMiddleware, EdgeJSONResponse, error_response
from .core.jwks import JwksCache
from .routers import alarm_commit, alarm_roll, alarm_suggest, appointment_edit, health, me, places
from .shared import supabase as shared_supabase


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or Settings()  # 환경 변수에서
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx는 나가는 요청의 URL(쿼리 포함)을 INFO로 찍음 → 이후 카카오 호출의 좌표 · 검색어가 로그에 남으므로 끔
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = httpx.AsyncClient(timeout=settings.http_timeout_seconds, transport=transport)
        app.state.http = client
        shared_supabase.configure(
            settings, client
        )  # shared Db(PostgREST)가 같은 설정 · 같은 httpx 클라이언트를 씀 (W0)
        app.state.deep_lock = asyncio.Lock()
        app.state.deep_cache = None  # (시각, 본문, 상태) — /health/deep 짧은 캐시
        app.state.jwks = JwksCache(
            client,
            settings.jwks_url,
            ttl_seconds=settings.jwks_cache_ttl_seconds,
            min_refetch_seconds=settings.jwks_min_refetch_seconds,
        )
        try:
            yield
        finally:
            shared_supabase.configure(None, None)
            await client.aclose()

    # /docs · /redoc · /openapi.json은 끔 (5장 규약) → 404가 Edge 모양으로 나감
    # redirect_slashes=False: `/me/` 같은 요청을 307로 돌리지 않고 404 (Edge 모양). default_response_class: 성공 응답도 Edge와 같은 Content-Type
    app = FastAPI(
        title="respec-api",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        default_response_class=EdgeJSONResponse,
    )
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> EdgeJSONResponse:
        return error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> EdgeJSONResponse:
        # FastAPI 기본 404 · 405 · … 를 Edge 모양으로. 메시지는 Edge의 한국어 문구 · 원문(detail)은 안 씀
        code = code_for_status(exc.status_code)
        messages = {404: "찾을 수 없어요", 405: "허용되지 않는 요청이에요"}
        if exc.status_code == 405 and request.url.path.startswith("/fn/"):
            messages[405] = "POST만 받아요"  # Edge serve()의 문구 — 함수 주소(/fn/*)는 POST만 받음
        # 상태 번호는 원래 것을 그대로 (표에 없는 413 등을 500 · 400으로 바꾸지 않음). 본문 코드만 Edge 표에서 고름
        body = error_body(code, messages.get(exc.status_code, "요청을 처리하지 못했어요"))
        return EdgeJSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> EdgeJSONResponse:
        # FastAPI 기본은 422 + 입력값까지 되돌려 줌 → Edge의 VALIDATION_FAILED 400. 입력값(좌표 · 검색어 등)은 싣지 않고 위치와 종류만
        fields = [{"loc": [str(p) for p in e["loc"]], "type": e["type"]} for e in exc.errors()]
        return error_response(
            AppError("VALIDATION_FAILED", "입력한 내용을 다시 확인해 주세요", {"fields": fields})
        )

    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(places.router)
    app.include_router(alarm_suggest.router)
    app.include_router(alarm_commit.router)
    app.include_router(alarm_roll.router)
    app.include_router(appointment_edit.router)
    app.add_middleware(EdgeCompatMiddleware)  # 맨 바깥 (마지막에 더한 것이 가장 바깥)
    return app

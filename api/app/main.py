"""앱 조립 — `uvicorn app.main:create_app --factory`

설정은 환경 변수(`app/core/config.py`). 시험은 create_app(settings, transport=...)로 가짜 설정 · 가짜 네트워크를 넣음.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .core.config import Settings
from .core.errors import AppError, code_for_status
from .core.http import EdgeCompatMiddleware, EdgeJSONResponse, error_response
from .core.jwks import JwksCache
from .routers import health, me


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or Settings()  # 환경 변수에서
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = httpx.AsyncClient(timeout=settings.http_timeout_seconds, transport=transport)
        app.state.http = client
        app.state.jwks = JwksCache(
            client,
            settings.jwks_url,
            ttl_seconds=settings.jwks_cache_ttl_seconds,
            min_refetch_seconds=settings.jwks_min_refetch_seconds,
        )
        try:
            yield
        finally:
            await client.aclose()

    # /docs · /redoc · /openapi.json은 끔 (5장 규약) → 404가 Edge 모양으로 나감
    app = FastAPI(title="respec-api", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> EdgeJSONResponse:
        return error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> EdgeJSONResponse:
        # FastAPI 기본 404 · 405 · … 를 Edge 모양으로. 메시지는 Edge의 한국어 문구 · 원문(detail)은 안 씀
        code = code_for_status(exc.status_code)
        messages = {404: "찾을 수 없어요", 405: "허용되지 않는 요청이에요"}
        response = error_response(AppError(code, messages.get(exc.status_code, "요청을 처리하지 못했어요")))
        if exc.headers:  # 405의 Allow 헤더 등
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> EdgeJSONResponse:
        # FastAPI 기본은 422 + 입력값까지 되돌려 줌 → Edge의 VALIDATION_FAILED 400. 입력값(좌표 · 검색어 등)은 싣지 않고 위치와 종류만
        fields = [{"loc": [str(p) for p in e["loc"]], "type": e["type"]} for e in exc.errors()]
        return error_response(
            AppError("VALIDATION_FAILED", "입력한 내용을 다시 확인해 주세요", {"fields": fields})
        )

    app.include_router(health.router)
    app.include_router(me.router)
    app.add_middleware(EdgeCompatMiddleware)  # 맨 바깥 (마지막에 더한 것이 가장 바깥)
    return app

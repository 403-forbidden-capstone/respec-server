"""스케줄러의 비동기 다리 — 동기 틱 루프(scheduler.py)가 async 작업 본문(jobs_server · jobs_roll)을 부르는 자리

스케줄러는 1초 틱의 동기 루프라 이벤트 루프가 없음. 틱마다 `asyncio.run()`을 새로 열면 공용 httpx 클라이언트(shared/supabase._http)가
처음 쓰인 루프에 묶인 채 그 루프가 닫혀 다음 틱에서 `Event loop is closed`로 죽음 (리뷰 지적).
→ 프로세스가 시작할 때 루프 하나와 AsyncClient 하나를 만들어 `supabase.configure(settings, client)`로 넣고,
   작업은 그 루프에서 `run_until_complete`로 돌리고, 종료할 때 클라이언트를 닫음. 루프는 프로세스와 수명이 같음.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

import httpx

from .core.config import Settings
from .shared import supabase as shared_supabase


class AsyncRuntime:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def started(self) -> bool:
        return self._loop is not None

    def start(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        """루프 · 클라이언트를 만들어 shared Db가 쓰게 함. client는 시험이 MockTransport를 넣는 자리"""
        if self._loop is not None:
            raise RuntimeError("async runtime이 이미 시작됨")
        self._loop = asyncio.new_event_loop()
        self._client = client or httpx.AsyncClient(timeout=settings.http_timeout_seconds)
        shared_supabase.configure(settings, self._client)

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """작업 하나를 끝까지 돌리고 결과(또는 예외)를 돌려줌"""
        if self._loop is None:
            coro.close()  # "never awaited" 경고를 막음
            raise RuntimeError("async runtime이 시작되지 않음")
        return self._loop.run_until_complete(coro)

    def close(self) -> None:
        loop, client = self._loop, self._client
        self._loop = None
        self._client = None
        if loop is None:
            return
        try:
            if client is not None:
                loop.run_until_complete(client.aclose())
        finally:
            shared_supabase.configure(None, None)
            loop.close()


runtime = AsyncRuntime()

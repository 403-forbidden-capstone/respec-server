"""푸시 시험 도우미 — Expo를 가짜로 바꿔 끼움 (실제 푸시는 절대 안 나감). TS 시험의 `fetchImpl` · `new Response(...)`에 해당.

fetch_impl(url, *, method, headers, body, timeout) → httpx.Response. 호출은 `Calls`에 남김.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx


async def no_sleep(ms: int) -> None:
    return None


def response(body: Any = None, status: int = 200, *, raw: str | None = None) -> httpx.Response:
    """new Response(JSON.stringify(body), { status }) — raw를 주면 JSON이 아닌 본문 그대로"""
    if raw is not None:
        return httpx.Response(status, content=raw.encode("utf-8"))
    return httpx.Response(status, content=json.dumps(body).encode("utf-8"))


class Seen:
    """fetch_impl가 받은 요청 한 건"""

    def __init__(self, url: str, method: str, headers: dict[str, str], body: str, timeout: float) -> None:
        self.url = url
        self.method = method
        self.headers = headers
        self.text = body
        self.timeout = timeout

    @property
    def json(self) -> Any:
        return json.loads(self.text)


def fake_fetch(handler: Callable[[Seen], Any], seen: list[Seen] | None = None):
    """handler(요청) → httpx.Response, 또는 Exception을 돌려주면 던짐. async 함수여도 됨"""
    log = seen if seen is not None else []

    async def fetch_impl(url: str, *, method: str = "POST", headers: dict, body: str, timeout: float):
        request = Seen(url, method, headers, body, timeout)
        log.append(request)
        out = handler(request)
        if hasattr(out, "__await__"):
            out = await out
        if isinstance(out, BaseException):
            raise out
        return out

    fetch_impl.seen = log  # type: ignore[attr-defined]
    return fetch_impl


def reply(body: Any, status: int = 200):
    """항상 같은 응답 (TS `reply`)"""
    return fake_fetch(lambda _req: response(body, status))


def replies_ok(tickets: Callable[[str, int], Any] | None = None, seen: list[Seen] | None = None):
    """요청 메시지마다 ticket을 만들어 돌려줌. tickets(to, i) 기본: ok + id"""

    def make(to: str, i: int) -> Any:
        return {"status": "ok", "id": f"ticket-{to[-6:-1]}"}

    pick = tickets or make

    def handler(req: Seen) -> httpx.Response:
        body = req.json
        return response({"data": [pick(b["to"], i) for i, b in enumerate(body)]})

    return fake_fetch(handler, seen)

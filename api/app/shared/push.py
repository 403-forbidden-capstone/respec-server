"""본 레포 supabase/functions/_shared/push.ts의 서버판 (기준 b140f70) — Expo Push 보내기 · 영수증 가져오기

  · 요청: 메시지 배열(최대 100개), Authorization: Bearer EXPO_ACCESS_TOKEN은 선택 (Expo의 "강화된 푸시 보안"을 켰을 때)
  · 응답: { data: [{ status: 'ok', id } | { status: 'error', message, details: { error } }] } — 요청 순서 그대로
      details.error: DeviceNotRegistered(토큰 만료 → 지움) · MessageTooBig · MessageRateExceeded · InvalidCredentials(FCM 키 없음 → 설정 안 됨)
  · HTTP 401 · 403: EXPO_ACCESS_TOKEN이 틀렸거나 "강화된 푸시 보안"을 켰는데 토큰이 없음 → 이것도 설정 문제(PUSH_NOT_CONFIGURED). 장애(PUSH_UNAVAILABLE)
  · 접수(ticket) 번호: 접수가 됐다는 뜻일 뿐 기기에 닿았다는 뜻이 아님 → 15분쯤 뒤 getReceipts로 확인 (push_receipts.py)
  · 네트워크 오류 · 5xx는 한 번 더 보내 봄. 401 · 403은 다시 보내도 같아서 안 함
TS의 fetch 바꿔 끼우기(FetchLike)는 `fetch_impl`로: `await fetch_impl(url, method="POST", headers=…, body=<JSON 글자>, timeout=<초>)` →
httpx.Response 모양(`status_code` · `json()`)을 돌려주는 함수. 시험은 이것을 가짜로 바꿔 끼움 (실제 푸시는 안 나감).
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

# 메시지 = {title, body, data?, to, channelId?, silent?} (TS PushMessage · OutgoingMessage — JSON 키 그대로)
PushMessage = dict[str, Any]
OutgoingMessage = dict[str, Any]
# PushResult = {sent, failed, reason?, invalidTokens} · SendResult = PushResult + {tickets: [{token, ticketId}]}
PushResult = dict[str, Any]
SendResult = dict[str, Any]
PushTicket = dict[str, str]
ExpoReceipt = dict[str, Any]

FetchLike = Callable[..., Awaitable[Any]]
SleepLike = Callable[[int], Awaitable[None]]  # 밀리초


@dataclass
class SendOptions:
    access_token: str | None = None
    fetch_impl: FetchLike | None = None
    retries: int | None = None  # 실패한 묶음을 다시 보내 보는 횟수 (기본 1)
    sleep: SleepLike | None = None  # 다시 보내기 전 쉬는 시간 (시험이 바꿔 끼움)
    # 한 번 보내는 데 기다리는 최대 시간 (기본 10초) — Expo가 응답을 안 줘도 깨우기 요청 · 5분 배치가 매달리지 않게
    timeout_ms: int | None = None


EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"
BATCH = 100
RECEIPT_BATCH = 300
RETRY_WAIT_MS = 500
REQUEST_TIMEOUT_MS = 10_000
EXPO_TOKEN = re.compile(r"Expo(?:nent)?PushToken\[[^\]]+\]")  # fullmatch로 씀 (JS ^…$)


async def _default_sleep(ms: int) -> None:
    await asyncio.sleep(ms / 1000)


async def _default_fetch(
    url: str, *, method: str = "POST", headers: dict[str, str], body: str, timeout: float
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(method, url, headers=headers, content=body.encode("utf-8"))


def _json_text(value: Any) -> str:
    """JS JSON.stringify — 공백 없이. UTF-16 반쪽 글자(이름을 잘라 생긴)가 있으면 utf-8로 못 쓰므로 \\u 이스케이프로"""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return text


def headers_of(access_token: str | None = None) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        **({"Authorization": f"Bearer {access_token}"} if access_token else {}),
    }


def _ok(status: int) -> bool:
    return 200 <= status < 300  # fetch Response.ok


def _data_of(parsed: Any) -> Any:
    """`json && typeof json === 'object' ? json.data : undefined` — 배열이면 data 없음"""
    return parsed.get("data") if isinstance(parsed, dict) else None


async def _fetch_json(
    do_fetch: FetchLike, url: str, access_token: str | None, body: Any, timeout_ms: int
) -> tuple[int, Any]:
    """요청 하나 — 시간 초과 · 네트워크 오류는 예외로 올라감. 본문이 JSON이 아니면 None (res.json().catch(() => null))"""

    async def go() -> tuple[int, Any]:
        res = await do_fetch(
            url,
            method="POST",
            headers=headers_of(access_token),
            body=_json_text(body),
            timeout=timeout_ms / 1000,
        )
        try:
            parsed = res.json()
        except Exception:  # noqa: BLE001 — JSON이 아님
            parsed = None
        return res.status_code, parsed

    return await asyncio.wait_for(go(), timeout_ms / 1000)  # AbortSignal.timeout


async def _post_chunk(
    do_fetch: FetchLike, access_token: str | None, body: list[Any], timeout_ms: int
) -> tuple[list[Any] | None, bool]:
    """한 묶음을 한 번 보냄. (tickets, rejectedByAuth) — tickets가 None이면 실패(이유: 인증이 거절됐는지 아니면 장애인지)"""
    try:
        status, parsed = await _fetch_json(do_fetch, EXPO_PUSH_URL, access_token, body, timeout_ms)
        data = _data_of(parsed)
        if _ok(status) and isinstance(data, list):
            return data, False
        return None, status in (401, 403)
    except Exception:  # noqa: BLE001 — 네트워크 오류 · 시간 초과 — 호출한 쪽이 이 묶음을 실패로 셈
        return None, False


def _body_of(m: OutgoingMessage) -> dict[str, Any]:
    data = m.get("data")
    if data is None:
        data = {}
    if m.get("silent"):
        return {"to": m["to"], "data": data, "priority": "high", "_contentAvailable": True}
    return {
        "to": m["to"],
        "title": m["title"],
        "body": m["body"],
        "data": data,
        "sound": "default",
        "priority": "high",
        **({"channelId": m["channelId"]} if m.get("channelId") else {}),
    }


async def send_expo_messages(
    messages: list[OutgoingMessage] | tuple[OutgoingMessage, ...], options: SendOptions | None = None
) -> SendResult:
    options = options or SendOptions()
    result: SendResult = {"sent": 0, "failed": 0, "invalidTokens": [], "tickets": []}
    valid = [m for m in messages if EXPO_TOKEN.fullmatch(m["to"])]
    result["failed"] += len(messages) - len(valid)  # Expo 토큰 모양이 아닌 것은 보내지 않음
    do_fetch = options.fetch_impl or _default_fetch
    sleep = options.sleep or _default_sleep
    retries = 1 if options.retries is None else options.retries
    timeout_ms = REQUEST_TIMEOUT_MS if options.timeout_ms is None else options.timeout_ms
    not_configured = 0
    unavailable = 0

    for i in range(0, len(valid), BATCH):
        chunk = valid[i : i + BATCH]
        body = [_body_of(m) for m in chunk]
        tickets, rejected = await _post_chunk(do_fetch, options.access_token, body, timeout_ms)
        attempt = 1
        while tickets is None and not rejected and attempt <= retries:
            await sleep(RETRY_WAIT_MS * attempt)
            tickets, rejected = await _post_chunk(do_fetch, options.access_token, body, timeout_ms)
            attempt += 1
        if tickets is None:
            result["failed"] += len(chunk)
            if rejected:
                not_configured += len(chunk)
            else:
                unavailable += len(chunk)
            continue
        for j, m in enumerate(chunk):
            raw = tickets[j] if j < len(tickets) else None
            t = raw if isinstance(raw, dict) else {}
            if t.get("status") == "ok":
                result["sent"] += 1
                if isinstance(t.get("id"), str):
                    result["tickets"].append({"token": m["to"], "ticketId": t["id"]})
                continue
            result["failed"] += 1
            details = t.get("details")
            error = details.get("error") if isinstance(details, dict) else None
            if error == "DeviceNotRegistered":
                result["invalidTokens"].append(m["to"])
            if error in ("InvalidCredentials", "MismatchSenderId"):
                not_configured += 1

    if result["sent"] == 0 and result["failed"] > 0:
        if not_configured > 0:
            result["reason"] = "PUSH_NOT_CONFIGURED"
        elif unavailable > 0:
            result["reason"] = "PUSH_UNAVAILABLE"
    return result


async def send_expo_push(
    tokens: list[str] | tuple[str, ...], message: PushMessage, options: SendOptions | None = None
) -> PushResult:
    """같은 문구를 여러 토큰에 (옛 모양 — 다시 보내지 않고 접수 번호도 안 돌려줌). 새 코드는 send_expo_messages"""
    options = options or SendOptions()
    retries = 0 if options.retries is None else options.retries
    full = await send_expo_messages(
        [{**message, "to": to} for to in tokens],
        SendOptions(options.access_token, options.fetch_impl, retries, options.sleep, options.timeout_ms),
    )
    return {k: v for k, v in full.items() if k != "tickets"}


# ---- 영수증 -----------------------------------------------------------------------------------


async def fetch_expo_receipts(
    ids: list[str] | tuple[str, ...],
    *,
    access_token: str | None = None,
    fetch_impl: FetchLike | None = None,
) -> dict[str, Any]:
    """접수 번호들의 영수증 {ok, receipts}. 응답에 없는 번호는 아직 준비되지 않은 것. 요청이 하나라도 실패하면 ok False (받은 것까지는 돌려줌)"""
    out: dict[str, Any] = {"ok": True, "receipts": {}}
    do_fetch = fetch_impl or _default_fetch
    for i in range(0, len(ids), RECEIPT_BATCH):
        try:
            status, parsed = await _fetch_json(
                do_fetch,
                EXPO_RECEIPTS_URL,
                access_token,
                {"ids": list(ids[i : i + RECEIPT_BATCH])},
                REQUEST_TIMEOUT_MS,
            )
            data = _data_of(parsed)
            if _ok(status) and isinstance(data, dict):
                out["receipts"].update(data)
            else:
                out["ok"] = False
        except Exception:  # noqa: BLE001
            out["ok"] = False
    return out

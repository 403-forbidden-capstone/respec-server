"""본 레포 supabase/functions/_shared/push-receipts.ts의 서버판 (기준 b140f70) — 푸시 영수증 확인 (wake-check 5분 배치가 함께 돌림)

Expo가 접수한 알림은 push_tickets에 접수 번호로 남음(notify.py). 15분쯤 뒤 영수증을 받아서
  · DeviceNotRegistered(앱을 지웠거나 기기가 바뀜) → 그 토큰을 push_tokens에서 지움. 받는 사람이 앱을 열면 "알림이 꺼져 있어요 [켜기]"
  · 확인한 줄은 표에서 지움 (표가 불어나지 않게)
영수증이 아직 없으면(Expo가 준비 전) 줄을 남겨 다음 배치에 다시, 24시간이 지나도 없으면 포기하고 지움
Expo 요청이나 DB가 실패해도 던지지 않고 로그만 — 같은 배치의 MISSED 기록을 막지 않음
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from .js import from_ms, ms, parse_iso, to_iso
from .push import FetchLike, fetch_expo_receipts
from .supabase import Db

log = logging.getLogger("respec.push_receipts")

RECEIPT_DELAY_MS = 15 * 60_000
TICKET_MAX_AGE_MS = 24 * 3_600_000
MAX_PER_RUN = 1000
# 주소(URL)에 실리는 값이 길면 요청이 거절되므로 지울 때는 100개씩 (한 번 거절되면 같은 줄을 영영 다시 잡음)
DELETE_CHUNK = 100


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _status_of(receipt: Any) -> Any:
    """JS `receipt.status` — null · undefined 위에서는 TypeError를 던짐 (바깥 try가 잡아 "아무것도 안 함"으로)"""
    if receipt is None:
        raise TypeError("영수증이 null")
    return receipt.get("status") if isinstance(receipt, dict) else None


async def run_receipt_check(
    service: Db,
    now: datetime,
    *,
    access_token: str | None = None,
    fetch_impl: FetchLike | None = None,
) -> dict[str, int]:
    """{checked, tokensRemoved, expired}"""
    nothing = {"checked": 0, "tokensRemoved": 0, "expired": 0}
    try:
        due = await (
            service.from_("push_tickets")
            .select("ticket_id, token, created_at")
            .lte("created_at", to_iso(from_ms(ms(now) - RECEIPT_DELAY_MS)))
            .order("created_at", ascending=True)
            .limit(MAX_PER_RUN)
        )
        if due.error:
            log.error("[wake-check] push_tickets 조회 실패 %s", due.error.get("message"))
            return nothing
        rows: list[dict[str, Any]] = due.data or []
        if not rows:
            return nothing

        # 영수증 요청이 일부 실패해도 받은 것은 씀. 못 받은 번호는 영수증이 아직 없는 것과 같이 다음 배치로
        fetched = await fetch_expo_receipts(
            [r["ticket_id"] for r in rows], access_token=access_token, fetch_impl=fetch_impl
        )
        ok, receipts = fetched["ok"], fetched["receipts"]
        resolved = [r for r in rows if r["ticket_id"] in receipts]
        # 앱이 지워진 토큰 → 그 토큰으로 보낸 가장 늦은 접수 시각 (그 뒤에 다시 등록된 토큰은 지우지 않으려고)
        gone: dict[str, str] = {}
        for r in resolved:
            receipt = receipts[r["ticket_id"]]
            if _status_of(receipt) != "error":
                continue
            details = receipt.get("details")
            error = details.get("error") if isinstance(details, dict) else None
            if error == "DeviceNotRegistered":
                prev = gone.get(r["token"])
                if not prev or ms(parse_iso(r["created_at"])) > ms(parse_iso(prev)):
                    gone[r["token"]] = r["created_at"]
            else:
                # Edge는 receipt.message도 찍었지만, Expo 문구에 푸시 토큰이 들어갈 수 있어 서버 로그엔 오류 코드만 (로그 규칙)
                log.error("[wake-check] 푸시 영수증 오류 %s", error or "unknown")
        expired = [
            r
            for r in rows
            if r["ticket_id"] not in receipts and ms(now) - ms(parse_iso(r["created_at"])) > TICKET_MAX_AGE_MS
        ]

        # 영수증이 늦게 오는 사이 같은 토큰이 다시 등록됐으면(last_seen_at이 접수 시각보다 늦음) 지우지 않음. 한 번에 10개씩 병렬로
        for part in _chunks(list(gone.items()), 10):
            dels = await asyncio.gather(
                *(
                    service.from_("push_tokens").delete().eq("token", token).lte("last_seen_at", at)
                    for token, at in part
                )
            )
            for dele in dels:
                if dele.error:
                    log.error("[wake-check] 만료 토큰 삭제 실패 %s", dele.error.get("message"))
        finished = [r["ticket_id"] for r in [*resolved, *expired]]
        for part in _chunks(finished, DELETE_CHUNK):
            dele = await service.from_("push_tickets").delete().in_("ticket_id", part)
            if dele.error:
                log.error("[wake-check] 영수증 표 정리 실패 %s", dele.error.get("message"))
        return {
            "checked": len(rows) if ok else len(resolved),
            "tokensRemoved": len(gone),
            "expired": len(expired),
        }
    except Exception as e:  # noqa: BLE001 — 영수증 확인은 같은 배치의 MISSED 기록을 막지 않음
        log.error("[wake-check] 영수증 확인 실패 %s", getattr(e, "code", None) or type(e).__name__)
        return nothing

"""본 레포 `_shared/push-receipts.test.ts`의 서버판 시험 — 15분 뒤 확인해 앱이 지워진 기기의 토큰을 지우고 표를 정리 (Expo 가짜)"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from app.shared.js import to_iso
from app.shared.push_receipts import RECEIPT_DELAY_MS, TICKET_MAX_AGE_MS, run_receipt_check
from tests.fake_db import FakeDb

from .push_support import fake_fetch, response

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
TA = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
TB = "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"
TC = "ExponentPushToken[cccccccccccccccccccccc]"


def ago(ms: int) -> str:
    return to_iso(NOW - timedelta(milliseconds=ms))


def run(coro):
    return asyncio.run(coro)


def setup():
    db = FakeDb()
    registered = ago(24 * 3_600_000)  # 등록한 지 오래된 토큰 (영수증 접수 시각보다 앞)
    db.rows("push_tokens").extend(
        [
            {"user_id": A, "token": TA, "last_seen_at": registered},
            {"user_id": B, "token": TB, "last_seen_at": registered},
            {"user_id": B, "token": TC, "last_seen_at": registered},
        ]
    )
    return db


def receipts_of(mapping: dict):
    def handler(req):
        ids = req.json["ids"]
        return response({"data": {i: mapping[i] for i in ids if i in mapping}})

    return fake_fetch(handler)


def ticket(ticket_id, user, token, kind, created_at):
    return {"ticket_id": ticket_id, "user_id": user, "token": token, "kind": kind, "created_at": created_at}


GONE = {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}


def test_15분_지난_접수_번호만_확인함():
    """15분 지난 접수 번호만 확인함 — 더 최근 것은 그대로 두고, 확인한 줄은 표에서 지움 (11-07)"""
    db = setup()
    db.rows("push_tickets").extend(
        [
            ticket("old", A, TA, "WAKE_PING", ago(RECEIPT_DELAY_MS + 1000)),
            ticket("fresh", B, TB, "WAKE_PING", ago(RECEIPT_DELAY_MS - 60_000)),
        ]
    )
    asked: list[str] = []
    inner = receipts_of({"old": {"status": "ok"}, "fresh": {"status": "ok"}})

    async def fetch(url, **kw):
        asked.extend(json.loads(kw["body"])["ids"])
        return await inner(url, **kw)

    result = run(run_receipt_check(db, NOW, fetch_impl=fetch))
    assert asked == ["old"]
    assert result == {"checked": 1, "tokensRemoved": 0, "expired": 0}
    assert [t["ticket_id"] for t in db.rows("push_tickets")] == ["fresh"]
    assert len(db.rows("push_tokens")) == 3


def test_11_06_영수증이_devicenotregistered면_그_토큰을_지움():
    """11-06: 영수증이 DeviceNotRegistered면 그 토큰을 push_tokens에서 지움 (다른 오류는 토큰을 건드리지 않고, 같은 사람의 다른 폰도 그대로)"""
    db = setup()
    db.rows("push_tickets").extend(
        [
            ticket("t-a", A, TA, "FRIEND_REQUEST", ago(RECEIPT_DELAY_MS + 1000)),
            ticket("t-b", B, TB, "FRIEND_REQUEST", ago(RECEIPT_DELAY_MS + 1000)),
            ticket("t-c", B, TC, "FRIEND_REQUEST", ago(RECEIPT_DELAY_MS + 1000)),
        ]
    )
    fetch = receipts_of(
        {
            "t-a": GONE,
            "t-b": {"status": "ok"},
            "t-c": {"status": "error", "message": "big", "details": {"error": "MessageTooBig"}},
        }
    )
    result = run(run_receipt_check(db, NOW, fetch_impl=fetch))
    assert result == {"checked": 3, "tokensRemoved": 1, "expired": 0}
    assert sorted(t["token"] for t in db.rows("push_tokens")) == [TB, TC]  # 앱을 지운 기기(A)만 사라짐
    assert len(db.rows("push_tickets")) == 0


def test_영수증이_아직_없으면_줄을_남기고_24시간이_지나면_포기하고_지움():
    """영수증이 아직 없으면(Expo가 준비 전) 줄을 남겨 다음 배치에 다시 확인하고, 24시간이 지나도 없으면 포기하고 지움"""
    db = setup()
    db.rows("push_tickets").extend(
        [
            ticket("wait", A, TA, "WAKE_PING", ago(RECEIPT_DELAY_MS + 1000)),
            ticket("give-up", B, TB, "WAKE_PING", ago(TICKET_MAX_AGE_MS + 1000)),
        ]
    )
    result = run(run_receipt_check(db, NOW, fetch_impl=receipts_of({})))
    assert result == {"checked": 2, "tokensRemoved": 0, "expired": 1}
    assert [t["ticket_id"] for t in db.rows("push_tickets")] == ["wait"]
    assert len(db.rows("push_tokens")) == 3


def test_expo_영수증_요청이_실패하면_아무것도_지우지_않고_미룸():
    """Expo 영수증 요청이 실패하면 아무것도 지우지 않고 다음 배치로 미룸 (오류로 던지지 않음)"""
    db = setup()
    db.rows("push_tickets").append(ticket("x", A, TA, "WAKE_PING", ago(RECEIPT_DELAY_MS + 1000)))
    result = run(run_receipt_check(db, NOW, fetch_impl=fake_fetch(lambda req: response({}, 503))))
    assert result == {"checked": 0, "tokensRemoved": 0, "expired": 0}
    assert len(db.rows("push_tickets")) == 1
    assert len(db.rows("push_tokens")) == 3


def test_확인할_줄이_없으면_expo를_부르지_않음():
    """확인할 줄이 없으면 Expo를 부르지 않음"""
    db = setup()

    def boom(req):
        raise AssertionError("호출하면 안 됨")

    result = run(run_receipt_check(db, NOW, fetch_impl=fake_fetch(boom)))
    assert result == {"checked": 0, "tokensRemoved": 0, "expired": 0}


def test_db_읽기가_실패해도_던지지_않고_로그만(caplog):
    """DB 읽기가 실패해도 던지지 않고 로그만 (MISSED 기록과 같은 배치라 서로 안 막음)"""
    db = setup()
    db.rows("push_tickets").append(ticket("x", A, TA, "WAKE_PING", ago(RECEIPT_DELAY_MS + 1000)))
    db.fail_on.append({"table": "push_tickets", "op": "select"})
    with caplog.at_level(logging.ERROR):
        result = run(run_receipt_check(db, NOW, fetch_impl=receipts_of({"x": {"status": "ok"}})))
    assert result == {"checked": 0, "tokensRemoved": 0, "expired": 0}


def test_상수():
    """상수: 영수증은 15분 뒤에 확인하고 24시간이 지나면 포기함 (문서의 숫자 그대로)"""
    assert RECEIPT_DELAY_MS == 15 * 60_000
    assert TICKET_MAX_AGE_MS == 24 * 3_600_000


def test_영수증이_늦게_오는_사이_같은_토큰이_다시_등록됐으면_지우지_않음():
    """영수증이 늦게 오는 사이 같은 토큰이 다시 등록됐으면(마지막 본 시각이 접수 시각보다 늦음) 지우지 않음 — 표 줄만 정리"""
    db = setup()
    next(t for t in db.rows("push_tokens") if t["token"] == TA)["last_seen_at"] = ago(
        60_000
    )  # 접수 뒤에 앱이 다시 등록
    db.rows("push_tickets").extend(
        [
            ticket("t-a", A, TA, "WAKE_PING", ago(RECEIPT_DELAY_MS + 1000)),
            ticket("t-b", B, TB, "WAKE_PING", ago(RECEIPT_DELAY_MS + 1000)),
        ]
    )
    run(run_receipt_check(db, NOW, fetch_impl=receipts_of({"t-a": GONE, "t-b": GONE})))
    assert sorted(t["token"] for t in db.rows("push_tokens")) == [
        TA,
        TC,
    ]  # 다시 등록한 A는 남고 B의 낡은 토큰만 사라짐
    assert len(db.rows("push_tickets")) == 0


def test_devicenotregistered가_아닌_영수증_오류는_서버_로그에_남김(caplog):
    """DeviceNotRegistered가 아닌 영수증 오류는 서버 로그에 남김 (토큰은 그대로)"""
    db = setup()
    db.rows("push_tickets").append(ticket("t-c", B, TC, "WAKE_PING", ago(RECEIPT_DELAY_MS + 1000)))
    fetch = receipts_of(
        {"t-c": {"status": "error", "message": "too big", "details": {"error": "MessageTooBig"}}}
    )
    with caplog.at_level(logging.ERROR):
        run(run_receipt_check(db, NOW, fetch_impl=fetch))
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "MessageTooBig" in errors[0].getMessage()
    assert len(db.rows("push_tokens")) == 3


def test_표_줄은_100개씩_나눠_지움():
    """표 줄은 100개씩 나눠 지움 (한 번에 긴 목록을 지우다 거절되면 같은 줄을 영영 다시 잡음) — 250줄이면 지우기 3번"""
    db = setup()
    mapping = {}
    for i in range(250):
        tid = f"t{i:04d}"
        db.rows("push_tickets").append(ticket(tid, A, TA, "WAKE_PING", ago(RECEIPT_DELAY_MS + 1000 + i)))
        mapping[tid] = {"status": "ok"}
    result = run(run_receipt_check(db, NOW, fetch_impl=receipts_of(mapping)))
    assert result["checked"] == 250
    assert len(db.rows("push_tickets")) == 0
    assert db.calls.count("delete push_tickets") == 3


def test_한_번에_1000줄까지_오래된_접수_번호부터():
    """한 번에 1000줄까지, 오래된 접수 번호부터 — 1001줄이 쌓였으면 가장 새 줄이 남음"""
    db = setup()
    mapping = {}
    for i in range(1000, -1, -1):  # 새것부터 쌓음 — 정렬이 없으면 새것 1000줄이 잡혀 가장 오래된 줄이 남음
        tid = f"t{i:04d}"
        # 번호가 클수록 새것
        db.rows("push_tickets").append(
            ticket(tid, A, TA, "WAKE_PING", ago(RECEIPT_DELAY_MS + (1001 - i) * 1000))
        )
        mapping[tid] = {"status": "ok"}
    result = run(run_receipt_check(db, NOW, fetch_impl=receipts_of(mapping)))
    assert result["checked"] == 1000
    assert [t["ticket_id"] for t in db.rows("push_tickets")] == ["t1000"]

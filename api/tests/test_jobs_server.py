"""서버 모드 작업 본문(app/jobs_server.py) — Edge `wake-check` · `suggestion-generate` index.ts가 돌려주는 JSON과 같은 모양인지.
가짜 DB(FakeDb) + Expo는 가짜 fetch (실제 푸시 안 나감)"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import AppError
from app.jobs_server import run_suggestion_generate, run_wake_check
from app.shared.js import to_iso
from app.shared.notify import NotifyOptions
from tests.fake_db import FakeDb
from tests.shared.push_support import fake_fetch, no_sleep, response

NOW = datetime(2026, 10, 16, 1, 0, 0, tzinfo=UTC)
T1 = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
T2 = "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"


def run(coro):
    return asyncio.run(coro)


def missed(n):
    return {
        "alarm_id": f"alarm-{n}",
        "user_id": f"user-{n}",
        "participant_id": None,
        "appointment_id": None,
        "group_id": None,
        "appointment_at": None,
        "scheduled_at": "2026-10-16T00:00:00.000Z",
        "missed_at": "2026-10-16T00:30:00.000Z",
        "event_key": f"key-{n}",
    }


def setup(rows=None, nudges=None):
    db = FakeDb()
    db.rpc_handlers["find_missed_wake_alarms"] = lambda args: rows or []
    db.rpc_handlers["claim_wake_nudges"] = lambda args: nudges or []
    sent: list[dict] = []
    receipt_requests: list[list[str]] = []

    def handler(req):
        if req.url.endswith("/getReceipts"):
            receipt_requests.append(req.json["ids"])
            return response({"data": {i: {"status": "ok"} for i in req.json["ids"]}})
        body = req.json
        sent.extend(body)
        return response({"data": [{"status": "ok", "id": f"ticket-{b['to'][-6:-1]}"} for b in body]})

    push = NotifyOptions(access_token="EXPO", fetch_impl=fake_fetch(handler), sleep=no_sleep)
    return db, push, sent, receipt_requests


def test_run_wake_check_edge_응답과_같은_모양():
    """wake-check 응답: { checked, recorded, pushed, claimed, nudged, receipts: { checked, tokensRemoved, expired } }"""
    db, push, sent, receipts = setup([missed(1), missed(2)])
    db.rows("push_tokens").append({"user_id": "user-1", "token": T1})
    old = to_iso(NOW - timedelta(minutes=16))
    db.rows("push_tickets").append(
        {"ticket_id": "old", "user_id": "user-9", "token": T2, "kind": "WAKE_PING", "created_at": old}
    )
    result = run(run_wake_check(db, NOW, push=push))
    assert result == {
        "checked": 2,
        "recorded": 2,
        "pushed": 1,
        "claimed": 0,
        "nudged": 0,
        "receipts": {"checked": 1, "tokensRemoved": 0, "expired": 0},
    }
    json.dumps(result)  # JSON으로 직렬화 가능
    assert len(sent) == 1 and sent[0]["to"] == T1
    assert receipts == [["old"]]  # 방금 보낸 접수(ticket-…)는 15분 전이 아니라 확인 안 함


def test_run_wake_check_기본_액세스_토큰은_env에서(monkeypatch):
    """Edge getEnv('EXPO_ACCESS_TOKEN') — env 값(앞뒤 공백 제거)이 Expo 요청 머리글에 실림"""
    monkeypatch.setenv("EXPO_ACCESS_TOKEN", "  from-env  ")
    from app import jobs_server

    assert jobs_server._expo_access_token() == "from-env"
    monkeypatch.setenv("EXPO_ACCESS_TOKEN", "   ")
    assert jobs_server._expo_access_token() is None
    monkeypatch.delenv("EXPO_ACCESS_TOKEN")
    assert jobs_server._expo_access_token() is None


def test_run_wake_check_기록이_실패해도_영수증_확인은_돌고_실패는_그_뒤에_던짐():
    """MISSED 기록이 실패해도 영수증 확인은 돌리고(서로 안 막음), 실패는 그 뒤에 그대로 던짐"""
    db, push, _sent, receipts = setup([missed(1)])
    db.rpc_handlers["find_missed_wake_alarms"] = lambda args: (_ for _ in ()).throw(RuntimeError("boom"))
    db.rows("push_tickets").append(
        {
            "ticket_id": "old",
            "user_id": "u",
            "token": T2,
            "kind": "WAKE_PING",
            "created_at": to_iso(NOW - timedelta(minutes=16)),
        }
    )
    with pytest.raises(AppError) as e:
        run(run_wake_check(db, NOW, push=push))
    assert e.value.status == 500
    assert receipts == [["old"]]
    assert db.rows("push_tickets") == []  # 영수증 확인이 돌아 정리됨


def test_run_suggestion_generate_edge_응답과_같은_모양():
    """suggestion-generate 응답: { candidates, created, skipped }"""
    db = FakeDb()
    db.rpc_handlers["prepare_suggestion_inputs"] = lambda args: [
        {
            "user_id": "u1",
            "participant_id": "p1",
            "target_alarm_id": "prep-1",
            "current_advance": 0,
            "before_prepare_minutes": 10,
            "cap_minutes": 10,
            "residuals": [2, 10, 14, 12],
        }
    ]
    result = run(run_suggestion_generate(db, NOW))
    assert result == {"candidates": 1, "created": 1, "skipped": 0}
    assert db.rpc_calls == [{"name": "prepare_suggestion_inputs", "args": {"p_now": to_iso(NOW)}}]
    assert db.rows("alarm_suggestions") == [
        {
            "user_id": "u1",
            "target_alarm_id": "prep-1",
            "suggestion_type": "PREPARE_ADVANCE",
            "current_value": "0",
            "suggested_value": "10",
            "evidence": [10, 14, 12],
        }
    ]

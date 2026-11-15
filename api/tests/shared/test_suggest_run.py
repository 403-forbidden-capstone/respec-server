"""본 레포 `_shared/suggest-run.test.ts`의 서버판 시험 — 후보 → 제안 (규칙 · 120분 상한), 넣기(겹친 대기 제안은 건너뜀)"""

import asyncio
from datetime import UTC, datetime

from app.shared.js import to_iso
from app.shared.suggest_run import plan_suggestions, run_suggest
from app.shared.supabase import Result
from tests.fake_db import FakeDb


def run(coro):
    return asyncio.run(coro)


def row(**over):
    return {
        "user_id": "u1",
        "participant_id": "p1",
        "target_alarm_id": "prep-1",
        "current_advance": 0,
        "before_prepare_minutes": 10,
        "cap_minutes": 10,
        "residuals": [2, 10, 14, 12],
        **over,
    }


def test_목업_s01_예시():
    """목업 S01 예시 — 최근 3번 +10 · +14 · +12분이면 준비 알람 10분 당김 제안 (지금 0 → 10)"""
    assert plan_suggestions([row()]) == [
        {
            "user_id": "u1",
            "target_alarm_id": "prep-1",
            "suggestion_type": "PREPARE_ADVANCE",
            "current_value": "0",
            "suggested_value": "10",
            "evidence": [10, 14, 12],
        }
    ]


def test_이미_당긴_분이_있으면_그_위에_더함():
    """이미 당긴 분이 있으면 그 위에 더함 (5 → 10), 규칙에 안 걸리면 제안 없음"""
    assert plan_suggestions([row(current_advance=5, cap_minutes=5)])[0]["suggested_value"] == "10"
    assert plan_suggestions([row(residuals=[12, -1, 14]), row(residuals=[3, 4, 6])]) == []


def test_당김_분_합은_120분을_넘지_않음():
    """당김 분 합은 120분을 넘지 않음 — 115분이면 5분만, 120분이면 제안 없음"""
    out = plan_suggestions([row(current_advance=115, residuals=[20, 20, 20], cap_minutes=10)])
    assert out[0]["suggested_value"] == "120"
    assert plan_suggestions([row(current_advance=120, residuals=[20, 20, 20])]) == []


def test_기상_알람이_없는_일정도_제안함():
    """기상 알람이 없는 일정(before_prepare_minutes null)도 제안함"""
    assert len(plan_suggestions([row(before_prepare_minutes=None)])) == 1


def test_run_suggest_재료_함수를_지금_시각으로_부르고_제안을_넣음_겹친_대기_제안은_건너뜀():
    """runSuggest — 재료 함수를 지금 시각으로 부르고 제안을 넣음, 겹친 대기 제안(23505)은 건너뜀"""
    db = FakeDb()
    now = datetime(2025, 5, 22, 9, 0, 0, tzinfo=UTC)
    db.rpc_handlers["prepare_suggestion_inputs"] = lambda args: [
        row(),
        row(participant_id="p2", target_alarm_id="prep-2"),
    ]
    inserts: list[dict] = []
    original = db.from_

    class FakeInsert:
        def insert(self, r):
            inserts.append(r)

            async def go():
                if r["target_alarm_id"] == "prep-2":
                    return Result(None, {"code": "23505", "message": "duplicate"})
                return Result(None, None)

            return go()

    def from_(table):
        return FakeInsert() if table == "alarm_suggestions" else original(table)

    db.from_ = from_
    result = run(run_suggest(db, now))
    assert db.rpc_calls == [{"name": "prepare_suggestion_inputs", "args": {"p_now": to_iso(now)}}]
    assert len(inserts) == 2
    assert result == {"candidates": 2, "created": 1, "skipped": 1}


def test_run_suggest_후보가_없으면_아무것도_안_넣음():
    """runSuggest — 후보가 없으면 아무것도 안 넣음"""
    db = FakeDb()
    db.rpc_handlers["prepare_suggestion_inputs"] = lambda args: []
    assert run(run_suggest(db, datetime.now(UTC))) == {"candidates": 0, "created": 0, "skipped": 0}

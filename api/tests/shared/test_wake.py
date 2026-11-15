"""본 레포 `_shared/wake.test.ts`의 서버판 시험 — MISSED 기록(중복 무시) · 이름 인자 · 본인 미확인 알림 · 친구 알림('깨워 볼까요?' · 후속)을
가짜 DB(rpc · upsert 포함)와 가짜 Expo 서버로. 받는 사람을 고르는 판정은 DB 함수 몫(claim_wake_nudges) — 여기서는 DB가 돌려준 행을 보내는 것만"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.core.errors import AppError
from app.shared.js import to_iso
from app.shared.notify import NotifyOptions
from app.shared.wake import LOOKBACK_HOURS, run_wake_check
from tests.fake_db import FakeDb

from .push_support import fake_fetch, no_sleep, response

NOW = datetime(2026, 10, 16, 1, 0, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def missed(n, **over):
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
        **over,
    }


class Setup:
    def __init__(self, rows=None, nudges=None):
        rows = rows or []
        nudges = nudges or []
        self.db = FakeDb()
        self.db.rpc_handlers["find_missed_wake_alarms"] = lambda args: rows
        self.db.rpc_handlers["claim_wake_nudges"] = lambda args: nudges
        self.pushes: list[dict] = []

        def handler(req):
            body = req.json
            self.pushes.extend(body)
            return response({"data": [{"status": "ok", "id": f"ticket-{b['to'][-6:-1]}"} for b in body]})

        self.push = NotifyOptions(fetch_impl=fake_fetch(handler), sleep=no_sleep)


T1 = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
T2 = "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"
T3 = "ExponentPushToken[cccccccccccccccccccccc]"
ZERO = {"checked": 0, "recorded": 0, "pushed": 0, "claimed": 0, "nudged": 0}


def test_찾은_알람마다_missed를_기록하고_db_함수는_이름_인자로_부름():
    """찾은 알람마다 MISSED를 기록하고, DB 함수는 이름 인자(p_now · p_lookback_hours)로 부름"""
    s = Setup([missed(1), missed(2)])
    result = run(run_wake_check(s.db, NOW))
    assert result == {**ZERO, "checked": 2, "recorded": 2}
    assert s.db.rpc_calls[0] == {
        "name": "find_missed_wake_alarms",
        "args": {"p_now": to_iso(NOW), "p_lookback_hours": LOOKBACK_HOURS},
    }
    events = s.db.rows("alarm_events")
    assert [
        [e["alarm_id"], e["user_id"], e["event_type"], e["event_at"], e["client_event_id"], e["input_source"]]
        for e in events
    ] == [
        ["alarm-1", "user-1", "MISSED", "2026-10-16T00:30:00.000Z", "key-1", None],
        ["alarm-2", "user-2", "MISSED", "2026-10-16T00:30:00.000Z", "key-2", None],
    ]


def test_겹쳐_돌거나_다시_돌아도_같은_알람의_missed는_한_번만():
    """겹쳐 돌거나 다시 돌아도 같은 알람의 MISSED는 한 번만 (client_event_id = event_key), 새로 들어간 것만 recorded"""
    s = Setup([missed(1), missed(2)])
    run(run_wake_check(s.db, NOW))

    again = run(run_wake_check(s.db, NOW))  # 5분 뒤 같은 후보가 또 나옴
    assert again == {**ZERO, "checked": 2}
    assert len(s.db.rows("alarm_events")) == 2

    s.db.rpc_handlers["find_missed_wake_alarms"] = lambda args: [missed(1), missed(2), missed(3)]
    third = run(run_wake_check(s.db, NOW))
    assert third == {**ZERO, "checked": 3, "recorded": 1}
    assert [e["client_event_id"] for e in s.db.rows("alarm_events")] == ["key-1", "key-2", "key-3"]


def test_후보가_없으면_기록_호출_없이_0_친구_알림_판정은_그래도_돎():
    """후보가 없으면 기록 호출 없이 0 — 친구 알림 판정(claim)은 그래도 돎 (옛 미확인의 10분이 이제야 찬 경우)"""
    s = Setup([])
    assert run(run_wake_check(s.db, NOW)) == ZERO
    assert not any(c.startswith("upsert") for c in s.db.calls)
    assert [c["name"] for c in s.db.rpc_calls] == ["find_missed_wake_alarms", "claim_wake_nudges"]


def _raise(message):
    def handler(args):
        raise RuntimeError(message)

    return handler


def test_db_함수나_기록이_실패하면_일반_문구의_500(caplog):
    """DB 함수나 기록이 실패하면 일반 문구의 500"""
    s = Setup([missed(1)])
    s.db.rpc_handlers["find_missed_wake_alarms"] = _raise("boom")
    with pytest.raises(AppError) as e:
        run(run_wake_check(s.db, NOW))
    assert e.value.status == 500

    s2 = Setup([missed(1)])
    s2.db.fail_on = [{"table": "alarm_events", "op": "upsert"}]
    with pytest.raises(AppError) as e2:
        run(run_wake_check(s2.db, NOW))
    assert e2.value.status == 500


def test_기록이_실패해도_친구_알림_판정은_돌고_반대도_마찬가지():
    """기록이 실패해도 친구 알림 판정은 돌고, 반대로 친구 알림 판정이 실패해도 기록은 남음 — 실패는 그 뒤에 던짐"""
    a = Setup([missed(1)], [])
    a.db.rpc_handlers["find_missed_wake_alarms"] = _raise("boom")
    with pytest.raises(AppError) as e:
        run(run_wake_check(a.db, NOW))
    assert e.value.status == 500
    assert any(c["name"] == "claim_wake_nudges" for c in a.db.rpc_calls)

    b = Setup([missed(1)])
    b.db.rpc_handlers["claim_wake_nudges"] = _raise("boom")
    with pytest.raises(AppError) as e2:
        run(run_wake_check(b.db, NOW))
    assert e2.value.status == 500
    assert len(b.db.rows("alarm_events")) == 1


# ---- 본인 알림 (8-너) ----


def test_8_20_새로_기록된_미확인의_주인_본인에게만(caplog):
    """8-20: 새로 기록된 미확인의 주인 본인에게만 "기상 확인 / 기상 확인이 안 됐어요" — 본인 기기 전부, 다른 사람 토큰은 안 건드림, 눌러 갈 곳은 H08(/wake)"""
    s = Setup([missed(1), missed(2)])
    s.db.rows("push_tokens").extend(
        [
            {"user_id": "user-1", "token": T1},
            {"user_id": "user-1", "token": T2},
            {"user_id": "user-3", "token": T3},
        ]
    )
    result = run(run_wake_check(s.db, NOW, push=s.push))
    assert result == {**ZERO, "checked": 2, "recorded": 2, "pushed": 2}
    assert sorted(p["to"] for p in s.pushes) == [T1, T2]
    assert all(
        p["title"] == "기상 확인" and p["body"] == "기상 확인이 안 됐어요" and p["channelId"] == "missed_v1"
        for p in s.pushes
    )
    assert all(
        p["data"]["kind"] == "WAKE_MISSED"
        and p["data"]["recipientUserId"] == "user-1"
        and p["data"]["path"] == "/wake"
        for p in s.pushes
    )
    # 영수증 표에 종류와 함께 남음
    assert [[t["user_id"], t["kind"]] for t in s.db.rows("push_tickets")] == [
        ["user-1", "WAKE_MISSED"],
        ["user-1", "WAKE_MISSED"],
    ]


def test_8_20_이미_기록된_미확인은_본인_알림을_다시_안_보냄():
    """8-20: 이미 기록된 미확인(다시 돈 배치)은 본인 알림을 다시 안 보냄, 한 사람의 알람 둘이 같이 미확인이어도 한 번"""
    s = Setup([missed(1), missed(1, alarm_id="alarm-1b", event_key="key-1b")])
    s.db.rows("push_tokens").append({"user_id": "user-1", "token": T1})
    run(run_wake_check(s.db, NOW, push=s.push))
    assert len(s.pushes) == 1

    run(run_wake_check(s.db, NOW, push=s.push))  # 5분 뒤 같은 후보 — 이미 기록돼 있음
    assert len(s.pushes) == 1


def test_본인_알림이_실패해도_missed_기록은_그대로_성공():
    """본인 알림이 실패해도(토큰 조회 오류 · Expo 장애) MISSED 기록은 그대로 성공"""
    a = Setup([missed(1)])
    a.db.rows("push_tokens").append({"user_id": "user-1", "token": T1})
    a.db.fail_on = [{"table": "push_tokens", "op": "select"}]
    assert run(run_wake_check(a.db, NOW, push=a.push)) == {**ZERO, "checked": 1, "recorded": 1}
    assert len(a.db.rows("alarm_events")) == 1

    b = Setup([missed(1)])
    b.db.rows("push_tokens").append({"user_id": "user-1", "token": T1})
    down = NotifyOptions(fetch_impl=fake_fetch(lambda req: response(raw="x", status=503)), sleep=no_sleep)
    assert run(run_wake_check(b.db, NOW, push=down)) == {**ZERO, "checked": 1, "recorded": 1}


def test_토큰이_없는_본인에게는_푸시_없이_0():
    """토큰이 없는 본인에게는 푸시 없이 0"""
    s = Setup([missed(1)])
    assert run(run_wake_check(s.db, NOW, push=s.push)) == {**ZERO, "checked": 1, "recorded": 1}
    assert s.pushes == []


# ---- 친구 알림 ('깨워 볼까요?' · 후속 '일어났어요') ----

GROUP = "55555555-5555-4555-8555-555555555555"


def nudge(recipient, **over):
    return {
        "event_key": f"NUDGE:s:2026-10-16:{recipient}",
        "kind": "NUDGE",
        "recipient_id": recipient,
        "sleeper_id": "sleeper",
        "group_id": GROUP,
        **over,
    }


def nudge_setup(nudges):
    s = Setup([], nudges)
    s.db.rows("profiles").extend(
        [{"user_id": "sleeper", "nickname": "지형"}, {"user_id": "v1", "nickname": "수연"}]
    )
    s.db.rows("push_tokens").extend(
        [
            {"user_id": "v1", "token": T1},
            {"user_id": "v1", "token": T2},
            {"user_id": "v2", "token": T3},
            {"user_id": "sleeper", "token": "ExponentPushToken[dddddddddddddddddddddd]"},
        ]
    )
    return s


def test_8_22_db가_정한_받는_사람에게():
    """8-22: DB가 정한 받는 사람에게 "깨워 볼까요? / ○○님이 아직 안 일어났어요" — 안 일어난 사람 이름만 문구에, 눌러 열 그룹 · 사람이 알림 데이터에, 친구 채널"""
    s = nudge_setup([nudge("v1"), nudge("v2")])
    result = run(run_wake_check(s.db, NOW, push=s.push))
    assert result == {**ZERO, "claimed": 2, "nudged": 3}
    claim = next(c for c in s.db.rpc_calls if c["name"] == "claim_wake_nudges")
    assert claim == {"name": "claim_wake_nudges", "args": {"p_now": to_iso(NOW)}}
    assert sorted(p["to"] for p in s.pushes) == [
        T1,
        T2,
        T3,
    ]  # 받는 사람 기기만 — 안 일어난 사람 본인 기기에는 안 감
    assert all(
        p["title"] == "깨워 볼까요?"
        and p["body"] == "지형님이 아직 안 일어났어요"
        and p["channelId"] == "friend_v1"
        for p in s.pushes
    )
    assert all(
        p["data"]["kind"] == "WAKE_NUDGE"
        and p["data"]["path"] == "/group"
        and p["data"]["groupId"] == GROUP
        and p["data"]["subjectUserId"] == "sleeper"
        for p in s.pushes
    )
    assert [p["data"]["recipientUserId"] for p in s.pushes if p["to"] == T3] == [
        "v2"
    ]  # 받는 사람 id는 기기 주인
    assert {t["kind"] for t in s.db.rows("push_tickets")} == {"WAKE_NUDGE"}
    assert sorted([t["token"], t["user_id"]] for t in s.db.rows("push_tickets")) == [
        [T1, "v1"],
        [T2, "v1"],
        [T3, "v2"],
    ]


def test_8_22_많은_사람은_100개씩_나눠_조회():
    """8-22(많은 사람): 받는 사람 · 안 일어난 사람이 100명을 넘어도 프로필 · 토큰 조회를 100개씩 나눠 하고 모두에게 보냄 — 이름은 사람마다 맞게"""
    rows = [nudge(f"r{i}", sleeper_id=f"s{i}", event_key=f"NUDGE:s{i}:2026-10-16:r{i}") for i in range(130)]
    s = Setup([], rows)
    for i in range(130):
        s.db.rows("profiles").append({"user_id": f"s{i}", "nickname": f"이름{i}"})
        s.db.rows("push_tokens").append({"user_id": f"r{i}", "token": f"ExponentPushToken[{i:022d}]"})
    result = run(run_wake_check(s.db, NOW, push=s.push))
    assert [result["claimed"], result["nudged"]] == [130, 130]
    assert s.db.calls.count("select profiles") == 2
    assert s.db.calls.count("select push_tokens") == 2
    found = next(p for p in s.pushes if p["data"]["recipientUserId"] == "r125")
    assert found["body"] == "이름125님이 아직 안 일어났어요"


def test_8_30_후속은_일어났어요():
    """8-30: 후속은 "일어났어요 / ○○님이 일어났어요", 종류 WAKE_NUDGE_UP — 같은 배치에 둘이 섞여도 각자 문구"""
    s = nudge_setup([nudge("v1"), nudge("v2", kind="UP", event_key="UP:s:2026-10-16:v2")])
    result = run(run_wake_check(s.db, NOW, push=s.push))
    assert result["nudged"] == 3
    up = [p for p in s.pushes if p["data"]["kind"] == "WAKE_NUDGE_UP"]
    assert [
        [p["to"], p["title"], p["body"], p["channelId"], p["data"]["path"], p["data"]["subjectUserId"]]
        for p in up
    ] == [[T3, "일어났어요", "지형님이 일어났어요", "friend_v1", "/group", "sleeper"]]
    assert len([p for p in s.pushes if p["data"]["kind"] == "WAKE_NUDGE"]) == 2
    assert sorted(t["kind"] for t in s.db.rows("push_tickets")) == [
        "WAKE_NUDGE",
        "WAKE_NUDGE",
        "WAKE_NUDGE_UP",
    ]


def test_8_22_빈_목록():
    """8-22(빈 목록): 받을 사람이 없으면(DB가 빈 목록) 푸시도 토큰 조회도 없음"""
    s = nudge_setup([])
    assert run(run_wake_check(s.db, NOW, push=s.push))["nudged"] == 0
    assert s.pushes == []
    assert "select push_tokens" not in s.db.calls


def test_받는_사람에게_토큰이_없으면_보낸_수_0_이름이_비어_있으면_친구():
    """받는 사람에게 토큰이 없으면 보낸 수 0, 이름이 비어 있으면 문구는 친구"""
    none = nudge_setup([nudge("nobody")])
    assert run(run_wake_check(none.db, NOW, push=none.push))["nudged"] == 0
    assert none.pushes == []

    nameless = nudge_setup([nudge("v1", sleeper_id="ghost")])
    run(run_wake_check(nameless.db, NOW, push=nameless.push))
    assert all(p["body"] == "친구님이 아직 안 일어났어요" for p in nameless.pushes)


def test_친구_알림_보내기가_실패해도_배치는_성공():
    """친구 알림 보내기가 실패해도(프로필 조회 오류 · Expo 장애) 배치는 성공 — DB는 이미 보낸 것으로 남겨서 다시 안 보냄"""
    a = nudge_setup([nudge("v1")])
    a.db.fail_on = [{"table": "profiles", "op": "select"}]
    assert run(run_wake_check(a.db, NOW, push=a.push)) == {**ZERO, "claimed": 1}

    b = nudge_setup([nudge("v1")])
    down = NotifyOptions(fetch_impl=fake_fetch(lambda req: response(raw="x", status=503)), sleep=no_sleep)
    assert run(run_wake_check(b.db, NOW, push=down)) == {**ZERO, "claimed": 1}


def test_8_20_많은_사람_본인_알림():
    """8-20(많은 사람): 한꺼번에 미확인이 된 사람이 100명을 넘어도 본인 알림의 토큰 조회를 100개씩 나눠 하고 모두에게 보냄"""
    rows = [missed(i, user_id=f"user-{i}", alarm_id=f"alarm-{i}", event_key=f"key-{i}") for i in range(130)]
    s = Setup(rows)
    for i in range(130):
        s.db.rows("push_tokens").append({"user_id": f"user-{i}", "token": f"ExponentPushToken[{i:022d}]"})
    result = run(run_wake_check(s.db, NOW, push=s.push))
    assert [result["recorded"], result["pushed"]] == [130, 130]
    assert s.db.calls.count("select push_tokens") == 2

"""본 레포 `_shared/notify.test.ts`의 서버판 시험 (친구 논리설계 11장, 시험케이스 11장 함수 층) — 종류별 고정 문구 · 권한 거절 ·
그룹은 받는 사람을 서버가 고름 · 토큰 없음 · 만료 토큰 삭제 · 영수증 표 · 채널 · 받는 사람 id를 가짜 DB(rpc 포함)와 가짜 Expo 서버로.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import AppError
from app.core.read import Reader
from app.shared.js import to_iso
from app.shared.notify import (
    NotifyOptions,
    plan_group_push,
    read_notify_request,
    run_notify,
    send_group_push,
)
from tests.fake_db import FakeDb

from .push_support import fake_fetch, no_sleep, response

SENDER = "11111111-1111-4111-8111-111111111111"
TARGET = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
ALARM = "44444444-4444-4444-8444-444444444444"
GROUP = "55555555-5555-4555-8555-555555555555"
APPT = "66666666-6666-4666-8666-666666666666"
R1 = "77777777-7777-4777-8777-777777777777"
R2 = "88888888-8888-4888-8888-888888888888"
T1 = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
T2 = "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"
T3 = "ExponentPushToken[cccccccccccccccccccccc]"
T4 = "ExponentPushToken[dddddddddddddddddddddd]"
PROPOSAL = "abababab-abab-4bab-8bab-abababababab"
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)  # 한국 11/8 21:00 — 문구의 '내일'을 가르는 기준
HOUR = timedelta(hours=1)


def run(coro):
    return asyncio.run(coro)


def minutes_ago(n: int) -> str:
    return to_iso(datetime.now(UTC) - timedelta(minutes=n))


def request(**over):
    return {"kind": "ALARM_CHANGED", "targetUserId": TARGET, **over}


def ok_all(to: str, i: int = 0):
    return {"status": "ok", "id": f"ticket-{to[-6:-1]}"}


class Setup:
    """can_send_push는 DB 함수라 여기서는 종류별로 "SENDER → TARGET이면 허용"으로 흉내. 깨우기 판정 plan_wake_ping도 DB 함수 — 기본은 '보낼 수 있음'(reserve면 count 1)"""

    def __init__(self, allowed=("ALARM_CHANGED", "FRIEND_REQUEST", "FRIEND_ACCEPTED")):
        self.db = db = FakeDb()
        db.rpc_handlers["can_send_push"] = lambda a: (
            a["p_kind"] in allowed and a["p_sender_id"] == SENDER and a["p_target_id"] == TARGET
        )

        # 깨우기는 can_send_push 없이 plan_wake_ping이 권한까지 판정 — SENDER → TARGET이면 보이는 사람(그룹 GROUP), 아니면 FORBIDDEN
        def plan_wake_ping(a):
            if (
                a["p_sender"] != SENDER
                or a["p_target"] != TARGET
                or ("p_group_id" in a and a["p_group_id"] != GROUP)
            ):
                return {"status": "FORBIDDEN"}
            if a["p_reserve"]:
                return {
                    "status": "OK",
                    "group_id": GROUP,
                    "count": 1,
                    "limit": 3,
                    "next_at": "2026-11-10T07:10:00Z",
                    "send_id": 7,
                }
            return {"status": "OK", "group_id": GROUP}

        db.rpc_handlers["plan_wake_ping"] = plan_wake_ping
        db.rows("push_sends").append(
            {"send_id": 7, "kind": "WAKE_PING", "sender_id": SENDER, "target_id": TARGET}
        )
        db.rows("alarms").append({"alarm_id": ALARM, "user_id": TARGET, "alarm_type": "WAKE"})
        # 알람 바뀜(제안) 알림은 방금 보낸 대기 중 제안에서 시각을 가져옴 — 한국 7:00 → 7:10
        db.rows("alarm_proposals").append(
            {
                "proposal_id": PROPOSAL,
                "alarm_id": ALARM,
                "owner_id": TARGET,
                "proposer_id": SENDER,
                "from_at": "2026-11-08T22:00:00.000Z",
                "to_at": "2026-11-08T22:10:00.000Z",
                "status": "PENDING",
                "created_at": minutes_ago(10),
            }
        )
        db.rows("profiles").extend(
            [
                {"user_id": SENDER, "nickname": "수연", "timezone": "Asia/Seoul"},
                {"user_id": TARGET, "nickname": "지형", "timezone": "Asia/Seoul"},
                {"user_id": OTHER, "nickname": "남", "timezone": "Asia/Seoul"},
            ]
        )
        db.rows("push_tokens").extend(
            [
                {"user_id": TARGET, "token": T1},
                {"user_id": TARGET, "token": T2},
                {"user_id": OTHER, "token": T3},
            ]
        )
        self.pushes: list[dict] = []
        self.call_count = 0

    def fetch(self, tickets=ok_all):
        def handler(req):
            self.call_count += 1
            body = req.json
            self.pushes.extend(body)
            return response({"data": [tickets(b["to"], i) for i, b in enumerate(body)]})

        return fake_fetch(handler)

    def opts(self, tickets=ok_all, **kw):
        return NotifyOptions(fetch_impl=self.fetch(tickets), sleep=no_sleep, **kw)


def assert_app_error(excinfo, status, code=None):
    assert isinstance(excinfo.value, AppError)
    assert excinfo.value.status == status
    if code:
        assert excinfo.value.code == code


def test_권한이_있으면_받는_사람의_토큰_전부에_보냄():
    """권한이 있으면 받는 사람의 토큰 전부(폰 2대)에 보냄 — 다른 사람 토큰은 안 건드림, can_send_push 인자 이름, 문구는 서버가 정함 (11-05 · 11-08)"""
    s = Setup()
    result = run(run_notify(s.db, SENDER, request(alarmId=ALARM), s.opts(now=NOW)))
    assert result == {"sent": 2, "failed": 0}
    assert s.db.rpc_calls == [
        {
            "name": "can_send_push",
            "args": {"p_sender_id": SENDER, "p_target_id": TARGET, "p_kind": "ALARM_CHANGED"},
        }
    ]
    assert sorted(p["to"] for p in s.pushes) == [T1, T2]
    assert s.pushes[0]["title"] == "알람 제안"
    assert s.pushes[0]["body"] == "수연님이 내일 기상 알람을 7:00 → 7:10으로 제안했어요"
    assert s.pushes[0]["channelId"] == "alarm_change_v1"
    # 알림 데이터: 종류 · 받는 사람 id(앱이 계정 대조) · 이동 경로 · 알람 id · 제안 id
    assert s.pushes[0]["data"] == {
        "kind": "ALARM_CHANGED",
        "recipientUserId": TARGET,
        "path": "/alarms",
        "alarmId": ALARM,
        "proposalId": PROPOSAL,
    }


def test_10_25_10_37_알림의_시각_알람_종류는_제안_기록에서():
    """10-25 · 10-37: 알림의 시각 · 알람 종류는 요청이 아니라 제안 기록에서 — 옛 앱이 보낸 previousAt · newAt은 무시(꾸밀 수 없음), 알람 id 없이 보내면 가장 최근 제안"""
    s = Setup()
    req = read_notify_request(
        Reader.root(
            {
                "kind": "ALARM_CHANGED",
                "targetUserId": TARGET,
                "previousAt": "2026-01-01T00:00:00Z",
                "newAt": "2026-01-01T00:10:00Z",
                "title": "무료 쿠폰",
            }
        )
    )
    assert req == {"kind": "ALARM_CHANGED", "targetUserId": TARGET}  # 요청 모양에 시각이 안 남음
    # 더 최근에 보낸 다른 알람(준비)의 제안이 있으면 알람 id 없이는 그쪽
    prepare = "12121212-1212-4212-8212-121212121212"
    s.db.rows("alarms").append({"alarm_id": prepare, "user_id": TARGET, "alarm_type": "PREPARE"})
    s.db.rows("alarm_proposals").append(
        {
            "proposal_id": "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd",
            "alarm_id": prepare,
            "owner_id": TARGET,
            "proposer_id": SENDER,
            "from_at": "2026-11-08T22:20:00.000Z",
            "to_at": "2026-11-08T22:25:00.000Z",
            "status": "PENDING",
            "created_at": minutes_ago(1),
        }
    )
    run(run_notify(s.db, SENDER, req, s.opts(now=NOW)))
    assert all(
        p["body"] == "수연님이 내일 준비 알람을 7:20 → 7:25로 제안했어요" and p["data"]["alarmId"] == prepare
        for p in s.pushes
    )
    s.pushes.clear()
    run(run_notify(s.db, SENDER, request(alarmId=ALARM), s.opts(now=NOW)))
    assert all(
        p["body"] == "수연님이 내일 기상 알람을 7:00 → 7:10으로 제안했어요" and p["data"]["alarmId"] == ALARM
        for p in s.pushes
    )


def test_알람_제안_알림_대기_중_제안이_없으면_404():
    """알람 제안 알림: 방금(1시간 안) 보낸 대기 중 제안이 없으면 404 — 오래된 제안 · 응답한 제안 · 다른 사람에게 보낸 제안 · 다른 알람 id는 없는 것과 같음, 권한 판정은 DB 함수가 먼저"""

    def outcome(mutate, **over):
        s = Setup()
        mutate(s.db)
        try:
            run(run_notify(s.db, SENDER, request(**over), s.opts(now=NOW)))
            return {"ok": True, "pushes": len(s.pushes)}
        except AppError as e:
            return {"status": e.status, "pushes": len(s.pushes)}

    def first(db):
        return db.rows("alarm_proposals")[0]

    def old(db):
        first(db)["created_at"] = to_iso(datetime.now(UTC) - 2 * HOUR)

    assert outcome(old) == {"status": 404, "pushes": 0}
    assert outcome(lambda db: first(db).update(status="DISMISSED")) == {"status": 404, "pushes": 0}
    assert outcome(lambda db: first(db).update(proposer_id=OTHER)) == {"status": 404, "pushes": 0}
    assert outcome(lambda db: first(db).update(owner_id=OTHER)) == {"status": 404, "pushes": 0}
    assert outcome(lambda db: None, alarmId="99999999-9999-4999-8999-999999999999") == {
        "status": 404,
        "pushes": 0,
    }
    assert outcome(lambda db: None) == {"ok": True, "pushes": 2}


def test_알람_제안_문구의_날짜_말과_시각은_받는_사람_시간대_기준():
    """알람 제안 문구의 날짜 말과 시각은 받는 사람 시간대 기준"""
    s = Setup()
    next(p for p in s.db.rows("profiles") if p["user_id"] == TARGET)["timezone"] = "America/New_York"
    run(run_notify(s.db, SENDER, request(), s.opts(now=NOW)))
    assert s.pushes[0]["body"] == "수연님이 오늘 기상 알람을 17:00 → 17:10으로 제안했어요"  # 뉴욕 11/8 17:00


def test_11_09_옛_앱이_보낸_title_body_data는_무시하고_고정_문구():
    """11-09: 옛 앱이 보낸 title · body · data는 무시하고 고정 문구를 씀 — 요청은 받음 (남이 쓴 문구가 알림이 되지 않음)"""
    s = Setup()
    req = read_notify_request(
        Reader.root(
            {
                "kind": "FRIEND_REQUEST",
                "targetUserId": TARGET,
                "title": "무료 쿠폰",
                "body": "링크를 누르세요",
                "data": {"path": "/evil", "recipientUserId": OTHER},
            }
        )
    )
    assert req == {"kind": "FRIEND_REQUEST", "targetUserId": TARGET}
    run(run_notify(s.db, SENDER, req, s.opts()))
    assert all(p["title"] == "친구 요청" and p["body"] == "새 친구 요청이 도착했어요" for p in s.pushes)
    assert all(p["data"]["recipientUserId"] == TARGET and p["data"]["path"] == "/friends" for p in s.pushes)


def test_15_10_본문에_다른_사람_이름을_실어도_읽지_않음():
    """15-10: 본문에 다른 사람 이름(senderId · userId · sender · from)을 실어 보내도 읽지 않음 — 보낸 사람은 로그인한 사람 하나(runNotify의 인자)로만 정해짐"""
    forged = read_notify_request(
        Reader.root(
            {
                "kind": "WAKE_PING",
                "targetUserId": TARGET,
                "senderId": OTHER,
                "userId": OTHER,
                "sender": OTHER,
                "from": OTHER,
            }
        )
    )
    assert forged == {"kind": "WAKE_PING", "targetUserId": TARGET}
    s = Setup(("WAKE_PING",))
    run(run_notify(s.db, SENDER, forged, s.opts()))
    assert len(s.db.rpc_calls) > 0 and all(c["args"]["p_sender"] == SENDER for c in s.db.rpc_calls)


def test_11_11_권한이_없으면_403():
    """11-11: 권한이 없으면 403 — 토큰도 Expo도 건드리지 않음"""
    s = Setup()
    with pytest.raises(AppError) as e:
        run(run_notify(s.db, OTHER, request(), s.opts()))
    assert_app_error(e, 403, "FORBIDDEN")
    assert "권한" in e.value.message
    assert s.pushes == []
    assert "select push_tokens" not in s.db.calls


def test_깨우기는_plan_wake_ping이_판정():
    """깨우기(WAKE_PING)는 plan_wake_ping이 FORBIDDEN이면 403, 허용되면 보낸 사람 이름이 든 고정 문구 + 응답에 ping 상태 + 누르면 받는 사람의 기상 확인 /wake (11-05 · 11-28 · 8-02)"""
    s = Setup(())
    with pytest.raises(AppError) as e:
        run(run_notify(s.db, OTHER, request(kind="WAKE_PING"), s.opts()))
    assert_app_error(e, 403)
    assert "깨우기" in e.value.message
    assert s.pushes == []
    # 깨우기는 can_send_push(DB에서 늘 false)를 안 부르고 plan_wake_ping 한 곳에서 판정
    assert not any(c["name"] == "can_send_push" for c in s.db.rpc_calls)

    open_ = Setup(())
    result = run(run_notify(open_.db, SENDER, request(kind="WAKE_PING"), open_.opts()))
    # send_id · group_id 같은 서버 안쪽 값은 앱에 안 줌
    assert result == {
        "sent": 2,
        "failed": 0,
        "ping": {"status": "OK", "count": 1, "limit": 3, "next_at": "2026-11-10T07:10:00Z"},
    }
    path = "/wake"  # 받는 사람이 누르면 자기 기상 확인(H08, 8-02) — 보낸 사람 쪽 그룹 화면(G03)이 아님
    assert [[p["title"], p["body"], p["channelId"], p["data"]["path"]] for p in open_.pushes] == [
        ["깨우기", "수연님이 깨워요", "friend_v1", path],
        ["깨우기", "수연님이 깨워요", "friend_v1", path],
    ]
    assert all(
        p["data"]["kind"] == "WAKE_PING" and p["data"]["recipientUserId"] == TARGET for p in open_.pushes
    )


def test_깨우기는_그룹_id를_받음():
    """깨우기는 그룹 id를 받음 — 주면 그 그룹에서 보일 때만(plan_wake_ping에 p_group_id로 넘김, 아니면 403), 안 주면 서버가 정한 그룹. 다른 한 사람 종류에서는 그룹 id를 버림"""
    s = Setup(())
    run(run_notify(s.db, SENDER, request(kind="WAKE_PING", groupId=GROUP), s.opts()))
    assert [c["args"].get("p_group_id") for c in s.db.rpc_calls] == [GROUP, GROUP]
    other = Setup(())
    with pytest.raises(AppError) as e:
        run(run_notify(other.db, SENDER, request(kind="WAKE_PING", groupId=OTHER), other.opts()))
    assert_app_error(e, 403)
    assert other.pushes == []
    assert read_notify_request(
        Reader.root({"kind": "WAKE_PING", "targetUserId": TARGET, "groupId": GROUP})
    ) == {
        "kind": "WAKE_PING",
        "targetUserId": TARGET,
        "groupId": GROUP,
    }
    assert read_notify_request(
        Reader.root({"kind": "ALARM_CHANGED", "targetUserId": TARGET, "groupId": GROUP})
    ) == {
        "kind": "ALARM_CHANGED",
        "targetUserId": TARGET,
    }


def test_8_차_8_14_깨우기_순서():
    """8-차 · 8-14: 깨우기 순서 = 판정(권한 포함, 횟수 안 셈) → 토큰 조회 → 횟수 세기(reserve) → 발송, DB 함수 인자 이름은 p_sender · p_target · p_reserve"""
    s = Setup(())
    run(run_notify(s.db, SENDER, request(kind="WAKE_PING"), s.opts()))
    assert s.db.rpc_calls == [
        {"name": "plan_wake_ping", "args": {"p_sender": SENDER, "p_target": TARGET, "p_reserve": False}},
        {"name": "plan_wake_ping", "args": {"p_sender": SENDER, "p_target": TARGET, "p_reserve": True}},
    ]
    order = [c for c in s.db.calls if c == "select push_tokens" or c.startswith("rpc plan_wake_ping")]
    assert order == ["rpc plan_wake_ping", "select push_tokens", "rpc plan_wake_ping"]


def test_8_14_보강_센_뒤에_푸시가_한_건도_못_나가면_횟수를_되돌림():
    """8-14(보강): 센 뒤에 푸시가 한 건도 못 나가면(전송 실패) 센 줄을 지워 횟수를 돌려놓고 ping.status = NO_TOKENS — 하나라도 나가면 센 채로 둠"""
    bad = Setup(())
    down = NotifyOptions(fetch_impl=fake_fetch(lambda req: response(raw="down", status=503)), sleep=no_sleep)
    failed_all = run(run_notify(bad.db, SENDER, request(kind="WAKE_PING"), down))
    assert failed_all == {
        "sent": 0,
        "failed": 2,
        "reason": "PUSH_UNAVAILABLE",
        "ping": {"status": "NO_TOKENS"},
    }
    assert len(bad.db.rows("push_sends")) == 0  # send_id 7이 지워짐

    half = Setup(())
    some = run(
        run_notify(
            half.db,
            SENDER,
            request(kind="WAKE_PING"),
            half.opts(
                lambda to, i: (
                    {"status": "error", "message": "x", "details": {"error": "MessageRateExceeded"}}
                    if to == T2
                    else ok_all(to)
                )
            ),
        )
    )
    assert some["sent"] == 1
    assert some["ping"]["status"] == "OK"
    assert len(half.db.rows("push_sends")) == 1  # 한 대라도 받았으면 횟수는 센 채로

    undo_fails = Setup(())
    undo_fails.db.fail_on.append({"table": "push_sends", "op": "delete"})
    res = run(run_notify(undo_fails.db, SENDER, request(kind="WAKE_PING"), down))
    assert (
        res["ping"]["status"] == "NO_TOKENS"
    )  # 되돌리기가 실패해도 푸시 요청은 끝난 일 — 오류로 만들지 않음


def test_8_14_탈퇴_판정과_프로필_조회_사이에_받는_사람이_사라지면():
    """8-14(탈퇴): 판정과 프로필 조회 사이에 받는 사람이 사라지면 횟수를 안 세고 NO_TOKENS"""
    s = Setup(())
    s.db.tables["profiles"] = [p for p in s.db.rows("profiles") if p["user_id"] != TARGET]
    result = run(run_notify(s.db, SENDER, request(kind="WAKE_PING"), s.opts()))
    assert result == {"sent": 0, "failed": 0, "reason": "NO_TOKENS", "ping": {"status": "NO_TOKENS"}}
    assert s.pushes == []
    assert [c["args"]["p_reserve"] for c in s.db.rpc_calls] == [False]


def test_8_사_앱에_돌려주는_ping은_약속한_칸만():
    """8-사: 앱에 돌려주는 ping은 약속한 칸만 — 판정 함수가 더 얹은 값(send_id · group_id · 모르는 칸)은 안 나감"""
    s = Setup(())
    s.db.rpc_handlers["plan_wake_ping"] = lambda a: (
        {
            "status": "OK",
            "group_id": GROUP,
            "count": 2,
            "limit": 3,
            "next_at": "N",
            "resets_at": "R",
            "send_id": 7,
            "secret": "x",
        }
        if a["p_reserve"]
        else {"status": "OK", "group_id": GROUP}
    )
    result = run(run_notify(s.db, SENDER, request(kind="WAKE_PING"), s.opts()))
    assert result["ping"] == {"status": "OK", "count": 2, "limit": 3, "next_at": "N", "resets_at": "R"}
    blocked = Setup(())
    blocked.db.rpc_handlers["plan_wake_ping"] = lambda a: {
        "status": "WAIT",
        "group_id": GROUP,
        "next_at": "N",
        "send_id": 9,
        "secret": "x",
    }
    res = run(run_notify(blocked.db, SENDER, request(kind="WAKE_PING"), blocked.opts()))
    assert res == {"sent": 0, "failed": 0, "ping": {"status": "WAIT", "next_at": "N"}}


def test_8_01_8_05_8_06_8_08_8_10_못_보내는_이유는_ping_상태로():
    """8-01 · 8-05 · 8-06 · 8-08 · 8-10: 못 보내는 이유(아직 안 울림 · 이미 일어남 · 5분 · 하루 3번 · 받기 꺼짐)는 오류가 아니라 ping 상태로 — 푸시도 토큰 조회도 횟수 세기도 없음"""
    cases = [
        {"status": "NOT_RINGING"},
        {"status": "ALREADY_UP"},
        {"status": "WAIT", "next_at": "2026-11-10T07:10:00Z"},
        {"status": "DAILY_LIMIT", "resets_at": "2026-11-10T15:00:00Z"},
        {"status": "OFF"},
    ]
    for verdict in cases:
        s = Setup(("WAKE_PING",))
        s.db.rpc_handlers["plan_wake_ping"] = lambda a, v=verdict: v
        result = run(run_notify(s.db, SENDER, request(kind="WAKE_PING"), s.opts()))
        assert result == {"sent": 0, "failed": 0, "ping": verdict}
        assert s.pushes == []
        assert "select push_tokens" not in s.db.calls
        assert len([c for c in s.db.rpc_calls if c["name"] == "plan_wake_ping"]) == 1  # 판정만 — 횟수는 안 셈


def test_8_14_받는_사람_토큰이_없으면_횟수를_안_셈():
    """8-14: 받는 사람 토큰이 없으면 횟수를 안 세고(reserve 안 부름) ping.status = NO_TOKENS"""
    s = Setup(("WAKE_PING",))
    s.db.tables["push_tokens"] = []
    result = run(run_notify(s.db, SENDER, request(kind="WAKE_PING"), s.opts()))
    assert result == {"sent": 0, "failed": 0, "reason": "NO_TOKENS", "ping": {"status": "NO_TOKENS"}}
    assert s.pushes == []
    assert [c["args"]["p_reserve"] for c in s.db.rpc_calls if c["name"] == "plan_wake_ping"] == [False]


def test_8_차_판정과_횟수_세기_사이에_다른_요청이_먼저_보냈으면():
    """8-차: 판정과 횟수 세기 사이에 다른 요청이 먼저 보냈으면(reserve가 WAIT) 푸시를 안 보냄"""
    s = Setup(("WAKE_PING",))
    s.db.rpc_handlers["plan_wake_ping"] = lambda a: (
        {"status": "WAIT", "next_at": "2026-11-10T07:10:00Z"} if a["p_reserve"] else {"status": "OK"}
    )
    result = run(run_notify(s.db, SENDER, request(kind="WAKE_PING"), s.opts()))
    assert result == {"sent": 0, "failed": 0, "ping": {"status": "WAIT", "next_at": "2026-11-10T07:10:00Z"}}
    assert s.pushes == []


def test_8_33_판정은_통과했는데_횟수_세기_때_그새_일어났으면():
    """8-33: 판정은 통과했는데 횟수 세기(reserve) 때 그새 일어났으면(ALREADY_UP) 푸시를 안 보내고 ping.status ALREADY_UP으로 돌려줌 — 보낸 사람이 "이미 일어났어요"를 봄"""
    s = Setup(("WAKE_PING",))
    s.db.rpc_handlers["plan_wake_ping"] = lambda a: (
        {"status": "ALREADY_UP"} if a["p_reserve"] else {"status": "OK"}
    )
    result = run(run_notify(s.db, SENDER, request(kind="WAKE_PING"), s.opts()))
    assert result == {"sent": 0, "failed": 0, "ping": {"status": "ALREADY_UP"}}
    assert s.pushes == []
    assert [c["args"]["p_reserve"] for c in s.db.rpc_calls] == [False, True]  # 판정 → 횟수 세기에서 멈춤


def test_8_11_8_35_기상이_안_보이는_사람은_403_모르는_응답이나_db_오류는_500():
    """8-11 · 8-35: 기상이 안 보이는 사람(DB 판정 FORBIDDEN)은 403 — 이유를 안 알려 줌. 모르는 응답이나 DB 오류는 500"""
    forbidden = Setup(("WAKE_PING",))
    forbidden.db.rpc_handlers["plan_wake_ping"] = lambda a: {"status": "FORBIDDEN"}
    with pytest.raises(AppError) as e:
        run(run_notify(forbidden.db, SENDER, request(kind="WAKE_PING"), forbidden.opts()))
    assert_app_error(e, 403)
    assert e.value.message == "이 사람에게는 깨우기 알림을 보낼 수 없어요"
    assert forbidden.pushes == []

    def boom(a):
        raise RuntimeError("boom")

    for bad in (lambda a: {"status": "SOMETHING"}, lambda a: None, boom):
        s = Setup(("WAKE_PING",))
        s.db.rpc_handlers["plan_wake_ping"] = bad
        with pytest.raises(AppError) as err:
            run(run_notify(s.db, SENDER, request(kind="WAKE_PING"), s.opts()))
        assert_app_error(err, 500)
        assert s.pushes == []


def test_11_12_11_13_친구_요청은_받는_사람에게만_수락은_요청한_사람에게만():
    """11-12 · 11-13: 친구 요청은 받는 사람에게만, 수락은 요청한 사람에게만 — 요청 채널, 고정 문구"""
    s = Setup()
    run(run_notify(s.db, SENDER, request(kind="FRIEND_REQUEST"), s.opts()))
    run(run_notify(s.db, SENDER, request(kind="FRIEND_ACCEPTED"), s.opts()))
    assert all(p["channelId"] == "request_v1" and p["data"]["recipientUserId"] == TARGET for p in s.pushes)
    assert sorted({p["body"] for p in s.pushes}) == sorted(
        ["새 친구 요청이 도착했어요", "친구 요청이 수락됐어요"]
    )
    assert not any(p["to"] == T3 for p in s.pushes)  # 남의 토큰에는 안 감


def test_알람_id는_받는_사람의_알람이어야_함():
    """알람 id는 받는 사람의 알람이어야 함 (남의 알람 id로 알림을 꾸미지 못함)"""
    s = Setup()
    s.db.rows("alarms").append(
        {"alarm_id": "99999999-9999-4999-8999-999999999999", "user_id": OTHER, "alarm_type": "WAKE"}
    )
    with pytest.raises(AppError) as e:
        run(run_notify(s.db, SENDER, request(alarmId="99999999-9999-4999-8999-999999999999"), s.opts()))
    assert_app_error(e, 404)
    assert s.pushes == []


def test_11_22_받는_사람의_토큰이_없으면_no_tokens():
    """11-22: 받는 사람의 토큰이 없으면(권한 거부 · 앱 삭제) 오류가 아니라 { sent: 0, reason: NO_TOKENS }"""
    s = Setup()
    s.db.tables["push_tokens"] = []
    assert run(run_notify(s.db, SENDER, request(), s.opts())) == {
        "sent": 0,
        "failed": 0,
        "reason": "NO_TOKENS",
    }
    assert s.pushes == []


def test_11_17_15_나_없는_탈퇴한_권한_없는_차단된_대상은_모두_같은_403():
    """11-17 · 15-나: 없는 · 탈퇴한 · 권한 없는 · 차단된 대상은 모두 같은 403 — 권한 판정이 먼저라 사용자 존재 여부가 응답으로 드러나지 않음"""
    ghost = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def outcome(target, kind="ALARM_CHANGED", mutate=None):
        s = Setup()
        if mutate:
            mutate(s.db)
        try:
            run(run_notify(s.db, SENDER, request(kind=kind, targetUserId=target), s.opts()))
            return {"ok": True, "pushes": len(s.pushes)}
        except AppError as e:
            return {"status": e.status, "message": e.message, "pushes": len(s.pushes)}

    def withdraw(db):  # 탈퇴: 프로필이 사라지고 권한 판정도 거절
        db.tables["profiles"] = [p for p in db.rows("profiles") if p["user_id"] != TARGET]
        db.rpc_handlers["can_send_push"] = lambda a: False
        db.rpc_handlers["plan_wake_ping"] = lambda a: {"status": "FORBIDDEN"}

    for kind in ("ALARM_CHANGED", "FRIEND_REQUEST", "FRIEND_ACCEPTED", "WAKE_PING"):
        no_permission = outcome(OTHER, kind)  # 있지만 권한 없음(차단도 같은 거절)
        never_existed = outcome(ghost, kind)  # 처음부터 없는 id
        withdrawn = outcome(TARGET, kind, withdraw)
        assert no_permission["status"] == 403, kind
        assert never_existed == no_permission, kind
        assert withdrawn == no_permission, kind


def test_11_17_순서_권한이_있는_대상만_프로필_토큰을_읽음():
    """11-17(순서): 권한이 있는 대상만 프로필 · 토큰을 읽음 — 권한 거절이면 프로필 · 토큰 조회 없이 끝"""
    s = Setup()
    reads: list[str] = []
    original = s.db.from_

    def from_(table):
        reads.append(table)
        return original(table)

    s.db.from_ = from_
    with pytest.raises(AppError) as e:
        run(run_notify(s.db, SENDER, request(targetUserId=OTHER), s.opts()))
    assert_app_error(e, 403)
    assert reads == []
    assert s.pushes == []


def test_11_06_접수된_푸시는_영수증_표에_기록하고_만료_토큰은_바로_지움():
    """11-06: 접수된 푸시는 영수증 표에 기록하고(영수증은 15분 뒤 배치가 확인), 접수 단계에서 만료로 돌아온 토큰은 바로 지움"""
    s = Setup()

    def tickets(to, i=0):
        if to == T2:
            return {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}
        return {"status": "ok", "id": "ticket-1"}

    result = run(run_notify(s.db, SENDER, request(), s.opts(tickets)))
    assert result == {"sent": 1, "failed": 1}
    assert [t["token"] for t in s.db.rows("push_tokens") if t["user_id"] == TARGET] == [T1]
    assert len([t for t in s.db.rows("push_tokens") if t["user_id"] == OTHER]) == 1  # 다른 사람 토큰은 그대로
    assert [
        {k: t[k] for k in ("ticket_id", "user_id", "token", "kind")} for t in s.db.rows("push_tickets")
    ] == [{"ticket_id": "ticket-1", "user_id": TARGET, "token": T1, "kind": "ALARM_CHANGED"}]


def test_영수증_표에_넣다_실패해도_푸시_자체는_성공으로_돌려줌():
    """영수증 표에 넣다 실패해도 푸시 자체는 성공으로 돌려줌 (기록은 다음 배치를 위한 것)"""
    s = Setup()
    s.db.fail_on.append({"table": "push_tickets", "op": "insert"})
    assert run(run_notify(s.db, SENDER, request(), s.opts())) == {"sent": 2, "failed": 0}


def test_11_20_11_21_expo_장애는_한_번_다시_보내_보고_안_되면_push_unavailable():
    """11-20 · 11-21: Expo 장애는 한 번 다시 보내 보고 안 되면 요청은 성공 + PUSH_UNAVAILABLE, 설정 없음(FCM 키 · 401)은 PUSH_NOT_CONFIGURED"""
    s = Setup()
    no_key = run(
        run_notify(
            s.db,
            SENDER,
            request(),
            s.opts(
                lambda to, i=0: {
                    "status": "error",
                    "message": "no fcm",
                    "details": {"error": "InvalidCredentials"},
                }
            ),
        )
    )
    assert no_key == {"sent": 0, "failed": 2, "reason": "PUSH_NOT_CONFIGURED"}
    unauthorized = run(
        run_notify(
            s.db,
            SENDER,
            request(),
            NotifyOptions(
                access_token="WRONG",
                fetch_impl=fake_fetch(lambda req: response({}, 401)),
                sleep=no_sleep,
            ),
        )
    )
    assert unauthorized == {"sent": 0, "failed": 2, "reason": "PUSH_NOT_CONFIGURED"}
    down_fetch = fake_fetch(lambda req: response({}, 503))
    down = run(run_notify(s.db, SENDER, request(), NotifyOptions(fetch_impl=down_fetch, sleep=no_sleep)))
    assert down["reason"] == "PUSH_UNAVAILABLE"
    assert len(down_fetch.seen) == 2  # 처음 + 다시 한 번


def test_11_20_한_번_실패_뒤_다시_보내서_되면_성공():
    """11-20: 한 번 실패 뒤 다시 보내서 되면 성공"""
    s = Setup()
    attempts = []

    def handler(req):
        attempts.append(1)
        if len(attempts) == 1:
            return response({}, 502)
        return response({"data": [ok_all(b["to"]) for b in req.json]})

    result = run(
        run_notify(s.db, SENDER, request(), NotifyOptions(sleep=no_sleep, fetch_impl=fake_fetch(handler)))
    )
    assert result == {"sent": 2, "failed": 0}
    assert len(attempts) == 2


def test_권한_판정_db_함수가_실패하면_일반_문구의_500():
    """권한 판정 DB 함수가 실패하면 일반 문구의 500 (원문은 안 보임)"""
    s = Setup()

    def boom(a):
        raise RuntimeError('relation "secret_table" missing')

    s.db.rpc_handlers["can_send_push"] = boom
    with pytest.raises(AppError) as e:
        run(run_notify(s.db, SENDER, request()))
    assert_app_error(e, 500)
    assert "secret_table" not in e.value.message


def test_11_18_11_19_연달아_5건도_건마다_하나씩():
    """11-18 · 11-19: 연달아 5건도 건마다 하나씩(합치거나 건너뛰지 않음), 새벽이든 낮이든 시각으로 막지 않음 — 재촉 · 묶음 필드 없음"""
    s = Setup()
    for _ in range(5):
        run(run_notify(s.db, SENDER, request(kind="FRIEND_REQUEST"), s.opts()))
    assert s.call_count == 5
    assert len(s.pushes) == 10  # 폰 2대 × 5건
    assert all("collapseId" not in p and "ttl" not in p and "_contentAvailable" not in p for p in s.pushes)


# ---- 그룹 알림 ----


def group_setup(plan):
    s = Setup()
    s.db.rpc_handlers["plan_group_push"] = lambda a: plan
    # 받는 사람은 R1 · R2, 보낸 사람 쪽 TARGET과 앱이 지정해 본 OTHER의 토큰은 가면 안 되는 토큰
    s.db.tables["push_tokens"] = [
        {"user_id": TARGET, "token": T1},
        {"user_id": R1, "token": T3},
        {"user_id": R2, "token": T4},
        {"user_id": OTHER, "token": "ExponentPushToken[eeeeeeeeeeeeeeeeeeeeee]"},
    ]
    return s


def test_11_14_11_15_새_멤버_알림():
    """11-14 · 11-15: 새 멤버 알림 — 앱은 그룹 id만 보내고 받는 사람은 서버가 고름(앱이 넘긴 대상 id는 무시), 그룹 채널 · 그룹 이름만 든 문구(사람 이름 없음)"""
    s = group_setup(
        {"status": "OK", "group_name": "캡스톤팀", "appointment_title": None, "recipients": [R1, R2]}
    )
    req = read_notify_request(
        Reader.root({"kind": "GROUP_MEMBER_JOINED", "groupId": GROUP, "targetUserId": OTHER})
    )
    assert req == {"kind": "GROUP_MEMBER_JOINED", "groupId": GROUP}
    result = run(run_notify(s.db, SENDER, req, s.opts()))
    assert result == {"sent": 2, "failed": 0}
    assert s.db.rpc_calls == [
        {
            "name": "plan_group_push",
            "args": {
                "p_sender_id": SENDER,
                "p_group_id": GROUP,
                "p_kind": "GROUP_MEMBER_JOINED",
                "p_appointment_id": None,
            },
        }
    ]
    assert sorted([p["to"], p["data"]["recipientUserId"]] for p in s.pushes) == sorted([[T3, R1], [T4, R2]])
    assert all(
        p["title"] == "캡스톤팀"
        and p["body"] == "새 멤버가 들어왔어요"
        and p["channelId"] == "group_v1"
        and p["data"]["path"] == "/group"
        and p["data"]["groupId"] == GROUP
        for p in s.pushes
    )
    assert "수연" not in str(s.pushes)


def test_공동_약속_변경_알림():
    """공동 약속 변경 알림: 약속 id를 DB 함수에 넘기고 약속 제목이 든 문구, 알림 데이터에 약속 id"""
    s = group_setup(
        {"status": "OK", "group_name": "캡스톤팀", "appointment_title": "팀 회의", "recipients": [R1]}
    )
    result = run(
        run_notify(
            s.db, SENDER, {"kind": "GROUP_APPT_CHANGED", "groupId": GROUP, "appointmentId": APPT}, s.opts()
        )
    )
    assert result == {"sent": 1, "failed": 0}
    assert s.db.rpc_calls[0]["args"] == {
        "p_sender_id": SENDER,
        "p_group_id": GROUP,
        "p_kind": "GROUP_APPT_CHANGED",
        "p_appointment_id": APPT,
    }
    assert s.pushes[0]["body"] == "‘팀 회의’ 약속이 바뀌었어요"
    assert s.pushes[0]["data"] == {
        "kind": "GROUP_APPT_CHANGED",
        "recipientUserId": R1,
        "path": "/group",
        "groupId": GROUP,
        "appointmentId": APPT,
    }


def test_그룹_알림_권한_없음은_403_받는_사람이_없거나_토큰이_없으면_no_tokens():
    """그룹 알림 권한 없음(FORBIDDEN)은 403 — 토큰 조회 없음, 받는 사람이 아무도 없거나 토큰이 없으면 NO_TOKENS"""
    forbidden = group_setup({"status": "FORBIDDEN"})
    with pytest.raises(AppError) as e:
        run(
            run_notify(
                forbidden.db, SENDER, {"kind": "GROUP_MEMBER_JOINED", "groupId": GROUP}, forbidden.opts()
            )
        )
    assert_app_error(e, 403)
    assert forbidden.pushes == []
    assert "select push_tokens" not in forbidden.db.calls

    empty = group_setup({"status": "OK", "group_name": "혼자", "appointment_title": None, "recipients": []})
    assert run(
        run_notify(empty.db, SENDER, {"kind": "GROUP_MEMBER_JOINED", "groupId": GROUP}, empty.opts())
    ) == {"sent": 0, "failed": 0, "reason": "NO_TOKENS"}
    assert "select push_tokens" not in empty.db.calls

    no_tokens = group_setup(
        {"status": "OK", "group_name": "토큰없음", "appointment_title": None, "recipients": [OTHER]}
    )
    no_tokens.db.tables["push_tokens"] = []
    assert run(
        run_notify(no_tokens.db, SENDER, {"kind": "GROUP_MEMBER_JOINED", "groupId": GROUP}, no_tokens.opts())
    ) == {"sent": 0, "failed": 0, "reason": "NO_TOKENS"}


def test_그룹_알림_db_함수가_이상한_값을_돌려주면_500():
    """그룹 알림 DB 함수가 이상한 값을 돌려주면(모양 밖) 일반 문구의 500"""
    s = group_setup({"weird": True})
    with pytest.raises(AppError) as e:
        run(run_notify(s.db, SENDER, {"kind": "GROUP_MEMBER_JOINED", "groupId": GROUP}, s.opts()))
    assert_app_error(e, 500)


# ---- 요청 읽기 ----


def test_요청_읽기_종류별_필수_칸():
    """요청 읽기: 종류별 필수 칸 — 한 사람 종류는 targetUserId, 그룹 종류는 groupId(약속 변경은 appointmentId도), 모르는 종류 · 서버 전용 종류 · 이상한 UUID · 시각은 한 번에 알려 줌 (11-10)"""
    ok = read_notify_request(
        Reader.root(
            {
                "kind": "ALARM_CHANGED",
                "targetUserId": TARGET,
                "alarmId": ALARM,
                "previousAt": "2026-11-08T22:00:00Z",
                "newAt": "2026-11-08T22:10:00Z",
            }
        )
    )
    # 옛 앱이 보낸 시각은 모양만 확인하고 안 씀(제안 기록에서)
    assert ok == {"kind": "ALARM_CHANGED", "targetUserId": TARGET, "alarmId": ALARM}

    def issues(payload):
        try:
            read_notify_request(Reader.root(payload))
            return "no-error"
        except AppError as e:
            assert e.status == 400
            return ",".join(sorted(i["path"] for i in e.details["issues"]))

    assert issues({"kind": "SPAM", "targetUserId": "x"}) == "kind,targetUserId"
    assert issues({"kind": "WAKE_MISSED", "targetUserId": TARGET}) == "kind"  # 미확인 본인은 서버 배치만
    assert issues({"kind": "WAKE_PING"}) == "targetUserId"
    assert issues({"kind": "FRIEND_REQUEST", "targetUserId": "not-a-uuid"}) == "targetUserId"
    assert issues({"kind": "GROUP_MEMBER_JOINED"}) == "groupId"
    assert issues({"kind": "GROUP_APPT_CHANGED", "groupId": GROUP}) == "appointmentId"
    assert (
        issues(
            {"kind": "ALARM_CHANGED", "targetUserId": TARGET, "previousAt": "yesterday", "newAt": "x" * 50}
        )
        == "newAt,previousAt"
    )
    assert issues({}) == "kind"
    assert issues({"kind": "GROUP_APPT_CHANGED", "groupId": GROUP, "appointmentId": APPT}) == "no-error"


def test_알람_id는_알람_바뀜에만():
    """알람 id는 알람 바뀜에만 — 다른 종류에 실려 와도 읽을 때 버리고, 알림 데이터에도 안 실림 (남의 알람 id를 시험하는 길을 막음)"""
    parsed = read_notify_request(
        Reader.root({"kind": "FRIEND_REQUEST", "targetUserId": TARGET, "alarmId": ALARM})
    )
    assert parsed == {"kind": "FRIEND_REQUEST", "targetUserId": TARGET}
    s = Setup()
    s.db.rows("alarms").append({"alarm_id": "99999999-9999-4999-8999-999999999999", "user_id": OTHER})
    # 이미 읽은 요청에 알람 id가 남아 있어도(직접 부른 경우) 다른 종류는 알람을 조회하지도 데이터에 싣지도 않음
    result = run(
        run_notify(
            s.db,
            SENDER,
            request(kind="FRIEND_REQUEST", alarmId="99999999-9999-4999-8999-999999999999"),
            s.opts(),
        )
    )
    assert result["sent"] == 2
    assert all("alarmId" not in p["data"] for p in s.pushes)


def test_11_20_보내지_못한_이유는_서버_로그에_남고_잘_보내면_로그_없음(caplog):
    """11-20: 보내지 못한 이유(설정 없음 · Expo 장애)는 서버 로그에 남고, 잘 보내면 로그 없음"""
    s = Setup()

    def count():
        return len([r for r in caplog.records if r.levelno >= logging.ERROR])

    with caplog.at_level(logging.ERROR):
        run(
            run_notify(
                s.db,
                SENDER,
                request(),
                s.opts(
                    lambda to, i=0: {
                        "status": "error",
                        "message": "no fcm",
                        "details": {"error": "InvalidCredentials"},
                    }
                ),
            )
        )
        run(
            run_notify(
                s.db,
                SENDER,
                request(),
                NotifyOptions(fetch_impl=fake_fetch(lambda req: response({}, 503)), sleep=no_sleep),
            )
        )
        clean = count()
        run(run_notify(s.db, SENDER, request(), s.opts()))
        assert count() == clean
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert "PUSH_NOT_CONFIGURED" in messages[0]
    assert "PUSH_UNAVAILABLE" in messages[1]


def test_그룹_알림을_둘로_나눠_부를_수_있음():
    """그룹 알림을 둘로 나눠 부를 수 있음: 받는 사람 정하기(planGroupPush)와 보내기(sendGroupPush) — 보내기는 취소됨 문구 · 알림 데이터 change · 받는 사람마다 더할 값을 받음 (9-20 · 9-11)"""
    s = group_setup(
        {"status": "OK", "group_name": "캡스톤팀", "appointment_title": "팀 회의", "recipients": [R1, R2]}
    )
    req = {"kind": "GROUP_APPT_CHANGED", "groupId": GROUP, "appointmentId": APPT}
    plan = run(plan_group_push(s.db, SENDER, req))
    assert plan == {
        "recipients": [R1, R2],
        "silentRecipients": [],
        "groupName": "캡스톤팀",
        "appointmentTitle": "팀 회의",
    }
    assert len(s.pushes) == 0  # 정하기만으로는 안 보냄
    result = run(
        send_group_push(
            s.db,
            req,
            plan,
            s.opts(),
            {"change": "CANCELED", "perRecipient": {R2: {"needsConfirm": True}}},
        )
    )
    assert result == {"sent": 2, "failed": 0}
    got = sorted(
        (
            [p["body"], p["data"]["change"], p["data"].get("needsConfirm"), p["data"]["recipientUserId"]]
            for p in s.pushes
        ),
        key=lambda x: x[3],
    )
    assert got == [
        ["‘팀 회의’ 약속이 취소됐어요", "CANCELED", None, R1],
        ["‘팀 회의’ 약속이 취소됐어요", "CANCELED", True, R2],
    ]
    assert len(s.db.rpc_calls) == 1
    # 권한이 없으면 정하는 데서 403
    forbidden = group_setup({"status": "FORBIDDEN"})
    with pytest.raises(AppError) as e:
        run(plan_group_push(forbidden.db, SENDER, req))
    assert_app_error(e, 403)
    # 앱 요청에서는 change를 읽지 않음 — 앱이 취소됨 문구 · 데이터를 직접 만들 수 없음
    assert (
        read_notify_request(
            Reader.root(
                {"kind": "GROUP_APPT_CHANGED", "groupId": GROUP, "appointmentId": APPT, "change": "CANCELED"}
            )
        )
        == req
    )


T5 = "ExponentPushToken[eeeeeeeeeeeeeeeeeeeeee]"  # group_setup이 OTHER에게 둔 토큰


def test_조용한_데이터_푸시_알림을_끈_멤버_차단_쌍():
    """조용한 데이터 푸시: 알림을 끈 멤버 · 차단 쌍(silentRecipients)에게는 문구 없이 data만(알람 다시 맞추기), 보이는 알림은 설정대로 빠짐 — 보낸 수(sent)에는 안 더함"""
    s = group_setup(
        {
            "status": "OK",
            "group_name": "캡스톤팀",
            "appointment_title": "팀 회의",
            "recipients": [R1],
            "silent_recipients": [R2, OTHER],
        }
    )
    req = {"kind": "GROUP_APPT_CHANGED", "groupId": GROUP, "appointmentId": APPT}
    plan = run(plan_group_push(s.db, SENDER, req))
    assert plan["silentRecipients"] == [R2, OTHER]
    result = run(
        send_group_push(
            s.db,
            req,
            plan,
            s.opts(),
            {"change": "CHANGED", "perRecipient": {R2: {"needsConfirm": True}}},
        )
    )
    assert result == {"sent": 1, "failed": 0}  # 보이는 알림은 R1 한 곳
    visible = [b for b in s.pushes if "title" in b]
    quiet = [b for b in s.pushes if "title" not in b]
    assert [b["data"]["recipientUserId"] for b in visible] == [R1]
    # perRecipient 값(needsConfirm)은 조용한 쪽에도 실림
    assert [
        [
            b["to"],
            b["_contentAvailable"],
            b["data"]["recipientUserId"],
            b["data"]["silent"],
            b["data"]["change"],
            b["data"].get("needsConfirm"),
        ]
        for b in quiet
    ] == [[T4, True, R2, True, "CHANGED", True], [T5, True, OTHER, True, "CHANGED", None]]
    assert all("channelId" not in b and "sound" not in b for b in quiet)


def test_조용한_푸시만_있는_경우에도_보냄():
    """조용한 푸시만 있는 경우(보이는 받는 사람이 없음)에도 보냄 — 보이는 알림 수는 0 · NO_TOKENS, 받는 사람이 아예 없으면 보내지 않음"""
    s = group_setup(
        {
            "status": "OK",
            "group_name": "캡스톤팀",
            "appointment_title": "팀 회의",
            "recipients": [],
            "silent_recipients": [R1],
        }
    )
    req = {"kind": "GROUP_APPT_CHANGED", "groupId": GROUP, "appointmentId": APPT}
    plan = run(plan_group_push(s.db, SENDER, req))
    result = run(send_group_push(s.db, req, plan, s.opts(), {"change": "CHANGED"}))
    assert result == {"sent": 0, "failed": 0, "reason": "NO_TOKENS"}
    assert [p["data"]["recipientUserId"] for p in s.pushes] == [R1]
    none = group_setup(
        {
            "status": "OK",
            "group_name": "캡스톤팀",
            "appointment_title": "팀 회의",
            "recipients": [],
            "silent_recipients": [],
        }
    )
    plan_none = run(plan_group_push(none.db, SENDER, req))
    assert run(send_group_push(none.db, req, plan_none, none.opts())) == {
        "sent": 0,
        "failed": 0,
        "reason": "NO_TOKENS",
    }
    assert none.pushes == []


def test_조용한_푸시_이미_보이는_알림을_받는_사람이_조용한_목록에도_있으면():
    """조용한 푸시: 이미 보이는 알림을 받는 사람이 조용한 목록에도 있으면 보이는 알림 하나만, 조용한 푸시 실패는 failed에 합침, 목록의 문자열이 아닌 값은 버림"""
    dup = group_setup(
        {
            "status": "OK",
            "group_name": "캡스톤팀",
            "appointment_title": "팀 회의",
            "recipients": [R1],
            "silent_recipients": [R1, R2, 5, None],
        }
    )
    req = {"kind": "GROUP_APPT_CHANGED", "groupId": GROUP, "appointmentId": APPT}
    plan = run(plan_group_push(dup.db, SENDER, req))
    assert plan["silentRecipients"] == [R1, R2]  # 문자열이 아닌 값은 버림
    wire: list[dict] = []

    def handler(r):
        body = r.json
        wire.extend(body)
        return response(
            {
                "data": [
                    {"status": "ok", "id": "t"}
                    if "title" in b
                    else {"status": "error", "message": "x", "details": {"error": "MessageRateExceeded"}}
                    for b in body
                ]
            }
        )

    result = run(
        send_group_push(
            dup.db,
            req,
            plan,
            NotifyOptions(fetch_impl=fake_fetch(handler), sleep=no_sleep),
            {"change": "CHANGED"},
        )
    )
    assert len([b for b in wire if b["data"]["recipientUserId"] == R1]) == 1  # R1은 한 번만
    assert result == {"sent": 1, "failed": 1}  # 조용한 푸시(R2) 실패가 failed에 합쳐짐

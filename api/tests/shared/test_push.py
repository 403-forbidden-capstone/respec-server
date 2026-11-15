"""본 레포 `_shared/push.test.ts` + `_shared/push-send.test.ts`의 서버판 시험 (Expo 가짜 — 실제 푸시 안 나감)"""

import asyncio

from app.shared.push import (
    EXPO_PUSH_URL,
    EXPO_RECEIPTS_URL,
    SendOptions,
    fetch_expo_receipts,
    send_expo_messages,
    send_expo_push,
)

from .push_support import fake_fetch, no_sleep, reply, response

T1 = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
T2 = "ExpoPushToken[bbbbbbbbbbbbbbbbbbbbbb]"
MESSAGE = {
    "title": "알람이 바뀌었어요",
    "body": "수연님이 기상 알람을 5분 앞당겼어요",
    "data": {"kind": "ALARM_CHANGED", "alarmId": "a"},
}


def run(coro):
    return asyncio.run(coro)


# ---- push.test.ts ----


def test_expo_서버에_메시지_배열을_보냄_토큰별_한_건씩_선택_액세스_토큰():
    """Expo 서버에 메시지 배열을 보냄 (토큰별 한 건씩, 선택 액세스 토큰)"""
    fetch = fake_fetch(
        lambda req: response({"data": [{"status": "ok", "id": "1"}, {"status": "ok", "id": "2"}]})
    )
    result = run(send_expo_push([T1, T2], MESSAGE, SendOptions(access_token="EXPO_TOKEN", fetch_impl=fetch)))
    assert result == {"sent": 2, "failed": 0, "invalidTokens": []}
    seen = fetch.seen[0]
    assert seen.url == EXPO_PUSH_URL
    assert seen.headers["Authorization"] == "Bearer EXPO_TOKEN"
    body = seen.json
    assert len(body) == 2
    assert body[0] == {
        "to": T1,
        "title": MESSAGE["title"],
        "body": MESSAGE["body"],
        "data": MESSAGE["data"],
        "sound": "default",
        "priority": "high",
    }


def test_액세스_토큰이_없으면_authorization_머리글을_안_붙임():
    """액세스 토큰이 없으면 Authorization 머리글을 안 붙임"""
    fetch = fake_fetch(lambda req: response({"data": [{"status": "ok"}]}))
    run(send_expo_push([T1], MESSAGE, SendOptions(fetch_impl=fetch)))
    assert "Authorization" not in fetch.seen[0].headers


def test_만료된_토큰_devicenotregistered는_지울_목록에():
    """만료된 토큰(DeviceNotRegistered)은 지울 목록에"""
    fetch = reply(
        {
            "data": [
                {"status": "ok"},
                {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}},
            ]
        }
    )
    result = run(send_expo_push([T1, T2], MESSAGE, SendOptions(fetch_impl=fetch)))
    assert result == {"sent": 1, "failed": 1, "invalidTokens": [T2]}


def test_fcm_키가_없으면_invalidcredentials_설정_안_됨으로():
    """FCM 키가 없으면(InvalidCredentials) 설정 안 됨으로"""
    fetch = reply(
        {"data": [{"status": "error", "message": "no fcm", "details": {"error": "InvalidCredentials"}}]}
    )
    result = run(send_expo_push([T1], MESSAGE, SendOptions(fetch_impl=fetch)))
    assert result["sent"] == 0
    assert result["reason"] == "PUSH_NOT_CONFIGURED"


def test_액세스_토큰이_틀리면_http_401_403_장애가_아니라_설정_안_됨으로():
    """액세스 토큰이 틀리면(HTTP 401 · 403) 장애가 아니라 설정 안 됨으로"""
    for status in (401, 403):
        fetch = reply({"errors": [{"code": "UNAUTHORIZED", "message": "bad token"}]}, status)
        result = run(send_expo_push([T1, T2], MESSAGE, SendOptions(access_token="WRONG", fetch_impl=fetch)))
        assert [result["sent"], result["failed"], result.get("reason")] == [0, 2, "PUSH_NOT_CONFIGURED"]
    # 401이어도 본문을 못 읽으면(HTML 등) 마찬가지
    html = fake_fetch(lambda req: response(raw="<html>Unauthorized</html>", status=401))
    result = run(send_expo_push([T1], MESSAGE, SendOptions(fetch_impl=html)))
    assert result["reason"] == "PUSH_NOT_CONFIGURED"


def test_expo_서버가_안_되면_push_unavailable_네트워크_오류_5xx():
    """Expo 서버가 안 되면 PUSH_UNAVAILABLE (네트워크 오류 · 5xx)"""
    down = run(send_expo_push([T1], MESSAGE, SendOptions(fetch_impl=reply({"errors": [{"code": "X"}]}, 503))))
    assert [down["sent"], down["failed"], down["reason"]] == [0, 1, "PUSH_UNAVAILABLE"]
    offline = run(
        send_expo_push([T1], MESSAGE, SendOptions(fetch_impl=fake_fetch(lambda req: Exception("offline"))))
    )
    assert offline["reason"] == "PUSH_UNAVAILABLE"


def test_expo_토큰_모양이_아닌_것은_보내지_않고_실패로만_셈_100개씩_나눠_보냄():
    """Expo 토큰 모양이 아닌 것은 보내지 않고 실패로만 셈, 100개씩 나눠 보냄"""
    tokens = [f"ExponentPushToken[{str(i).zfill(22)}]" for i in range(150)]

    def handler(req):
        return response({"data": [{"status": "ok"} for _ in req.json]})

    fetch = fake_fetch(handler)
    result = run(send_expo_push([*tokens, "not-a-token"], MESSAGE, SendOptions(fetch_impl=fetch)))
    assert len(fetch.seen) == 2
    assert [result["sent"], result["failed"]] == [150, 1]


def test_토큰이_없으면_호출_없이_0():
    """토큰이 없으면 호출 없이 0"""

    def boom(req):
        raise AssertionError("호출하면 안 됨")

    result = run(send_expo_push([], MESSAGE, SendOptions(fetch_impl=fake_fetch(boom))))
    assert result == {"sent": 0, "failed": 0, "invalidTokens": []}


def test_조용한_데이터_푸시_silent_문구_소리_채널_없이_data만_content_available():
    """조용한 데이터 푸시(silent): 문구 · 소리 · 채널 없이 data만 + _contentAvailable, 보이는 알림과 섞여도 각자 모양대로"""
    fetch = fake_fetch(lambda req: response({"data": [{"status": "ok"}, {"status": "ok"}]}))
    result = run(
        send_expo_messages(
            [
                {
                    "to": T1,
                    "title": "캡스톤팀",
                    "body": "약속이 바뀌었어요",
                    "channelId": "group_v1",
                    "data": {"kind": "GROUP_APPT_CHANGED"},
                },
                {
                    "to": T2,
                    "title": "",
                    "body": "",
                    "silent": True,
                    "channelId": "group_v1",
                    "data": {"kind": "GROUP_APPT_CHANGED", "silent": True},
                },
            ],
            SendOptions(fetch_impl=fetch),
        )
    )
    assert [result["sent"], result["failed"]] == [2, 0]
    body = fetch.seen[0].json
    assert body[0] == {
        "to": T1,
        "title": "캡스톤팀",
        "body": "약속이 바뀌었어요",
        "data": {"kind": "GROUP_APPT_CHANGED"},
        "sound": "default",
        "priority": "high",
        "channelId": "group_v1",
    }
    assert body[1] == {
        "to": T2,
        "data": {"kind": "GROUP_APPT_CHANGED", "silent": True},
        "priority": "high",
        "_contentAvailable": True,
    }


def test_expo가_응답을_안_주면_시간_제한에서_끊고_그_묶음을_실패로_셈_한_번_더_보내_본_뒤에도_안_오면_push_unavailable():
    """Expo가 응답을 안 주면 시간 제한(timeoutMs)에서 끊고 그 묶음을 실패로 셈 — 한 번 더 보내 본 뒤에도 안 오면 PUSH_UNAVAILABLE"""
    import time

    timeouts: list[float] = []

    async def hang(url, *, method="POST", headers, body, timeout):
        timeouts.append(timeout)
        await asyncio.sleep(30)

    started = time.monotonic()
    result = run(
        send_expo_messages(
            [{"to": T1, **MESSAGE}], SendOptions(fetch_impl=hang, timeout_ms=20, sleep=no_sleep)
        )
    )
    assert time.monotonic() - started < 3  # 옵션이 쓰여 20ms 만에 끊김 (기본 10초였다면 20초 가까이)
    assert len(timeouts) == 2  # 처음 + 다시 보내기 1번
    assert timeouts == [0.02, 0.02]  # 시간 제한이 요청에 실림 (TS: signal이 AbortSignal)
    assert [result["sent"], result["failed"], result["reason"]] == [0, 1, "PUSH_UNAVAILABLE"]


# ---- push-send.test.ts ----


def test_send_expo_messages_메시지마다_제목_본문_데이터_채널을_따로_싣고_접수_번호를_토큰과_짝지어_돌려줌():
    """sendExpoMessages: 메시지마다 제목 · 본문 · 데이터 · 채널을 따로 싣고 접수 번호를 토큰과 짝지어 돌려줌"""
    fetch = fake_fetch(
        lambda req: response({"data": [{"status": "ok", "id": "t-1"}, {"status": "ok", "id": "t-2"}]})
    )
    result = run(
        send_expo_messages(
            [
                {
                    "to": T1,
                    "title": "A",
                    "body": "a",
                    "channelId": "group_v1",
                    "data": {"recipientUserId": "u1"},
                },
                {"to": T2, "title": "B", "body": "b", "data": {"recipientUserId": "u2"}},
            ],
            SendOptions(sleep=no_sleep, fetch_impl=fetch),
        )
    )
    assert result == {
        "sent": 2,
        "failed": 0,
        "invalidTokens": [],
        "tickets": [{"token": T1, "ticketId": "t-1"}, {"token": T2, "ticketId": "t-2"}],
    }
    body = fetch.seen[0].json
    assert body[0] == {
        "to": T1,
        "title": "A",
        "body": "a",
        "channelId": "group_v1",
        "data": {"recipientUserId": "u1"},
        "sound": "default",
        "priority": "high",
    }
    assert "channelId" not in body[1]  # 채널을 안 정한 메시지는 필드 없음


def test_send_expo_messages_네트워크_오류_5xx는_한_번_다시_보내고_401_403은_다시_보내지_않음_횟수는_바꿀_수_있음():
    """sendExpoMessages: 네트워크 오류 · 5xx는 한 번 다시 보내고(처음 + 1), 401 · 403은 다시 보내지 않음, 횟수는 바꿀 수 있음"""
    one = [{"to": T1, "title": "t", "body": "b"}]
    n = []

    def flaky(req):
        n.append(1)
        if len(n) == 1:
            return Exception("network down")
        return response({"data": [{"status": "ok", "id": "x"}]})

    result = run(send_expo_messages(one, SendOptions(sleep=no_sleep, fetch_impl=fake_fetch(flaky))))
    assert len(n) == 2
    assert result["sent"] == 1

    denied_fetch = fake_fetch(lambda req: response({}, 401))
    denied = run(send_expo_messages(one, SendOptions(sleep=no_sleep, fetch_impl=denied_fetch)))
    assert len(denied_fetch.seen) == 1
    assert denied["reason"] == "PUSH_NOT_CONFIGURED"

    down_fetch = fake_fetch(lambda req: response({}, 503))
    down = run(send_expo_messages(one, SendOptions(sleep=no_sleep, retries=2, fetch_impl=down_fetch)))
    assert len(down_fetch.seen) == 3
    assert [down["sent"], down["failed"], down.get("reason")] == [0, 1, "PUSH_UNAVAILABLE"]

    none_fetch = fake_fetch(lambda req: response({}, 503))
    run(send_expo_messages(one, SendOptions(sleep=no_sleep, retries=0, fetch_impl=none_fetch)))
    assert len(none_fetch.seen) == 1


def test_send_expo_messages_다시_보내기_사이에_쉬는_시간을_둠_처음_실패_뒤_한_번():
    """sendExpoMessages: 다시 보내기 사이에 쉬는 시간을 둠 (처음 실패 뒤 한 번)"""
    waits: list[int] = []

    async def sleep(ms: int) -> None:
        waits.append(ms)

    run(
        send_expo_messages(
            [{"to": T1, "title": "t", "body": "b"}],
            SendOptions(sleep=sleep, fetch_impl=fake_fetch(lambda req: response({}, 500))),
        )
    )
    assert len(waits) == 1
    assert 0 < waits[0] <= 2000


def test_send_expo_messages_접수_단계에서_devicenotregistered인_토큰은_invalid_tokens_접수_번호는_성공한_것만():
    """sendExpoMessages: 접수 단계에서 DeviceNotRegistered인 토큰은 invalidTokens, 접수 번호는 성공한 것만, Expo 토큰 모양이 아닌 것은 보내지 않고 실패로 셈"""
    fetch = fake_fetch(
        lambda req: response(
            {
                "data": [
                    {"status": "ok", "id": "t-1"},
                    {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}},
                ]
            }
        )
    )
    result = run(
        send_expo_messages(
            [
                {"to": T1, "title": "t", "body": "b"},
                {"to": T2, "title": "t", "body": "b"},
                {"to": "garbage", "title": "t", "body": "b"},
            ],
            SendOptions(sleep=no_sleep, fetch_impl=fetch),
        )
    )
    assert result["invalidTokens"] == [T2]
    assert result["tickets"] == [{"token": T1, "ticketId": "t-1"}]
    assert [result["sent"], result["failed"]] == [1, 2]


def test_fetch_expo_receipts_접수_번호를_get_receipts에_묶어_보내고_영수증_표를_돌려줌_300개씩():
    """fetchExpoReceipts: 접수 번호를 getReceipts에 묶어 보내고 영수증 표를 돌려줌 (300개씩, 응답 없는 번호는 아직 준비 안 됨)"""
    ids = [f"id-{i}" for i in range(450)]

    def handler(req):
        assert req.headers["Authorization"] == "Bearer EXPO_TOKEN"
        sent = req.json["ids"]
        data = {
            i: (
                {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}
                if i == "id-3"
                else {"status": "ok"}
            )
            for i in sent
            if i != "id-7"
        }
        return response({"data": data})

    fetch = fake_fetch(handler)
    out = run(fetch_expo_receipts(ids, access_token="EXPO_TOKEN", fetch_impl=fetch))
    assert out["ok"] is True
    assert [(c.url, len(c.json["ids"])) for c in fetch.seen] == [
        (EXPO_RECEIPTS_URL, 300),
        (EXPO_RECEIPTS_URL, 150),
    ]
    assert len(out["receipts"]) == 449
    assert out["receipts"]["id-3"]["status"] == "error"
    assert "id-7" not in out["receipts"]


def test_fetch_expo_receipts_요청이_실패하면_ok_false_번호가_없으면_부르지_않음():
    """fetchExpoReceipts: 요청이 실패하면 ok:false (영수증을 못 받은 것이지 만료가 아님), 번호가 없으면 부르지 않음"""
    assert run(fetch_expo_receipts(["a"], fetch_impl=fake_fetch(lambda req: response({}, 500)))) == {
        "ok": False,
        "receipts": {},
    }
    assert run(fetch_expo_receipts(["a"], fetch_impl=fake_fetch(lambda req: Exception("x"))))["ok"] is False

    def boom(req):
        raise AssertionError("호출하면 안 됨")

    assert run(fetch_expo_receipts([], fetch_impl=fake_fetch(boom))) == {"ok": True, "receipts": {}}

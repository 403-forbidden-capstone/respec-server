"""본 레포 supabase/functions/_shared/notify.ts의 서버판 (기준 b140f70) — 알림 보내기 본문 (모듈만: `notify` Edge 함수는 안 옮김, wake-check · 약속 수정이 이 모듈을 씀)

  요청: { kind, targetUserId?, groupId?, appointmentId?, alarmId? } (옛 앱이 보내던 previousAt · newAt은 읽기만 하고 안 씀 — ALARM_CHANGED의 시각은 제안 기록에서)
       한 사람에게 가는 종류(깨우기 · 알람 바뀜 · 친구 요청 · 수락)는 targetUserId, 그룹 종류(새 멤버 · 약속 변경)는 groupId(+ appointmentId)만 — 받는 사람은 서버가 고름
       깨우기는 groupId도 받음(어느 그룹의 G03에서 눌렀는지): 그 그룹에서 보이는 사람일 때만 되고, 푸시를 받은 사람이 누르면 자기 기상 확인 화면(/wake, H08)으로 감
       옛 앱이 보내던 title · body · data는 읽지 않음: 문구는 종류별로 서버가 고정하고(push_copy.py) 알림 데이터도 서버가 채움
  처리(한 사람): ① can_send_push(부른 사람, 받는 사람, kind) — 서버 권한 판정(DB 함수, service_role 전용). 없는 · 탈퇴한 · 권한 없는 · 차단된 대상은 모두 같은 403이라
       사용자 존재 여부가 응답으로 드러나지 않음. 깨우기만 can_send_push를 안 부르고 plan_wake_ping이 권한까지 한 곳에서 판정함 ② 받는 사람 프로필(문구에 쓸 이름 · 시간대)
       ③ 알람 바뀜(제안)은 받는 사람에게 방금(1시간 안) 보낸 대기 중 제안을 조회해 문구의 시각 · 알람 종류를 채움 ④ push_tokens 조회 ⑤ Expo Push로 보냄(실패하면 한 번 더)
       ⑥ 접수 번호를 push_tickets에 기록(영수증은 15분 뒤 배치가 확인) ⑦ 접수 단계에서 만료로 돌아온 토큰은 바로 지움
  처리(그룹): plan_group_push(DB 함수)가 보낸 사람이 그 그룹에 참여 중인지 보고 받는 사람 = 참여 중 멤버 − 보낸 사람 − 알림 끈 멤버 − 차단 쌍을 돌려줌 → 받는 사람마다 토큰 조회 · 발송
  깨우기(WAKE_PING)는 plan_wake_ping(판정만) → 토큰 확인 → plan_wake_ping(횟수 세기, 같은 쌍 잠금 아래 다시 판정) → 발송. 기상이 안 보이는 사람이면 403.
       못 보내는 이유(아직 안 울림 · 이미 일어남 · 5분 · 하루 3번 · 받기 꺼짐)는 오류가 아니라 응답의 ping.status로.
       토큰이 없거나, 센 뒤에 푸시가 한 건도 못 나가면(전송 실패) 센 횟수를 되돌리고 ping.status = NO_TOKENS
  응답: { sent, failed, reason?, ping? } — 토큰이 없거나 푸시 설정(FCM 키 · Expo 액세스 토큰)이 아직 없으면 오류가 아니라 { sent: 0, reason }

TS의 `can_send_push` · `plan_wake_ping`은 이 파일 안에서 DB 함수(rpc)로만 불림 (별도 TS 함수 없음).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..core.errors import AppError
from ..core.read import Reader
from .db import db_fail, must, safe_db_message
from .js import to_iso
from .push import FetchLike, OutgoingMessage, SendOptions, SendResult, SleepLike, send_expo_messages
from .push_copy import CHANNEL_IDS, CLIENT_KINDS, PATHS, compose_copy, is_group_kind
from .supabase import Db

log = logging.getLogger("respec.notify")

# 종류별 권한은 DB 함수 can_send_push(한 사람)와 plan_group_push(그룹)가 정함. 종류가 늘어도 여기는 이름만 더함
PUSH_KINDS = CLIENT_KINDS

DENIED: dict[str, str] = {
    "ALARM_CHANGED": "이 사람에게 알람 제안 알림을 보낼 수 없어요 (권한이 없거나 방금 보낸 제안이 없어요)",
    "WAKE_PING": "이 사람에게는 깨우기 알림을 보낼 수 없어요",
    "FRIEND_REQUEST": "이 사람에게는 친구 요청 알림을 보낼 수 없어요",
    "FRIEND_ACCEPTED": "이 사람에게는 수락 알림을 보낼 수 없어요",
    "GROUP_MEMBER_JOINED": "이 그룹에는 알림을 보낼 수 없어요",
    "GROUP_APPT_CHANGED": "이 그룹에는 알림을 보낼 수 없어요",
}

# NotifyRequest = dict {kind, targetUserId?, groupId?, appointmentId?, alarmId?} — 키 그대로
NotifyRequest = dict[str, Any]
# WakePing = dict {status: 'OK'|'NOT_RINGING'|'ALREADY_UP'|'WAIT'|'DAILY_LIMIT'|'OFF'|'NO_TOKENS', count?, limit?, next_at?, resets_at?}
WakePing = dict[str, Any]
# NotifyResult = dict {sent, failed, reason?, ping?}
NotifyResult = dict[str, Any]

PING_KEYS = ("count", "limit", "next_at", "resets_at")
PING_STATUSES = frozenset({"OK", "NOT_RINGING", "ALREADY_UP", "WAIT", "DAILY_LIMIT", "OFF"})

INTERNAL_PROCESS_MESSAGE = "데이터를 처리하지 못했어요. 잠시 후 다시 시도해 주세요"


def read_notify_request(root: Reader) -> NotifyRequest:
    """요청을 읽고 문제가 있으면 한 번에 던짐 (root.finish() 포함)"""
    kind = root.one_of("kind", CLIENT_KINDS, required=True)
    target_user_id = root.uuid("targetUserId")
    group_id = root.uuid("groupId")
    appointment_id = root.uuid("appointmentId")
    alarm_id = root.uuid("alarmId")
    root.date(
        "previousAt"
    )  # 옛 앱이 보내던 값 — 모양만 확인하고 안 씀 (문구의 시각은 서버가 제안 기록에서 가져옴)
    root.date("newAt")

    def absent(key: str) -> bool:  # 값이 틀려서 없는 것(이미 문제로 기록됨)과 아예 안 보낸 것을 가름
        return not root.has(key) or root.is_null(key)

    if kind:
        if is_group_kind(kind):
            if absent("groupId"):
                root.problem_at("groupId", "필수 값이에요")
            if kind == "GROUP_APPT_CHANGED" and absent("appointmentId"):
                root.problem_at("appointmentId", "필수 값이에요")
        elif absent("targetUserId"):
            root.problem_at("targetUserId", "필수 값이에요")
    root.finish()
    if not kind:
        raise AppError("VALIDATION_FAILED", "입력값을 확인해 주세요")
    if is_group_kind(kind):
        if not group_id or (kind == "GROUP_APPT_CHANGED" and not appointment_id):
            raise AppError("VALIDATION_FAILED", "입력값을 확인해 주세요")
        # 앱이 넘긴 받는 사람은 버림
        return {
            "kind": kind,
            "groupId": group_id,
            **({"appointmentId": appointment_id} if kind == "GROUP_APPT_CHANGED" and appointment_id else {}),
        }
    if not target_user_id:
        raise AppError("VALIDATION_FAILED", "입력값을 확인해 주세요")
    return {
        "kind": kind,
        "targetUserId": target_user_id,
        **({"groupId": group_id} if kind == "WAKE_PING" and group_id else {}),  # 깨우기만 그룹 id를 가짐
        # 알람 id는 알람 바뀜에만 (다른 종류에 실어 남의 알람 id를 시험하지 못하게)
        **({"alarmId": alarm_id} if kind == "ALARM_CHANGED" and alarm_id else {}),
    }


@dataclass
class NotifyOptions:
    access_token: str | None = None
    fetch_impl: FetchLike | None = None
    sleep: SleepLike | None = None
    now: datetime | None = None  # 문구의 '내일' 같은 말을 가를 기준 시각(시험용, 기본 지금)


async def run_notify(
    service: Db, sender_id: str, req: NotifyRequest, options: NotifyOptions | None = None
) -> NotifyResult:
    options = options or NotifyOptions()
    if is_group_kind(req["kind"]):
        return await _notify_group(service, sender_id, req, options)
    return await _notify_one(service, sender_id, req, options)


def _no_tokens(with_ping: bool = False) -> NotifyResult:
    return {
        "sent": 0,
        "failed": 0,
        "reason": "NO_TOKENS",
        **({"ping": {"status": "NO_TOKENS"}} if with_ping else {}),
    }


def _shape(value: Any) -> str:
    """RPC 응답을 로그에 남길 때 모양만 — 응답에 받는 사람 id 목록(recipients)이 있어 통째로 찍지 않음 (Edge는 JSON 전체를 찍었음)"""
    if isinstance(value, dict):
        status = value.get("status")
        return (
            f"dict status={status!r} keys={sorted(value)}"
            if isinstance(status, str | None)
            else f"dict keys={sorted(value)}"
        )
    return type(value).__name__


async def _plan_ping(
    service: Db, sender_id: str, target: str, reserve: bool, group_id: str | None = None
) -> dict[str, Any]:
    """깨우기 판정 (DB 함수 한 군데). 기상이 안 보이는 사람이면 403(이유 구분 없음), 모르는 응답이면 500.
    앱에 돌려줄 값은 약속한 칸만(send_id 같은 서버 안쪽 값은 뺌). 돌려주는 값: {ping, sendId?, groupId?}"""
    r = await service.rpc(
        "plan_wake_ping",
        {
            "p_sender": sender_id,
            "p_target": target,
            "p_reserve": reserve,
            **({"p_group_id": group_id} if group_id else {}),
        },
    )
    if r.error:
        db_fail("plan_wake_ping", r.error)
    data = r.data if isinstance(r.data, dict) else None
    status = data.get("status") if data is not None else None
    if status == "FORBIDDEN":
        raise AppError("FORBIDDEN", DENIED["WAKE_PING"])
    if not isinstance(status, str) or status not in PING_STATUSES:
        log.error("[notify] plan_wake_ping 응답이 약속과 다름 %s", _shape(r.data))
        raise AppError("INTERNAL", INTERNAL_PROCESS_MESSAGE)
    assert data is not None
    ping: WakePing = {"status": status}
    for k in PING_KEYS:
        if data.get(k) is not None:
            ping[k] = data[k]
    send_id = data.get("send_id")
    out: dict[str, Any] = {"ping": ping}
    if isinstance(send_id, int | float) and not isinstance(send_id, bool):
        out["sendId"] = send_id
    if isinstance(data.get("group_id"), str):
        out["groupId"] = data["group_id"]
    return out


async def _notify_one(
    service: Db, sender_id: str, req: NotifyRequest, options: NotifyOptions
) -> NotifyResult:
    target = req["targetUserId"]
    kind = req["kind"]
    wake = kind == "WAKE_PING"
    if wake:
        # 깨우기: 권한(기상이 보이는 사람)과 보낼 수 있는 때를 plan_wake_ping 한 곳에서 먼저 (토큰 조회 전에 — 못 보내는 이유를 보낸 쪽에 알려 주려고)
        verdict = await _plan_ping(service, sender_id, target, False, req.get("groupId"))
        if verdict["ping"]["status"] != "OK":
            return {"sent": 0, "failed": 0, "ping": verdict["ping"]}
    else:
        # 권한 판정이 먼저, DB 함수 한 군데. 없는 · 탈퇴한 사람은 권한 행이 없어 권한 없음 · 차단과 같은 거절이 됨
        allowed = await service.rpc(
            "can_send_push", {"p_sender_id": sender_id, "p_target_id": target, "p_kind": kind}
        )
        if allowed.error:
            db_fail("can_send_push", allowed.error)
        if allowed.data is not True:
            raise AppError("FORBIDDEN", DENIED[kind])

    profiles = must(
        "profiles",
        await service.from_("profiles")
        .select("user_id, nickname, timezone")
        .in_("user_id", [target, sender_id]),
    )
    target_profile = next((p for p in profiles if p["user_id"] == target), None)
    if target_profile is None:
        return _no_tokens(wake)  # 판정과 조회 사이에 탈퇴한 경우뿐

    # 알람 바뀜(제안): 문구의 알람 종류 · 이전 → 새 시각은 요청이 아니라 방금 보낸 대기 중 제안 기록에서 — 시각을 꾸밀 수 없음.
    # 알람 id를 같이 받았으면 그 알람의 제안이어야 함
    proposal: dict[str, Any] | None = None
    if kind == "ALARM_CHANGED":
        query = (
            service.from_("alarm_proposals")
            .select("proposal_id, alarm_id, from_at, to_at")
            .eq("proposer_id", sender_id)
            .eq("owner_id", target)
            .eq("status", "PENDING")
            # DB의 can_send_push와 같은 1시간 (options.now는 문구용이라 안 씀)
            .gte("created_at", to_iso(datetime.now(UTC) - timedelta(hours=1)))
        )
        if req.get("alarmId"):
            query = query.eq("alarm_id", req["alarmId"])
        found = must("alarm_proposals", await query.order("created_at", ascending=False).limit(1))
        if not found:
            raise AppError("NOT_FOUND", "보낸 제안을 찾을 수 없어요")
        alarm = must(
            "alarm lookup",
            await service.from_("alarms")
            .select("alarm_type")
            .eq("alarm_id", found[0]["alarm_id"])
            .eq("user_id", target)
            .maybe_single(),
        )
        proposal = {**found[0], "alarm_type": alarm.get("alarm_type") if alarm else None}

    rows = must("push_tokens", await service.from_("push_tokens").select("token").eq("user_id", target))
    if not rows:
        return _no_tokens(wake)  # 토큰이 없으면 횟수를 안 셈

    # 보내기 직전에 횟수를 셈 — 같은 쌍 잠금 아래에서 다시 판정하므로 동시에 두 번 눌러도 한 번만 나감
    reserved: dict[str, Any] | None = None
    if wake:
        reserved = await _plan_ping(service, sender_id, target, True, req.get("groupId"))
        if reserved["ping"]["status"] != "OK":
            return {"sent": 0, "failed": 0, "ping": reserved["ping"]}

    sender_profile = next((p for p in profiles if p["user_id"] == sender_id), None)
    copy = compose_copy(
        kind,
        {
            "senderName": sender_profile.get("nickname") if sender_profile else None,
            **(
                {
                    "previousAt": proposal["from_at"],
                    "newAt": proposal["to_at"],
                    "alarmType": proposal["alarm_type"],
                    "now": options.now,
                }
                if proposal
                else {}
            ),
            "timeZone": target_profile.get("timezone"),
        },
    )
    path = PATHS[kind]  # 깨우기를 누르면 받는 사람의 기상 확인(H08) — 보낸 사람이 본 그룹 화면(G03)이 아님
    data = {
        "kind": kind,
        "recipientUserId": target,
        "path": path,
        **({"alarmId": proposal["alarm_id"], "proposalId": proposal["proposal_id"]} if proposal else {}),
    }
    messages: list[OutgoingMessage] = [
        {"to": r["token"], **copy, "channelId": CHANNEL_IDS[kind], "data": data} for r in rows
    ]
    delivered = await deliver(service, kind, messages, {r["token"]: target for r in rows}, options)
    if reserved is None:
        return delivered
    if delivered["sent"] == 0:
        # 센 뒤인데 한 건도 못 나감(전송 실패 · 푸시 설정 없음) — 받는 사람이 못 받은 깨우기가 하루 3번을 쓰지 않게 센 줄을 지움
        if "sendId" in reserved:
            undo = await service.from_("push_sends").delete().eq("send_id", reserved["sendId"])
            if undo.error:
                log.error("[notify] 깨우기 횟수 되돌리기 실패 %s", safe_db_message(undo.error.get("message")))
        return {**delivered, "ping": {"status": "NO_TOKENS"}}
    return {**delivered, "ping": reserved["ping"]}


async def _notify_group(
    service: Db, sender_id: str, req: NotifyRequest, options: NotifyOptions
) -> NotifyResult:
    return await send_group_push(service, req, await plan_group_push(service, sender_id, req), options)


# GroupPlan = dict {recipients, silentRecipients, groupName, appointmentTitle}
GroupPlan = dict[str, Any]


async def plan_group_push(service: Db, sender_id: str, req: NotifyRequest) -> GroupPlan:
    """그룹 알림의 받는 사람 · 문구 재료를 DB 함수로 정함 — 권한이 없으면 403. 보내기와 나눈 이유: 약속을 지우기 전에 받는 사람을 정해 두고
    (지운 뒤엔 약속이 없음) 지운 다음에 보내려고. silentRecipients = 보이는 알림이 빠진 참여자(알림 끈 멤버 · 차단 쌍) — 알람만 다시 맞추게 조용한 데이터 푸시 (약속 변경만)"""
    planned = await service.rpc(
        "plan_group_push",
        {
            "p_sender_id": sender_id,
            "p_group_id": req["groupId"],
            "p_kind": req["kind"],
            "p_appointment_id": req.get("appointmentId"),
        },
    )
    if planned.error:
        db_fail("plan_group_push", planned.error)
    plan = planned.data if isinstance(planned.data, dict) else {}
    if plan.get("status") == "FORBIDDEN":
        raise AppError("FORBIDDEN", DENIED[req["kind"]])
    recipients = plan.get("recipients")
    if (
        plan.get("status") != "OK"
        or not isinstance(recipients, list)
        or not all(isinstance(r, str) for r in recipients)
    ):
        log.error("[notify] plan_group_push 응답이 약속과 다름 %s", _shape(planned.data))
        raise AppError("INTERNAL", INTERNAL_PROCESS_MESSAGE)
    silent_raw = plan.get("silent_recipients")
    silent = [r for r in silent_raw if isinstance(r, str)] if isinstance(silent_raw, list) else []
    group_name = plan.get("group_name")
    appointment_title = plan.get("appointment_title")
    return {
        "recipients": recipients,
        "silentRecipients": silent,
        "groupName": group_name if isinstance(group_name, str) else None,
        "appointmentTitle": appointment_title if isinstance(appointment_title, str) else None,
    }


async def send_group_push(
    service: Db,
    req: NotifyRequest,
    plan: GroupPlan,
    options: NotifyOptions,
    extra: dict[str, Any] | None = None,
) -> NotifyResult:
    """정해 둔 받는 사람의 토큰으로 보냄. extra.change = 바뀜 / 취소됨(문구 · 알림 데이터 `change`),
    extra.perRecipient = 받는 사람마다 알림 데이터에 더할 값(예: 확인이 필요한 사람) {user_id: dict}"""
    extra = extra or {}
    quiet_ids = [i for i in plan["silentRecipients"] if i not in plan["recipients"]]
    if not plan["recipients"] and not quiet_ids:
        return _no_tokens()

    rows = must(
        "push_tokens",
        await service.from_("push_tokens")
        .select("user_id, token")
        .in_("user_id", [*plan["recipients"], *quiet_ids]),
    )
    if not rows:
        return _no_tokens()

    # 문구에는 그룹 이름 · 약속 제목만 (사람 이름 없음). 받는 사람마다 알림 데이터의 받는 사람 id가 다름
    kind = req["kind"]
    change = extra.get("change")
    copy = compose_copy(
        kind,
        {
            "groupName": plan["groupName"],
            "appointmentTitle": plan["appointmentTitle"],
            **({"change": change} if change else {}),
        },
    )
    base = {
        "kind": kind,
        "path": PATHS[kind],
        "groupId": req["groupId"],
        **({"appointmentId": req["appointmentId"]} if req.get("appointmentId") else {}),
        **({"change": change} if change else {}),
    }
    per_recipient = extra.get("perRecipient") or {}
    owners = {r["token"]: r["user_id"] for r in rows}
    messages: list[OutgoingMessage] = [
        {
            "to": r["token"],
            **copy,
            "channelId": CHANNEL_IDS[kind],
            "data": {**base, **(per_recipient.get(r["user_id"]) or {}), "recipientUserId": r["user_id"]},
        }
        for r in rows
        if r["user_id"] in plan["recipients"]
    ]
    # 알림을 끈 멤버 · 차단 쌍: 보이는 알림은 없어도 알람은 다시 맞춰야 하므로 문구 없는 데이터 푸시만
    quiet: list[OutgoingMessage] = [
        {
            "to": r["token"],
            "title": "",
            "body": "",
            "silent": True,
            "data": {
                **base,
                **(per_recipient.get(r["user_id"]) or {}),
                "silent": True,
                "recipientUserId": r["user_id"],
            },
        }
        for r in rows
        if r["user_id"] in quiet_ids
    ]

    shown = await deliver(service, kind, messages, owners, options) if messages else _no_tokens()
    if not quiet:
        return shown
    # 보낸 수(sent)에는 안 더함 — '알림이 간 참여자 수'는 보이는 알림만. 실패는 합침
    hidden = await deliver(service, kind, quiet, owners, options)
    return {**shown, "failed": shown["failed"] + hidden["failed"]}


async def deliver(
    service: Db,
    kind: str,
    messages: list[OutgoingMessage],
    owners: dict[str, str],
    options: NotifyOptions,
) -> NotifyResult:
    """보내고, 접수 번호를 영수증 표에 남기고, 접수 단계에서 만료로 돌아온 토큰은 지움"""
    result: SendResult = await send_expo_messages(
        messages, SendOptions(options.access_token, options.fetch_impl, None, options.sleep)
    )

    # 영수증 기록이 안 돼도 푸시는 이미 나갔으므로 오류로 만들지 않음 (다음 배치에서 확인할 대상이 빠질 뿐)
    if result["tickets"]:
        ins = await service.from_("push_tickets").insert(
            [
                {
                    "ticket_id": t["ticketId"],
                    "user_id": owners.get(t["token"]),
                    "token": t["token"],
                    "kind": kind,
                }
                for t in result["tickets"]
            ]
        )
        if ins.error:
            log.error("[notify] 푸시 영수증 기록 실패 %s", safe_db_message(ins.error.get("message")))

    # 앱을 지웠거나 토큰이 바뀌어 못 받는 토큰은 지움
    if result["invalidTokens"]:
        dele = await service.from_("push_tokens").delete().in_("token", result["invalidTokens"])
        if dele.error:
            log.error("[notify] 만료 토큰 삭제 실패 %s", safe_db_message(dele.error.get("message")))

    # 보내지 못한 이유를 서버 로그에 남김 — 앱은 푸시 실패를 무시하므로 FCM 키 없음 · Expo 장애를 운영자가 알 길이 이것뿐
    if result.get("reason"):
        log.error(
            "[notify] 푸시 전송 실패 %s %s sent=%d failed=%d",
            kind,
            result["reason"],
            result["sent"],
            result["failed"],
        )
    return {
        "sent": result["sent"],
        "failed": result["failed"],
        **({"reason": result["reason"]} if result.get("reason") else {}),
    }

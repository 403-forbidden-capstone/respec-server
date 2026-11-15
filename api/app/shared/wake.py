"""본 레포 supabase/functions/_shared/wake.ts의 서버판 (기준 b140f70) — wake-check 본문

① MISSED 기록: find_missed_wake_alarms(DB 함수)가 "마지막으로 울리기 시작한 때(알람 시각 또는 마지막 미루기가 끝나 다시 울린 때) + 30분"이 지났는데
     기상 확인이 없는 알람을 찾음 → alarm_events에 MISSED로 기록. client_event_id = event_key라서 겹쳐 돌거나 다시 돌아도 한 번만 들어감
② 본인 알림: 이번에 새로 기록된 MISSED의 주인에게 '기상 확인 / 기상 확인이 안 됐어요' — 친구가 아니라 본인에게 먼저
③ 친구 알림: claim_wake_nudges(DB 함수)가 받을 사람을 정함 — 미확인 10분 뒤 '깨워 볼까요?', 늦게 확인했으면 후속 '일어났어요' 1회.
     받는 사람은 DB가 정하고 push_sends에 남기면서 돌려주므로 5분 배치가 겹쳐 돌아도 한 번만 나감. 남기는 것이 먼저라 보내기가 실패해도 다시 보내지 않음
푸시가 실패해도 MISSED 기록은 그대로 (푸시는 일이 끝난 뒤의 알림이라 오류로 만들지 않고 서버 로그에만)
"""

import logging
from datetime import datetime
from typing import Any

from .db import db_fail, must
from .js import to_iso
from .notify import NotifyOptions, deliver
from .push import OutgoingMessage
from .push_copy import CHANNEL_IDS, PATHS, compose_copy
from .supabase import Db

log = logging.getLogger("respec.wake")

# find_missed_wake_alarms 반환 행 (MissedRow, dict — JSON 키 그대로). appointment_id · group_id · appointment_at · participant_id는
# 약속 없는 알람이면 None: alarm_id · user_id · participant_id · appointment_id · group_id · appointment_at · scheduled_at · missed_at · event_key
MissedRow = dict[str, Any]
# claim_wake_nudges 반환 행 (NudgeRow): {event_key, kind: 'NUDGE'|'UP', recipient_id, sleeper_id, group_id}
NudgeRow = dict[str, Any]
# WakeCheckResult = {checked, recorded, pushed, claimed, nudged}
#   claimed = 이번에 친구 알림 대상으로 정한 (받는 사람, 안 일어난 사람) 쌍 수, nudged = 실제로 나간 기기 수 (토큰이 없거나 전송이 실패하면 claimed보다 적음)
WakeCheckResult = dict[str, int]

LOOKBACK_HOURS = 24  # 오래된 알람이 한꺼번에 MISSED로 쏟아지지 않게 (DB 함수 기본값과 같음)

# .in()에 한꺼번에 넣는 id 수 (요청 주소가 너무 길어지지 않게)
IN_CHUNK = 100


async def _select_in(
    service: Db, table: str, columns: str, column: str, ids: list[str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(0, len(ids), IN_CHUNK):
        out.extend(must(table, await service.from_(table).select(columns).in_(column, ids[i : i + IN_CHUNK])))
    return out


async def _push_missed_to_self(service: Db, rows: list[MissedRow], push: NotifyOptions) -> int:
    """본인 알림: 이번에 새로 기록된 MISSED의 주인마다 한 번 (한 사람의 알람이 둘 이상 같이 미확인이어도 한 번). 보낸 기기 수를 돌려줌"""
    users = list(dict.fromkeys(r["user_id"] for r in rows))
    if not users:
        return 0
    tokens = await _select_in(service, "push_tokens", "user_id, token", "user_id", users)
    if not tokens:
        return 0
    copy = compose_copy("WAKE_MISSED", {})
    messages: list[OutgoingMessage] = [
        {
            "to": t["token"],
            **copy,
            "channelId": CHANNEL_IDS["WAKE_MISSED"],
            "data": {"kind": "WAKE_MISSED", "recipientUserId": t["user_id"], "path": PATHS["WAKE_MISSED"]},
        }
        for t in tokens
    ]
    result = await deliver(service, "WAKE_MISSED", messages, {t["token"]: t["user_id"] for t in tokens}, push)
    return result["sent"]


async def _record_missed(service: Db, now: datetime, push: NotifyOptions) -> dict[str, int]:
    # 이름 인자(p_now · p_lookback_hours)는 DB 함수 매개변수 이름과 같아야 함 — 매개변수 이름을 바꾸면 여기도 같이
    found = await service.rpc(
        "find_missed_wake_alarms", {"p_now": to_iso(now), "p_lookback_hours": LOOKBACK_HOURS}
    )
    if found.error:
        db_fail("find_missed_wake_alarms", found.error)
    rows: list[MissedRow] = found.data or []
    if not rows:
        return {"checked": 0, "recorded": 0, "pushed": 0}

    # input_source는 비움 (폰 입력이 아니라 서버 판정). 이미 기록된 event_key는 건너뜀 (ON CONFLICT DO NOTHING)
    events = [
        {
            "alarm_id": r["alarm_id"],
            "user_id": r["user_id"],
            "event_type": "MISSED",
            "event_at": r["missed_at"],
            "sequence_no": 1,
            "client_event_id": r["event_key"],
            "input_source": None,
        }
        for r in rows
    ]
    inserted = await (
        service.from_("alarm_events")
        .upsert(events, on_conflict="client_event_id", ignore_duplicates=True)
        .select("client_event_id")
    )
    if inserted.error:
        db_fail("alarm_events MISSED", inserted.error)

    # 이번에 새로 기록된 것만 푸시 대상 (다시 돌았을 때 같은 푸시가 두 번 가지 않게)
    fresh = {r["client_event_id"] for r in (inserted.data or [])}
    pushed = 0
    try:
        pushed = await _push_missed_to_self(service, [r for r in rows if r["event_key"] in fresh], push)
    except Exception as e:  # noqa: BLE001 — 푸시는 일이 끝난 뒤의 알림
        log.error("[wake-check] 본인 미확인 푸시 실패 %s", getattr(e, "code", None) or type(e).__name__)
    return {"checked": len(rows), "recorded": len(fresh), "pushed": pushed}


async def _push_nudges(service: Db, now: datetime, push: NotifyOptions) -> dict[str, int]:
    """친구 알림: 받을 사람을 DB가 정해 돌려주면 종류별로 묶어 보냄. 이름은 안 일어난(일어난) 사람 것만 문구에"""
    claimed = await service.rpc("claim_wake_nudges", {"p_now": to_iso(now)})
    if claimed.error:
        db_fail("claim_wake_nudges", claimed.error)
    rows: list[NudgeRow] = claimed.data or []
    if not rows:
        return {"claimed": 0, "sent": 0}

    sent = 0
    try:
        profiles = await _select_in(
            service,
            "profiles",
            "user_id, nickname",
            "user_id",
            list(dict.fromkeys(r["sleeper_id"] for r in rows)),
        )
        names = {p["user_id"]: p["nickname"] for p in profiles}
        tokens = await _select_in(
            service,
            "push_tokens",
            "user_id, token",
            "user_id",
            list(dict.fromkeys(r["recipient_id"] for r in rows)),
        )
        for kind, row_kind in (("WAKE_NUDGE", "NUDGE"), ("WAKE_NUDGE_UP", "UP")):
            messages: list[OutgoingMessage] = [
                {
                    "to": t["token"],
                    **compose_copy(kind, {"subjectName": names.get(r["sleeper_id"])}),
                    "channelId": CHANNEL_IDS[kind],
                    "data": {
                        "kind": kind,
                        "recipientUserId": r["recipient_id"],
                        "path": PATHS[kind],
                        "groupId": r["group_id"],
                        "subjectUserId": r["sleeper_id"],
                    },
                }
                for r in rows
                if r["kind"] == row_kind
                for t in tokens
                if t["user_id"] == r["recipient_id"]
            ]
            if messages:
                result = await deliver(
                    service, kind, messages, {t["token"]: t["user_id"] for t in tokens}, push
                )
                sent += result["sent"]
    except Exception as e:  # noqa: BLE001
        log.error("[wake-check] 친구 미확인 푸시 실패 %s", getattr(e, "code", None) or type(e).__name__)
    if sent < len(rows):
        # 토큰이 없거나 전송이 실패한 몫 — 기록이 먼저라 다시 보내지 않음
        log.error("[wake-check] 친구 알림 대상보다 적게 나감 claimed=%d sent=%d", len(rows), sent)
    return {"claimed": len(rows), "sent": sent}


async def run_wake_check(service: Db, now: datetime, *, push: NotifyOptions | None = None) -> WakeCheckResult:
    """①②와 ③은 서로 안 막음: 한쪽이 실패해도 다른 쪽은 돌리고, 실패는 그 뒤에 그대로 던짐"""
    push = push or NotifyOptions()
    failure: Exception | None = None
    recorded = {"checked": 0, "recorded": 0, "pushed": 0}
    nudges = {"claimed": 0, "sent": 0}
    try:
        recorded = await _record_missed(service, now, push)
    except Exception as e:  # noqa: BLE001
        failure = e
    try:
        nudges = await _push_nudges(service, now, push)
    except Exception as e:  # noqa: BLE001
        if failure is None:
            failure = e
    if failure is not None:
        raise failure
    return {**recorded, "claimed": nudges["claimed"], "nudged": nudges["sent"]}

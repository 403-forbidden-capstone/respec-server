"""본 레포 supabase/functions/_shared/push-copy.ts의 서버판 (기준 b140f70) — 푸시 종류 표 (문구 · 안드로이드 채널 · 누르면 갈 화면)

종류마다 문구 · 채널 · 경로를 서버가 고정함. 앱이 보낸 제목 · 본문은 쓰지 않음(남이 쓴 문구가 알림이 되지 않게). 문구에 들어가는 건 이름뿐:
  깨우기 · 알람 바뀜 → 보낸 사람 이름 / 그룹 알림 → 그룹 이름 · 약속 제목만(사람 이름 없음) / 요청 · 수락 · 미확인 → 고정 문구 /
  친구 미확인 · 후속 → 안 일어난 사람 이름
채널 id는 `_v1` — 안드로이드 채널은 만든 뒤 설정을 못 바꿔서 바꿀 일이 생기면 `_v2`를 새로 만듦.
JS와 맞춘 곳: 글자 수 · 자르기는 UTF-16 단위 · trim은 JS가 지우는 글자 · Intl 시각 표기는 zoneinfo로.
"""

import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.read import js_length, js_trim
from .js import parse_iso

CLIENT_KINDS = (
    "WAKE_PING",
    "ALARM_CHANGED",
    "FRIEND_REQUEST",
    "FRIEND_ACCEPTED",
    "GROUP_MEMBER_JOINED",
    "GROUP_APPT_CHANGED",
)
# 앱이 부를 수 없고 5분 배치(wake-check)가 보냄: 본인 미확인 · 친구 '깨워 볼까요?' · 후속 '일어났어요'
SERVER_ONLY_KINDS = ("WAKE_MISSED", "WAKE_NUDGE", "WAKE_NUDGE_UP")
GROUP_KINDS = ("GROUP_MEMBER_JOINED", "GROUP_APPT_CHANGED")

# 종류 이름 글자 (TS ClientKind · PushKind · GroupKind · GroupChange — 'CHANGED' | 'CANCELED')
ClientKind = str
PushKind = str
GroupKind = str
GroupChange = str


def is_group_kind(kind: str) -> bool:
    return kind in GROUP_KINDS


CHANNEL_IDS: dict[str, str] = {
    "WAKE_PING": "friend_v1",
    "ALARM_CHANGED": "alarm_change_v1",
    "FRIEND_REQUEST": "request_v1",
    "FRIEND_ACCEPTED": "request_v1",
    "GROUP_MEMBER_JOINED": "group_v1",
    "GROUP_APPT_CHANGED": "group_v1",
    "WAKE_MISSED": "missed_v1",
    "WAKE_NUDGE": "friend_v1",
    "WAKE_NUDGE_UP": "friend_v1",
}

# 누르면: 깨우기 · 본인 미확인 → 내 기상 확인(H08), 알람 바뀜 → 받은 알람(G04), 요청 · 수락 → 친구(F01), 그룹 → 그룹(G02),
# 친구 미확인 · 후속 → 그 사람의 기상 확인(G03, 그룹 id + 사람 id가 알림 데이터에)
PATHS: dict[str, str] = {
    "WAKE_PING": "/wake",  # 받는 사람이 누르면 자기 기상 확인(H08, 8-02)
    "ALARM_CHANGED": "/alarms",
    "FRIEND_REQUEST": "/friends",
    "FRIEND_ACCEPTED": "/friends",
    "GROUP_MEMBER_JOINED": "/group",
    "GROUP_APPT_CHANGED": "/group",
    "WAKE_MISSED": "/wake",
    "WAKE_NUDGE": "/group",
    "WAKE_NUDGE_UP": "/group",
}

# CopyContext = dict (키는 TS 그대로, 모두 선택):
#   senderName · subjectName(친구 미확인 · 후속: 안 일어난(일어난) 사람 이름) · groupName · appointmentTitle ·
#   previousAt · newAt(알람 바뀜: 이전 → 새 시각, 둘 다 있을 때만 문구에 넣음) · alarmType(WAKE · PREPARE · DEPARTURE · ARRIVE) ·
#   now(datetime, '오늘 · 내일 · 모레'를 가를 기준 시각, 기본 지금) · timeZone(받는 사람 시간대, 기본 Asia/Seoul) · change(취소됐으면 '취소됐어요')
CopyContext = dict[str, Any]

NAME_MAX = 40
DEFAULT_TZ = "Asia/Seoul"


def _js_prefix(text: str, count: int) -> str:
    """JS `text.slice(0, count)` — UTF-16 단위로 자름 (반쪽 글자가 생기면 그대로 둠, JS도 같음)"""
    return text.encode("utf-16-le", errors="surrogatepass")[: count * 2].decode(
        "utf-16-le", errors="surrogatepass"
    )


def _clip(text: str, max_len: int = NAME_MAX) -> str:
    return f"{_js_prefix(text, max_len)}…" if js_length(text) > max_len else text


def _name_or(value: str | None, fallback: str) -> str:
    t = js_trim(value or "")
    return _clip(t) if t else fallback


def _parse(iso: str | None) -> datetime | None:
    """Date.parse가 NaN이 아니면 그 시각 (UTC aware), 아니면 None"""
    if not iso:
        return None
    try:
        return parse_iso(iso)
    except Exception:  # noqa: BLE001 — 이상한 시각 글자
        return None


def _zone(time_zone: str | None) -> ZoneInfo:
    """모르는 시간대면 Asia/Seoul (Intl이 RangeError를 내는 경우)"""
    try:
        return ZoneInfo(time_zone or DEFAULT_TZ)
    except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError · ValueError(이상한 이름)
        return ZoneInfo(DEFAULT_TZ)


def _clock(iso: str | None, time_zone: str | None) -> str | None:
    """ "HH:mm" (받는 사람 시간대). 시각이 이상하면 None"""
    at = _parse(iso)
    if at is None:
        return None
    local = at.astimezone(_zone(time_zone))
    return f"{local.hour:02d}:{local.minute:02d}"


def _clock_short(iso: str | None, time_zone: str | None) -> str | None:
    """ "H:mm" (받는 사람 시간대, 앞 0 없음 — '7:30 → 7:00'). 시각이 이상하면 None"""
    full = _clock(iso, time_zone)
    return re.sub(r"^0", "", full) if full else None


def _particle_for(time: str) -> str:
    """시각 뒤 '으로 / 로' — 마지막 숫자를 읽었을 때 받침이 있으면(영 · 삼 · 육) '으로', 없으면(일 이 사 오 칠 팔 구) '로'. 7:00 → 으로, 7:55 → 로"""
    return "으로" if time[-1:] in ("0", "3", "6") else "로"


ALARM_LABELS = {"WAKE": "기상", "PREPARE": "준비", "DEPARTURE": "출발", "ARRIVE": "도착"}


def _day_word(iso: str | None, now: datetime | None, time_zone: str | None) -> str:
    """'오늘 · 내일 · 모레', 그 밖은 'M월 D일' (받는 사람 시간대의 날짜). 날짜를 못 읽으면 빈 글자"""
    at = _parse(iso)
    if at is None:
        return ""
    zone = _zone(time_zone)
    target = at.astimezone(zone)
    today = (now or datetime.now(UTC)).astimezone(zone)
    days = round(
        (
            datetime(target.year, target.month, target.day, tzinfo=UTC)
            - datetime(today.year, today.month, today.day, tzinfo=UTC)
        ).total_seconds()
        / 86_400
    )
    if days == 0:
        return "오늘"
    if days == 1:
        return "내일"
    if days == 2:
        return "모레"
    return f"{target.month}월 {target.day}일"


def compose_copy(kind: str, ctx: CopyContext) -> dict[str, str]:
    if kind == "WAKE_PING":
        return {"title": "깨우기", "body": f"{_name_or(ctx.get('senderName'), '친구')}님이 깨워요"}
    if kind == "ALARM_CHANGED":
        # 친구가 알람 시각을 '제안'했을 때 — 주인이 [적용]해야 바뀜 (친구 10장). 시각은 받는 사람 시간대의 H:mm
        who = _name_or(ctx.get("senderName"), "친구")
        time_zone = ctx.get("timeZone")
        what = " ".join(
            part
            for part in (
                _day_word(ctx.get("newAt"), ctx.get("now"), time_zone),
                ALARM_LABELS.get(ctx.get("alarmType") or ""),
                "알람",
            )
            if part
        )
        from_ = _clock_short(ctx.get("previousAt"), time_zone)
        to = _clock_short(ctx.get("newAt"), time_zone)
        body = (
            f"{who}님이 {what}을 {from_} → {to}{_particle_for(to)} 제안했어요"
            if from_ and to
            else f"{who}님이 {what}을 제안했어요"
        )
        return {"title": "알람 제안", "body": body}
    if kind == "FRIEND_REQUEST":
        return {"title": "친구 요청", "body": "새 친구 요청이 도착했어요"}
    if kind == "FRIEND_ACCEPTED":
        return {"title": "친구 요청", "body": "친구 요청이 수락됐어요"}
    if kind == "GROUP_MEMBER_JOINED":
        return {"title": _name_or(ctx.get("groupName"), "그룹"), "body": "새 멤버가 들어왔어요"}
    if kind == "GROUP_APPT_CHANGED":
        title = js_trim(ctx.get("appointmentTitle") or "")
        verb = "취소됐어요" if ctx.get("change") == "CANCELED" else "바뀌었어요"
        return {
            "title": _name_or(ctx.get("groupName"), "그룹"),
            "body": f"‘{_clip(title)}’ 약속이 {verb}" if title else f"약속이 {verb}",
        }
    if kind == "WAKE_MISSED":
        return {"title": "기상 확인", "body": "기상 확인이 안 됐어요"}
    if kind == "WAKE_NUDGE":
        return {
            "title": "깨워 볼까요?",
            "body": f"{_name_or(ctx.get('subjectName'), '친구')}님이 아직 안 일어났어요",
        }
    if kind == "WAKE_NUDGE_UP":
        return {"title": "일어났어요", "body": f"{_name_or(ctx.get('subjectName'), '친구')}님이 일어났어요"}
    raise ValueError(f"알 수 없는 푸시 종류: {kind}")

"""본 레포 supabase/functions/_shared/ring-package.ts의 서버판 (기준 b140f70) — 예약 꾸러미(RingPackage) 조립

alarm-commit이 돌려주고 앱이 그대로 알람 코어 schedule(pkg)에 넘김. 모양은 알람 코어 RingPackage와 같음(camelCase 그대로):
  alarmId · type · atMillis · title · subtitle? · intensity · snoozeLimit · soundKey · vibration · departAt? · notice? · voiceText?
soundKey · vibration은 "해석된 값"을 모든 알람에 항상 실음 (D1 A). notice = 준비 · 출발 알림의 한 줄, voiceText = 기상 알람의 음성 문장 — 약속 알람만.
"""

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.read import JS_WHITESPACE, js_trim
from app.shared.calc.depart import depart_now
from app.shared.calc.intensity import AXES_PICKER_ENABLED, sound_key_of, vibration_of
from app.shared.calc.time import minutes_between
from app.shared.js import from_ms, ms, parse_iso
from app.shared.tz import format_ko_duration, format_ko_spoken_time, format_ko_time

log = logging.getLogger(__name__)

# RingPackage = dict (JSON 모양): alarmId · type · atMillis · title · subtitle? · intensity · snoozeLimit · soundKey · vibration · departAt? · voiceText? · notice?
RingPackage = dict[str, Any]
# PackageSource = alarms 행(dict)에서 필요한 칸: alarm_id · alarm_type · scheduled_at · intensity · sound_key · vibration? · voice_tone? · snooze_limit · is_enabled
PackageSource = Mapping[str, Any]


def subtitle_for(at: datetime, place: str | None, time_zone: str) -> str:
    time = format_ko_time(at, time_zone)
    return f"{time} · {place}" if place else time


# ---------------------------------------------------------------------------
# 알림 문구 — 약속 알람만. 서버(alarm-commit)와 앱(lib/alarmSync reconcile)이 이 함수들로 같은 글자를 만듦
# ---------------------------------------------------------------------------


def prepare_notice(*, prepare_at: datetime, depart_at: datetime, time_zone: str) -> str | None:
    """ "오전 8:40 출발까지 30분" — 준비가 출발 뒤거나 같으면 문구 없음(화면 줄 그대로)"""
    minutes = minutes_between(prepare_at, depart_at)
    if not minutes > 0:
        return None
    return f"{format_ko_time(depart_at, time_zone)} 출발까지 {format_ko_duration(minutes)}"


def departure_notice(
    *, depart_at: datetime, appointment_at: datetime, travel_minutes: float, time_zone: str
) -> str | None:
    """ "지금 나가면 오전 9:50 도착 · 여유 10분" — 도착 = 출발 알람 시각 + 이동분, 여유 = 약속 − 도착 (7장 7.5.2 departNow)"""
    result = depart_now({"appointmentAt": appointment_at, "travelMinutes": travel_minutes, "now": depart_at})
    arrive_at = result["arriveAt"]
    slack_minutes = result["slackMinutes"]
    if not math.isfinite(slack_minutes):
        return None
    if slack_minutes > 0:
        slack = f"여유 {format_ko_duration(slack_minutes)}"
    elif slack_minutes == 0:
        slack = "여유 없음"
    else:
        slack = f"{format_ko_duration(-slack_minutes)} 늦어요"
    return f"지금 나가면 {format_ko_time(arrive_at, time_zone)} 도착 · {slack}"


# 닫는 괄호로 끝나면 괄호 안을 빼고 그 앞 글자만 ("자료구조(월)" → "자료구조"). 짝이 없거나 제목 전체가 괄호면 그대로
_OPEN_BRACKETS = "([{（［｛"
_CLOSE_BRACKETS = ")]}）］｝"


def _strip_trailing_brackets(text: str) -> str:
    rest = text.rstrip(JS_WHITESPACE)
    while rest and rest[-1] in _CLOSE_BRACKETS:
        depth = 0
        start = -1
        for i in range(len(rest) - 1, -1, -1):
            if rest[i] in _CLOSE_BRACKETS:
                depth += 1
            elif rest[i] in _OPEN_BRACKETS:
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start <= 0:
            return rest
        rest = rest[:start].rstrip(JS_WHITESPACE)
    return rest


# 읽을 때 받침이 있는 숫자: 0 영 · 1 일 · 3 삼 · 6 육 · 7 칠 · 8 팔 (2 이 · 4 사 · 5 오 · 9 구는 받침 없음)
_BATCHIM_DIGITS = frozenset("013678")


def _copula(word: str) -> str:
    """받침 있는 말 뒤에는 "이에요", 없으면 "예요". 끝이 숫자면 읽는 소리로, 닫는 괄호면 괄호 앞 글자로. 영문 · 기타는 받침 없음으로 봄"""
    last = _strip_trailing_brackets(word)[-1:]
    code = ord(last) if last else -1
    has_batchim = last in _BATCHIM_DIGITS or (0xAC00 <= code <= 0xD7A3 and (code - 0xAC00) % 28 != 0)
    return "이에요" if has_batchim else "예요"


# 기상 알람의 음성 문장 — 인사 → 오늘 첫 일정 한 줄 → 나갈 시각. 말투는 말소리 축(alarms.voice_tone)이 정함
#   NEUTRAL(일정 안내형)이 원래 문구 그대로. 비었거나 모르는 값이면 NEUTRAL
_WAKE_TONES: dict[str, dict[str, Any]] = {
    "CALM": {
        "greeting": "좋은 아침이에요. 천천히 일어나 볼까요?",
        "depart": lambda time: f"{time}쯤 나가면 여유로워요.",
    },
    "NEUTRAL": {"greeting": "좋은 아침이에요.", "depart": lambda time: f"{time}에 나가면 돼요."},
    "DIRECT": {"greeting": "일어날 시간이에요.", "depart": lambda time: f"{time}에 나가야 해요."},
}


def axes_of(a: Mapping[str, Any], axes_enabled: bool = AXES_PICKER_ENABLED) -> dict[str, Any]:
    """알람 한 줄에서 꾸러미에 실을 축별 값 — 축별 선택을 끄면 저장된 sound_key · vibration · voice_tone을 무시하고 강도를 따름(값은 지우지 않음)

    a: sound_key · vibration · voice_tone · intensity 칸 (없는 칸은 None). 반환: soundKey · vibration · voiceTone
    """
    return {
        "soundKey": sound_key_of(a.get("sound_key") if axes_enabled else None, a["intensity"]),
        "vibration": vibration_of(a.get("vibration") if axes_enabled else None, a["intensity"]),
        "voiceTone": a.get("voice_tone") if axes_enabled else None,
    }


def wake_voice_text(
    *,
    title: str,
    appointment_at: datetime,
    time_zone: str,
    depart_at: datetime | None = None,
    voice_tone: str | None = None,
) -> str:
    title = js_trim(title) or "일정"
    tone = (
        _WAKE_TONES["CALM"]
        if voice_tone == "CALM"
        else _WAKE_TONES["DIRECT"]
        if voice_tone == "DIRECT"
        else _WAKE_TONES["NEUTRAL"]
    )
    sentences = [
        tone["greeting"],
        f"오늘 첫 일정은 {format_ko_spoken_time(appointment_at, time_zone)} {title}{_copula(title)}.",
    ]
    if depart_at is not None:
        sentences.append(tone["depart"](format_ko_spoken_time(depart_at, time_zone)))
    return " ".join(sentences)


def _try_text(what: str, alarm_id: str, make: Callable[[], Any]) -> Any:
    """문구는 덧붙임일 뿐 — 값이 이상해도 꾸러미(알람 예약)는 그대로 만들어야 하므로 문구만 빼고 넘어감.
    무엇이 실패했는지(알람 종류 · id · 오류)만 한 줄 남김. 제목 · 장소 같은 사용자 글자는 안 남김"""
    try:
        return make()
    except Exception as e:
        log.warning(
            "[ring-package] %s 문구를 못 만들어 빼고 알람만 걸어요 (알람 %s): %s: %s",
            what,
            alarm_id,
            type(e).__name__,
            e,
        )
        return None


@dataclass
class AppointmentNotice:
    """약속 알람이 문구를 만들 때 쓰는 값 (일정 없는 알람 · 루틴은 넘기지 않음 → 문구 없음)"""

    appointment_at: datetime
    time_zone: str
    travel_minutes: float | None = None  # 이동 (est_travel_minutes). 없으면 출발 문구가 빠짐


def build_ring_packages(
    *,
    alarms: Sequence[PackageSource],
    title: str,
    subtitle: str | None = None,
    depart_at: datetime | None = None,  # 출발 시각 (단축 준비 안내용)
    appointment: AppointmentNotice | None = None,
) -> list[RingPackage]:
    """켜진 알람만 꾸러미로. 꺼진 알람(지난 알림 · 사용자가 끈 알람)은 폰에 걸지 않음 → canceled_alarm_ids 쪽

    appointment가 있으면 문구도 채움: 준비 = notice(출발까지 몇 분), 출발 = notice(지금 나가면), 기상 = voiceText.
    준비 · 기상 문구는 같은 약속의 켜진 출발 알람을 기준으로 함 (출발 알람이 없으면 해당 줄이 빠짐)
    """
    live = [a for a in alarms if a["is_enabled"]]
    departure = next((a for a in live if a["alarm_type"] == "DEPARTURE"), None)
    departure_at = from_ms(ms(parse_iso(departure["scheduled_at"]))) if departure else None

    def text_for(a: PackageSource) -> dict[str, str]:
        if appointment is None:
            return {}
        at = from_ms(ms(parse_iso(a["scheduled_at"])))
        if a["alarm_type"] == "PREPARE" and departure_at is not None:
            notice = _try_text(
                a["alarm_type"],
                a["alarm_id"],
                lambda: prepare_notice(
                    prepare_at=at, depart_at=departure_at, time_zone=appointment.time_zone
                ),
            )
            return {"notice": notice} if notice else {}
        if (
            a["alarm_type"] == "DEPARTURE"
            and appointment.travel_minutes is not None
            and math.isfinite(appointment.travel_minutes)
        ):
            travel_minutes = appointment.travel_minutes
            notice = _try_text(
                a["alarm_type"],
                a["alarm_id"],
                lambda: departure_notice(
                    depart_at=at,
                    appointment_at=appointment.appointment_at,
                    travel_minutes=travel_minutes,
                    time_zone=appointment.time_zone,
                ),
            )
            return {"notice": notice} if notice else {}
        if a["alarm_type"] == "WAKE":
            voice_text = _try_text(
                a["alarm_type"],
                a["alarm_id"],
                lambda: wake_voice_text(
                    title=title,
                    appointment_at=appointment.appointment_at,
                    depart_at=departure_at,
                    time_zone=appointment.time_zone,
                    voice_tone=axes_of(a)["voiceTone"],
                ),
            )
            return {"voiceText": voice_text} if voice_text else {}
        return {}

    packages: list[RingPackage] = []
    for a in live:
        axes = axes_of(a)
        pkg: RingPackage = {
            "alarmId": a["alarm_id"],
            "type": a["alarm_type"],
            "atMillis": ms(parse_iso(a["scheduled_at"])),
            "title": title,
        }
        if subtitle:
            pkg["subtitle"] = subtitle
        pkg["intensity"] = a["intensity"]
        if "snooze_limit" in a:  # JS는 칸이 없으면 undefined → JSON에서 빠짐 (DB 행에는 항상 있음)
            pkg["snoozeLimit"] = a["snooze_limit"]
        pkg["soundKey"] = axes["soundKey"]
        pkg["vibration"] = axes["vibration"]
        if depart_at is not None:
            pkg["departAt"] = ms(depart_at)
        pkg.update(text_for(a))
        packages.append(pkg)
    return sorted(packages, key=lambda p: p["atMillis"])  # 안정 정렬 — JS Array.sort와 같음


def canceled_alarm_ids(
    *, before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]
) -> list[str]:
    """폰에서 지워야 할 예약: 이번 저장으로 지워졌거나 꺼진 알람 (5장 5.4.2 "canceled_alarm_ids를 반드시 반환함")"""
    live = {a["alarm_id"] for a in after if a["is_enabled"]}
    return [a["alarm_id"] for a in before if a["alarm_id"] not in live]

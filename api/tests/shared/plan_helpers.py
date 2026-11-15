"""plan-core · commit-core 시험이 같이 쓰는 도우미 (원본 시험 파일 안의 kst · hm · profile · row 같은 것)"""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.shared.js import parse_iso, to_iso
from app.shared.route import ResolvedTravel, TravelChoice


def kst(day: int, minutes: float) -> datetime:
    """2025년 5월 day일 한국 시각 minutes분 (원본: new Date(Date.UTC(2025, 4, day) + (minutes - 9 * 60) * 60_000))"""
    return datetime(2025, 5, day, tzinfo=UTC) + timedelta(minutes=minutes - 9 * 60)


def hm(iso: str) -> str:
    d = parse_iso(iso)
    m = (d.hour * 60 + d.minute + 9 * 60) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def iso_of(d: datetime) -> str:
    return to_iso(d)


PROFILE: dict[str, Any] = {
    "timezone": "Asia/Seoul",
    "default_buffer_minutes": 10,
    "default_transport": "TRANSIT",
    "home_name": "우리 집",
    "home_lat": 37.5547,
    "home_lng": 126.9707,
}

# commit-core 시험의 profile (집 좌표 없음)
PROFILE_BARE: dict[str, Any] = {
    "timezone": "Asia/Seoul",
    "default_buffer_minutes": 10,
    "default_transport": "TRANSIT",
    "home_name": None,
    "home_lat": None,
    "home_lng": None,
}


def api(minutes: float) -> ResolvedTravel:
    choice = TravelChoice(minutes=minutes, source="API", needs_confirmation=False, note="길찾기 기준")
    return ResolvedTravel(chosen=choice, auto=choice, warnings=[])


def alarm_row(alarm_type: str, minutes: float, **over: Any) -> dict[str, Any]:
    return {
        "alarm_id": f"al-{alarm_type}",
        "participant_id": "pt1",
        "user_id": "u1",
        "alarm_type": alarm_type,
        "scheduled_at": to_iso(kst(22, minutes)),
        "intensity": "NORMAL",
        "voice_tone": "NEUTRAL",
        "sound_key": "theme_default",
        "vibration": None,
        "is_enabled": True,
        "snooze_limit": 3,
        "time_source": "AUTO",
        "intensity_source": "AUTO",
        "recurrence_rule": None,
        **over,
    }

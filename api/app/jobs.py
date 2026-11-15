"""예약 작업 목록 — 작업 이름은 본 레포 M1의 server_routes(kind = 'job') · server_heartbeat 줄과 같아야 함

작업마다 두 손잡이:
  shadow  세기만 함 (쓰기 · 푸시 없음). 한 줄 요약을 돌려주고 로그에 남음 — Edge와 대조하려고
  server  실제로 함. 성공하면 스케줄러가 last_success_at을 기록 (pg_cron이 이것을 보고 Edge 호출을 건너뜀)
없는 손잡이(None)는 "아직 서버에 구현 없음" — 표가 그 손잡이를 고르면 스케줄러가 낮추고 오류 로그를 남김 (scheduler.py).
S3(완결점 1-10)에서 구현된 것은 wake-check의 shadow뿐. server 손잡이는 3차(푸시 모듈 뒤)에 채움.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .db import Db

Handler = Callable[[Db, datetime], str | None]  # (DB, 기준 시각) → 로그에 남길 요약 (없으면 None)


@dataclass(frozen=True)
class Job:
    name: str
    next_boundary: Callable[[datetime], datetime]  # t보다 뒤인 가장 가까운 정시 (UTC)
    lead_seconds: int = 0  # 정시보다 이만큼 먼저 돎 — shadow가 Edge(pg_cron)가 기록하기 전에 세려고
    shadow: Handler | None = None
    server: Handler | None = None


def every_minutes(minutes: int) -> Callable[[datetime], datetime]:
    """UTC 기준 minutes분마다 (5분이면 :00 :05 :10 …) — pg_cron `*/5 * * * *`과 같은 정시"""
    step = minutes * 60

    def boundary(t: datetime) -> datetime:
        return datetime.fromtimestamp((int(t.timestamp()) // step + 1) * step, UTC)

    return boundary


def daily_at(hour: int, minute: int = 0) -> Callable[[datetime], datetime]:
    """UTC 기준 하루 한 번 (한국 18:00 = 09:00 UTC)"""

    def boundary(t: datetime) -> datetime:
        candidate = t.astimezone(UTC).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return candidate if candidate > t else candidate + timedelta(days=1)

    return boundary


# find_missed_wake_alarms의 조회 폭 — Edge wake-check(`_shared/wake.ts` LOOKBACK_HOURS)와 같아야 같은 후보가 나옴
WAKE_LOOKBACK_HOURS = 24
# pg_cron이 Edge를 부르기(정시) 이만큼 전에 셈 — Edge가 MISSED를 기록한 뒤에 세면 후보가 0이 되어 대조가 안 됨
WAKE_SHADOW_LEAD_SECONDS = 30


def wake_check_shadow(db: Db, boundary: datetime) -> str:
    """기상 미확인 후보를 세기만 함 (읽기 전용 DB 함수, 기록 · 푸시 없음). Edge wake-check 응답의 checked와 같은 수여야 함"""
    rows = db.rpc(
        "find_missed_wake_alarms",
        {"p_now": boundary.isoformat(), "p_lookback_hours": WAKE_LOOKBACK_HOURS},
    )
    return f"checked={len(rows)}"  # 알람 · 사용자 번호는 로그에 안 남기고 수만


JOBS: tuple[Job, ...] = (
    Job("wake-check", every_minutes(5), lead_seconds=WAKE_SHADOW_LEAD_SECONDS, shadow=wake_check_shadow),
    Job("alarm-roll", every_minutes(5)),
    Job("suggestion-generate", daily_at(9)),  # 매일 18:00 KST
)

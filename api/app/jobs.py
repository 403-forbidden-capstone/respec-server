"""예약 작업 목록 — 작업 이름은 본 레포 M1의 server_routes(kind = 'job') · server_heartbeat 줄과 같아야 함

작업마다 두 손잡이:
  shadow  세기만 함 (쓰기 · 푸시 없음). 한 줄 요약을 돌려주고 로그에 남음 — Edge와 대조하려고
  server  실제로 함. 성공하면 스케줄러가 last_success_at을 기록 (pg_cron이 이것을 보고 Edge 호출을 건너뜀)
없는 손잡이(None)는 "아직 서버에 구현 없음" — 표가 그 손잡이를 고르면 스케줄러가 낮추고 오류 로그를 남김 (scheduler.py).
shadow는 wake-check만 구현(S3). server는 세 작업 모두 구현(S7, 완결점 3-2 · 3-3 · 3-5): 서비스 키 클라이언트로 Edge와 같은 본문을 돌림.
서버 본문은 async라 `async_runtime.runtime`(스케줄러 프로세스의 이벤트 루프 하나)에서 돌리고, 동기 Handler 모양에 맞춰 한 줄 요약(수만)으로 줄임.
실패는 예외로 올림 → 스케줄러가 last_success_at을 안 씀 → pg_cron이 Edge를 부름.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .async_runtime import runtime
from .db import Db
from .jobs_roll import kakao_lookup, run_alarm_roll
from .jobs_server import run_suggestion_generate, run_wake_check
from .shared.supabase import create_service_client

Handler = Callable[[Db, datetime], str | None]  # (DB, 기준 시각) → 로그에 남길 요약 (없으면 None)


@dataclass(frozen=True)
class Job:
    name: str
    next_boundary: Callable[[datetime], datetime]  # t보다 뒤인 가장 가까운 정시 (UTC)
    # 정시보다 이만큼 먼저 돎 — shadow가 Edge(pg_cron)가 기록하기 전에 세려고. 그래서 정시가 마감: 정시를 넘기면 그 회차는 놓친 것.
    # server 손잡이는 지금 시각을 받음(앞당긴 채 정시 값을 받으면 아직 안 온 후보까지 처리하게 됨 — scheduler.py)
    lead_seconds: int = 0
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


def _counts(result: dict[str, Any], keys: tuple[str, ...]) -> str:
    """응답에서 수만 뽑아 한 줄로 (알람 · 사용자 번호, 푸시 토큰은 로그에 안 남김). 리스트 값은 길이로"""
    parts = []
    for key in keys:
        value = result.get(key)
        parts.append(f"{key}={len(value) if isinstance(value, list) else value}")
    return " ".join(parts)


def wake_check_server(_db: Db, now: datetime) -> str:
    """Edge wake-check와 같은 처리 — MISSED 기록 · 푸시 · 영수증 확인 (push 토큰은 EXPO_ACCESS_TOKEN, 선택)"""

    async def go() -> dict[str, Any]:
        return await run_wake_check(create_service_client(), now)

    result = runtime.run(go())
    receipts = result.get("receipts") or {}
    return (
        _counts(result, ("checked", "recorded", "pushed", "claimed", "nudged"))
        + f" receipts_checked={receipts.get('checked')} tokens_removed={receipts.get('tokensRemoved')}"
        + f" expired={receipts.get('expired')}"
    )


def alarm_roll_server(_db: Db, now: datetime) -> str:
    """Edge alarm-roll 배치 길과 같은 처리 — 카카오 키가 없으면 길찾기 없이(roll이 AUTH와 같게 다룸)"""

    async def go() -> dict[str, Any]:
        return await run_alarm_roll(create_service_client(), now, lookup=kakao_lookup())

    result = runtime.run(go())
    return _counts(result, ("created", "moved_alarm_ids", "pending", "canceled_alarm_ids", "kakao_calls"))


def suggestion_generate_server(_db: Db, now: datetime) -> str:
    """Edge suggestion-generate와 같은 처리 — 알람 조정 제안 만들기"""

    async def go() -> dict[str, Any]:
        return await run_suggestion_generate(create_service_client(), now)

    return _counts(runtime.run(go()), ("candidates", "created", "skipped"))


JOBS: tuple[Job, ...] = (
    Job(
        "wake-check",
        every_minutes(5),
        lead_seconds=WAKE_SHADOW_LEAD_SECONDS,
        shadow=wake_check_shadow,
        server=wake_check_server,
    ),
    Job("alarm-roll", every_minutes(5), server=alarm_roll_server),
    Job("suggestion-generate", daily_at(9), server=suggestion_generate_server),  # 매일 18:00 KST
)

"""스케줄러용 alarm-roll 배치 — 본 레포 `alarm-roll/index.ts`의 배치 길(서비스 호출) 부분

`app/jobs.py`의 server 손잡이가 5분마다 부를 함수. 아직 jobs.py의 모드에는 연결하지 않음 (헤드가 합칠 때).
service_role 클라이언트 하나로 읽기 · 쓰기를 다 하므로 RLS를 안 탐 → shared/roll.py가 모든 쿼리를 사용자(creator_id · user_id)로 직접 좁힘.
lookup(카카오 길찾기 1번)은 스케줄러가 키 · http 클라이언트를 들고 있을 때만 넘김 — 없으면 길찾기가 늘 안 되는 것으로 보고(AUTH와 같음) 다음 날 0시 뒤로 미룸.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .shared.js import from_ms, ms
from .shared.roll import RollDeps, TravelLookup, run_roll
from .shared.supabase import Db


async def run_alarm_roll(
    db: Db,
    now: datetime,
    *,
    lookup: TravelLookup | None = None,
    log: Callable[[str], None] | None = None,
    warn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """배치 길 한 번. 응답 모양은 Edge와 같음 ({created, moved_alarm_ids, pending, ring_packages: [], canceled_alarm_ids, kakao_calls})"""
    # Edge의 new Date()는 밀리초 단위 — 마이크로초를 잘라 같은 값으로 계산
    now = from_ms(ms(now))
    result = await run_roll(
        RollDeps(path="batch", now=now, read=db, write=db, lookup=lookup, log=log, warn=warn)
    )
    return dict(result)

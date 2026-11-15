"""서버 모드(server)에서 돌 작업 본문 — 본 레포 Edge `wake-check` · `suggestion-generate`의 index.ts를 옮긴 것 (기준 b140f70)

스케줄러가 부름 (앱이 부르지 않음): 키 확인(requireServiceCall · isServiceCall)은 Edge 쪽 일이라 여기엔 없음.
돌려주는 값은 Edge 응답과 같은 JSON 모양 (dict):
  run_wake_check             → { checked, recorded, pushed, claimed, nudged, receipts: { checked, tokensRemoved, expired } }
  run_suggestion_generate    → { candidates, created, skipped }
`db`는 서비스 키 클라이언트(`app.shared.supabase.create_service_client()`, Edge의 createServiceClient와 같음).

연결 자리 (아직 안 함 — 스케줄러 · jobs.py 모드 로직은 이 PR에서 안 바꿈):
  `app/jobs.py`의 `Job("wake-check", …)` · `Job("suggestion-generate", …)`에 `server=` 손잡이로 연결.
  Job.Handler는 동기 `(Db, datetime) -> str | None`이므로, 연결할 때 `asyncio.run(run_wake_check(create_service_client(), now))`로 감싸고
  결과를 한 줄 요약(로그에 안 남길 값 없이 수만)으로 줄이는 어댑터가 필요함. 실패는 예외로 올려야 스케줄러가 성공 기록을 안 함.
"""

import os
from datetime import datetime
from typing import Any

from .shared.notify import NotifyOptions
from .shared.push_receipts import run_receipt_check
from .shared.suggest_run import run_suggest
from .shared.supabase import Db
from .shared.wake import run_wake_check as run_wake_check_body


def _expo_access_token() -> str | None:
    """Edge getEnv('EXPO_ACCESS_TOKEN') — 앞뒤 공백을 지우고 비면 없는 것"""
    value = (os.environ.get("EXPO_ACCESS_TOKEN") or "").strip()
    return value or None


async def run_wake_check(db: Db, now: datetime, *, push: NotifyOptions | None = None) -> dict[str, Any]:
    """wake-check/index.ts — MISSED 기록 · 푸시, 이어서 영수증 확인.
    MISSED 기록이 실패해도 영수증 확인은 돌리고(서로 안 막음), 실패는 그 뒤에 그대로 던짐.
    push는 시험이 Expo 주소를 가짜로 바꿔 끼우는 자리 (기본: env의 EXPO_ACCESS_TOKEN + 실제 Expo)"""
    options = push or NotifyOptions(access_token=_expo_access_token())
    wake: dict[str, int] | None = None
    failure: Exception | None = None
    try:
        wake = await run_wake_check_body(db, now, push=options)
    except Exception as e:  # noqa: BLE001
        failure = e
    receipts = await run_receipt_check(
        db, now, access_token=options.access_token, fetch_impl=options.fetch_impl
    )
    if failure is not None:
        raise failure
    return {**(wake or {}), "receipts": receipts}


async def run_suggestion_generate(db: Db, now: datetime) -> dict[str, Any]:
    """suggestion-generate/index.ts — 알람 조정 제안 만들기"""
    return await run_suggest(db, now)

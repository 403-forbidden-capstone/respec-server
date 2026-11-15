"""서버 쪽 예약 작업 본문을 한 번 돌림 (계약 시험 전용) — 스케줄러가 server 모드에서 부르는 것과 같은 본문(`app/jobs_server.py`)을
지금 시각으로 한 번 돌리고, Edge 응답과 같은 모양의 JSON을 마지막 줄에 찍음. Expo 주소는 가짜 서버(fake_expo.py)로 바꿔 끼움.

  api 폴더에서, 로컬 스택 api.env를 환경 변수로 넣고:  python ../dev/contract/server_job.py wake-check | suggestion-generate
  마지막 줄: {"ok": true, "result": {…}} 또는 {"ok": false, "error": "…"}"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.getcwd())

from app import jobs_server  # noqa: E402
from app.async_runtime import runtime  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.shared import push  # noqa: E402
from app.shared.supabase import create_service_client  # noqa: E402

BASE = os.environ.get("CONTRACT_EXPO_BASE", "http://127.0.0.1:28999")
push.EXPO_PUSH_URL = f"{BASE}/--/api/v2/push/send"
push.EXPO_RECEIPTS_URL = f"{BASE}/--/api/v2/push/getReceipts"


def main() -> int:
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    runner = {
        "wake-check": jobs_server.run_wake_check,
        "suggestion-generate": jobs_server.run_suggestion_generate,
    }.get(job)
    if runner is None:
        print(json.dumps({"ok": False, "error": f"모르는 작업: {job}"}))
        return 2
    runtime.start(Settings())
    try:

        async def go() -> dict:
            return await runner(create_service_client(), datetime.now(UTC))

        print(json.dumps({"ok": True, "result": runtime.run(go())}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 — 실패도 한 줄 JSON으로 (틀이 상태로 바꿔 견줌)
        print(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}, ensure_ascii=False)
        )
        return 1
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())

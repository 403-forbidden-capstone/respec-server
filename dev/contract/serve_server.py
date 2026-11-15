"""서버(uvicorn)를 띄우되 Expo 주소만 가짜 서버(fake_expo.py)로 바꿔 끼움 (계약 시험 전용 — 서버 코드는 안 고침, 실행 때 모듈 값만 바꿈).

api 폴더에서, 로컬 스택 api.env를 환경 변수로 넣고:  python ../dev/contract/serve_server.py   (28000)"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.getcwd())

import uvicorn  # noqa: E402

from app.shared import push  # noqa: E402

BASE = os.environ.get("CONTRACT_EXPO_BASE", "http://127.0.0.1:28999")
push.EXPO_PUSH_URL = f"{BASE}/--/api/v2/push/send"
push.EXPO_RECEIPTS_URL = f"{BASE}/--/api/v2/push/getReceipts"

if __name__ == "__main__":
    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=28000, access_log=False)

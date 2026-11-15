"""서버(uvicorn)를 띄우되 Expo 주소만 가짜 서버(fake_expo.py)로 바꿔 끼움 (계약 시험 전용 — 서버 코드는 안 고침, 실행 때 모듈 값만 바꿈).

api 폴더에서:  uv run --env-file ../dev/.stack/api.env python ../dev/contract/serve_server.py   (28000)
셸에 남은 다른 환경 변수(운영 카카오 키 · PREPARE_ML_* 등)는 서버를 불러오기 전에 버림 — 서버 설정은 api.env만 (clean_env.py)"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.getcwd())

import clean_env  # noqa: E402

clean_env.scrub_process_env()  # 서버 코드(app.*)가 환경을 읽기 전에

import uvicorn  # noqa: E402

from app.shared import push  # noqa: E402

BASE = os.environ.get("CONTRACT_EXPO_BASE", "http://127.0.0.1:28999")
push.EXPO_PUSH_URL = f"{BASE}/--/api/v2/push/send"
push.EXPO_RECEIPTS_URL = f"{BASE}/--/api/v2/push/getReceipts"

if __name__ == "__main__":
    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=28000, access_log=False)

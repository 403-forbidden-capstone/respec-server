"""가짜 Expo 푸시 서버 (계약 시험 전용, 127.0.0.1:28999) — 받은 요청을 기록하고 접수 번호를 돌려줌. 진짜 Expo(exp.host)는 안 부름.

  POST /--/api/v2/push/send        메시지 배열 → {"data": [접수 결과…]}. 토큰에 "Gone"이 들어 있으면 DeviceNotRegistered 오류, 아니면 ok + 토큰에서 뽑은 접수 번호
  POST /--/api/v2/push/getReceipts {"data": {}} (영수증 없음)
  GET  /__log                      지금까지 받은 보내기 요청 목록 [{path, authorized, messages}]
  POST /__reset                    기록 비우기
Edge · 서버 둘 다 Expo 주소가 코드에 박혀 있어서(`push.ts` · `push.py`) 시험 틀이 사본 · 실행 때 이 주소로 바꿔 끼움 (patch_edge_expo.py · serve_server.py · server_job.py).
사용: python dev/contract/fake_expo.py"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 28999
LOG: list[dict] = []
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:  # 접속 로그는 찍지 않음
        pass

    def _reply(self, status: int, payload: object) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/__log":
            with LOCK:
                return self._reply(200, list(LOG))
        self._reply(404, {})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        if self.path == "/__reset":
            with LOCK:
                LOG.clear()
            return self._reply(200, {"ok": True})
        if self.path.endswith("/push/send"):
            messages = body if isinstance(body, list) else [body]
            with LOCK:
                LOG.append(
                    {
                        "path": self.path,
                        "authorized": bool(self.headers.get("Authorization")),
                        "messages": messages,
                    }
                )
            tickets = []
            for message in messages:
                token = str((message or {}).get("to", ""))
                if "Gone" in token:
                    tickets.append(
                        {
                            "status": "error",
                            "message": "not registered",
                            "details": {"error": "DeviceNotRegistered"},
                        }
                    )
                else:
                    tickets.append(
                        {"status": "ok", "id": "fake-" + hashlib.md5(token.encode()).hexdigest()[:12]}
                    )
            return self._reply(200, {"data": tickets})
        if self.path.endswith("/push/getReceipts"):
            return self._reply(200, {"data": {}})
        self._reply(404, {})


if __name__ == "__main__":
    print(f"가짜 Expo 서버 http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

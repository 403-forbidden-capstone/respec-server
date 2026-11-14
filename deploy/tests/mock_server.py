"""smoke.sh 시험용 가짜 서버. 표준 라이브러리만 씀. 동작은 환경 변수로 바꿈.

  python mock_server.py <포트 기록 파일>
환경 변수 (기본값)
  VERSION=sha-abc1234    /health 의 version
  NOAUTH_BUG=0           1 이면 토큰 없는 /me 도 200 (버그 흉내)
  CORS_STATUS=204        OPTIONS /me 의 상태 코드
  CORS_HEADER=1          0 이면 access-control-allow-origin 헤더를 안 줌
  ME_STATUS=200          토큰 있는 /me 의 상태 코드
  LOGIN_STATUS=200       Supabase 로그인 응답 상태 (200 아니면 토큰 없음)
  ML_OK=1                0 이면 /ml/health 503 + ok=false (경로는 /health 가 아니라 /ml/health)
  TTL=60                 이 초 뒤에 스스로 종료 (정리 못 했을 때 대비)
기대하는 시험 계정: email=smoke@example.test password=pa ss$1"x  / apikey=PUBKEY
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

E = os.environ.get
TOKEN = "valid-token-xyz"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 조용히
        pass

    def _send(self, status, body=None, headers=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if body is not None:
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        if status != 204:
            self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, {"status": "ok", "version": E("VERSION", "sha-abc1234")})
        if path == "/ml/health":
            ok = E("ML_OK", "1") == "1"
            return self._send(200 if ok else 503, {"ok": ok, "loaded_models": ["c1_mae", "w4_mae"]})
        if path == "/me":
            auth = self.headers.get("authorization", "")
            if auth == f"Bearer {TOKEN}":
                return self._send(int(E("ME_STATUS", "200")), {"id": "user-1"})
            if E("NOAUTH_BUG", "0") == "1":
                return self._send(200, {"id": "anonymous"})
            return self._send(401, {"error": {"code": "unauthorized", "message": "no"}})
        self._send(404, {"error": {"code": "not_found", "message": "no"}})

    def do_OPTIONS(self):
        headers = {}
        if E("CORS_HEADER", "1") == "1":
            headers["access-control-allow-origin"] = "*"
        self._send(int(E("CORS_STATUS", "204")), None, headers)

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(n)
        if path == "/auth/v1/token":
            try:
                body = json.loads(raw)
            except ValueError:
                return self._send(400, {"error": "bad json"})
            good = (
                self.headers.get("apikey") == "PUBKEY"
                and body.get("email") == "smoke@example.test"
                and body.get("password") == 'pa ss$1"x'
            )
            status = int(E("LOGIN_STATUS", "200"))
            if good and status == 200:
                return self._send(200, {"access_token": TOKEN, "token_type": "bearer"})
            return self._send(status if status != 200 else 400, {"error": "invalid_grant"})
        self._send(404, {"error": "no"})


def main():
    srv = HTTPServer(("127.0.0.1", 0), H)
    with open(sys.argv[1], "w") as f:
        f.write(str(srv.server_address[1]))
    threading.Timer(float(E("TTL", "60")), srv.shutdown).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()

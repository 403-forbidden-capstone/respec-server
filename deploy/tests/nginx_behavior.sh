#!/usr/bin/env bash
# nginx 컨테이너 안에서 도는 시험 (test_nginx.sh [5] 가 docker exec 로 부름).
# 실제 nginx(443)에 curl 로 요청해 응답 헤더 · 본문을 확인하고, 한 줄에 하나씩 "이름<TAB>ok|FAIL<TAB>설명" 으로 출력함.
set -u
T=$(mktemp -d)
# IP 로 접속하면 SNI 가 없어 기본 서버(ssl_reject_handshake)가 연결을 끊으므로, 도메인 이름으로 부르고 이름만 127.0.0.1 로 풀어 줌
RESOLVE="api.respecalarm.com:443:127.0.0.1"

# req [curl 인자...] — 응답 헤더는 $T/h, 본문은 $T/b, 상태는 $code
req() {
  code=$(curl -sk --max-time 10 --resolve "$RESOLVE" -D "$T/h" -o "$T/b" -w '%{http_code}' "$@") || code=000
  tr -d '\r' <"$T/h" >"$T/h2" && mv "$T/h2" "$T/h"
}
hcount() { grep -ic "^$1:" "$T/h" || true; }
hval() { grep -i -m1 "^$1:" "$T/h" | sed -E 's/^[^:]*:[[:space:]]*//' || true; }
body() { cat "$T/b"; }
out() { printf '%s\t%s\t%s\n' "$1" "$2" "$3"; }
expect() { # expect 이름 기대 실제
  if [[ $2 == "$3" ]]; then out "$1" ok ""; else out "$1" FAIL "기대 '$2' 실제 '$3'"; fi
}
expect_has() { # expect_has 이름 전체 부분
  if [[ $2 == *"$3"* ]]; then out "$1" ok ""; else out "$1" FAIL "'$3' 이(가) 없음: $2"; fi
}

# 1 api 가 CORS · 요청 번호를 붙인 응답: nginx 가 또 붙이면 안 됨(같은 헤더가 두 번 나가면 브라우저가 거부)
req https://api.respecalarm.com/health
expect "api 응답 상태" 200 "$code"
expect "api 응답: Access-Control-Allow-Origin 한 번만" 1 "$(hcount access-control-allow-origin)"
expect "api 응답: Access-Control-Expose-Headers 한 번만" 1 "$(hcount access-control-expose-headers)"
expect "api 응답: X-Request-Id 한 번만" 1 "$(hcount x-request-id)"
expect "api 응답: X-Request-Id 는 api 값 그대로" "from-api" "$(hval x-request-id)"

# 2 api 가 보낸 오류는 그대로
req https://api.respecalarm.com/me
expect "api 오류 상태 그대로(401)" 401 "$code"
expect "api 오류: Access-Control-Allow-Origin 한 번만" 1 "$(hcount access-control-allow-origin)"
expect_has "api 오류 본문 그대로" "$(body)" AUTH_REQUIRED

req https://api.respecalarm.com/fn/missing
expect "api 가 보낸 404 상태 그대로" 404 "$code"
expect "api 가 보낸 404: Access-Control-Allow-Origin 한 번만" 1 "$(hcount access-control-allow-origin)"
expect_has "api 가 보낸 404 는 nginx 본문으로 안 덮임" "$(body)" from-api

# 3 api 가 CORS 를 빠뜨린 응답은 nginx 가 채움
req https://api.respecalarm.com/fn/bare
expect "api 가 CORS 를 빠뜨리면 nginx 가 채움(한 번)" 1 "$(hcount access-control-allow-origin)"

# 4 nginx 가 만드는 404
req https://api.respecalarm.com/admin
expect "404 상태" 404 "$code"
expect "404: Access-Control-Allow-Origin" "*" "$(hval access-control-allow-origin)"
expect "404: Access-Control-Allow-Origin 한 번만" 1 "$(hcount access-control-allow-origin)"
expect "404: Access-Control-Expose-Headers" "x-request-id" "$(hval access-control-expose-headers)"
expect "404: X-Request-Id 있음" 1 "$(hcount x-request-id)"

# 5 쿼리스트링 → 400
req "https://api.respecalarm.com/health?x=1"
expect "쿼리스트링 400 상태" 400 "$code"
expect "쿼리스트링 400: Access-Control-Allow-Origin" "*" "$(hval access-control-allow-origin)"

# 6 요청 번호: 안전한 값은 그대로, 이상한 값은 버리고 새 번호
req -H 'X-Request-Id: abc-123' https://api.respecalarm.com/admin
expect "X-Request-Id 안전한 값은 그대로" "abc-123" "$(hval x-request-id)"
req -H 'X-Request-Id: bad value!' https://api.respecalarm.com/admin
rid=$(hval x-request-id)
expect "X-Request-Id 이상한 값은 버리고 새 번호" 1 "$([[ -n $rid && $rid != 'bad value!' ]] && echo 1 || echo 0)"

# 7 502: 예측 서버 없음(ml 주소는 CORS 를 안 붙임) / api 주소의 502(CORS 붙임)
req https://api.respecalarm.com/ml/health
expect "502 상태(예측 서버 없음)" 502 "$code"
expect "502(ml): CORS 안 붙임" 0 "$(hcount access-control-allow-origin)"
req https://api.respecalarm.com/fn/boom
expect "502 상태(api 가 연결을 끊음)" 502 "$code"
expect "502(api): Access-Control-Allow-Origin" "*" "$(hval access-control-allow-origin)"

# 8 413: api 주소(1m 초과)는 CORS 붙임, ml 주소(4k 초과)는 안 붙임
head -c 2097152 /dev/zero >"$T/big"
req -X POST -H 'Expect:' --data-binary @"$T/big" https://api.respecalarm.com/fn/x
expect "413 상태(api, 1m 초과)" 413 "$code"
expect "413(api): Access-Control-Allow-Origin" "*" "$(hval access-control-allow-origin)"
head -c 5000 /dev/zero >"$T/mid"
req -X POST -H 'Expect:' --data-binary @"$T/mid" https://api.respecalarm.com/ml/predict
expect "413 상태(ml, 4k 초과)" 413 "$code"
expect "413(ml): CORS 안 붙임" 0 "$(hcount access-control-allow-origin)"

# 9 405 (ml 주소는 POST 만)
req https://api.respecalarm.com/ml/predict
expect "405 상태" 405 "$code"

# 10 429: /health/deep 은 분당 10회(burst 3)
got=000
for _ in $(seq 1 12); do
  req https://api.respecalarm.com/health/deep
  if [[ $code == 429 ]]; then got=429; break; fi
done
expect "429 가 나옴(분당 10회 제한)" 429 "$got"
if [[ $got == 429 ]]; then
  expect "429: Access-Control-Allow-Origin" "*" "$(hval access-control-allow-origin)"
fi

rm -rf "$T"

#!/usr/bin/env bash
# shellcheck disable=SC2034
# nginx 설정 시험.
#   [정적] 열린 주소 목록이 정해진 것과 같은지 (예: /ml/models 가 실수로 열리지 않았는지) — 항상 돎
#   [동적] 실제 nginx 로 문법 검사 — Ubuntu 24.04 apt 판(1.24)과 26.04 apt 판(1.28) 둘 다. docker 엔진이 있을 때만 (CI 에서 돎)
set -Eeuo pipefail
SUITE="nginx"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

NG="$DEPLOY_DIR/nginx"

echo "[1] 열린 주소(location)가 화이트리스트와 같음"
got=$(grep -E '^\s*location ' "$NG/respec-api.conf" | sed -E 's/^\s+//; s/\s*\{\s*$//' | sort | tr '\n' '|')
# 'location /' 은 두 번: 80 번 서버의 443 넘김, 443 번 서버의 마지막 404 처리
want=$(printf '%s\n' \
  'location ^~ /fn/' 'location = /me' 'location = /health' 'location = /health/deep' \
  'location = /ml/predict' 'location = /ml/health' 'location /' 'location /' 'location /.well-known/acme-challenge/' | sort | tr '\n' '|')
assert_eq "location 목록" "$want" "$got"
assert_eq "마지막 location / 은 404" 1 "$(grep -c 'return 404;' "$NG/respec-api.conf")"

echo "[2] /ml 은 두 주소만 예측 서버로 감"
ml_proxy=$(grep -E 'proxy_pass http://127.0.0.1:8091' "$NG/respec-api.conf" | sed -E 's/^\s+//' | sort | tr '\n' '|')
assert_eq "예측 서버로 가는 proxy_pass" "proxy_pass http://127.0.0.1:8091/health;|proxy_pass http://127.0.0.1:8091/predict;|" "$ml_proxy"
api_proxy=$(grep -cE 'proxy_pass http://127.0.0.1:8000;' "$NG/respec-api.conf")
assert_eq "api 로 가는 proxy_pass 4개 (/fn · /me · /health · /health/deep)" 4 "$api_proxy"

echo "[3] 헤더·로그 규칙"
conf=$(cat "$NG/respec-api.conf")
assert_contains "X-Forwarded-For 는 실제 접속 IP 로 덮어씀" "$conf" 'proxy_set_header X-Forwarded-For $remote_addr;'
assert_not_contains "클라이언트 X-Forwarded-For 를 이어 붙이지 않음" "$conf" 'proxy_add_x_forwarded_for'
assert_contains "예측 호출 속도 제한" "$conf" 'limit_req zone=ml_predict'
assert_contains "예측 본문 4k 제한" "$conf" 'client_max_body_size 4k;'
glob=$(cat "$NG/respec-global.conf")
assert_contains "로그에 쿼리스트링이 없음 (\$uri)" "$glob" '"$request_method $uri"'
assert_not_contains "로그에 \$request(쿼리 포함)를 안 씀" "$glob" '$request '
assert_not_contains "공통 설정에 server_tokens 를 안 둠(Ubuntu 26.04 nginx.conf 와 중복돼 실패함)" "$glob" 'server_tokens off;'
assert_eq "버전 숨김: 서버 블록마다(443 포함 4개)" 4 "$(grep -c 'server_tokens off;' "$NG/respec-api.conf")"
assert_eq "버전 숨김: HTTP 모양 서버 블록마다(2개)" 2 "$(grep -c 'server_tokens off;' "$NG/respec-api.http.conf")"

echo "[3b] 요청 수 제한 · 쿼리스트링 거절 · 기본 서버 접속 기록 끔"
block() { awk -v lit="$1" 'index($0, lit) {on=1} on {print} on && /^    }/ {exit}' "$NG/respec-api.conf"; }
assert_contains "/health/deep 에 요청 수 제한" "$(block 'location = /health/deep')" 'limit_req zone=health_deep'
assert_contains "/fn/ 에 거친 상한" "$(block 'location ^~ /fn/')" 'limit_req zone=api_general'
assert_contains "/me 에 거친 상한" "$(block 'location = /me')" 'limit_req zone=api_general'
assert_contains "health_deep 구역 정의" "$glob" 'limit_req_zone $binary_remote_addr zone=health_deep:'
assert_contains "api_general 구역 정의" "$glob" 'limit_req_zone $binary_remote_addr zone=api_general:'
assert_contains "쿼리스트링이 붙으면 400" "$conf" 'if ($args != "") {'
assert_eq "기본 서버(80 · 443) 접속 기록 끔" 2 "$(grep -c 'access_log off;' "$NG/respec-api.conf")"
assert_eq "HTTP 모양의 기본 서버도 접속 기록 끔" 1 "$(grep -c 'access_log off;' "$NG/respec-api.http.conf")"

echo "[3c] nginx 가 직접 만드는 오류 응답에도 CORS · 요청 번호 (본문은 그대로 — 앱이 JSON 이 아닌 본문은 상태 번호로 판정)"
assert_contains "ACAO 는 변수로(upstream 이 안 붙인 경우만)" "$conf" 'add_header Access-Control-Allow-Origin $respec_cors_origin always;'
assert_contains "Expose-Headers 도 변수로" "$conf" 'add_header Access-Control-Expose-Headers $respec_cors_expose always;'
assert_contains "X-Request-Id 도 변수로" "$conf" 'add_header X-Request-Id $respec_request_id_header always;'
assert_not_contains "ACAO 를 고정값으로 붙이면 api 응답에 두 번 나감" "$conf" 'add_header Access-Control-Allow-Origin "*"'
assert_contains "ml 주소는 CORS 범위에서 뺌" "$glob" '~^/ml(/|$)  "ml";'
# 위치 안에 add_header 가 있으면 서버 수준 add_header(HSTS · CORS …)를 물려받지 못함 → 443 서버 블록의 location 에는 add_header 를 두지 않음
loc_adds=$(awk '/^server \{/ {n++} n==4 && /^    location / {inloc=1} inloc && /add_header/ {c++} inloc && /^    }/ {inloc=0} END {print c+0}' "$NG/respec-api.conf")
assert_eq "443 서버 블록의 location 안 add_header 수" 0 "$loc_adds"

echo "[4] 실제 nginx 로 문법 검사 (docker 엔진이 있을 때)"
if ! docker info >/dev/null 2>&1; then
  echo "  skip docker 엔진이 없어 nginx -t 를 건너뜀 (CI · 새 서버 bootstrap 에서 검사함)"
else
  for ver in 1.24 1.28; do
   for site in respec-api.http.conf respec-api.conf; do
    rc=0
    out=$(docker run --rm \
      -v "$(native_path "$NG/respec-global.conf"):/etc/nginx/conf.d/00-global.conf:ro" \
      -v "$(native_path "$NG/$site"):/etc/nginx/conf.d/10-site.conf:ro" \
      "nginx:$ver" sh -c '
        set -e
        rm -f /etc/nginx/conf.d/default.conf
        # Ubuntu 26.04 의 nginx.conf 는 http 수준에 server_tokens 를 이미 적어 둠 — 같은 조건으로 검사
        sed -i "0,/^http {/s//http {\n    server_tokens build;/" /etc/nginx/nginx.conf
        d=/etc/letsencrypt/live/api.respecalarm.com
        mkdir -p "$d" /var/www/certbot
        openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=api.respecalarm.com \
          -keyout "$d/privkey.pem" -out "$d/fullchain.pem" 2>/dev/null
        nginx -t' 2>&1) || rc=$?
    assert_rc "nginx $ver -t: $site" 0 "$rc"
    [[ $rc == 0 ]] || echo "$out"
   done
  done
fi

echo "[5] 실제 nginx 로 응답 동작 (오류 응답의 CORS · 요청 번호, api 응답은 헤더 중복 없음) — docker 엔진이 있을 때"
if ! docker info >/dev/null 2>&1; then
  echo "  skip docker 엔진이 없어 응답 동작 시험을 건너뜀 (CI 에서 돎)"
else
  for ver in 1.24 1.28; do
    cid=""
    rc=0
    # MSYS_NO_PATHCONV: Windows Git Bash 가 컨테이너 안 경로(/start.sh)를 C:/Program Files/Git/start.sh 로 바꾸지 않게
    cid=$(MSYS_NO_PATHCONV=1 docker run -d --rm \
      -v "$(native_path "$NG/respec-global.conf"):/etc/nginx/conf.d/00-global.conf:ro" \
      -v "$(native_path "$NG/respec-api.conf"):/etc/nginx/conf.d/10-site.conf:ro" \
      -v "$(native_path "$DEPLOY_DIR/tests/nginx_stub.conf"):/etc/nginx/conf.d/20-stub.conf:ro" \
      -v "$(native_path "$DEPLOY_DIR/tests/nginx_start.sh"):/start.sh:ro" \
      -v "$(native_path "$DEPLOY_DIR/tests/nginx_behavior.sh"):/behavior.sh:ro" \
      "nginx:$ver" bash /start.sh) || rc=$?
    assert_rc "nginx $ver 컨테이너 시작" 0 "$rc"
    [[ $rc == 0 ]] || continue
    # 서버가 뜰 때까지 기다림
    for _ in $(seq 1 40); do
      MSYS_NO_PATHCONV=1 docker exec "$cid" curl -sk -o /dev/null --resolve api.respecalarm.com:443:127.0.0.1 https://api.respecalarm.com/health 2>/dev/null && break
      sleep 0.5
    done
    # MSYS_NO_PATHCONV: Windows Git Bash 가 /behavior.sh 를 C:/Program Files/Git/behavior.sh 로 바꾸지 않게
    res=$(MSYS_NO_PATHCONV=1 docker exec "$cid" bash /behavior.sh 2>&1) || true
    docker rm -f "$cid" >/dev/null 2>&1 || true
    n=0
    while IFS=$'\t' read -r name status detail; do
      [[ -n $name ]] || continue
      n=$((n + 1))
      assert_eq "nginx $ver 동작: $name" ok "$status"
      [[ $status == ok ]] || echo "      $detail"
    done <<<"$res"
    assert_eq "nginx $ver 동작 시험 항목 수(32개)" 1 "$([[ $n -ge 32 ]] && echo 1 || echo 0)"
  done
fi

finish

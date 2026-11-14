#!/usr/bin/env bash
# shellcheck disable=SC2034
# nginx 설정 시험.
#   [정적] 열린 주소 목록이 정해진 것과 같은지 (예: /ml/models 가 실수로 열리지 않았는지) — 항상 돎
#   [동적] 실제 nginx(Ubuntu 24.04 apt 판과 같은 1.24) 로 문법 검사 — docker 엔진이 있을 때만 (CI 에서 돎)
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
assert_contains "버전 숨김" "$glob" 'server_tokens off;'

echo "[4] 실제 nginx 로 문법 검사 (docker 엔진이 있을 때)"
if ! docker info >/dev/null 2>&1; then
  echo "  skip docker 엔진이 없어 nginx -t 를 건너뜀 (CI · 새 서버 bootstrap 에서 검사함)"
else
  for site in respec-api.http.conf respec-api.conf; do
    rc=0
    out=$(docker run --rm \
      -v "$(native_path "$NG/respec-global.conf"):/etc/nginx/conf.d/00-global.conf:ro" \
      -v "$(native_path "$NG/$site"):/etc/nginx/conf.d/10-site.conf:ro" \
      nginx:1.24 sh -c '
        set -e
        rm -f /etc/nginx/conf.d/default.conf
        d=/etc/letsencrypt/live/api.respecalarm.com
        mkdir -p "$d" /var/www/certbot
        openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=api.respecalarm.com \
          -keyout "$d/privkey.pem" -out "$d/fullchain.pem" 2>/dev/null
        nginx -t' 2>&1) || rc=$?
    assert_rc "nginx -t: $site" 0 "$rc"
    [[ $rc == 0 ]] || echo "$out"
  done
fi

finish

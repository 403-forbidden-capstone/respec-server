#!/usr/bin/env bash
# 배포 직후 점검. 하나라도 FAIL 이면 종료 코드 1 → deploy.sh 가 직전 버전으로 되돌림.
#
#   smoke.sh --base http://127.0.0.1:8000 [--version sha-abc1234] [--ml http://127.0.0.1:8091]
#            [--env-file /srv/respec/smoke.env] [--origin https://smoke.invalid]
#
# 점검
#   1 GET  /health                 200, --version 을 주면 본문 "version" 이 같아야 함
#   2 GET  /me  (토큰 없음)        401
#   3 OPTIONS /me (CORS 사전 요청)  204 + access-control-allow-origin 헤더
#   4 GET  /me  (시험 계정 토큰)    200 + 본문에 "id"   (토큰은 Supabase 로그인으로 받음)
#   5 --ml 이면 GET /health         200 + "ok": true
#
# 4번에서 Supabase 로그인 자체가 안 되면(Supabase 장애) WARN 으로 넘어감 — 멀쩡한 배포를 되돌리지 않기 위함.
# 단 시험 계정 설정이 비어 있으면 실패. SMOKE_STRICT=1 이면 로그인 실패도 FAIL (손으로 돌릴 때).
# 비밀번호 · 토큰은 명령줄 인자로 넘기지 않음(ps 에 안 보이게). 권한 700 임시 폴더의 파일로 넘기고 곧바로 지움.
set -Eeuo pipefail

BASE=""
VERSION=""
ML=""
ENV_FILE=/srv/respec/smoke.env
ORIGIN="https://smoke.invalid"
STRICT=${SMOKE_STRICT:-0}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) BASE=${2:?}; shift 2 ;;
    --version) VERSION=${2:?}; shift 2 ;;
    --ml) ML=${2:?}; shift 2 ;;
    --env-file) ENV_FILE=${2:?}; shift 2 ;;
    --origin) ORIGIN=${2:?}; shift 2 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
done
[[ -n $BASE ]] || { echo "--base 필요" >&2; exit 2; }
BASE=${BASE%/}

pass() { printf 'PASS  %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }

env_get() { { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }
json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

# 시험 계정 설정은 임시 폴더로 옮기기 전에 읽음 (--env-file 이 상대 경로여도 되게)
s_url=$(env_get SMOKE_SUPABASE_URL)
s_key=$(env_get SMOKE_SUPABASE_PUBLISHABLE_KEY)
s_mail=$(env_get SMOKE_EMAIL)
s_pass=$(env_get SMOKE_PASSWORD)

# 비밀(비밀번호 · 토큰)은 명령줄 대신, 나만 읽는 임시 폴더의 파일로 curl 에 넘기고 곧바로 지움
umask 077
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
FAILS=0

# curl 래퍼: 상태 코드를 stdout 에, 본문을 $TMP/body, 헤더를 $TMP/head 에
req() {
  # 연결 실패여도 curl 이 -w 로 000 을 찍으므로 종료 코드만 무시함
  curl -sS --max-time 10 -o "$TMP/body" -D "$TMP/head" -w '%{http_code}' "$@" 2>"$TMP/err" || true
}

# 1 /health
code=$(req "$BASE/health")
if [[ $code != 200 ]]; then
  fail "1 /health: 상태 $code (200 이어야 함) $(head -c 200 "$TMP/err")"
else
  got=$(sed -n '/"version"/{s/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p;q}' "$TMP/body")
  if [[ -n $VERSION && $got != "$VERSION" ]]; then
    fail "1 /health: version \"$got\" (기대 \"$VERSION\")"
  else
    pass "1 /health 200 version=${got:-없음}"
  fi
fi

# 2 /me 토큰 없음 → 401
code=$(req "$BASE/me")
if [[ $code == 401 ]]; then pass "2 /me 토큰 없음 401"; else fail "2 /me 토큰 없음: 상태 $code (401 이어야 함)"; fi

# 3 CORS 사전 요청 → 204
code=$(req -X OPTIONS "$BASE/me" \
  -H "Origin: $ORIGIN" -H "Access-Control-Request-Method: GET" -H "Access-Control-Request-Headers: authorization")
if [[ $code != 204 ]]; then
  fail "3 OPTIONS /me: 상태 $code (204 이어야 함)"
elif ! grep -qi '^access-control-allow-origin:' "$TMP/head"; then
  fail "3 OPTIONS /me: access-control-allow-origin 헤더 없음"
else
  pass "3 OPTIONS /me 204 + CORS 헤더"
fi

# 4 /me 시험 계정
if [[ -z $s_url || -z $s_key || -z $s_mail || -z $s_pass ]]; then
  fail "4 /me: 시험 계정 설정이 비어 있음 (smoke.env 의 SMOKE_* 4개)"
else
  s_url=${s_url%/}
  printf '{"email":"%s","password":"%s"}' "$(json_escape "$s_mail")" "$(json_escape "$s_pass")" >login.json
  printf 'apikey: %s\n' "$s_key" >hdr_apikey
  login=$(req -X POST "$s_url/auth/v1/token?grant_type=password" \
    -H "Content-Type: application/json" -H @hdr_apikey --data @login.json)
  rm -f login.json hdr_apikey
  token=""
  if [[ $login == 200 ]]; then
    token=$(sed -n '/"access_token"/{s/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p;q}' "$TMP/body")
  fi
  if [[ -z $token ]]; then
    if [[ $STRICT == 1 ]]; then
      fail "4 /me: Supabase 로그인 실패 (상태 $login)"
    else
      warn "4 /me: Supabase 로그인 실패 (상태 $login) — Supabase 쪽 문제일 수 있어 건너뜀"
    fi
  else
    printf 'Authorization: Bearer %s\n' "$token" >hdr_auth
    code=$(req "$BASE/me" -H @hdr_auth)
    rm -f hdr_auth
    if [[ $code == 200 ]] && grep -q '"id"' "$TMP/body"; then
      pass "4 /me 시험 계정 200"
    else
      fail "4 /me 시험 계정: 상태 $code (200 + id 이어야 함)"
    fi
  fi
  unset token s_pass s_key
fi

# 5 예측 서버
if [[ -n $ML ]]; then
  ML=${ML%/}
  code=$(req "$ML/health")
  if [[ $code == 200 ]] && grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$TMP/body"; then
    pass "5 prepare-ml /health 200 ok=true"
  else
    fail "5 prepare-ml /health: 상태 $code (200 + ok=true 이어야 함)"
  fi
fi

if [[ $FAILS -gt 0 ]]; then
  printf '점검 실패 %d건\n' "$FAILS"
  exit 1
fi
echo "점검 통과"

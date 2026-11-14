#!/usr/bin/env bash
# shellcheck disable=SC2034
# smoke.sh 시험: 가짜 서버(mock_server.py)에 대해 통과 · 실패 경우를 모두 돌림
set -Eeuo pipefail
SUITE="smoke"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SMOKE="$DEPLOY_DIR/smoke.sh"
PY=$(find_python) || { echo "python 을 못 찾아 smoke 시험을 건너뜀" >&2; exit 0; }
TMP=$(new_tmp)
SECRET_PW='pa ss$1"x'

# 가짜 서버를 띄우고 BASE 를 정함. 인자: KEY=VALUE ... (mock_server.py 의 환경 변수)
MOCK_PID=""
start_mock() {
  stop_mock
  rm -f "$TMP/port"
  env TTL=60 "$@" "$PY" "$(native_path "$TESTS_DIR/mock_server.py")" "$(native_path "$TMP/port")" &
  MOCK_PID=$!
  local i
  for i in $(seq 1 50); do
    if [[ -s $TMP/port ]]; then break; fi
    sleep 0.1
  done
  [[ -s $TMP/port ]] || { echo "가짜 서버가 안 떠서 중단" >&2; exit 1; }
  BASE="http://127.0.0.1:$(cat "$TMP/port")"
}
stop_mock() {
  if [[ -n $MOCK_PID ]]; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
    MOCK_PID=""
  fi
  return 0
}
trap 'stop_mock; cleanup_all' EXIT

# smoke.env 틀(시험 계정 설정). 인자로 BASE 를 받아 Supabase 주소도 같은 가짜 서버로
write_env() {
  cat >"$TMP/smoke.env" <<ENV
SMOKE_SUPABASE_URL=$BASE
SMOKE_SUPABASE_PUBLISHABLE_KEY=PUBKEY
SMOKE_EMAIL=smoke@example.test
SMOKE_PASSWORD=$SECRET_PW
ENV
}

run() { # run [smoke 옵션...] → OUT, RC
  RC=0
  OUT=$("$SMOKE" --base "$BASE" --env-file "$TMP/smoke.env" "$@" 2>&1) || RC=$?
}

echo "[1] 전부 정상"
start_mock VERSION=sha-abc1234
write_env
run --version sha-abc1234
assert_rc "종료 코드 0" 0 "$RC"
assert_contains "1 /health" "$OUT" "PASS  1 /health 200 version=sha-abc1234"
assert_contains "2 토큰 없음 401" "$OUT" "PASS  2 /me 토큰 없음 401"
assert_contains "3 OPTIONS 204" "$OUT" "PASS  3 OPTIONS /me 204"
assert_contains "4 시험 계정 200" "$OUT" "PASS  4 /me 시험 계정 200"
assert_contains "통과 문구" "$OUT" "점검 통과"
assert_not_contains "비밀번호가 출력에 없음" "$OUT" "pa ss"
assert_not_contains "토큰이 출력에 없음" "$OUT" "valid-token-xyz"

echo "[2] version 이 다르면 실패 (이전 버전이 아직 떠 있는 경우)"
start_mock VERSION=sha-old0000
write_env
run --version sha-abc1234
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "version 불일치" "$OUT" 'FAIL  1 /health: version "sha-old0000"'

echo "[3] 토큰 없는 /me 가 200 이면 실패 (인증이 풀린 버그)"
start_mock NOAUTH_BUG=1
write_env
run
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "2번 실패" "$OUT" "FAIL  2 /me 토큰 없음: 상태 200"

echo "[4] OPTIONS 가 204 가 아니거나 CORS 헤더가 없으면 실패"
start_mock CORS_STATUS=200
write_env
run
assert_rc "상태 코드 틀림 → 1" 1 "$RC"
assert_contains "3번 상태" "$OUT" "FAIL  3 OPTIONS /me: 상태 200"
start_mock CORS_HEADER=0
write_env
run
assert_rc "헤더 없음 → 1" 1 "$RC"
assert_contains "3번 헤더" "$OUT" "access-control-allow-origin 헤더 없음"

echo "[5] 시험 계정 토큰으로 /me 가 500 이면 실패"
start_mock ME_STATUS=500
write_env
run
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "4번 실패" "$OUT" "FAIL  4 /me 시험 계정: 상태 500"

echo "[6] Supabase 로그인 실패(장애 흉내): 기본은 경고만, SMOKE_STRICT=1 이면 실패"
start_mock LOGIN_STATUS=503
write_env
run
assert_rc "기본은 통과" 0 "$RC"
assert_contains "WARN 표시" "$OUT" "WARN  4 /me: Supabase 로그인 실패 (상태 503)"
SMOKE_STRICT=1 run
assert_rc "strict 는 실패" 1 "$RC"

echo "[7] 시험 계정 설정이 비어 있으면 실패"
start_mock
write_env
sed -i 's/^SMOKE_EMAIL=.*/SMOKE_EMAIL=/' "$TMP/smoke.env"
run
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "설정 비어 있음" "$OUT" "시험 계정 설정이 비어 있음"

echo "[8] 시험 계정 비밀번호가 틀리면(비밀번호 변경 · 계정 삭제) 로그인 거부 → 실패 (조용히 넘기지 않음)"
start_mock
write_env
sed -i 's/^SMOKE_PASSWORD=.*/SMOKE_PASSWORD=wrong/' "$TMP/smoke.env"
run
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "로그인 거부 문구" "$OUT" "FAIL  4 /me: 시험 계정 로그인이 거부됨 (상태 400)"
for st in 401 403; do
  start_mock LOGIN_STATUS=$st
  write_env
  run
  assert_rc "로그인 $st → 실패" 1 "$RC"
  assert_contains "로그인 $st 문구" "$OUT" "로그인이 거부됨 (상태 $st)"
done
start_mock LOGIN_STATUS=429
write_env
run
assert_rc "로그인 429(요청 제한) → 경고만" 0 "$RC"
assert_contains "429 경고" "$OUT" "WARN  4 /me: Supabase 로그인 실패 (상태 429)"

echo "[9] 예측 서버: ok=true 면 통과, 503 이면 실패"
start_mock ML_OK=1
write_env
run --ml "$BASE/ml"
assert_rc "ok=true 통과" 0 "$RC"
assert_contains "5번 통과" "$OUT" "PASS  5 prepare-ml /health 200 ok=true"
start_mock ML_OK=0
write_env
run --ml "$BASE/ml"
assert_rc "ok=false 실패" 1 "$RC"
assert_contains "5번 실패" "$OUT" "FAIL  5 prepare-ml /health: 상태 503"

echo "[10] 서버가 안 떠 있으면 실패 (상태 000)"
stop_mock
RC=0
OUT=$("$SMOKE" --base "http://127.0.0.1:1" --env-file "$TMP/smoke.env" 2>&1) || RC=$?
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "1번 000" "$OUT" "FAIL  1 /health: 상태 000"

echo "[11] --base 가 없으면 종료 코드 2"
RC=0
"$SMOKE" >/dev/null 2>&1 || RC=$?
assert_rc "종료 코드 2" 2 "$RC"

finish

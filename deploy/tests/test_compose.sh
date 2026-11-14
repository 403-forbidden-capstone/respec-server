#!/usr/bin/env bash
# shellcheck disable=SC2034
# compose.yml 시험: 변수 치환 · 프로필 · 정책(compose_policy.py). docker 엔진은 필요 없고 docker compose 명령(config)만 씀.
set -Eeuo pipefail
SUITE="compose"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose 가 없어 compose 시험을 건너뜀" >&2
  exit 0
fi
PY=$(find_python) || { echo "python 을 못 찾아 compose 시험을 건너뜀" >&2; exit 0; }

T=$(new_tmp)
cp "$DEPLOY_DIR/compose.yml" "$T/compose.yml"
: >"$T/api.env"
: >"$T/ml.env"
mkdir -p "$T/models/v4_age_only"

cfg() { # cfg 형식 [API_TAG 값]  — deploy.env · versions.env 를 시험 폴더에 쓰고 config 출력
  docker compose -p respec --project-directory "$T" -f "$T/compose.yml" \
    --env-file "$T/deploy.env" --env-file "$T/versions.env" config "$@"
}
policy() { # policy 모드 → 정책 점검기에 JSON 을 넘김
  cfg --format json | "$PY" "$TESTS_DIR/compose_policy.py" "$1"
}

echo "[1] 예측 서버 없음(기본): api · scheduler 만"
printf 'API_TAG=sha-abc1234\nCOMPOSE_PROFILES=\n' >"$T/deploy.env"
printf 'ML_TAG=none\n' >"$T/versions.env"
out=$(cfg --services | sort | tr '\n' ' ')
assert_eq "서비스" "api scheduler " "$out"
rc=0
res=$(policy without-ml 2>&1) || rc=$?
assert_rc "정책 통과" 0 "$rc"
assert_contains "정책 문구" "$res" "compose 정책 통과"

echo "[2] 예측 서버 켬: COMPOSE_PROFILES=ml + ML_TAG"
printf 'API_TAG=sha-abc1234\nCOMPOSE_PROFILES=ml\n' >"$T/deploy.env"
printf 'ML_TAG=sha-def5678\n' >"$T/versions.env"
out=$(cfg --services | sort | tr '\n' ' ')
assert_eq "서비스" "api prepare-ml scheduler " "$out"
rc=0
res=$(policy with-ml 2>&1) || rc=$?
assert_rc "정책 통과" 0 "$rc"
images=$(cfg --images | sort | tr '\n' ' ')
assert_contains "api 이미지 태그" "$images" "ghcr.io/403-forbidden-capstone/respec-api:sha-abc1234"
assert_contains "ml 이미지 태그" "$images" "ghcr.io/403-forbidden-capstone/respec-prepare-ml:sha-def5678"

echo "[3] API_TAG 가 비면 config 실패 (태그 없이 올리지 않음)"
printf 'API_TAG=\nCOMPOSE_PROFILES=\n' >"$T/deploy.env"
rc=0
cfg -q >/dev/null 2>&1 || rc=$?
assert_eq "실패" 1 "$rc"

echo "[4] APP_VERSION 이 API_TAG 와 같음 (/health 의 version 이 배포 태그를 따라감)"
printf 'API_TAG=sha-abc1234\nCOMPOSE_PROFILES=\n' >"$T/deploy.env"
ver=$(cfg --format json | "$PY" -c 'import json,sys; s=json.load(sys.stdin)["services"]; print(s["api"]["environment"]["APP_VERSION"], s["scheduler"]["environment"]["APP_VERSION"])')
assert_eq "api · scheduler 의 APP_VERSION" "sha-abc1234 sha-abc1234" "$ver"

echo "[5] 정책 점검기가 실제로 위반을 잡는지 (공개 포트 · 마운트 · 비밀 environment · mem_limit)"
printf 'API_TAG=sha-abc1234\nCOMPOSE_PROFILES=\n' >"$T/deploy.env"
mutate() { # mutate "sed 식" → 정책 결과 코드
  sed "$1" "$DEPLOY_DIR/compose.yml" >"$T/compose.yml"
  local rc=0
  policy without-ml >/dev/null 2>&1 || rc=$?
  printf '%s' "$rc"
}
assert_eq "포트를 0.0.0.0 으로 열면 잡힘" 1 "$(mutate 's#"127.0.0.1:8000:8000"#"8000:8000"#')"
assert_eq "mem_limit 을 키우면 잡힘" 1 "$(mutate 's#mem_limit: 300m#mem_limit: 900m#')"
assert_eq "restart 를 바꾸면 잡힘" 1 "$(mutate '0,/restart: unless-stopped/s//restart: always/')"
assert_eq "비밀 environment 를 직접 넣으면 잡힘" 1 "$(mutate 's#APP_VERSION: ${API_TAG}#SUPABASE_SECRET_KEY: abc#')"
cp "$DEPLOY_DIR/compose.yml" "$T/compose.yml"

finish

#!/usr/bin/env bash
# shellcheck disable=SC2034
# compose.yml 시험: 변수 치환 · 프로필 · 정책(deploy/compose-policy.py, 서버 배포 직전에도 같은 점검기를 씀).
# docker 엔진은 필요 없고 docker compose 명령(config)만 씀.
set -Eeuo pipefail
SUITE="compose"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose 가 없어 compose 시험을 건너뜀" >&2
  exit 0
fi
PY=$(find_python) || { echo "python 을 못 찾아 compose 시험을 건너뜀" >&2; exit 0; }
POLICY="$DEPLOY_DIR/compose-policy.py"

T=$(new_tmp)
cp "$DEPLOY_DIR/compose.yml" "$T/compose.yml"
echo "SUPABASE_SECRET_KEY=$SECRET_MARKER" >"$T/api.env"
echo "CATBOOST_API_TOKEN=$SECRET_MARKER" >"$T/ml.env"
mkdir -p "$T/models/v4_age_only"

cfg() { # cfg [전역 옵션...] -- config 옵션...   (예: cfg -- --services)
  local -a globals=()
  while [[ $1 != -- ]]; do globals+=("$1"); shift; done
  shift
  docker compose -p respec --project-directory "$T" -f "$T/compose.yml" \
    --env-file "$T/deploy.env" --env-file "$T/versions.env" "${globals[@]}" config "$@"
}
# 서버 배포 직전과 같은 방식: 모든 프로필 · env 값 안 풀기 → 정책 점검기
policy() {
  cfg --profile '*' -- --no-env-resolution --format json | "$PY" "$POLICY" --app-dir "$T"
}

printf 'API_TAG=sha-abc1234\nCOMPOSE_PROFILES=\n' >"$T/deploy.env"
printf 'ML_TAG=none\n' >"$T/versions.env"

echo "[1] 예측 서버 없음(기본): api · scheduler 만"
out=$(cfg -- --services | sort | tr '\n' ' ')
assert_eq "서비스" "api scheduler " "$out"

echo "[2] 예측 서버 켬: COMPOSE_PROFILES=ml + ML_TAG"
printf 'API_TAG=sha-abc1234\nCOMPOSE_PROFILES=ml\n' >"$T/deploy.env"
printf 'ML_TAG=sha-def5678\n' >"$T/versions.env"
out=$(cfg -- --services | sort | tr '\n' ' ')
assert_eq "서비스" "api prepare-ml scheduler " "$out"
images=$(cfg -- --images | sort | tr '\n' ' ')
assert_contains "api 이미지 태그" "$images" "ghcr.io/403-forbidden-capstone/respec-api:sha-abc1234"
assert_contains "ml 이미지 태그" "$images" "ghcr.io/403-forbidden-capstone/respec-prepare-ml:sha-def5678"

echo "[3] API_TAG 가 비면 config 실패 (태그 없이 올리지 않음)"
printf 'API_TAG=\nCOMPOSE_PROFILES=\n' >"$T/deploy.env"
rc=0
cfg -- -q >/dev/null 2>&1 || rc=$?
assert_eq "실패" 1 "$rc"

echo "[4] APP_VERSION 이 API_TAG 와 같음 (/health 의 version 이 배포 태그를 따라감)"
printf 'API_TAG=sha-abc1234\nCOMPOSE_PROFILES=\n' >"$T/deploy.env"
printf 'ML_TAG=none\n' >"$T/versions.env"
ver=$(cfg -- --format json | "$PY" -c 'import json,sys; s=json.load(sys.stdin)["services"]; print(s["api"]["environment"]["APP_VERSION"], s["scheduler"]["environment"]["APP_VERSION"])')
assert_eq "api · scheduler 의 APP_VERSION" "sha-abc1234 sha-abc1234" "$ver"

echo "[5] 정책 점검: 실제 compose.yml 은 통과하고, 비밀 값은 점검기까지 오지 않음"
rc=0
res=$(policy 2>&1) || rc=$?
assert_rc "정책 통과" 0 "$rc"
assert_contains "정책 문구" "$res" "compose 정책 통과"
json=$(cfg --profile '*' -- --no-env-resolution --format json)
assert_not_contains "config JSON 에 비밀 값이 없음(--no-env-resolution)" "$json" "$SECRET_MARKER"
init_count=$(printf '%s' "$json" | "$PY" -c 'import json,sys; s=json.load(sys.stdin)["services"]; print(sum(1 for v in s.values() if v.get("init") is True))')
assert_eq "세 서비스 모두 init: true" 3 "$init_count"

echo "[6] 정책 점검기가 실제로 위반을 잡는지 (공개 포트 · 마운트 · 특권 · 이미지 · env_file · 추가 서비스 · 메모리 ...)"
mutate() { # mutate 이름 "sed 식" → 정책이 위반 문구로 거부해야 함 (compose 가 깨져서 거부된 것은 인정 안 함)
  sed "$2" "$DEPLOY_DIR/compose.yml" >"$T/compose.yml"
  local rc=0 res
  res=$(policy 2>&1) || rc=$?
  assert_eq "위반 → 거부: $1" 1 "$rc"
  assert_contains "  └ 정책 위반 문구: $1" "$res" "compose 정책 위반:"
  assert_not_contains "  └ compose 가 깨진 게 아니라 정책이 잡음: $1" "$res" "JSON 으로 읽지 못함"
}
mutate "포트를 0.0.0.0 으로 열기" 's#"127.0.0.1:8000:8000"#"8000:8000"#'
mutate "기본 네트워크 driver 바꾸기" 's#^name: respec$#name: respec\nnetworks:\n  default:\n    driver: macvlan#'
mutate "기본 네트워크 driver_opts" 's#^name: respec$#name: respec\nnetworks:\n  default:\n    driver_opts:\n      com.docker.network.bridge.host_binding_ipv4: 0.0.0.0#'
mutate "기본 네트워크 ipam 설정" 's#^name: respec$#name: respec\nnetworks:\n  default:\n    ipam:\n      config:\n        - subnet: 10.9.0.0/24#'
mutate "기본 네트워크 external" 's#^name: respec$#name: respec\nnetworks:\n  default:\n    external: true\n    name: bridge#'
mutate "기본 네트워크 이름을 host 로" 's#^name: respec$#name: respec\nnetworks:\n  default:\n    name: host#'
mutate "mem_limit 을 키워 합계 초과" 's#mem_limit: 300m#mem_limit: 900m#'
mutate "restart 를 always 로" '0,/restart: unless-stopped/s//restart: always/'
mutate "environment 에 비밀 이름 직접 넣기" 's#APP_VERSION: ${API_TAG}#SUPABASE_SECRET_KEY: abc#'
mutate "api 에 호스트 루트 마운트" 's#^    env_file: ./api.env#    volumes: ["/:/host"]\n    env_file: ./api.env#'
mutate "api 에 privileged" 's#^    env_file: ./api.env#    privileged: true\n    env_file: ./api.env#'
mutate "api 에 network_mode host" 's#^    env_file: ./api.env#    network_mode: host\n    env_file: ./api.env#'
mutate "api 에 cap_add" 's#^    env_file: ./api.env#    cap_add: ["SYS_ADMIN"]\n    env_file: ./api.env#'
mutate "api entrypoint 재정의" 's#^    env_file: ./api.env#    entrypoint: ["sh","-c","id"]\n    env_file: ./api.env#'
mutate "api 이미지를 다른 레지스트리로" '0,/image: \*api-image/s//image: docker.io\/library\/alpine:3/'
mutate "이미지 태그 latest" 's#respec-api:\${API_TAG:?[^}]*}#respec-api:latest#'
mutate "env_file 을 다른 곳으로" 's#env_file: ./api.env#env_file: /etc/shadow#'
mutate "scheduler 시작 명령 바꾸기" 's#\["python", "-m", "app.scheduler"\]#["sh", "-c", "id"]#'
mutate "prepare-ml profiles 제거" '/profiles: \["ml"\]/d'
mutate "prepare-ml 모델 마운트를 쓰기 가능으로" 's#read_only: true#read_only: false#'
mutate "로그 크기 제한 제거" 's#max-size: "10m"#max-size: "1g"#'
mutate "security_opt 를 seccomp=unconfined 로" 's#no-new-privileges:true#seccomp=unconfined#'
# 서비스 하나 추가 (sed 로는 어려워서 직접)
{ cat "$DEPLOY_DIR/compose.yml"; printf '\n  evil:\n    image: ghcr.io/403-forbidden-capstone/respec-api:sha-x\n    restart: unless-stopped\n'; } >"$T/compose.yml"
rc=0
policy >/dev/null 2>&1 || rc=$?
assert_eq "위반 → 거부: 서비스 추가" 1 "$rc"
cp "$DEPLOY_DIR/compose.yml" "$T/compose.yml"
rc=0
policy >/dev/null 2>&1 || rc=$?
assert_eq "원본으로 돌리면 다시 통과" 0 "$rc"

finish

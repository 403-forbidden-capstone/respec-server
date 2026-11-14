#!/usr/bin/env bash
# Re:Spec 서버 배포 · 되돌리기 · 상태. deploy 계정의 SSH 강제 명령(command=)으로만 실행됨.
#
#   ssh deploy@서버 "deploy sha-abc1234"   그 태그로 배포 (실패하면 자동으로 직전 정상 버전으로 되돌림)
#   ssh deploy@서버 "rollback"             마지막 정상 버전으로 수동 되돌리기
#   ssh deploy@서버 "status"               지금 태그 · 컨테이너 상태 (비밀값 없음)
#
# 이 파일은 root 소유(/usr/local/lib/respec/deploy.sh)라 deploy 계정이 못 고침. 고치면 bootstrap.sh 를 다시 돌려 반영.
# 받는 입력은 위 세 가지뿐이고, 태그는 ^sha-[0-9a-f]{7,40}$ 만 통과. 나머지는 전부 거부(종료 코드 2).
#
# SSH 가 끊겨도 배포는 끝까지 돎: deploy · rollback 은 실제 일을 별도 세션(setsid)의 worker 로 돌리고, SSH 에 붙은
# 이 프로세스는 worker 의 출력 파일을 따라 보여 주기만 함. (Actions 취소 · 타임아웃 · 네트워크 끊김으로 SSH 가 죽어도
# worker 는 계속 돌아 "새 버전은 떴는데 점검도 되돌림도 안 한" 중간 상태가 안 남음.)
#
# 배포 순서
#   1 번들 이미지(respec-deploy:<태그>)에서 compose.yml · versions.env · env.example 을 꺼냄
#   2 점검: compose 문법 · compose 정책(compose-policy.py: 허용한 모양만) · env 이름 점검(check-env.sh)
#           — 여기서 실패하면 돌고 있는 것은 그대로
#   3 교체: 새 compose 설치 → 이미지 받기 → up -d --wait (healthy 될 때까지)
#   4 점검: 컨테이너가 안 죽고 재시작 0 인지 → 배포 직후 점검(smoke.sh). 통과하면 이 버전을 LAST_GOOD 로 기록,
#           실패하면 직전 정상 버전으로 되돌림
set -Eeuo pipefail

# 아래 값은 일부러 환경 변수로 안 받음 (SSH 쪽에서 못 바꾸게). 시험은 파일을 source 한 뒤 변수를 덮어씀.
APP_DIR=/srv/respec
LIB_DIR=/usr/local/lib/respec
REGISTRY=ghcr.io/403-forbidden-capstone
PROJECT=respec
TAG_RE='^sha-[0-9a-f]{7,40}$'
KEEP_IMAGES=3
API_PORT=8000
ML_PORT=8091
LOCK_WAIT=600
SETTLE_SECONDS=20 # 올린 뒤 컨테이너가 바로 죽는지(import 오류 등) 지켜보는 시간(초)
PYTHON_BIN=python3
SELF=${BASH_SOURCE[0]}
WORKER_LAUNCH=(setsid bash "$SELF")
FLUSH_OFFSET=0

ACTION=""
TAG=""
RUN_DIR=""

ts() { date '+%H:%M:%S'; }
# 화면(SSH)이 끊겨 있어도 로그 출력 때문에 일이 죽지 않게 모든 쓰기에 || true
log() {
  local line
  line="[deploy $(ts)] $*"
  printf '%s\n' "$line" 2>/dev/null || true
  if [[ -d $APP_DIR ]]; then printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$APP_DIR/deploy.log" 2>/dev/null || true; fi
}
die() {
  printf '[deploy %s] 오류: %s\n' "$(ts)" "$*" >&2 || true
  exit 1
}
die_usage() {
  printf '[deploy %s] 거부: %s\n사용법: deploy sha-<커밋 7~40자리 16진수> | rollback | status\n' "$(ts)" "$*" >&2 || true
  exit 2
}

# ---------- 입력 검사 ----------
parse_command() {
  local cmd="${1-}" re='^[A-Za-z0-9 _.:-]+$'
  [[ -n $cmd ]] || die_usage "명령이 없음"
  [[ $cmd =~ $re ]] || die_usage "허용하지 않는 문자가 있음"
  local -a w
  read -r -a w <<<"$cmd"
  ACTION=${w[0]}
  TAG=""
  case "$ACTION" in
    deploy)
      [[ ${#w[@]} -eq 2 ]] || die_usage "deploy 는 태그 하나만 받음"
      TAG=${w[1]}
      [[ $TAG =~ $TAG_RE ]] || die_usage "태그 모양이 sha-<7~40자리 16진수> 가 아님"
      ;;
    rollback | status)
      [[ ${#w[@]} -eq 1 ]] || die_usage "$ACTION 은 인자를 받지 않음"
      ;;
    *) die_usage "알 수 없는 명령" ;;
  esac
}

# ---------- 상태 파일 (deploy.env: KEY=VALUE 한 줄씩, 읽을 때 source 하지 않음) ----------
env_get() { # env_get 이름 [파일]
  local f=${2:-$APP_DIR/deploy.env}
  { grep -E "^$1=" "$f" 2>/dev/null || true; } | tail -1 | cut -d= -f2-
}
env_set() { # env_set 이름 값 — 값은 [A-Za-z0-9._,-] 만 허용
  local name=$1 value=$2 f="$APP_DIR/deploy.env" tmp
  [[ $value =~ ^[A-Za-z0-9._,-]*$ ]] || die "env_set: 허용하지 않는 값"
  tmp=$(mktemp "$APP_DIR/.deploy.env.XXXXXX")
  if grep -qE "^$name=" "$f"; then
    sed "s/^$name=.*/$name=$value/" "$f" >"$tmp"
  else
    cat "$f" >"$tmp"
    [[ -z $(tail -c1 "$f") ]] || echo >>"$tmp"
    echo "$name=$value" >>"$tmp"
  fi
  chmod 600 "$tmp"
  mv -f "$tmp" "$f"
}
profile_has() { # profile_has ml — deploy.env 의 COMPOSE_PROFILES(쉼표 목록)에 있는지
  local p
  p=",$(env_get COMPOSE_PROFILES),"
  [[ $p == *",$1,"* ]]
}

compose() {
  docker compose -p "$PROJECT" --project-directory "$APP_DIR" -f "$APP_DIR/compose.yml" \
    --env-file "$APP_DIR/deploy.env" --env-file "$APP_DIR/versions.env" "$@"
}

acquire_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$APP_DIR/.deploy.lock"
    flock -w "$LOCK_WAIT" 9 || die "다른 배포가 ${LOCK_WAIT}초 넘게 진행 중"
  else
    log "경고: flock 이 없어 동시 실행 잠금을 건너뜀"
  fi
}

trim_log() {
  local f="$APP_DIR/deploy.log"
  if [[ -f $f ]] && [[ $(wc -c <"$f") -gt 1048576 ]]; then
    tail -n 2000 "$f" >"$f.tmp" && mv -f "$f.tmp" "$f"
  fi
}

# ---------- 번들 꺼내기 ----------
extract_bundle() { # → $APP_DIR/.next/{compose.yml,versions.env,env.example}
  local img="$REGISTRY/respec-deploy:$TAG" dest="$APP_DIR/.next" cid
  rm -rf "$dest"
  mkdir -p "$dest"
  docker pull --quiet "$img" >/dev/null || return 1
  # scratch 이미지는 시작 명령이 없어서 create 에 아무 경로나 넘김 (실행은 안 함)
  cid=$(docker create "$img" /compose.yml) || return 1
  if ! docker cp "$cid:/compose.yml" "$dest/compose.yml" ||
    ! docker cp "$cid:/versions.env" "$dest/versions.env" ||
    ! docker cp "$cid:/env.example" "$dest/env.example"; then
    docker rm -f "$cid" >/dev/null 2>&1 || true
    return 1
  fi
  docker rm "$cid" >/dev/null 2>&1 || true
  # docker cp 는 이미지 안의 심볼릭 링크를 링크 그대로 꺼내므로, compose 가 서버의 다른 파일(예: 비밀 파일)을 따라 읽지 않게
  # 꺼낸 것은 일반 파일만 받음
  local f
  for f in compose.yml versions.env env.example; do
    if [[ -L $dest/$f || ! -f $dest/$f ]]; then
      log "번들 거부: $f 가 일반 파일이 아님(심볼릭 링크 · 폴더 등)"
      return 1
    fi
  done
}

preflight() { # 돌고 있는 것을 건드리기 전 점검. 실패하면 return 1
  local dest="$APP_DIR/.next" ml_tag
  grep -qE '^ML_TAG=' "$dest/versions.env" || { log "점검 실패: versions.env 에 ML_TAG 가 없음"; return 1; }
  if ! API_TAG="$TAG" docker compose -p "$PROJECT" --project-directory "$APP_DIR" -f "$dest/compose.yml" \
    --env-file "$APP_DIR/deploy.env" --env-file "$dest/versions.env" config -q; then
    log "점검 실패: compose 파일 문법 · 변수"
    return 1
  fi
  # 번들의 compose 가 "허용한 모양"인지 (호스트 마운트 · 특권 옵션 등을 거부). --profile '*' 로 꺼져 있는 서비스도 검사,
  # --no-env-resolution 으로 env 파일 값(비밀)이 점검기까지 오지 않게 함
  if ! API_TAG="$TAG" docker compose -p "$PROJECT" --project-directory "$APP_DIR" -f "$dest/compose.yml" \
    --env-file "$APP_DIR/deploy.env" --env-file "$dest/versions.env" --profile '*' config --no-env-resolution --format json |
    "$PYTHON_BIN" "$LIB_DIR/compose-policy.py" --app-dir "$APP_DIR"; then
    log "점검 실패: compose 파일이 정책(허용한 모양)에 안 맞음 (위 목록 참고)"
    return 1
  fi
  ml_tag=$(env_get ML_TAG "$dest/versions.env")
  if profile_has ml && [[ $ml_tag == none ]]; then
    log "점검 실패: COMPOSE_PROFILES 에 ml 이 있는데 versions.env 의 ML_TAG 가 none"
    return 1
  fi
  if ! "$LIB_DIR/check-env.sh" --dir "$APP_DIR" --example "$dest/env.example"; then
    log "점검 실패: 서버 env 파일이 이번 버전의 .env.example 과 안 맞음 (위 목록 참고, 값은 출력 안 함)"
    return 1
  fi
}

# 올린 뒤 컨테이너가 계속 떠 있는지: 모두 running 이고 재시작 횟수 0 (scheduler 처럼 헬스체크가 없는 컨테이너의 crash loop 을 잡음)
check_containers() {
  local ids cid line bad=0
  if [[ $SETTLE_SECONDS -gt 0 ]]; then sleep "$SETTLE_SECONDS"; fi
  ids=$(compose ps -q) || { log "컨테이너 목록을 못 읽음"; return 1; }
  [[ -n $ids ]] || { log "떠 있는 컨테이너가 없음"; return 1; }
  while IFS= read -r cid; do
    [[ -n $cid ]] || continue
    if ! line=$(docker inspect -f '{{.Name}} running={{.State.Running}} restarts={{.RestartCount}}' "$cid"); then
      bad=1
      continue
    fi
    case "$line" in
      *"running=true restarts=0") ;;
      *) log "컨테이너 이상: $line"; bad=1 ;;
    esac
  done <<<"$ids"
  return "$bad"
}

run_smoke() { # run_smoke <기대 version> [no-ml]
  local ver=$1 skip_ml=${2:-}
  local -a args=(--base "http://127.0.0.1:$API_PORT" --version "$ver" --env-file "$APP_DIR/smoke.env")
  if [[ -z $skip_ml ]] && profile_has ml; then args+=(--ml "http://127.0.0.1:$ML_PORT"); fi
  "$LIB_DIR/smoke.sh" "${args[@]}"
}

# ---------- 이미지 정리: 저장소별 최근 KEEP_IMAGES 개 + 쓰는 태그 · 마지막 정상 태그는 남김 ----------
prune_images() {
  local -a protected=()
  local t repo tags idx tag
  for t in "$(env_get API_TAG)" "$(env_get LAST_GOOD_API_TAG)" \
    "$(env_get ML_TAG "$APP_DIR/versions.env")" "$(env_get ML_TAG "$APP_DIR/versions.prev.env")"; do
    [[ -n $t ]] && protected+=("$t")
  done
  for repo in respec-api respec-deploy respec-prepare-ml; do
    idx=0
    tags=$(docker image ls "$REGISTRY/$repo" --format '{{.CreatedAt}}|{{.Tag}}' 2>/dev/null | sort -r | cut -d'|' -f2) || tags=""
    while IFS= read -r tag; do
      [[ -n $tag && $tag != '<none>' ]] || continue
      idx=$((idx + 1))
      [[ $idx -le $KEEP_IMAGES ]] && continue
      [[ " ${protected[*]} " == *" $tag "* ]] && continue
      docker image rm "$REGISTRY/$repo:$tag" >/dev/null 2>&1 || true
    done <<<"$tags"
  done
  docker image prune -f >/dev/null 2>&1 || true
}

promote_good() {
  cp -f "$APP_DIR/compose.yml" "$APP_DIR/compose.prev.yml"
  cp -f "$APP_DIR/versions.env" "$APP_DIR/versions.prev.env"
  env_set LAST_GOOD_API_TAG "$TAG"
  prune_images
}

# 마지막 정상 버전 파일로 되돌려 다시 띄움.
# 종료 코드: 0 되돌림 + 점검 통과 / 1 되돌리기 실패 / 2 되돌릴 정상 버전이 없음 / 3 되돌렸지만 점검 실패
restore_last_good() {
  local good ml_prev no_ml=""
  good=$(env_get LAST_GOOD_API_TAG)
  if [[ -z $good || ! -f $APP_DIR/compose.prev.yml || ! -f $APP_DIR/versions.prev.env ]]; then
    return 2
  fi
  cp -f "$APP_DIR/compose.prev.yml" "$APP_DIR/compose.yml" || return 1
  cp -f "$APP_DIR/versions.prev.env" "$APP_DIR/versions.env" || return 1
  env_set API_TAG "$good"
  # 지금 예측 프로필이 켜져 있는데 직전 정상 버전엔 예측 이미지가 없으면(ML_TAG=none) 이번 복구는 예측 서버 없이 올림
  ml_prev=$(env_get ML_TAG "$APP_DIR/versions.prev.env")
  if profile_has ml && [[ $ml_prev == none ]]; then
    no_ml=no-ml
    log "경고: 예측 서버 프로필이 켜져 있지만 직전 정상 버전에는 예측 이미지가 없음(ML_TAG=none) → 이번 복구는 예측 서버 없이 올림"
  fi
  log "직전 정상 버전 $good 으로 되돌리는 중"
  if [[ -n $no_ml ]]; then
    COMPOSE_PROFILES="" compose up -d --wait --wait-timeout 180 --remove-orphans || return 1
  else
    compose up -d --wait --wait-timeout 180 --remove-orphans || return 1
  fi
  if check_containers && run_smoke "$good" "$no_ml"; then
    log "되돌림 완료: 지금 $good"
    return 0
  fi
  log "경고: 되돌린 버전($good)도 점검에 실패함. 컨테이너는 떠 있으니 서버를 직접 확인할 것"
  return 3
}

# ---------- 동작 ----------
do_deploy() {
  [[ -n $(docker info --format '{{.ServerVersion}}' 2>/dev/null) ]] || die "docker 에 연결할 수 없음"
  log "배포 시작: $TAG (지금 $(env_get API_TAG | sed 's/^$/없음/'), 마지막 정상 $(env_get LAST_GOOD_API_TAG | sed 's/^$/없음/'))"

  log "1/4 번들 받기"
  extract_bundle || { log "번들 이미지 respec-deploy:$TAG 를 받지 못함 (태그가 GHCR 에 있는지, 서버의 ghcr 로그인이 살아 있는지)"; exit 1; }

  log "2/4 점검"
  preflight || exit 1

  log "3/4 교체"
  cp -f "$APP_DIR/.next/compose.yml" "$APP_DIR/compose.yml"
  cp -f "$APP_DIR/.next/versions.env" "$APP_DIR/versions.env"
  cp -f "$APP_DIR/.next/env.example" "$APP_DIR/env.example"
  env_set API_TAG "$TAG"

  local ok=1
  if ! compose pull --quiet; then
    log "이미지 받기 실패"
    ok=0
  fi
  if [[ $ok == 1 ]] && ! compose up -d --wait --wait-timeout 180 --remove-orphans; then
    log "컨테이너가 healthy 가 되지 않음"
    ok=0
  fi

  if [[ $ok == 1 ]]; then
    log "4/4 점검 (컨테이너 상태 · 배포 직후 점검)"
    if ! check_containers; then
      log "컨테이너가 계속 떠 있지 않음 (재시작 반복 또는 종료)"
      ok=0
    elif ! run_smoke "$TAG"; then
      log "배포 직후 점검 실패"
      ok=0
    fi
  fi

  if [[ $ok == 1 ]]; then
    promote_good
    log "배포 성공: $TAG"
    return 0
  fi

  log "되돌리기 시작"
  local rc=0
  restore_last_good || rc=$?
  if [[ $rc == 2 ]]; then
    log "직전 정상 버전이 없어(첫 배포) 컨테이너를 내림"
    compose down || true
  fi
  log "배포 실패: $TAG"
  exit 1
}

do_rollback() {
  local rc=0
  restore_last_good || rc=$?
  case "$rc" in
    0) ;;
    2) die "되돌릴 정상 버전이 없음 (LAST_GOOD_API_TAG 가 비어 있음)" ;;
    3) die "되돌렸지만 점검에 실패함 (컨테이너는 떠 있음, 서버를 직접 확인할 것)" ;;
    *) die "되돌리기 실패" ;;
  esac
}

do_status() {
  printf 'API_TAG=%s\nLAST_GOOD_API_TAG=%s\nML_TAG=%s\nCOMPOSE_PROFILES=%s\n' \
    "$(env_get API_TAG)" "$(env_get LAST_GOOD_API_TAG)" \
    "$(env_get ML_TAG "$APP_DIR/versions.env")" "$(env_get COMPOSE_PROFILES)"
  compose ps --format 'table {{.Name}}\t{{.Service}}\t{{.Status}}' || true
}

# ---------- worker: 실제 일. SSH 와 분리된 세션에서 돎 ----------
run_worker() {
  trap '' HUP PIPE
  # 끝날 때 종료 코드를 남김 (SSH 쪽 프로세스가 이 파일을 읽고 같은 코드로 끝남)
  if [[ -n ${RESPEC_RUN_DIR:-} && $RESPEC_RUN_DIR == "$APP_DIR"/.run.* && -d $RESPEC_RUN_DIR ]]; then
    # shellcheck disable=SC2064
    trap "echo \"\$?\" >\"$RESPEC_RUN_DIR/status\"" EXIT
  fi
  acquire_lock
  trim_log
  case "$ACTION" in
    deploy) do_deploy ;;
    rollback) do_rollback ;;
  esac
}

# worker 가 쓴 출력 파일의 새 부분을 화면에 내보냄 (화면이 끊겨 있어도 이 프로세스만 영향받고 worker 는 무관)
flush_out() {
  local size
  size=$(stat -c %s "$RUN_DIR/out" 2>/dev/null || echo 0)
  if ((size > FLUSH_OFFSET)); then
    tail -c +$((FLUSH_OFFSET + 1)) "$RUN_DIR/out" 2>/dev/null | head -c $((size - FLUSH_OFFSET)) || true
    FLUSH_OFFSET=$size
  fi
}

launch_worker() {
  local cmd="$ACTION" pid rc
  [[ -z $TAG ]] || cmd="$ACTION $TAG"
  find "$APP_DIR" -maxdepth 1 -name '.run.*' -mmin +1440 -exec rm -rf {} + 2>/dev/null || true
  RUN_DIR=$(mktemp -d "$APP_DIR/.run.XXXXXX")
  : >"$RUN_DIR/out"
  (
    export RESPEC_WORKER=1 RESPEC_RUN_DIR="$RUN_DIR" SSH_ORIGINAL_COMMAND="$cmd"
    "${WORKER_LAUNCH[@]}"
  ) >>"$RUN_DIR/out" 2>&1 </dev/null &
  pid=$!
  while :; do
    flush_out
    if [[ -f $RUN_DIR/status ]] || ! kill -0 "$pid" 2>/dev/null; then
      sleep 0.2
      flush_out
      break
    fi
    sleep 1
  done
  rc=$(cat "$RUN_DIR/status" 2>/dev/null || true)
  if [[ ! $rc =~ ^[0-9]+$ ]]; then
    log "작업 프로세스가 종료 코드를 남기지 않고 끝남 — $APP_DIR/deploy.log 를 확인할 것"
    rc=1
  fi
  rm -rf "$RUN_DIR"
  exit "$rc"
}

main() {
  parse_command "${SSH_ORIGINAL_COMMAND:-$*}"
  [[ -d $APP_DIR && -f $APP_DIR/deploy.env ]] || die "$APP_DIR/deploy.env 가 없음 (bootstrap.sh 를 먼저 돌려야 함)"
  case "$ACTION" in
    status) do_status ;;
    *)
      if [[ -n ${RESPEC_WORKER:-} ]]; then run_worker; else launch_worker; fi
      ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi

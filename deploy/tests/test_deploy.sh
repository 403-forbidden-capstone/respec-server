#!/usr/bin/env bash
# shellcheck disable=SC1090,SC2034
# deploy.sh 시험: docker 를 가짜로 바꿔 끼우고 입력 검사 · 배포 · 실패 시 되돌림 · 수동 되돌리기 · 정리를 돌림.
# deploy.sh 는 source 만 하고(main 은 안 돎) APP_DIR · LIB_DIR 을 임시 폴더로 덮어쓴 뒤 main 을 부름.
set -Eeuo pipefail
SUITE="deploy"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
fake_stat

DEPLOY_SH="$DEPLOY_DIR/deploy.sh"
EXAMPLE="$REPO_DIR/.env.example"
REG=ghcr.io/403-forbidden-capstone

# ---------- 가짜 docker ----------
# 시험 폴더 안 파일로 동작을 바꿈:  bad_up(올리면 실패할 API_TAG 목록) · fail_pull(번들 받기 실패 태그) · fail_config(config 실패 태그)
#                                 fail_smoke(점검 실패 --version 목록) · image_ls_<저장소>(docker image ls 출력)
docker() {
  echo "docker $*" >>"$CALLS"
  case "$1" in
    info) echo "29.0.0" ;;
    pull)
      local img=${*: -1}
      LAST_TAG=${img##*:}
      if grep -qx "$LAST_TAG" "$T/fail_pull" 2>/dev/null; then return 1; fi
      [[ -d $BUNDLES/$LAST_TAG ]] || return 1
      ;;
    create) echo "cid-$LAST_TAG" ;;
    cp)
      local src=$2 dest=$3 tag file
      tag=${src%%:*}
      tag=${tag#cid-}
      file=${src#*:}
      cp "$BUNDLES/$tag$file" "$dest"
      ;;
    rm) ;;
    image)
      case "$2" in
        ls) cat "$T/image_ls_$(basename "$3")" 2>/dev/null || true ;;
        rm) echo "$3" >>"$T/removed" ;;
        prune) ;;
      esac
      ;;
    compose)
      shift
      local sub=""
      while [[ $# -gt 0 ]]; do
        case "$1" in
          -p | --project-directory | --env-file | -f) shift 2 ;;
          *) sub=$1; shift; break ;;
        esac
      done
      case "$sub" in
        config)
          if grep -qx "${API_TAG:-}" "$T/fail_config" 2>/dev/null; then return 1; fi
          ;;
        up)
          local cur
          cur=$(grep -E '^API_TAG=' "$APP/deploy.env" | cut -d= -f2)
          echo "up API_TAG=$cur" >>"$CALLS"
          if grep -qx "$cur" "$T/bad_up" 2>/dev/null; then return 1; fi
          ;;
        down) echo "down" >>"$CALLS" ;;
        ps) echo "NAME SERVICE STATUS" ;;
      esac
      ;;
  esac
  return 0
}

# ---------- 시험 폴더 ----------
setup() {
  T=$(new_tmp)
  APP="$T/app"
  LIB="$T/lib"
  BUNDLES="$T/bundles"
  CALLS="$T/calls.log"
  mkdir -p "$APP" "$LIB" "$BUNDLES"
  : >"$CALLS"
  cp "$DEPLOY_DIR/check-env.sh" "$LIB/check-env.sh"
  cat >"$LIB/smoke.sh" <<'SH'
#!/usr/bin/env bash
ver=""
args="$*"
while [[ $# -gt 0 ]]; do case "$1" in --version) ver=$2; shift 2 ;; *) shift ;; esac; done
echo "smoke version=$ver args=[$args]" >>"$CALLS_FILE"
if [[ -f $FAIL_SMOKE_FILE ]] && grep -qx "$ver" "$FAIL_SMOKE_FILE"; then echo "FAIL smoke $ver"; exit 1; fi
echo "PASS smoke $ver"
SH
  chmod +x "$LIB/smoke.sh" "$LIB/check-env.sh"
  export CALLS_FILE="$CALLS" FAIL_SMOKE_FILE="$T/fail_smoke"
  make_env_set "$APP" "$EXAMPLE"
  : >"$APP/versions.env"
}

make_bundle() { # make_bundle 태그 [ML_TAG]
  local tag=$1 ml=${2:-none}
  mkdir -p "$BUNDLES/$tag"
  echo "compose-of-$tag" >"$BUNDLES/$tag/compose.yml"
  echo "ML_TAG=$ml" >"$BUNDLES/$tag/versions.env"
  cp "$EXAMPLE" "$BUNDLES/$tag/env.example"
}

# deploy.sh 를 서브셸에서 돌림 → OUT(출력), RC(종료 코드)
run_main() {
  RC=0
  OUT=$( (
    # shellcheck source=../deploy.sh
    source "$DEPLOY_SH"
    APP_DIR=$APP
    LIB_DIR=$LIB
    main "$@"
  ) 2>&1) || RC=$?
}
env_of() { grep -E "^$1=" "$APP/deploy.env" | tail -1 | cut -d= -f2-; }
calls() { cat "$CALLS"; }

# ============================================================ 1 입력 검사
echo "[1] 입력 검사: 정해진 모양만 통과, 나머지는 종료 코드 2"
setup
parse_rc() { # parse_rc "명령"
  local rc=0
  ( source "$DEPLOY_SH"; APP_DIR=$APP; parse_command "$1" ) >/dev/null 2>&1 || rc=$?
  printf '%s' "$rc"
}
H40=0123456789abcdef0123456789abcdef01234567
for c in "deploy sha-abc1234" "deploy sha-$H40" "rollback" "status" "deploy  sha-abc1234"; do
  assert_eq "허용: $c" 0 "$(parse_rc "$c")"
done
NL=$'deploy sha-abc1234\nid'
for c in "" "deploy" "deploy latest" "deploy sha-ABC1234" "deploy sha-abc123" "deploy sha-${H40}0" \
  "deploy sha-abc1234 extra" "deploy sha-abc1234;id" 'deploy sha-abc1234 && id' 'deploy $(id)' \
  "$NL" "rollback now" "status -v" "bash" "rm -rf /" "deploy ../../x" "DEPLOY sha-abc1234" \
  "deploy sha-abc1234|cat" "deploy sha-abc1234 > /tmp/x" "deploy 'sha-abc1234'"; do
  assert_eq "거부: ${c//$'\n'/\\n}" 2 "$(parse_rc "$c")"
done

# ============================================================ 2 첫 배포 성공
echo "[2] 첫 배포 성공"
setup
make_bundle sha-aaaaaaa
run_main deploy sha-aaaaaaa
assert_rc "종료 코드 0" 0 "$RC"
assert_eq "API_TAG" sha-aaaaaaa "$(env_of API_TAG)"
assert_eq "LAST_GOOD_API_TAG" sha-aaaaaaa "$(env_of LAST_GOOD_API_TAG)"
assert_eq "compose.yml 이 번들 것" "compose-of-sha-aaaaaaa" "$(cat "$APP/compose.yml")"
assert_eq "compose.prev.yml 도 같음" "compose-of-sha-aaaaaaa" "$(cat "$APP/compose.prev.yml")"
assert_eq "env.example 설치됨" 0 "$(cmp -s "$APP/env.example" "$EXAMPLE" && echo 0 || echo 1)"
assert_contains "번들 이미지를 받음" "$(calls)" "docker pull --quiet $REG/respec-deploy:sha-aaaaaaa"
assert_contains "컨테이너를 올림" "$(calls)" "up API_TAG=sha-aaaaaaa"
assert_contains "배포 직후 점검을 함" "$(calls)" "smoke version=sha-aaaaaaa"
assert_contains "성공 문구" "$OUT" "배포 성공: sha-aaaaaaa"
assert_not_contains "출력에 비밀 값 없음" "$OUT" "$SECRET_MARKER"
assert_contains "점검 인자: smoke.env 사용" "$(calls)" "--env-file $APP/smoke.env"
assert_contains "deploy.log 기록" "$(cat "$APP/deploy.log")" "배포 성공: sha-aaaaaaa"
perm_ok=1
[[ $(stat -c %a "$APP/deploy.env") == 600 ]] || perm_ok=0
assert_eq "deploy.env 권한 600 유지" 1 "$perm_ok"

# ============================================================ 3 두 번째 배포 성공
echo "[3] 두 번째 배포 성공: 마지막 정상 · prev 파일이 갱신됨"
make_bundle sha-bbbbbbb
run_main deploy sha-bbbbbbb
assert_rc "종료 코드 0" 0 "$RC"
assert_eq "API_TAG" sha-bbbbbbb "$(env_of API_TAG)"
assert_eq "LAST_GOOD_API_TAG" sha-bbbbbbb "$(env_of LAST_GOOD_API_TAG)"
assert_eq "compose.prev.yml 갱신" "compose-of-sha-bbbbbbb" "$(cat "$APP/compose.prev.yml")"

# ============================================================ 4 올리기 실패 → 자동 되돌림
echo "[4] 새 버전이 healthy 가 안 되면(up 실패) 직전 정상 버전으로 자동 복귀"
setup
make_bundle sha-aaaaaaa
make_bundle sha-bbbbbbb
run_main deploy sha-aaaaaaa
echo sha-bbbbbbb >"$T/bad_up"
: >"$CALLS"
run_main deploy sha-bbbbbbb
assert_rc "종료 코드 1" 1 "$RC"
assert_eq "API_TAG 가 직전 정상으로" sha-aaaaaaa "$(env_of API_TAG)"
assert_eq "LAST_GOOD 는 그대로" sha-aaaaaaa "$(env_of LAST_GOOD_API_TAG)"
assert_eq "compose.yml 도 직전 것" "compose-of-sha-aaaaaaa" "$(cat "$APP/compose.yml")"
ups=$(grep '^up API_TAG=' "$CALLS" | tr '\n' ' ')
assert_eq "up 순서: 새 버전 시도 → 직전 버전" "up API_TAG=sha-bbbbbbb up API_TAG=sha-aaaaaaa " "$ups"
assert_contains "되돌린 버전도 점검함" "$(calls)" "smoke version=sha-aaaaaaa"
assert_contains "실패 문구" "$OUT" "배포 실패: sha-bbbbbbb"
assert_contains "되돌림 완료 문구" "$OUT" "되돌림 완료: 지금 sha-aaaaaaa"

# ============================================================ 5 배포 직후 점검 실패 → 자동 되돌림
echo "[5] 컨테이너는 뜨지만 배포 직후 점검이 실패하면 직전 버전으로 복귀"
setup
make_bundle sha-aaaaaaa
make_bundle sha-bbbbbbb
run_main deploy sha-aaaaaaa
echo sha-bbbbbbb >"$T/fail_smoke"
: >"$CALLS"
run_main deploy sha-bbbbbbb
assert_rc "종료 코드 1" 1 "$RC"
assert_eq "API_TAG 가 직전 정상으로" sha-aaaaaaa "$(env_of API_TAG)"
assert_eq "LAST_GOOD 는 그대로" sha-aaaaaaa "$(env_of LAST_GOOD_API_TAG)"
sm=$(grep '^smoke version=' "$CALLS" | sed 's/ args=.*//' | tr '\n' ' ')
assert_eq "점검 순서: 새 버전 → 직전 버전" "smoke version=sha-bbbbbbb smoke version=sha-aaaaaaa " "$sm"
assert_contains "점검 실패 문구" "$OUT" "배포 직후 점검 실패"

# ============================================================ 6 첫 배포 실패
echo "[6] 첫 배포가 실패하면 되돌릴 버전이 없어 컨테이너를 내림"
setup
make_bundle sha-aaaaaaa
echo sha-aaaaaaa >"$T/bad_up"
run_main deploy sha-aaaaaaa
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "컨테이너를 내림" "$(calls)" "down"
assert_eq "LAST_GOOD 없음" "" "$(env_of LAST_GOOD_API_TAG)"
assert_contains "안내 문구" "$OUT" "직전 정상 버전이 없어(첫 배포) 컨테이너를 내림"

# ============================================================ 7 점검 단계에서 막힘 (돌고 있는 것을 건드리지 않음)
echo "[7] env 이름이 안 맞으면 교체 전에 중단 — 돌고 있는 버전은 그대로"
setup
make_bundle sha-aaaaaaa
make_bundle sha-bbbbbbb
run_main deploy sha-aaaaaaa
sed -i '/^SUPABASE_URL=/d' "$APP/api.env"
: >"$CALLS"
run_main deploy sha-bbbbbbb
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "MISSING 이 보임" "$OUT" "MISSING SUPABASE_URL"
assert_not_contains "교체(up)를 안 함" "$(calls)" "up API_TAG"
assert_eq "API_TAG 그대로" sha-aaaaaaa "$(env_of API_TAG)"
assert_eq "compose.yml 그대로" "compose-of-sha-aaaaaaa" "$(cat "$APP/compose.yml")"
assert_not_contains "출력에 비밀 값 없음" "$OUT" "$SECRET_MARKER"

echo "[7b] compose 문법 검사 실패도 교체 전에 중단"
setup
make_bundle sha-aaaaaaa
echo sha-aaaaaaa >"$T/fail_config"
run_main deploy sha-aaaaaaa
assert_rc "종료 코드 1" 1 "$RC"
assert_not_contains "교체(up)를 안 함" "$(calls)" "up API_TAG"
assert_eq "API_TAG 안 바뀜" "" "$(env_of API_TAG)"
assert_contains "문구" "$OUT" "compose 파일 문법"

echo "[7c] 번들 이미지를 못 받으면 중단"
setup
make_bundle sha-aaaaaaa
echo sha-aaaaaaa >"$T/fail_pull"
run_main deploy sha-aaaaaaa
assert_rc "종료 코드 1" 1 "$RC"
assert_not_contains "교체(up)를 안 함" "$(calls)" "up API_TAG"
assert_contains "문구" "$OUT" "respec-deploy:sha-aaaaaaa 를 받지 못함"

echo "[7d] 번들에 versions.env 의 ML_TAG 가 없으면 중단"
setup
make_bundle sha-aaaaaaa
echo "NOPE=1" >"$BUNDLES/sha-aaaaaaa/versions.env"
run_main deploy sha-aaaaaaa
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "문구" "$OUT" "ML_TAG 가 없음"

# ============================================================ 8 예측 서버 프로필
echo "[8] COMPOSE_PROFILES=ml 인데 ML_TAG=none 이면 중단, 태그가 있으면 점검에 --ml 이 붙음"
setup
make_bundle sha-aaaaaaa none
sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=ml/' "$APP/deploy.env"
run_main deploy sha-aaaaaaa
assert_rc "ML_TAG=none → 종료 코드 1" 1 "$RC"
assert_contains "문구" "$OUT" "ML_TAG 가 none"
make_bundle sha-ccccccc sha-1234567
run_main deploy sha-ccccccc
assert_rc "ML_TAG 있음 → 종료 코드 0" 0 "$RC"
assert_contains "점검에 --ml" "$(calls)" "--ml http://127.0.0.1:8091"

# ============================================================ 9 수동 되돌리기
echo "[9] rollback: 마지막 정상 버전 파일로 복원 · 정상 버전이 없으면 거부"
setup
run_main rollback
assert_rc "정상 버전 없음 → 종료 코드 1" 1 "$RC"
assert_contains "문구" "$OUT" "되돌릴 정상 버전이 없음"
make_bundle sha-aaaaaaa
make_bundle sha-bbbbbbb
run_main deploy sha-aaaaaaa
run_main deploy sha-bbbbbbb
# 사고 흉내: 운영 중 값이 어긋남
echo "garbage" >"$APP/compose.yml"
sed -i 's/^API_TAG=.*/API_TAG=sha-ccccccc/' "$APP/deploy.env"
: >"$CALLS"
run_main rollback
assert_rc "종료 코드 0" 0 "$RC"
assert_eq "API_TAG 복원" sha-bbbbbbb "$(env_of API_TAG)"
assert_eq "compose.yml 복원" "compose-of-sha-bbbbbbb" "$(cat "$APP/compose.yml")"
assert_contains "올림" "$(calls)" "up API_TAG=sha-bbbbbbb"

# ============================================================ 10 status
echo "[10] status: 태그 · 프로필만 보이고 비밀 값은 안 보임"
run_main status
assert_rc "종료 코드 0" 0 "$RC"
assert_contains "API_TAG 줄" "$OUT" "API_TAG=sha-bbbbbbb"
assert_contains "LAST_GOOD 줄" "$OUT" "LAST_GOOD_API_TAG=sha-bbbbbbb"
assert_not_contains "비밀 값 없음" "$OUT" "$SECRET_MARKER"

# ============================================================ 11 이미지 정리
echo "[11] 이미지 정리: 저장소별 최근 3개 + 쓰는 태그 · 마지막 정상 태그는 남김"
setup
make_bundle sha-aaaaaaa
cat >"$T/image_ls_respec-api" <<'LS'
2026-11-14 10:05:00 +0000 UTC|sha-eeeeeee
2026-11-14 10:04:00 +0000 UTC|sha-ddddddd
2026-11-14 10:03:00 +0000 UTC|sha-ccccccc
2026-11-14 10:02:00 +0000 UTC|sha-bbbbbbb
2026-11-14 10:01:00 +0000 UTC|sha-aaaaaaa
2026-11-14 10:00:00 +0000 UTC|<none>
LS
run_main deploy sha-aaaaaaa
assert_rc "종료 코드 0" 0 "$RC"
removed=$(tr '\n' ' ' <"$T/removed" 2>/dev/null || true)
assert_eq "오래된 것만 지움 (쓰는 태그 aaaaaaa 는 남김)" "$REG/respec-api:sha-bbbbbbb " "$removed"

# ============================================================ 12 기타
echo "[12] env_set 은 허용하지 않는 값을 거부"
setup
rc=0
( source "$DEPLOY_SH"; APP_DIR=$APP; env_set API_TAG 'sha-a; rm -rf /' ) >/dev/null 2>&1 || rc=$?
assert_eq "거부(종료 코드 1)" 1 "$rc"
assert_eq "파일 안 바뀜" "" "$(env_of API_TAG)"

echo "[13] SSH_ORIGINAL_COMMAND 가 있으면 인자보다 우선"
setup
RC=0
OUT=$( (
  source "$DEPLOY_SH"
  APP_DIR=$APP
  LIB_DIR=$LIB
  SSH_ORIGINAL_COMMAND="status" main deploy sha-aaaaaaa
) 2>&1) || RC=$?
assert_rc "종료 코드 0" 0 "$RC"
assert_contains "status 가 실행됨" "$OUT" "LAST_GOOD_API_TAG="
assert_not_contains "deploy 는 안 돎" "$(calls)" "docker pull"

echo "[14] deploy.env 가 없으면 bootstrap 안내와 함께 종료"
setup
rm "$APP/deploy.env"
run_main status
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "안내" "$OUT" "bootstrap.sh 를 먼저 돌려야 함"

finish

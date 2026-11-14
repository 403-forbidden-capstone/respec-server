#!/usr/bin/env bash
# shellcheck disable=SC2034,SC2329
# 시험 공통 도구. 각 test_*.sh 가 source 해서 씀.
# 시험은 운영 스크립트를 그대로 실행하고(docker · stat 만 가짜로 바꿈), 임시 폴더에서만 파일을 만듦.

TESTS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEPLOY_DIR=$(cd "$TESTS_DIR/.." && pwd)
REPO_DIR=$(cd "$DEPLOY_DIR/.." && pwd)
SUITE=${SUITE:-시험}
PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL %s\n' "$1"; }

assert_eq() { # 이름 기대 실제
  if [[ $2 == "$3" ]]; then ok "$1"; else bad "$1 (기대 '$2', 실제 '$3')"; fi
}
assert_contains() { # 이름 전체글 찾는글
  if [[ $2 == *"$3"* ]]; then ok "$1"; else bad "$1 ('$3' 가 없음)"; fi
}
assert_not_contains() {
  if [[ $2 != *"$3"* ]]; then ok "$1"; else bad "$1 ('$3' 가 나오면 안 됨)"; fi
}
assert_rc() { # 이름 기대코드 실제코드
  if [[ $2 == "$3" ]]; then ok "$1"; else bad "$1 (종료 코드 기대 $2, 실제 $3)"; fi
}

# 임시 폴더: 시험마다 루트 폴더 하나 아래에 만들고, 끝나면 루트째 지움
ROOT_TMP=$(mktemp -d)
new_tmp() { mktemp -d -p "$ROOT_TMP"; }
cleanup_all() { [[ -n ${ROOT_TMP:-} ]] && rm -rf "$ROOT_TMP"; return 0; }
trap cleanup_all EXIT

# 윈도우 네이티브 프로그램(python)에 넘길 경로로 바꿈 (/tmp/x → C:/Users/.../x). 리눅스에선 그대로
native_path() { cygpath -m "$1" 2>/dev/null || printf '%s' "$1"; }

# 파이썬 실행 파일 찾기 (윈도우 Git Bash 에는 python, 리눅스에는 python3)
find_python() {
  local c
  for c in python3 python; do
    if "$c" -c 'import sys' >/dev/null 2>&1; then printf '%s' "$c"; return 0; fi
  done
  return 1
}

# 파일 권한은 윈도우 PC 에서 chmod 가 안 먹으므로 stat 을 가짜로 바꿔 끼움 (자식 bash 에도 전달됨)
fake_stat() {
  # 권한 조회(stat -c %a)만 가짜로, 나머지(크기 등)는 진짜 stat
  # shellcheck disable=SC2317  # export -f 로 자식 bash 가 부르는 함수라 여기서는 안 쓰는 것으로 보임
  stat() { if [[ ${1-} == -c && ${2-} == %a ]]; then printf '%s\n' "${FAKE_STAT_MODE:-600}"; else command stat "$@"; fi; }
  export -f stat
}

# .env.example 의 모든 필수 이름에 비밀 표시 값을 넣은 env 파일 4개를 폴더에 만듦 (선택 이름은 빈 값)
SECRET_MARKER="SECRET_MARKER_zq81"
make_env_set() { # make_env_set 폴더 예시파일
  local d=$1 example=$2 f n opt
  for f in api.env ml.env deploy.env smoke.env; do
    : >"$d/$f"
    while IFS=$'\t' read -r n opt; do
      [[ -z $n ]] && continue
      if [[ $opt == 0 ]]; then echo "$n=$SECRET_MARKER-$n" >>"$d/$f"; else echo "$n=" >>"$d/$f"; fi
    done < <(awk -v want="$f" '
      /^# === end ===/ { cur = ""; opt = 0; next }
      /^# === .* ===[[:space:]]*$/ { name = $0; sub(/^# === /, "", name); sub(/ ===[[:space:]]*$/, "", name); cur = name; opt = 0; next }
      cur != want { next }
      /^[[:space:]]*$/ { opt = 0; next }
      /^# optional[[:space:]]*$/ { opt = 1; next }
      /^[A-Za-z_][A-Za-z0-9_]*=/ { n = $0; sub(/=.*/, "", n); print n "\t" opt; opt = 0 }
    ' "$example")
  done
}

finish() {
  printf '%s: 통과 %d, 실패 %d\n' "$SUITE" "$PASS" "$FAIL"
  [[ $FAIL -eq 0 ]]
}

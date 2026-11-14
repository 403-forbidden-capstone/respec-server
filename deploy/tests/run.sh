#!/usr/bin/env bash
# deploy/ 스크립트 · 설정 시험 전체. 로컬(윈도우 Git Bash · 리눅스)과 CI(deploy-check.yml)에서 같은 명령으로 돎.
#   bash deploy/tests/run.sh
# 모든 시험은 임시 폴더에서만 파일을 만들고, 실서버 · 네트워크(가짜 서버 제외)에는 닿지 않음.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

FAILED=()

run_suite() { # run_suite 이름 명령...
  local name=$1
  shift
  printf '\n######## %s\n' "$name"
  if ! "$@"; then FAILED+=("$name"); fi
}

lint() {
  local rc=0 f
  for f in ../*.sh ./*.sh; do
    if ! bash -n "$f"; then echo "문법 오류: $f"; rc=1; fi
  done
  if command -v shellcheck >/dev/null 2>&1; then
    echo "shellcheck $(shellcheck --version | sed -n 's/^version: //p')"
    shellcheck -x --severity=style ../*.sh ./*.sh || rc=1
  else
    echo "shellcheck 가 없어 정적 분석을 건너뜀 (CI 에서는 돎)"
  fi
  # 서버(리눅스)에서 도는 파일에 CR(윈도우 줄바꿈)이 섞이면 스크립트가 깨짐
  for f in ../*.sh ./*.sh ../*.yml ../nginx/*.conf ../../.env.example; do
    if [[ $(tr -cd '\r' <"$f" | wc -c) -gt 0 ]]; then echo "CRLF 줄바꿈이 섞임: $f"; rc=1; fi
  done
  return $rc
}

run_suite "lint (bash -n · shellcheck · 줄바꿈)" lint
run_suite "check-env" bash ./test_check_env.sh
run_suite "smoke" bash ./test_smoke.sh
run_suite "deploy" bash ./test_deploy.sh
run_suite "compose" bash ./test_compose.sh
run_suite "nginx" bash ./test_nginx.sh

printf '\n========================================\n'
if [[ ${#FAILED[@]} -gt 0 ]]; then
  printf '실패한 묶음: %s\n' "${FAILED[*]}"
  exit 1
fi
echo "전부 통과"

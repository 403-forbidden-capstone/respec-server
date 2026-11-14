#!/usr/bin/env bash
# 서버 env 파일(api.env · ml.env · deploy.env · smoke.env)의 "이름"이 .env.example 과 맞는지 확인함.
# 값은 어떤 경우에도 출력하지 않음 (이름과 비어 있는지만 봄).
#
#   check-env.sh [--dir /srv/respec] [--example 파일] [--strict] [--quiet]
#   check-env.sh --print-skeleton api.env [--example 파일]      이름만 있는 빈 틀을 출력 (선택 이름은 "# 이름=" 주석)
#
# 판정
#   실패: 파일 없음 · 권한이 600 이 아님 · 필수 이름이 없음(MISSING) · 필수 값이 비어 있음(EMPTY)
#   --strict 에서 추가 실패: 선택 이름이 아예 없음(ABSENT) · 예시에 없는 이름이 있음(EXTRA)
set -Eeuo pipefail

DIR=/srv/respec
EXAMPLE=/srv/respec/env.example
STRICT=0
QUIET=0
SKELETON=""
FILES=(api.env ml.env deploy.env smoke.env)

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR=${2:?--dir 값 필요}; shift 2 ;;
    --example) EXAMPLE=${2:?--example 값 필요}; shift 2 ;;
    --strict) STRICT=1; shift ;;
    --quiet) QUIET=1; shift ;;
    --print-skeleton) SKELETON=${2:?--print-skeleton 값 필요}; shift 2 ;;
    -h | --help) usage ;;
    *) echo "알 수 없는 옵션: $1" >&2; usage ;;
  esac
done

[[ -f $EXAMPLE ]] || { echo "예시 파일 없음: $EXAMPLE" >&2; exit 2; }

say() { [[ $QUIET == 1 ]] || printf '%s\n' "$*"; }

# 예시 파일에서 한 섹션의 "이름<TAB>선택여부(0/1)" 목록을 뽑음
example_names() {
  awk -v want="$1" '
    /^# === end ===/ { cur = ""; opt = 0; next }
    /^# === .* ===[[:space:]]*$/ {
      name = $0; sub(/^# === /, "", name); sub(/ ===[[:space:]]*$/, "", name)
      cur = name; opt = 0; next
    }
    cur != want { next }
    /^[[:space:]]*$/ { opt = 0; next }
    /^# optional[[:space:]]*$/ { opt = 1; next }
    /^[A-Za-z_][A-Za-z0-9_]*=/ { n = $0; sub(/=.*/, "", n); print n "\t" opt; opt = 0 }
  ' "$EXAMPLE"
}

# 실제 파일에 이름이 있는지 / 값이 비었는지 (값은 출력하지 않음)
has_name() { grep -qE "^$1=" "$2"; }
value_empty() {
  local line v
  line=$(grep -E "^$1=" "$2" | tail -1) || true
  v=${line#*=}
  v=${v//[[:space:]]/}
  v=${v//\"/}
  v=${v//\'/}
  [[ -z $v ]]
}

if [[ -n $SKELETON ]]; then
  # 필수 이름은 "이름=", 선택 이름은 "# 이름="(주석)으로 출력함 — 빈 값 "이름=" 이 컨테이너에 빈 문자열로 들어가
  # 앱의 설정 읽기(숫자 · 주소 형식)가 깨지는 것을 막음. 선택 값이 필요해지면 # 를 지우고 값을 넣음
  while IFS=$'\t' read -r name opt; do
    if [[ $opt == 1 ]]; then printf '# %s=\n' "$name"; else printf '%s=\n' "$name"; fi
  done < <(example_names "$SKELETON")
  exit 0
fi

status=0
for f in "${FILES[@]}"; do
  path="$DIR/$f"
  if [[ ! -f $path ]]; then
    say "FAIL  $f: 파일 없음"
    status=1
    continue
  fi
  problems=0

  mode=$(stat -c %a "$path")
  if [[ $mode != 600 ]]; then
    say "FAIL  $f: 권한 $mode (600 이어야 함)"
    problems=1
  fi

  names=$(example_names "$f")
  if [[ -z $names ]]; then
    say "FAIL  $f: 예시 파일에 이 파일의 이름 목록이 없음"
    status=1
    continue
  fi

  expected=()
  while IFS=$'\t' read -r name opt; do
    expected+=("$name")
    if ! has_name "$name" "$path"; then
      if [[ $opt == 0 ]]; then
        say "FAIL  $f: MISSING $name"
        problems=1
      elif [[ $STRICT == 1 ]]; then
        say "FAIL  $f: ABSENT  $name (선택 이름이 없음)"
        problems=1
      fi
    elif [[ $opt == 0 ]] && value_empty "$name" "$path"; then
      say "FAIL  $f: EMPTY   $name (값이 비어 있음)"
      problems=1
    fi
  done <<<"$names"

  while IFS= read -r have; do
    found=0
    for e in "${expected[@]}"; do [[ $e == "$have" ]] && found=1 && break; done
    if [[ $found == 0 ]]; then
      if [[ $STRICT == 1 ]]; then
        say "FAIL  $f: EXTRA   $have (예시에 없는 이름)"
        problems=1
      else
        say "WARN  $f: EXTRA   $have (예시에 없는 이름)"
      fi
    fi
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$path" | sed 's/=.*//' | sort -u)

  if [[ $problems == 0 ]]; then
    say "OK    $f (${#expected[@]}개 이름)"
  else
    status=1
  fi
done

exit "$status"

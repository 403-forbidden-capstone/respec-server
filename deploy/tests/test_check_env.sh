#!/usr/bin/env bash
# shellcheck disable=SC2034
# check-env.sh 시험: 이름 비교 · 빈 값 · 권한 · 선택 이름 · 값이 출력에 새지 않는지
set -Eeuo pipefail
SUITE="check-env"
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
fake_stat

CHECK="$DEPLOY_DIR/check-env.sh"
EXAMPLE="$REPO_DIR/.env.example"
SECRET="SECRET_MARKER_zq81"

# 예시의 모든 필수 이름에 값을 넣은 env 파일 세트를 만듦
make_set() { # make_set 폴더
  local d=$1 f n opt
  for f in api.env ml.env deploy.env smoke.env; do
    : >"$d/$f"
    while IFS=$'\t' read -r n opt; do
      [[ -z $n ]] && continue
      # 필수는 값 있음, 선택은 비움
      if [[ $opt == 0 ]]; then echo "$n=$SECRET-$n" >>"$d/$f"; else echo "$n=" >>"$d/$f"; fi
    done < <(awk -v want="$f" '
      /^# === end ===/ { cur = ""; opt = 0; next }
      /^# === .* ===[[:space:]]*$/ { name = $0; sub(/^# === /, "", name); sub(/ ===[[:space:]]*$/, "", name); cur = name; opt = 0; next }
      cur != want { next }
      /^[[:space:]]*$/ { opt = 0; next }
      /^# optional[[:space:]]*$/ { opt = 1; next }
      /^[A-Za-z_][A-Za-z0-9_]*=/ { n = $0; sub(/=.*/, "", n); print n "\t" opt; opt = 0 }
    ' "$EXAMPLE")
  done
}

run() { # run 폴더 [옵션...] → OUT, RC
  local d=$1
  shift
  RC=0
  OUT=$("$CHECK" --dir "$d" --example "$EXAMPLE" "$@" 2>&1) || RC=$?
}

echo "[1] 정상: 필수 값이 다 있으면 통과"
D=$(new_tmp)
make_set "$D"
run "$D"
assert_rc "정상 종료 코드" 0 "$RC"
assert_contains "api.env OK" "$OUT" "OK    api.env"
assert_contains "ml.env OK" "$OUT" "OK    ml.env"
assert_contains "deploy.env OK" "$OUT" "OK    deploy.env"
assert_contains "smoke.env OK" "$OUT" "OK    smoke.env"
assert_not_contains "출력에 값이 없음" "$OUT" "$SECRET"

echo "[2] 필수 이름이 없으면 실패"
D=$(new_tmp)
make_set "$D"
sed -i '/^SUPABASE_URL=/d' "$D/api.env"
run "$D"
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "MISSING 표시" "$OUT" "MISSING SUPABASE_URL"
assert_not_contains "출력에 값이 없음" "$OUT" "$SECRET"

echo "[3] 필수 값이 비어 있으면 실패 (따옴표 · 공백만 있어도 빈 값)"
D=$(new_tmp)
make_set "$D"
sed -i 's/^KAKAO_REST_API_KEY=.*/KAKAO_REST_API_KEY=" "/' "$D/api.env"
run "$D"
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "EMPTY 표시" "$OUT" "EMPTY   KAKAO_REST_API_KEY"

echo "[4] 선택 이름: 값이 비어도 통과, 아예 없으면 --strict 에서만 실패"
D=$(new_tmp)
make_set "$D"
sed -i '/^EXPO_ACCESS_TOKEN=/d' "$D/api.env"
run "$D"
assert_rc "기본은 통과" 0 "$RC"
run "$D" --strict
assert_rc "strict 는 실패" 1 "$RC"
assert_contains "ABSENT 표시" "$OUT" "ABSENT  EXPO_ACCESS_TOKEN"

echo "[5] 예시에 없는 이름: 기본은 경고, --strict 는 실패"
D=$(new_tmp)
make_set "$D"
echo "OLD_LEFTOVER=$SECRET" >>"$D/api.env"
run "$D"
assert_rc "기본은 통과" 0 "$RC"
assert_contains "WARN 표시" "$OUT" "WARN  api.env: EXTRA   OLD_LEFTOVER"
run "$D" --strict
assert_rc "strict 는 실패" 1 "$RC"
assert_not_contains "출력에 값이 없음" "$OUT" "$SECRET"

echo "[6] 권한이 600 이 아니면 실패"
D=$(new_tmp)
make_set "$D"
FAKE_STAT_MODE=644 run "$D"
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "권한 표시" "$OUT" "권한 644"

echo "[7] 파일이 없으면 실패"
D=$(new_tmp)
make_set "$D"
rm "$D/ml.env"
run "$D"
assert_rc "종료 코드 1" 1 "$RC"
assert_contains "파일 없음 표시" "$OUT" "ml.env: 파일 없음"

echo "[8] --print-skeleton: 이름만 있는 빈 틀"
RC=0
OUT=$("$CHECK" --print-skeleton ml.env --example "$EXAMPLE") || RC=$?
assert_rc "종료 코드 0" 0 "$RC"
assert_eq "ml.env 틀" "CATBOOST_API_TOKEN=" "$OUT"
OUT=$("$CHECK" --print-skeleton api.env --example "$EXAMPLE")
assert_contains "api.env 틀에 SUPABASE_SECRET_KEY" "$OUT" "SUPABASE_SECRET_KEY="
assert_not_contains "틀에는 값이 없음" "$OUT" "SUPABASE_URL=h"

echo "[9] 예시 파일이 없으면 종료 코드 2"
RC=0
"$CHECK" --dir "$D" --example /nonexistent >/dev/null 2>&1 || RC=$?
assert_rc "종료 코드 2" 2 "$RC"

finish

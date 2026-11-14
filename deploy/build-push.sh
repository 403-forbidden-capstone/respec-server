#!/usr/bin/env bash
# Actions 없이 지형님 PC 에서 이미지를 올리고 서버에 배포하는 길 (Actions 분이 끝났거나 멈췄을 때).
# 서버가 arm64 라서 PC(x86)에서는 에뮬레이션으로 빌드함 — 몇 분~십수 분 걸림.
#
#   deploy/build-push.sh [--no-deploy]
#
# 준비
#   - Docker Desktop 실행 + buildx
#   - docker login ghcr.io  (GitHub 개인 토큰 classic, write:packages)
#   - 환경 변수 EC2_HOST (서버 주소). 선택: EC2_USER(기본 deploy), EC2_SSH_KEY_FILE (ssh -i 로 쓸 열쇠 파일)
# 태그는 현재 커밋(sha-<7자리>)으로 고정됨 — 이미지와 커밋이 1:1 이 되도록 작업 트리가 깨끗해야 함.
set -Eeuo pipefail

OWNER=ghcr.io/403-forbidden-capstone
SOURCE=https://github.com/403-forbidden-capstone/respec-server
DEPLOY=1
[[ ${1-} == --no-deploy ]] && DEPLOY=0

cd "$(git rev-parse --show-toplevel)"

[[ -z $(git status --porcelain) ]] || { echo "작업 트리에 커밋 안 한 변경이 있음. 커밋하거나 되돌린 뒤 다시 실행" >&2; exit 1; }
[[ -f api/Dockerfile ]] || { echo "api/Dockerfile 이 없음" >&2; exit 1; }

TAG="sha-$(git rev-parse --short=7 HEAD)"
echo "태그: $TAG"

echo "== api 이미지 (arm64) 빌드 · 업로드"
docker buildx build --platform linux/arm64 -f api/Dockerfile \
  --label "org.opencontainers.image.source=$SOURCE" --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
  -t "$OWNER/respec-api:$TAG" --push api

echo "== 배포 번들 이미지 빌드 · 업로드"
docker buildx build --platform linux/arm64 -f deploy/bundle/Dockerfile \
  --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
  -t "$OWNER/respec-deploy:$TAG" --push .

if [[ $DEPLOY == 0 ]]; then
  echo "업로드만 끝. 배포는: ssh deploy@서버 \"deploy $TAG\""
  exit 0
fi

: "${EC2_HOST:?EC2_HOST 환경 변수 필요}"
ssh_args=(-o BatchMode=yes)
[[ -n ${EC2_SSH_KEY_FILE-} ]] && ssh_args+=(-i "$EC2_SSH_KEY_FILE")
echo "== 서버 배포: ${EC2_USER:-deploy}@$EC2_HOST  deploy $TAG"
# shellcheck disable=SC2029  # 의도: 태그를 이 PC 에서 펼쳐서 서버의 강제 명령(deploy.sh)에 넘김
ssh "${ssh_args[@]}" "${EC2_USER:-deploy}@$EC2_HOST" "deploy $TAG"

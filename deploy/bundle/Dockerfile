# 배포 번들 이미지: 실행하지 않는 상자. compose.yml · versions.env · env.example 세 파일만 담음.
#
# 왜 이미지로 나르나: 배포 열쇠(GitHub Secrets)는 브랜치를 올릴 수 있는 사람이면 누구나 워크플로로 꺼낼 수 있음.
# compose 파일을 SSH 로 직접 보내게 하면 그 열쇠로 아무 컨테이너(호스트 마운트 등)나 띄울 수 있음 = 서버 root.
# 그래서 서버는 SSH 로 "태그"만 받고, compose 는 그 태그의 번들 이미지(GHCR, main 에서 Actions 가 올린 것)에서 꺼냄.
#
# 빌드(저장소 루트에서): docker build -f deploy/bundle/Dockerfile -t ghcr.io/403-forbidden-capstone/respec-deploy:<태그> .
FROM scratch
LABEL org.opencontainers.image.source="https://github.com/403-forbidden-capstone/respec-server"
COPY deploy/compose.yml /compose.yml
COPY deploy/versions.env /versions.env
COPY .env.example /env.example

# 서버 다시 세우기 — 새 Ubuntu 24.04 / 26.04 LTS (arm64)를 처음부터

서버를 지워도 이 문서와 `deploy/` 스크립트, `.env.example` 만으로 반나절 안에 같은 서버를 다시 세움(설계 N10).
사진(AMI)으로 복제하지 않고 **레시피(bootstrap.sh)로 처음부터** 세우는 방식 — 서버에 쌓인 기록이 안 따라오고, 무엇이 깔려 있는지 코드로 읽힘.

| 파일 | 역할 | 서버에서의 자리 |
|---|---|---|
| `deploy/bootstrap.sh` | 서버 한 번에 세팅 (두 번 돌려도 같음) | 한 번 올려서 실행 |
| `deploy/deploy.sh` | 배포 · 되돌리기 · 상태 (SSH 강제 명령) | `/usr/local/lib/respec/deploy.sh` (root 소유) |
| `deploy/smoke.sh` | 배포 직후 점검 | `/usr/local/lib/respec/smoke.sh` |
| `deploy/check-env.sh` | env 파일 이름 비교 (값 출력 안 함) | `/usr/local/lib/respec/check-env.sh` |
| `deploy/compose-policy.py` | 배포 직전에 번들 compose 가 "허용한 모양"인지 점검 (호스트 마운트 · 특권 · 포트 공개 등 거부) | `/usr/local/lib/respec/compose-policy.py` |
| `deploy/compose.yml` | 컨테이너 3개 (api · scheduler · prepare-ml) | 배포 때 번들 이미지에서 `/srv/respec/compose.yml` 로 꺼냄 |
| `deploy/versions.env` | 예측 서버 이미지 태그 고정 | 같이 `/srv/respec/versions.env` |
| `deploy/nginx/*.conf` | nginx 설정 | `/etc/nginx/...` (bootstrap 이 설치) |
| `.env.example` | env 이름 목록 | `/srv/respec/env.example` (배포 때 갱신) |

서버 폴더 `/srv/respec/`

```
compose.yml · compose.prev.yml      지금 것 / 마지막 정상 것 (배포가 관리)
versions.env · versions.prev.env    같은 짝
deploy.env                          API_TAG · LAST_GOOD_API_TAG · COMPOSE_PROFILES (배포가 씀)
api.env · ml.env · smoke.env        비밀 값 (사람이 채움, 권한 600)
env.example                         이번 버전의 이름 목록 (check-env 가 비교)
models/v4_age_only/                 예측 모델 파일 2개 (읽기 전용 마운트, 깃 · 이미지에 없음)
deploy.log                          배포 기록 (비밀 없음)
```

---

## 1. 콘솔에서 만들 것 (지형님)

단계별 화면 안내는 지형님 가이드(U5 · U8). 사양만 적음.

| 항목 | 값 |
|---|---|
| 인스턴스 이름 | `respec-server` |
| 이미지 | Ubuntu Server LTS 64비트(Arm) — 24.04 또는 26.04 (bootstrap 이 둘 다 받음. 지금 서버는 26.04.1 `resolute`) |
| 유형 | t4g.small · 리전 서울(ap-northeast-2) |
| 스토리지 | gp3 20GiB |
| 크레딧 사양 | standard (무제한 모드 끔) |
| 키 페어 | `respec-server-key` (RSA .pem, PC `C:\Users\user\.ssh\`) |
| 보안 그룹 | `respec-server-sg`: 인바운드 22 · 80 · 443 (소스 0.0.0.0/0) |
| 탄력적 IP | 새로 할당해서 연결 |
| DNS | Cloudflare `api.respecalarm.com` A 레코드 → 탄력적 IP, **DNS only(회색 구름)** |

## 2. 서버 세팅 (bootstrap.sh)

**배포 열쇠 만들기** (PC에서 한 번): 비밀 키는 GitHub Secrets 로, 공개 키는 서버로 감.

```bash
ssh-keygen -t ed25519 -N "" -C "respec-server-deploy" -f ~/.ssh/respec-deploy-key
```

**올리고 실행** (서버가 생기고 지형님 OK 가 난 뒤)

```bash
ssh -i ~/.ssh/respec-server-key.pem ubuntu@<탄력적 IP> 'mkdir -p /tmp/respec-src'
scp -i ~/.ssh/respec-server-key.pem -r deploy .env.example ~/.ssh/respec-deploy-key.pub ubuntu@<탄력적 IP>:/tmp/respec-src/
ssh -i ~/.ssh/respec-server-key.pem ubuntu@<탄력적 IP> \
  'sudo env DEPLOY_PUBKEY_FILE=/tmp/respec-src/respec-deploy-key.pub bash /tmp/respec-src/deploy/bootstrap.sh'
```

OS 판 차이: Ubuntu 26.04 의 `sudo` 는 Rust 로 다시 쓴 `sudo-rs` 라서, 명령줄 환경 변수는 `sudo VAR=값 명령` 대신 `sudo env VAR=값 명령` 으로 넘김(어느 판에서도 되는 쪽).

하는 일: apt 업그레이드 · docker-ce(공식 저장소, 버전 hold) · docker 로그 크기 제한 · swap 2GB · ufw(22 · 80 · 443) · fail2ban · ssh 강화(비밀번호 · root 로그인 끔) · nginx · certbot · `deploy` 계정(강제 명령) · `/srv/respec` · env 이름 틀(값 비어 있음) · 보안 업데이트 자동(자동 재부팅 끔).
안 하는 것: 재부팅 · 컨테이너 시작 · 모델 파일 · ghcr 로그인 · 인증서 발급. 끝에 "재부팅이 필요함"이 나오면 지형님 OK 뒤에 따로 재부팅.

**알아 둘 것 (11/14 새 서버 26.04 에서 실제로 돌려 본 결과)**

- `/tmp` 는 재부팅하면 비워짐 → 재부팅 뒤에 `bootstrap.sh` 를 다시 돌릴 때는 위 `scp` 를 다시 함(서버에 스크립트가 없으면 종료 코드 127).
- 업그레이드가 끝나도 `apt list --upgradable` 에 몇 개가 남을 수 있음: 새 커널(`linux-aws`)은 `apt upgrade` 가 보류하고, 일부는 Ubuntu 단계 배포(phased)라 시간이 지나야 들어옴. 커널은 보안 자동 업데이트(`unattended-upgrades`)가 설치하지만 자동 재부팅을 껐으므로 `*** System restart required ***` 가 뜨면 조용한 시간에 지형님 OK 뒤 재부팅.
- Ubuntu 26.04 의 `/etc/nginx/nginx.conf` 는 http 수준에 `server_tokens` 를 이미 적어 둠 → 우리 설정은 server 블록마다 `server_tokens off;` (공통 설정에 두면 duplicate 로 `nginx -t` 실패).

끝났을 때 보이는 것: 맨 아래 요약 표에 docker · compose 버전, hold 목록, swap 2G, ufw active, nginx · fail2ban active.

## 3. env 값 채우기 (지형님, 값은 채팅에 안 붙임)

서버에 접속해서 `sudo nano /srv/respec/<파일>`. 이름 목록은 `.env.example`.

| 파일 | 넣을 것 | 값을 얻는 곳 |
|---|---|---|
| `api.env` | `SUPABASE_URL` · `SUPABASE_PUBLISHABLE_KEY` | Supabase → Project Settings → API |
| | `SUPABASE_SECRET_KEY` | 같은 곳 Secret keys 에서 **새로 발급**(옛 service_role 키 · Vault 는 그대로) |
| | `KAKAO_REST_API_KEY` | Edge Secrets 에 있는 값과 같은 키 |
| `ml.env` | `CATBOOST_API_TOKEN` | `openssl rand -hex 32` 로 새로 만든 값 (1-11 에서, Edge 비밀 `PREPARE_ML_API_TOKEN` 과 같은 값) |
| `smoke.env` | `SMOKE_SUPABASE_URL` · `SMOKE_SUPABASE_PUBLISHABLE_KEY` | `api.env` 와 같은 값 |
| | `SMOKE_EMAIL` · `SMOKE_PASSWORD` | Supabase 에서 만든 점검용 시험 계정(실사용자 아님) |
| `deploy.env` | 비워 둠 | `API_TAG` · `LAST_GOOD_API_TAG` 는 배포가 씀, `COMPOSE_PROFILES` 는 예측 서버를 켤 때 `ml` |

확인 (값은 안 나옴): `sudo /usr/local/lib/respec/check-env.sh` → 파일마다 `OK`.
한 줄 더: 비밀 값 사본을 서버 밖 한 곳(비밀번호 관리자 등)에 둠 — 옛 서버처럼 "유일한 사본" 금지.

## 4. ghcr 로그인 (서버가 이미지를 받으려면)

GitHub 개인 토큰(classic, `read:packages` 만, 만료일 설정)을 만들어 서버에서 한 번:

```bash
sudo -iu deploy docker login ghcr.io -u <GitHub 사용자> --password-stdin
```

(토큰을 붙여 넣고 Enter → Ctrl+D). 토큰은 `/home/deploy/.docker/config.json` 에 저장됨. 토큰 주인 · 만료일을 런북에 적음. 만료되면 배포의 번들 받기가 실패하고 메시지로 알려 줌.

## 5. HTTPS (DNS 가 서버를 가리킨 뒤)

```bash
ssh -i ~/.ssh/respec-server-key.pem ubuntu@<탄력적 IP> \
  'sudo env CERTBOT_EMAIL=<알림 받을 메일> bash /tmp/respec-src/deploy/bootstrap.sh --issue-cert --skip-upgrade'
```

- DNS 가 이 서버를 안 가리키면 발급을 안 하고 멈춤(Let's Encrypt 실패 횟수 제한 보호).
- 발급 뒤 nginx 가 HTTPS 모양으로 바뀌고 `certbot renew --dry-run` 까지 돎. 이 단계가 Let's Encrypt 이용 약관 동의(`--agree-tos`)를 포함함.

확인: `curl -i https://api.respecalarm.com/health` (api 가 뜬 뒤엔 200). 뜨기 전엔 502.

## 6. 첫 배포

1. 서버 레포 GitHub → Settings → Secrets and variables → Actions
   - Secrets: `EC2_HOST`(탄력적 IP) · `EC2_USER`(`deploy`) · `EC2_SSH_KEY`(`respec-deploy-key` 비밀 키 전체)
   - Variables: `EC2_KNOWN_HOSTS`(선택, `ssh-keyscan -t ed25519 <IP>` 출력 줄), `DEPLOY_ENABLED` = `true`
2. main 에 머지(또는 Actions → deploy → Run workflow). 빌드(arm64) → GHCR → SSH `deploy sha-<7자리>`.
3. 서버에서 확인
   - `sudo -u deploy /usr/local/lib/respec/deploy.sh status` — `API_TAG` = `LAST_GOOD_API_TAG`
   - `sudo /usr/local/lib/respec/check-env.sh --strict` — 이름 차이 0
   - `curl -s https://api.respecalarm.com/health` — `version` 이 방금 태그

Actions 가 막혔을 때: PC 에서 `EC2_HOST=<IP> deploy/build-push.sh` (arm64 에뮬레이션이라 느림, 같은 `deploy <태그>` 명령으로 끝남).

## 7. 예측 서버 켜기 (1-11)

1. 모델 파일 2개를 `/srv/respec/models/v4_age_only/` 에 올리고(경로 · 전달 방법은 인계 문서) `sha256sum -c` 가 통과하는지 확인
   - 파일 소유 root, 읽기 전용(644). 깃 · 채팅 X.
2. `ml.env` 의 `CATBOOST_API_TOKEN` 채움 (Edge 비밀 `PREPARE_ML_API_TOKEN` 과 같은 값, 같은 시간에).
3. 서버 레포 PR: `deploy/versions.env` 의 `ML_TAG` 를 본 레포가 올린 이미지 태그(`sha-…`)로 — 머지하면 번들에 실려 감.
4. 서버 `deploy.env` 에 `COMPOSE_PROFILES=ml` → 다음 배포부터 prepare-ml 이 켜지고 점검에 `/health ok=true` 가 포함됨. `ML_TAG=none` 인데 켜면 배포 전 점검이 막음.

## 8. 서버 반영이 필요한 변경

`deploy.sh` · `smoke.sh` · `check-env.sh` · `nginx/*` · `bootstrap.sh` 는 root 소유 파일이라 **배포로는 안 바뀜**(배포 열쇠로 자기 실행 파일을 못 바꾸게 한 설계). 이 파일을 바꾸는 PR 은 본문에 "서버 반영: 필요"를 적고, 머지 뒤 같은 명령으로 올려서 `bootstrap.sh --skip-upgrade` 를 다시 돌림(이미 된 것은 건너뛰고 바뀐 파일만 갱신).
`compose.yml` · `versions.env` · `.env.example` 은 배포(번들)로 자동 반영.

## 9. 되돌리기

| 상황 | 방법 |
|---|---|
| 배포 중 새 버전이 안 뜸 · 점검 실패 | 자동으로 직전 정상 버전 (Actions 가 실패로 끝남, `deploy.log` 에 이유) |
| 배포는 됐는데 나중에 문제 | `ssh deploy@서버 rollback` (또는 서버에서 `sudo -u deploy /usr/local/lib/respec/deploy.sh rollback`). `rollback` 은 "마지막으로 점검을 통과한 버전"으로 돌리므로, 바로 직전 배포가 이미 통과 버전이었다면 같은 버전을 다시 올릴 뿐임. 한 칸 전으로 가려면 이전 태그로 `deploy <이전 태그>` (이미지는 GHCR 에 남아 있음) |
| SSH 가 끊김 · Actions 취소 | 괜찮음: 배포는 SSH 와 분리된 worker 가 끝까지 돌아 점검 · 승격 · 되돌림까지 함. 결과는 `ssh deploy@서버 status` 와 서버 `/srv/respec/deploy.log` |
| 첫 배포가 실패 | 되돌릴 버전이 없어 컨테이너를 내림 → 원인 수정 후 다시 |
| env 이름이 안 맞아 배포 거부 | 위 목록(이름만)대로 서버 env 를 채우고 다시 — 돌고 있던 버전은 그대로 |
| nginx · 스크립트를 잘못 바꿈 | 레포에서 되돌린 뒤 8절대로 다시 반영 |
| 서버 자체가 이상 | 새 인스턴스에서 1 ~ 6절을 다시 (반나절) — 비밀 값 사본은 서버 밖에서 |

## 10. 개념 (웹 비유)

- **bootstrap.sh = 레시피, AMI = 완성 요리 사진.** 사진은 안에 뭐가 들었는지 몰라서 복제하면 옛 찌꺼기도 따라옴. 레시피는 읽으면 서버가 어떻게 됐는지 알고, 새 서버에 몇 번이든 같은 결과가 나옴.
- **멱등성**: `mkdir -p` 처럼 두 번 돌려도 같은 결과. 그래서 중간에 멈춰도 고치고 다시 돌리면 됨. 값이 든 파일(`*.env`, 모델, 인증서)은 이미 있으면 안 건드림.
- **SSH 강제 명령(`command=`)**: 열쇠 앞에 `command="…/deploy.sh"` 를 붙이면 그 열쇠로는 그 스크립트만 실행됨. 권한이 `deploy` 하나뿐인 API 키와 같음. 이 열쇠는 GitHub Secrets 에 있어서 브랜치를 올릴 수 있는 팀원은 워크플로로 꺼낼 수 있음 → 그래서 열쇠로 할 수 있는 일을 "태그 하나 받아서 배포"로 좁힘(`^sha-[0-9a-f]{7,40}$` 만 통과).
- **번들 이미지**: compose 파일을 SSH 로 직접 보내게 하면 그 열쇠로 아무 컨테이너(서버 디스크 마운트 등)나 띄울 수 있어 사실상 root. 그래서 compose 를 이미지에 담아 GHCR 에 올리고, 서버는 태그로 받아서 꺼낸 뒤 `compose-policy.py` 로 "허용한 모양"(서비스 3개, 포트 127.0.0.1 만, 마운트는 모델 폴더 읽기 전용뿐, 특권 옵션 없음 …)인지 점검한 다음에만 올림. npm 패키지 tarball 을 받아 쓰되 설치 전에 package.json 을 검사하는 것과 같음.
- **이 설계가 막는 것 · 못 막는 것**: 별도 절 "11. 보안 경계 · 한계" 에 정리함.
- **nginx 리버스 프록시**: 밖에서 HTTPS 로 받아 안쪽 `127.0.0.1:8000`(api) · `:8091`(예측)으로 넘기는 문지기(Vercel rewrites 의 서버판). 여는 주소를 화이트리스트로 정해서 목록에 없으면 404 — `/ml/models` · `/docs` 가 실수로 안 열림.
- **nginx 가 직접 만드는 오류(400 · 404 · 405 · 413 · 429 · 502 · 504)에도 CORS · 요청 번호 · JSON 본문**: 브라우저(웹 앱 fetch)는 CORS 헤더가 없는 응답을 막아 본문을 못 읽고 TypeError 가 나서 앱에는 NETWORK 오류로만 보임. 그래서 `Access-Control-Allow-Origin` · `Access-Control-Expose-Headers` · `X-Request-Id` 를 nginx 가 만든 오류에 붙이고, 본문도 api 와 같은 `{"error":{"code","message"}}` 로 맞춤(앱이 code 로 문구를 고름). api(FastAPI)가 만든 응답에는 api 가 이미 같은 헤더를 붙이므로 nginx 는 안 붙임 — 같은 헤더가 두 번 나가면 브라우저가 거부하기 때문(`respec-global.conf` 의 map 이 "upstream 이 안 보낸 경우에만" 값을 가짐). `/ml/*` 은 서버끼리만 쓰는 주소라 CORS 를 안 붙임. 시험: `deploy/tests/test_nginx.sh [5]` 가 실제 nginx(1.24 · 1.28)에 요청해 헤더 · 본문을 확인함.
- **인증서 자동 갱신**: Let's Encrypt 인증서는 90일. `certbot.timer`(systemd 타이머, cron 의 친척)가 하루 두 번 확인하다가 만료 30일 전쯤 갱신하고, 갱신 훅이 nginx 를 reload 해서 새 인증서를 읽게 함. `certbot renew --dry-run` 이 그 전체 길을 시험함.
- **방화벽 3겹**: AWS 보안 그룹(밖) → ufw(서버 안) → 컨테이너 포트를 `127.0.0.1` 에만 열기. Docker 는 ufw 를 건너뛰고 포트를 여는 성질이 있어서 마지막 겹이 필요함.
- **swap**: 메모리(2GB)가 모자라면 디스크를 메모리처럼 씀. 느리지만 프로세스가 죽지는 않음. `vm.swappiness=10` 은 "정말 필요할 때만" 쓰라는 값.
- **docker-ce hold**: `apt-mark hold` 로 버전을 얼림. 자동 보안 업데이트가 docker 를 올리면서 컨테이너를 재시작하는 일을 막음(올릴 땐 사람이 `unhold` 하고 점검).

## 11. 보안 경계 · 한계 (정직하게)

배포 길이 막는 것과 못 막는 것. 첫 실서버 배포 전에 지형님이 정할 일이 1개 있음(아래 "정할 일").

- **막는 것**: 배포 열쇠(`EC2_SSH_KEY`)가 *밖으로 새는 경우*(로그 · 분실 등). 그 열쇠로는 `deploy sha-<태그>` · `rollback` · `status` 만 보낼 수 있고, 번들 compose 가 허용 목록(`compose-policy.py`)을 벗어나거나 번들 파일이 심볼릭 링크이면 올라가지 않아서 서버 root 는 못 얻음.
- **못 막는 것**: 이 레포에서 워크플로를 돌릴 수 있는 사람이 `respec-api` 이미지 안에 코드를 넣는 경우. `.github/workflows/deploy.yml` 의 `if: github.ref == main` 은 보안 장치가 아님(브랜치에 올린 워크플로는 자기 파일을 씀). 그 사람은 api 이미지가 `api.env`(Supabase 비밀키 등)를 읽어 밖으로 보내게 할 수 있음(서버 root 까지는 못 감).
- **완전히 막으려면**: `main` 브랜치 보호 + 배포 열쇠를 GitHub Environment(배포 브랜치 = main)로 옮겨야 함. 무료 조직의 private 레포에는 둘 다 없음.
- **정할 일 (첫 실서버 배포 전)**: ① 팀원 신뢰로 두고 이 한계를 기록 ② 레포를 public 으로 (main 보호 · Environment 사용 가능) ③ 유료 플랜 ④ 이미지 서명(cosign) 검증을 서버에 추가. 지금은 ①(이 절이 그 기록)이고, 팀이 4명이라 현실적인 위험은 계정 탈취뿐(탈취되면 main 에 직접 push 도 가능해 어차피 같음).

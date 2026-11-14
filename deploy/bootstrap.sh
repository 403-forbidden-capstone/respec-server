#!/usr/bin/env bash
# 새 Ubuntu 24.04 (arm64) 서버를 한 번에 세팅함. 두 번 돌려도 결과가 같음(이미 된 것은 건너뜀).
# 값 · 열쇠 · 비밀번호가 든 파일(api.env · ml.env · deploy.env · 모델 파일 · 인증서)은 절대 덮어쓰지 않음.
#
# 실행 (새 서버가 생기고 지형님 OK 가 난 뒤, 세션이 SSH 로)
#   PC:     scp -i <열쇠> -r deploy .env.example ubuntu@<서버>:/tmp/respec-src/
#   서버:   sudo DEPLOY_PUBKEY_FILE=/tmp/respec-src/respec-deploy-key.pub bash /tmp/respec-src/deploy/bootstrap.sh
#   인증서: sudo CERTBOT_EMAIL=<알림 받을 메일> bash /tmp/respec-src/deploy/bootstrap.sh --issue-cert   (DNS 가 이 서버를 가리킨 뒤)
#
# 옵션: --skip-upgrade (apt 업그레이드 건너뜀) · --issue-cert (Let's Encrypt 인증서 발급 + nginx 를 HTTPS 모양으로)
# 환경 변수
#   DEPLOY_PUBKEY / DEPLOY_PUBKEY_FILE  배포 열쇠의 공개 키(한 줄). 없으면 deploy 계정은 만들되 열쇠는 안 넣음
#   CERTBOT_EMAIL                       --issue-cert 때 필수 (인증서 만료 알림 메일)
#   DOCKER_VERSION                      docker-ce 버전 문자열 고정(예: 5:29.9.3-1~ubuntu.24.04~noble). 없으면 최신 설치 후 hold
#
# 하는 일: 업그레이드 · 기본 패키지 · docker-ce(공식 저장소, 버전 hold) · docker 로그 제한 · swap 2GB · ufw · fail2ban · ssh 강화
#          · nginx · certbot · deploy 계정(강제 명령) · /srv/respec 폴더와 env 틀 · 보안 업데이트 자동(자동 재부팅 끔)
# 안 하는 것: 재부팅 · 컨테이너 시작 · 모델 파일 · docker login(ghcr) · 인증서 발급(--issue-cert 가 따로)
set -Eeuo pipefail

DOMAIN=api.respecalarm.com
APP_DIR=/srv/respec
LIB_DIR=/usr/local/lib/respec
WEBROOT=/var/www/certbot
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
EXAMPLE_SRC=""
for c in "$SRC_DIR/../.env.example" "$SRC_DIR/.env.example"; do [[ -f $c ]] && EXAMPLE_SRC=$c && break; done

SKIP_UPGRADE=0
ISSUE_CERT=0
for a in "$@"; do
  case "$a" in
    --skip-upgrade) SKIP_UPGRADE=1 ;;
    --issue-cert) ISSUE_CERT=1 ;;
    *) echo "알 수 없는 옵션: $a" >&2; exit 2 ;;
  esac
done

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a # 업그레이드 뒤 서비스 재시작 질문을 자동 처리

die() { printf '오류: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "root 로 실행해야 함 (sudo bash bootstrap.sh)"

LOG=/var/log/respec-bootstrap.log
exec > >(tee -a "$LOG") 2>&1

step() { printf '\n== [%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
note() { printf '   %s\n' "$*"; }
trap 'printf "오류: %s 번째 줄에서 멈춤 (종료 코드 %s). 고친 뒤 다시 돌려도 됨(이미 된 것은 건너뜀)\n" "$LINENO" "$?" >&2' ERR

# 파일 내용이 다를 때만 덮어씀. 바꿨으면 0, 그대로면 1
install_if_changed() { # install_if_changed 모드 소유자 원본 대상
  local mode=$1 owner=$2 src=$3 dst=$4
  if [[ -f $dst ]] && cmp -s "$src" "$dst"; then
    chown "$owner" "$dst"
    chmod "$mode" "$dst"
    return 1
  fi
  install -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$src" "$dst"
}

# ---------------------------------------------------------------- 0 사전 확인
step "0 사전 확인"
. /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || die "Ubuntu 24.04 에서만 실행함 (지금 ${PRETTY_NAME:-알 수 없음})"
[[ $(uname -m) == aarch64 ]] || die "arm64(aarch64) 서버에서만 실행함 (지금 $(uname -m))"
for f in deploy.sh smoke.sh check-env.sh nginx/respec-global.conf nginx/respec-api.conf nginx/respec-api.http.conf; do
  [[ -f $SRC_DIR/$f ]] || die "$SRC_DIR/$f 없음 — deploy 폴더 전체와 .env.example 을 같이 올렸는지 확인"
done
[[ -n $EXAMPLE_SRC ]] || die ".env.example 을 못 찾음 (deploy 폴더 옆이나 안에 있어야 함)"
note "$PRETTY_NAME / $(uname -m) / 메모리 $(free -m | awk '/^Mem:/{print $2}')MB / 디스크 여유 $(df -h --output=avail / | tail -1 | tr -d ' ')"

# ---------------------------------------------------------------- 1 업그레이드 · 기본 패키지
step "1 apt 업그레이드 · 기본 패키지"
if [[ $SKIP_UPGRADE == 0 ]]; then
  apt-get update -q
  apt-get -y -q -o Dpkg::Options::=--force-confold upgrade
else
  note "--skip-upgrade: 업그레이드 건너뜀"
  apt-get update -q
fi
apt-get install -y -q ca-certificates curl gnupg nginx certbot fail2ban python3-systemd ufw unattended-upgrades

# ---------------------------------------------------------------- 2 docker-ce
step "2 docker-ce (공식 저장소) · compose 플러그인 · 버전 hold"
install -m 0755 -d /etc/apt/keyrings
if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
repo_line="deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME} stable"
if [[ ! -f /etc/apt/sources.list.d/docker.list ]] || [[ $(cat /etc/apt/sources.list.d/docker.list) != "$repo_line" ]]; then
  echo "$repo_line" >/etc/apt/sources.list.d/docker.list
  apt-get update -q
fi
if ! dpkg -s docker-ce >/dev/null 2>&1; then
  if [[ -n ${DOCKER_VERSION:-} ]]; then
    apt-get install -y -q "docker-ce=$DOCKER_VERSION" "docker-ce-cli=$DOCKER_VERSION" containerd.io docker-buildx-plugin docker-compose-plugin
  else
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
else
  note "docker-ce 이미 설치됨 — 건드리지 않음"
fi
# hold: 자동 업데이트가 docker 를 올리면서 컨테이너를 재시작하는 일을 막음. 올릴 땐 사람이 unhold
apt-mark hold docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
note "docker $(docker --version | awk '{print $3}' | tr -d ,) / $(docker compose version | head -1) (hold)"

# docker 로그 크기 제한: 컨테이너별 10MB x 3개
install -d -m 0755 /etc/docker
daemon_json=$(mktemp)
cat >"$daemon_json" <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
if install_if_changed 0644 root:root "$daemon_json" /etc/docker/daemon.json; then
  note "/etc/docker/daemon.json 갱신 → docker 재시작"
  systemctl restart docker
else
  note "/etc/docker/daemon.json 그대로"
fi
rm -f "$daemon_json"
systemctl enable --now docker >/dev/null

# ---------------------------------------------------------------- 3 swap 2GB
step "3 swap 2GB"
if [[ -n $(swapon --show=NAME --noheadings) ]]; then
  note "swap 이미 있음: $(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')"
else
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
  note "/swapfile 2GB 켬"
fi
echo 'vm.swappiness=10' >/etc/sysctl.d/99-respec.conf
sysctl -q -p /etc/sysctl.d/99-respec.conf

# ---------------------------------------------------------------- 4 ssh 강화 · fail2ban · ufw
step "4 ssh 강화 · fail2ban · ufw"
ssh_dropin=$(mktemp)
cat >"$ssh_dropin" <<'CONF'
# respec: 비밀번호 로그인 · root 로그인 끔 (열쇠 로그인만)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
CONF
if install_if_changed 0644 root:root "$ssh_dropin" /etc/ssh/sshd_config.d/10-respec.conf; then
  sshd -t || die "sshd 설정 검사 실패 — /etc/ssh/sshd_config.d/10-respec.conf 확인"
  systemctl reload ssh
  note "sshd 설정 갱신 (reload, 지금 접속은 안 끊김)"
else
  note "sshd 설정 그대로"
fi
rm -f "$ssh_dropin"

f2b=$(mktemp)
cat >"$f2b" <<'CONF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
CONF
if install_if_changed 0644 root:root "$f2b" /etc/fail2ban/jail.d/respec-sshd.local; then systemctl restart fail2ban; fi
rm -f "$f2b"
systemctl enable --now fail2ban >/dev/null

# 22 를 먼저 열고 나서 켬 (순서가 바뀌면 접속이 끊김)
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
note "ufw: 22 · 80 · 443 만 허용"

# ---------------------------------------------------------------- 5 deploy 계정 · 폴더
step "5 deploy 계정 · /srv/respec · 스크립트"
if ! id deploy >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash deploy
  passwd -l deploy >/dev/null
  note "deploy 계정 만듦 (비밀번호 로그인 잠금)"
fi
usermod -aG docker deploy

install -d -m 0755 -o root -g root "$LIB_DIR"
install_if_changed 0755 root:root "$SRC_DIR/deploy.sh" "$LIB_DIR/deploy.sh" && note "deploy.sh 설치(갱신)" || true
install_if_changed 0755 root:root "$SRC_DIR/smoke.sh" "$LIB_DIR/smoke.sh" && note "smoke.sh 설치(갱신)" || true
install_if_changed 0755 root:root "$SRC_DIR/check-env.sh" "$LIB_DIR/check-env.sh" && note "check-env.sh 설치(갱신)" || true

install -d -m 0750 -o deploy -g deploy "$APP_DIR"
# 모델 파일 폴더: root 소유 읽기 전용 (컨테이너에는 읽기 전용으로 붙음). 파일은 1-11 에서 올림
install -d -m 0755 -o root -g root "$APP_DIR/models" "$APP_DIR/models/v4_age_only"

# 번들 이미지가 오기 전 임시 예시 (첫 배포가 이 파일을 번들 것으로 갈아 놓음)
if [[ ! -f $APP_DIR/env.example ]]; then install -m 0644 -o deploy -g deploy "$EXAMPLE_SRC" "$APP_DIR/env.example"; fi
# env 틀: 이름만 있고 값은 비어 있음. 이미 있으면 절대 안 건드림 (값이 들어 있을 수 있음)
for f in api.env ml.env deploy.env smoke.env; do
  if [[ ! -f $APP_DIR/$f ]]; then
    "$LIB_DIR/check-env.sh" --print-skeleton "$f" --example "$EXAMPLE_SRC" >"$APP_DIR/$f"
    chown deploy:deploy "$APP_DIR/$f"
    chmod 600 "$APP_DIR/$f"
    note "$f 틀 만듦 (이름만, 값 비어 있음)"
  else
    chmod 600 "$APP_DIR/$f"
    chown deploy:deploy "$APP_DIR/$f"
    note "$f 이미 있음 — 값은 안 건드림"
  fi
done
[[ -f $APP_DIR/versions.env ]] || install -m 0644 -o deploy -g deploy /dev/null "$APP_DIR/versions.env"

# deploy 의 ssh 열쇠: authorized_keys 는 root 소유(deploy 가 자기 강제 명령을 못 바꿈). 열쇠 1개, 이 스크립트만 실행 가능
pubkey=${DEPLOY_PUBKEY:-}
[[ -z $pubkey && -n ${DEPLOY_PUBKEY_FILE:-} ]] && pubkey=$(head -n1 "$DEPLOY_PUBKEY_FILE")
install -d -m 0755 -o root -g root /home/deploy/.ssh
if [[ -n $pubkey ]]; then
  key_re='^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+( [^"]*)?$'
  # 한 줄 · 큰따옴표 없음만 허용 (authorized_keys 에 다른 줄 · 옵션이 끼어드는 것을 막음)
  [[ $pubkey != *$'\n'* && $pubkey =~ $key_re ]] || die "DEPLOY_PUBKEY 모양이 공개 키 한 줄이 아님"
  ak=$(mktemp)
  printf 'restrict,command="%s/deploy.sh" %s\n' "$LIB_DIR" "$pubkey" >"$ak"
  if install_if_changed 0644 root:root "$ak" /home/deploy/.ssh/authorized_keys; then
    note "deploy 의 authorized_keys 갱신 (강제 명령 deploy.sh, restrict)"
  else
    note "deploy 의 authorized_keys 그대로"
  fi
  rm -f "$ak"
else
  note "DEPLOY_PUBKEY 없음 — deploy 의 ssh 열쇠는 아직 안 넣음 (자동 배포 전에 다시 돌려서 넣음)"
fi

# ---------------------------------------------------------------- 6 nginx · certbot
step "6 nginx · certbot"
install -d -m 0755 "$WEBROOT"
install_if_changed 0644 root:root "$SRC_DIR/nginx/respec-global.conf" /etc/nginx/conf.d/respec-global.conf && note "respec-global.conf 설치(갱신)" || true
rm -f /etc/nginx/sites-enabled/default

CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
if [[ $ISSUE_CERT == 1 ]]; then
  [[ -n ${CERTBOT_EMAIL:-} ]] || die "--issue-cert 에는 CERTBOT_EMAIL 이 필요함"
  if [[ -s $CERT ]]; then
    note "인증서 이미 있음 — 발급 건너뜀"
  else
    # 발급 전에 80 번에서 확인 파일을 내줄 수 있어야 하므로 HTTP 모양을 먼저 깔고 reload
    install_if_changed 0644 root:root "$SRC_DIR/nginx/respec-api.http.conf" /etc/nginx/sites-available/respec-api || true
    ln -sf /etc/nginx/sites-available/respec-api /etc/nginx/sites-enabled/respec-api
    nginx -t
    systemctl reload nginx || systemctl restart nginx
    mine=$(curl -fsS --max-time 10 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)
    dns=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)
    note "이 서버 공인 IP=$mine / $DOMAIN 이 가리키는 IP=${dns:-없음}"
    [[ -n $mine && $mine == "$dns" ]] || die "DNS 가 아직 이 서버를 안 가리킴 (A 레코드 · Proxy 끔 확인). 발급을 안 하고 멈춤"
    certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL"
  fi
  # 갱신되면 nginx 가 새 인증서를 읽도록
  hook=/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  install -d -m 0755 "$(dirname "$hook")"
  printf '#!/bin/sh\nsystemctl reload nginx\n' >"$hook"
  chmod 755 "$hook"
  systemctl enable --now certbot.timer >/dev/null
  certbot renew --dry-run
fi

if [[ -s $CERT ]]; then
  nginx_src=respec-api.conf
else
  nginx_src=respec-api.http.conf
  note "인증서 없음 → nginx 는 HTTP 모양(80 번, 503 응답). 발급은 DNS 연결 뒤 --issue-cert"
fi
install_if_changed 0644 root:root "$SRC_DIR/nginx/$nginx_src" /etc/nginx/sites-available/respec-api && note "nginx 사이트 갱신: $nginx_src" || true
ln -sf /etc/nginx/sites-available/respec-api /etc/nginx/sites-enabled/respec-api
nginx -t
systemctl enable nginx >/dev/null
systemctl reload nginx || systemctl restart nginx
systemctl enable --now certbot.timer >/dev/null

# ---------------------------------------------------------------- 7 보안 업데이트 자동 (재부팅은 안 함)
step "7 보안 업데이트 자동 · 자동 재부팅 끔"
au1=$(mktemp)
cat >"$au1" <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
install_if_changed 0644 root:root "$au1" /etc/apt/apt.conf.d/20auto-upgrades || true
rm -f "$au1"
au2=$(mktemp)
cat >"$au2" <<'CONF'
// respec: 자동 업데이트는 보안 저장소만(Ubuntu 기본), 자동 재부팅은 끔 — 재부팅은 사람이 정함
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "false";
CONF
install_if_changed 0644 root:root "$au2" /etc/apt/apt.conf.d/52respec-unattended || true
rm -f "$au2"

# ---------------------------------------------------------------- 요약
step "요약"
printf '   docker      %s\n' "$(docker --version)"
printf '   compose     %s\n' "$(docker compose version | head -1)"
printf '   hold        %s\n' "$(apt-mark showhold | tr '\n' ' ')"
printf '   swap        %s\n' "$(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')"
printf '   ufw         %s\n' "$(ufw status | head -1)"
printf '   nginx       %s\n' "$(systemctl is-active nginx)"
printf '   fail2ban    %s\n' "$(systemctl is-active fail2ban)"
printf '   certbot.timer %s\n' "$(systemctl is-enabled certbot.timer 2>/dev/null || echo 없음)"
printf '   인증서      %s\n' "$([[ -s $CERT ]] && echo "있음 ($(openssl x509 -enddate -noout -in "$CERT" 2>/dev/null))" || echo 없음)"
printf '   deploy 열쇠 %s\n' "$([[ -s /home/deploy/.ssh/authorized_keys ]] && echo 있음 || echo 없음)"
if [[ -f /var/run/reboot-required ]]; then
  echo
  echo "   >> 재부팅이 필요함 (커널 · 라이브러리 업데이트). 재부팅은 지형님 OK 뒤에 따로 함"
fi
cat <<'NEXT'

다음 할 일
  1 env 값 채우기:  sudo nano /srv/respec/api.env  (ml.env · smoke.env 도. deploy.env 는 비워 둠)  — 값은 채팅에 붙이지 않음
  2 ghcr 로그인:    sudo -iu deploy docker login ghcr.io -u <GitHub 사용자> --password-stdin   (토큰 read:packages)
  3 deploy 열쇠:    DEPLOY_PUBKEY_FILE=... 로 이 스크립트를 다시 돌리면 들어감 (이미 넣었으면 건너뜀)
  4 인증서:         DNS 연결 뒤  sudo CERTBOT_EMAIL=... bash deploy/bootstrap.sh --issue-cert --skip-upgrade
  5 첫 배포:        sudo -u deploy /usr/local/lib/respec/deploy.sh deploy sha-<태그>
NEXT

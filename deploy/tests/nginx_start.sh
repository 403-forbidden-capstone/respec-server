#!/usr/bin/env bash
# nginx 컨테이너 안에서 서버를 띄우는 스크립트 (test_nginx.sh [5] 가 마운트해서 실행).
# 시험용 인증서를 만들고, Ubuntu 의 nginx.conf 처럼 http 수준에 server_tokens 가 이미 있는 조건으로 nginx 를 띄움.
set -e
rm -f /etc/nginx/conf.d/default.conf
sed -i '0,/^http {/s//http {\n    server_tokens build;/' /etc/nginx/nginx.conf
d=/etc/letsencrypt/live/api.respecalarm.com
mkdir -p "$d" /var/www/certbot
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=api.respecalarm.com \
  -keyout "$d/privkey.pem" -out "$d/fullchain.pem" 2>/dev/null
exec nginx -g 'daemon off;'

# 운영 런북 — 되돌리기 · 이어받기 · 점검

서버(FastAPI) 전환 중과 전환 뒤에 이상이 생겼을 때, 어디서 무엇을 실행하면 원래대로 돌아가는지 모은 문서. SQL은 통째로 복사해 붙여 쓰게 적음.

- 판단하는 사람: 지형님. 부재 때를 위해 Supabase 대시보드에 들어갈 수 있는 팀원 전원이 이 문서를 열 수 있어야 함(7절 점검표에 포함).
- SQL 실행 위치: [Supabase SQL 편집기](https://supabase.com/dashboard/project/gomrhctwjvttoqifxktt/sql/new). 여러 문장을 한 번에 실행하면 마지막 문장의 결과만 보임 — 결과를 문장마다 보려면 하나씩 실행.
- 서버 명령 위치: PC 터미널(Git Bash)에서 `ssh -i ~/.ssh/respec-deploy-key deploy@3.38.123.6 <명령>`. 이 계정은 `deploy sha-…` · `rollback` · `status` 세 명령만 받음. 서버 안을 보는 명령(`docker ps` 등)은 관리 계정 `ubuntu`로(`ssh -i ~/.ssh/respec-server-key.pem ubuntu@3.38.123.6`). `3.38.123.6`은 서버의 탄력적 IP(DNS `api.respecalarm.com`이 가리키는 주소) — 서버를 새로 세우면 바뀌므로 그때 이 문서의 주소도 바꿈. 도메인으로도 접속되지만 처음 접속할 때 호스트 지문 확인 질문이 나오고, 지형님 PC에는 IP로 등록돼 있음.
- SQL 원본: 본 레포 `supabase/ops/server-switch-rollback.sql` · `server-switch-reapply.sql`(2절), 표 정의는 마이그레이션 `20261114100000_server_switch.sql`(1절). 기준 커밋 `b140f701`. 원본이 바뀌면 이 문서도 같이 바꿈.
- 용어: **함수 줄** = 앱이 부르는 Edge 함수의 담당(`edge` · `canary` · `server`), **작업 줄** = pg_cron이 부르는 예약 작업의 담당(`edge` · `shadow` · `server`). `edge`로 되돌리는 것이 언제나 안전한 쪽.

## 0. 신호별로 갈 곳

| 신호 | 할 일 | 절 |
|---|---|---|
| 서버 5xx가 5분 동안 5% 넘음 · `/health/deep` 2번 연속 실패 · p95가 Edge의 2배로 30분 | 해당 함수 줄을 `edge`로 | 1 |
| M1 적용 뒤 30분 안에 `net._http_response`에 wake-check · alarm-roll 200이 안 보임 | M1 원복 | 2 |
| 배포 뒤 문제가 늦게 보임 | 이미지 되돌리기 | 3 |
| Actions가 멈춤 | PC에서 빌드해 올리기 | 3 |
| 스케줄러가 멈춤(감시 메일 · `/health/deep` scheduler 실패) | pg_cron이 알아서 Edge로 이어받음(20분 안). 원인 확인 | 4 · 5 |
| 컨테이너 재시작 반복 · 디스크 80% · CPU 크레딧 바닥 | 해당 줄을 `edge`로 → 원인 처리 | 1 |
| 전원 401/503(JWKS 못 받음) | `edge`로 | 1 |
| 카카오 429(일 한도) | 되돌려도 안 풀림(같은 키) → 앱 안내, 다음 날 | — |
| Supabase 자체 장애 | 되돌려도 소용없음 → 기다림 | — |
| 인증서 만료 임박 | `certbot renew` 확인 | 7 |

## 1. 표 값 되돌리기 (`server_routes`)

앱은 함수 줄을, 서버 스케줄러와 pg_cron은 작업 줄을 읽음. 값만 바꾸는 운영 조작이고 마이그레이션이 아님. 열쇠는 `(kind, name)` — `alarm-roll`은 함수 줄과 작업 줄이 둘 다 있어서 `kind`를 꼭 같이 씀.

반영 시점: 표는 SQL을 실행한 순간 바뀜. 앱은 표를 최대 5분 캐시(못 읽었으면 30초 뒤 다시)하므로 앱에서 보이는 데는 **최대 5분**. 서버가 연속 3번 못 쓸 상태면 앱의 차단기가 5분 동안 서버를 건너뛰고 바로 Edge로 감. 작업 줄은 다음 회차(5분 작업은 5분 안)부터.

**1-1. 지금 값 캡처** (바꾸기 전에 먼저, 화면 캡처 또는 복사)

```sql
select kind, name, mode, updated_at from public.server_routes order by kind, name;
```

```sql
select name, user_id from public.server_route_users order by name, user_id;
```

**1-2. 한 줄만 `edge`로** (이름만 바꿔 씀)

```sql
update public.server_routes set mode = 'edge'
where kind = 'function' and name = 'place-search'
returning kind, name, mode;
```

**1-3. 전부 `edge`로** (함수 · 작업 모두 `edge`를 허용함. 이미 `edge`인 줄은 안 건드림)

```sql
update public.server_routes set mode = 'edge' where mode <> 'edge' returning kind, name, mode;
```

**1-4. `canary` 다시 켜기** (지정한 계정만 서버로, 나머지는 Edge)

```sql
insert into public.server_route_users (name, user_id) values ('place-search', '<대상 user_id>') on conflict do nothing;
```

```sql
update public.server_routes set mode = 'canary'
where kind = 'function' and name = 'place-search'
returning kind, name, mode;
```

`<대상 user_id>`는 `public.profiles.user_id`(Supabase 대시보드 Authentication → Users의 User UID와 같음). `canary`는 함수 줄만 가능(작업 줄은 `shadow` · `server`).

**1-5. 확인**: 1-1의 첫 SQL을 다시 실행 → 바꾼 줄의 `mode`와 `updated_at`이 방금 값. 앱 쪽은 최대 5분 뒤.

## 2. M1 원복 · 다시 적용 (pg_cron 명령)

M1 마이그레이션이 pg_cron의 세 작업(wake-check · alarm-roll · suggestion-generate) 명령을 `private.run_job(…)`으로 바꿨고, 제안 안전망(`suggestion-generate-safety`)을 새로 만들었음. **원복** = 이 네 가지를 M1 이전 모양으로 되돌림(Edge를 바로 부름). 표 · 함수는 둬도 해로울 게 없어서 안 지움. 여러 번 실행해도 같은 결과.

순서:

1. **먼저 1-3**(전부 `edge`) 실행 — 서버 스케줄러가 `shadow` · `server`로 돌고 있으면, 안 하고 원복할 때 pg_cron이 Edge를 부르는 동안 서버도 같은 일을 함.
2. 아래 **원복 SQL**을 통째로 실행. 마지막 결과(`cron.job` 목록)에서 같은 `jobname`이 `username` 다르게 두 줄이면 교체가 아니라 추가된 것 → 알림.
3. **확인**: 목록에서 wake-check · alarm-roll · suggestion-generate의 `command`가 `private.invoke_edge_function('…')`이고 `suggestion-generate-safety` 줄이 없음. 20분 안에 6절의 `net._http_response`에 200이 새로 쌓임(wake-check · alarm-roll이 둘 다 5분마다 돌아 같이 쌓임).
4. 다시 적용할 때는 아래 **다시 적용 SQL**을 통째로 실행. 확인: 세 작업의 `command`가 `private.run_job(…)`이고 `suggestion-generate-safety`가 생김. 20분 안에 `net._http_response`에 200.

원복 SQL (`supabase/ops/server-switch-rollback.sql`)

```sql
-- 서버 전환 스위치(M1) 원복 — pg_cron 명령을 M1 이전 모양으로 되돌림 (운영 조작 · 마이그레이션 아님)
-- 언제: M1 적용 뒤 30분 안에 net._http_response에 wake-check · alarm-roll 200이 안 보일 때 등 (서버_완결점 10절)
-- 어디서: Supabase 대시보드 → SQL Editor에 통째로 붙여 실행. 여러 번 실행해도 같은 결과
-- 효과: wake-check · alarm-roll · suggestion-generate가 private.invoke_edge_function(이름)을 바로 부름 = M1 이전과 똑같음. 제안 안전망 cron(18:15 KST)은 지움
-- 안 지우는 것: 표 server_routes · server_route_users · server_heartbeat, 함수 get_server_routes · private.run_job (추가만 한 것이라 둬도 해롭지 않음)
-- voice-auto-commit · friend-requests-expire · friend-records-purge는 M1이 안 건드렸으므로 여기서도 안 건드림
-- 먼저: 서버 스케줄러가 돌고 있으면(server_routes 작업 줄이 shadow · server) 이 SQL 전에 아래 한 줄로 전부 edge로 — 안 하면 pg_cron이 Edge를 부르는 동안 서버도 같은 일을 함
--   update public.server_routes set mode = 'edge' where mode <> 'edge';
-- 끝에 cron.job 목록을 보여 줌: 같은 jobname이 username 다르게 두 줄이면 교체가 아니라 추가된 것 (pg_cron은 jobname + username 쌍으로 찾음) → 알림
-- 다시 적용: server-switch-reapply.sql

select cron.schedule('wake-check', '*/5 * * * *', $cmd$select private.invoke_edge_function('wake-check')$cmd$);
select cron.schedule('alarm-roll', '*/5 * * * *', $cmd$select private.invoke_edge_function('alarm-roll')$cmd$);
select cron.schedule('suggestion-generate', '0 9 * * *', $cmd$select private.invoke_edge_function('suggestion-generate')$cmd$);

do $$
begin
  if exists (select 1 from cron.job where jobname = 'suggestion-generate-safety') then
    perform cron.unschedule('suggestion-generate-safety');
  end if;
end;
$$;

select jobid, jobname, username, schedule, command, active from cron.job order by jobname;
```

다시 적용 SQL (`supabase/ops/server-switch-reapply.sql`)

```sql
-- 서버 전환 스위치(M1) 다시 적용 — 원복(server-switch-rollback.sql)한 뒤 pg_cron 명령을 M1 모양으로 되돌림 (운영 조작 · 마이그레이션 아님)
-- 어디서: Supabase 대시보드 → SQL Editor에 통째로 붙여 실행. 여러 번 실행해도 같은 결과
-- 전제: M1 마이그레이션(20261114100000_server_switch.sql)이 이미 적용돼 private.run_job이 있음
-- 효과: wake-check · alarm-roll · suggestion-generate가 run_job을 부르고, 제안 안전망 cron(18:15 KST)이 다시 생김
-- 이 파일의 cron 문 네 줄은 마이그레이션 6절과 같아야 함 (supabase/tests가 같은지 확인)
-- 끝에 cron.job 목록을 보여 줌: 같은 jobname이 username 다르게 두 줄이면 교체가 아니라 추가된 것 → 알림

select cron.schedule('wake-check', '*/5 * * * *', $cmd$select private.run_job('wake-check')$cmd$);
select cron.schedule('alarm-roll', '*/5 * * * *', $cmd$select private.run_job('alarm-roll')$cmd$);
select cron.schedule('suggestion-generate', '0 9 * * *', $cmd$select private.run_job('suggestion-generate', 'primary')$cmd$);
select cron.schedule('suggestion-generate-safety', '15 9 * * *', $cmd$select private.run_job('suggestion-generate', 'safety')$cmd$);

select jobid, jobname, username, schedule, command, active from cron.job order by jobname;
```

원복해도 `voice-auto-commit` · `friend-requests-expire` · `friend-records-purge`는 M1이 안 건드렸으므로 그대로.

## 3. 이미지 되돌리기 (서버)

서버의 배포 상태는 `API_TAG`(지금)와 `LAST_GOOD_API_TAG`(마지막으로 점검을 통과한 버전). 이미지는 GHCR에 남아 있고 서버에도 최근 3개를 둠.

| 하고 싶은 것 | 명령 (PC) | 끝 확인 |
|---|---|---|
| 지금 상태 보기 | `ssh -i ~/.ssh/respec-deploy-key deploy@3.38.123.6 status` | `API_TAG` · 컨테이너 목록 |
| 마지막 정상 버전으로 | `… rollback` | `/health`의 `version` = `LAST_GOOD_API_TAG` |
| **한 칸 전 버전으로** | `… "deploy sha-<이전 7자리>"` | `/health`의 `version` = 그 태그 |
| PC에서 만든 이미지 올리기(Actions 멈춤) | 서버 레포 루트에서 `EC2_HOST=3.38.123.6 EC2_SSH_KEY_FILE=~/.ssh/respec-deploy-key deploy/build-push.sh` — 빌드 · 올리기 · `deploy`까지 한 번에(Docker Desktop과 `docker login ghcr.io`(write 토큰) 필요, arm64 에뮬레이션이라 느림). 올리기만 하려면 `--no-deploy` 뒤 위의 `deploy sha-…` | 위와 같음 |

- `rollback`은 "마지막 정상 버전"으로 돌리므로, 바로 직전 배포가 이미 정상이었다면 같은 버전을 다시 올릴 뿐임. 한 칸 전으로 가려면 이전 태그로 `deploy`.
- 이전 태그 찾기: 서버 레포 `main`의 커밋 앞 7자리 앞에 `sha-`(예: 커밋 `3fb3868` → `sha-3fb3868`). 배포 기록은 서버 `/srv/respec/deploy.log`(`sudo tail -30 /srv/respec/deploy.log`, ubuntu 계정).
- 새 버전이 안 뜨거나 배포 직후 점검이 실패하면 자동으로 직전 정상 버전으로 돌아가고, 이때 Actions는 실패로 끝남(`deploy.log`에 이유).
- SSH가 끊겨도 배포는 서버에서 끝까지 돎(점검 · 되돌림까지). 결과는 `status`와 `deploy.log`.
- 확인(PC): `curl -s https://api.respecalarm.com/health` → `{"status":"ok","version":"sha-…"}`.
- 이미지만 되돌리고 nginx · bootstrap 쪽을 바꾼 경우는 `deploy/server-setup.md` 8 · 9절.
- 시험(2026-11-15, 실서버, 트래픽 없음): `deploy sha-3fb3868`(한 칸 전) **32초** → `/health`의 `version`이 `sha-3fb3868` → `deploy sha-da6d4d5`(원래 버전) **30초** → `version`이 `sha-da6d4d5`. 컨테이너가 바뀌는 순간 `/health`가 약 2초 502. 한 번 되돌리는 데 1분이 안 걸림(완결점 기준 5분 이하).

## 4. 스케줄러 점검 · 강제 종료 시험

스케줄러(`scheduler` 컨테이너)는 1분마다 `server_heartbeat.alive_at`을 갱신하고, 작업 줄이 `shadow` · `server`인 작업을 함. `edge`인 작업은 안 함. `/health/deep`은 `HEALTH_REQUIRE_SCHEDULER=true`일 때만 `alive_at`이 15분 넘게 멈춘 것을 실패로 봄(꺼져 있으면 `scheduler: skipped`).

점검(ubuntu 계정):

```bash
sudo docker ps --format '{{.Names}} {{.Status}}'
sudo docker logs --tail 20 respec-scheduler-1
```

SQL 편집기: `select job_name, alive_at, last_success_at from public.server_heartbeat order by job_name;` — `alive_at`이 2분 안이면 정상. `last_success_at`은 그 작업이 `server`로 성공한 시각이라 `edge` · `shadow`에선 비어 있어도 정상.

강제 종료 시험(감시 · 이어받기 확인용, 트래픽이 거의 없을 때):

1. `sudo docker stop respec-scheduler-1` (되돌림: `sudo docker start respec-scheduler-1`, 또는 `deploy`로 다시 올리면 `restart: unless-stopped`로 다시 뜸)
2. `alive_at`이 멈춤 → 15분 뒤 `/health/deep`의 `scheduler`가 `fail`(`HEALTH_REQUIRE_SCHEDULER=true`일 때) → UptimeRobot 감시 메일.
3. 작업 줄이 `server`였다면 pg_cron이 20분 안에 Edge로 이어받음(5절).
4. 시작해서 `alive_at`이 다시 갱신 → `/health/deep` 200 → 복구 메일.

## 5. 이어받기 시간표

서버가 멈추거나 배포 중이어도 pg_cron이 Edge로 이어받음. 겹쳐 돌아도 기록 · 푸시의 "한 번만"이 DB 유니크 키와 DB 함수에 있어 중복되지 않음.

| 작업 | 작업 줄 `edge` · `shadow` | 작업 줄 `server` |
|---|---|---|
| wake-check · alarm-roll (5분마다) | pg_cron이 Edge를 부름(M1 이전과 같음) | 서버의 마지막 성공(`last_success_at`)이 **12분 안**이면 pg_cron이 건너뜀. 없으면 Edge를 부름 → 서버가 멈춘 뒤 이어받기까지 최악 약 17분, 문서에는 **20분 안** |
| suggestion-generate (매일 18:00 KST) | 18:00에 pg_cron이 Edge를 부름. 18:15 안전망은 아무것도 안 함 | 18:00 pg_cron은 건너뜀(서버가 같은 시각에 함). **18:15 안전망**이 "오늘(한국 날짜) 서버 성공 기록 없음"이면 Edge를 부름 |
| 앱의 함수 호출(`place-search` 등) | `edge`면 Edge | `server`면 서버. 못 읽으면(표 읽기 실패) 전부 Edge. 서버가 연속 3번 못 쓸 상태면 5분간 Edge |

- 앱이 표를 읽는 간격은 5분(실패 30초). 그래서 1절의 값을 바꿔도 앱은 최대 5분 뒤에 따라옴.
- 서버 구현이 없는 작업이 `server`로 설정되면 스케줄러가 `shadow`로 낮추고 오류 로그를 남김(성공 기록 안 씀) → pg_cron이 계속 Edge를 부름. **지금(S3 머지 기준)은 wake-check의 `shadow`만 구현**이고 `server` 손잡이는 푸시 모듈 뒤에 채움 — 그 전까지는 `server`로 두어도 위 오른쪽 칸의 "서버가 함"은 일어나지 않고 Edge가 계속 함.
- 시간이 필요한 시험은 이 표의 숫자를 기준으로 기다림(5분 작업은 20분, 제안은 18:15 이후).

## 6. 확인 SQL

wake-check · alarm-roll이 Edge로 돌았는지(2절 확인, 서버가 멈춘 뒤 이어받기 확인):

```sql
select id, status_code, timed_out, created from net._http_response order by created desc limit 10;
```

```sql
select d.jobid, j.jobname, d.status, d.return_message, d.start_time
from cron.job_run_details d left join cron.job j on j.jobid = d.jobid
order by d.start_time desc limit 10;
```

- `net._http_response`는 보관이 짧음(몇 시간). 상태 코드 200이 5분 간격으로 새로 쌓이면 Edge가 호출되고 있는 것. 서버가 `server`로 하는 중이면 pg_cron은 건너뛰어 새 줄이 안 쌓이는 것이 정상.
- `cron.job_run_details`의 `status = succeeded`는 pg_cron이 SQL을 실행했다는 뜻이지 Edge가 200을 줬다는 뜻은 아님. 200은 위의 `net._http_response`로 봄.

현재 pg_cron 작업 목록(2절 확인 · 데모 전날):

```sql
select jobid, jobname, username, schedule, command, active from cron.job order by jobname;
```

## 7. 데모 전날 점검표

데모 전날에 위에서 아래로 한 번. 화면 글자는 영어 콘솔 기준(한글 콘솔은 메뉴 이름만 번역). 콘솔 경로는 개편되면 달라질 수 있어 이름으로 찾음.

| # | 항목 | 어디서 · 어떻게 | 정상 |
|---|---|---|---|
| 1 | 표 현재값 캡처 | 1-1의 SQL 두 개 | 캡처해 둠(데모 중 되돌릴 때 기준) |
| 2 | heartbeat | 4절의 `server_heartbeat` SQL | `alive_at` 2분 안 |
| 3 | 서버 상태 | `curl -s https://api.respecalarm.com/health/deep` · ubuntu에서 `sudo docker ps` | 200 · `status: ok` · 컨테이너 전부 healthy · `restarts` 0 |
| 4 | 탄력적 IP | EC2 → Elastic IPs: 서버 인스턴스에 연결됨. PC에서 `nslookup api.respecalarm.com` | 같은 IP |
| 5 | 인증서 만료일 | ubuntu에서 `sudo certbot certificates` | 만료까지 30일 넘게 남음. 갱신 시험: `sudo certbot renew --dry-run` |
| 6 | 예산 | Billing and Cost Management → Budgets | 사용액이 예산 안, 알림 메일 구독됨 |
| 7 | 무료 플랜 종료일 · 크레딧 잔액 | Billing → Credits | 종료일 전(2027-03-03 근처), 잔액 기록 |
| 8 | CPU 크레딧 | EC2 → 인스턴스 → Monitoring → CPU credit balance. CloudWatch 알람 상태 | 바닥이 아님, 알람 OK |
| 9 | 카카오 일 한도 사용량 | 카카오 개발자 콘솔 → 내 애플리케이션 → 통계 | 오늘 호출 수가 한도(도보 길찾기 1,000건/일)보다 충분히 아래 |
| 10 | docker-ce 고정 | ubuntu에서 `apt-mark showhold` | `docker-ce` · `docker-ce-cli` · `containerd.io` · `docker-compose-plugin` |
| 11 | 자동 업데이트의 자동 재시작 꺼짐 | ubuntu에서 `grep -rh '^Unattended-Upgrade::Automatic-Reboot "' /etc/apt/apt.conf.d/` | `Unattended-Upgrade::Automatic-Reboot "false";` 한 줄 |
| 12 | 디스크 · 메모리 | ubuntu에서 `df -h /` · `free -m` | 디스크 80% 아래, swap 사용 100MB 이하 |
| 13 | 감시 | UptimeRobot 대시보드 | 감시 전부 Up |
| 14 | 되돌리기 SQL 접근 | Supabase 대시보드에 들어갈 수 있는 팀원 전원이 SQL 편집기와 이 문서의 1 · 2절을 열 수 있음 | 각자 로그인해 보고 확인 |
| 15 | 배포 금지 기간 | 서버 레포 Variable `DEPLOY_ENABLED` | 데모 앞뒤 규칙은 실행 문서(`서버전환_실행_1112.md`) 기준 |

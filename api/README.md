# api — 앱 요청 처리 서버 (FastAPI)

앱이 부르는 서버. 로그인 토큰을 직접 검사하고, 이후 Edge 함수를 하나씩 옮겨 받음. 구조 · 결정은 본 레포 `docs/서버_요구사항_설계_1112.md`.

## 지금 있는 것 (S1 · 완결점 1-4)

| 주소 | 하는 일 | 토큰 |
|---|---|---|
| `GET` · `HEAD /health` | 프로세스가 떠 있는가만. 본문 `{"status":"ok","version":"<배포 태그>"}` (`version` = env `APP_VERSION`, 없으면 `dev`) | 없음 |
| `GET` · `HEAD /health/deep` | 공개 키(JWKS) · DB(PostgREST) · 스케줄러 최근 실행. 하나라도 실패면 503. 결과를 5초(`HEALTH_DEEP_CACHE_SECONDS`) 재사용(실패 포함) | 없음 |
| `GET /me` | 토큰 주인의 `{"id": ...}` | 필요 |
| `OPTIONS *` | CORS 사전 확인. 토큰 없이 204 | 없음 |

`/docs` · `/redoc` · `/openapi.json`은 꺼 둠(404). 끝에 `/`를 붙인 주소(`/me/`)도 리다이렉트 없이 404.
`/health/deep`은 토큰 없이 부르므로 nginx에서 요청 수를 제한하고(AWS 세션이 분당 10회로 설정), 앱도 결과를 5초 재사용함.

## 토큰 검사 (`app/core/auth.py` · `jwks.py`)

웹 비유: 로그인 토큰 = 도장 찍힌 편지, 공개 키 목록(JWKS) = 도장 원본 모음. 서버가 목록을 받아 두고 요청마다 도장을 대조함 → Supabase Auth에 요청마다 물어보지 않아 빠름.

| 상황 | 결과 |
|---|---|
| 정상 토큰 (ES256 · 만료 전 · 발급자 · 대상 · `role=authenticated`) | 통과 |
| 헤더 없음 · Bearer 아님 | 401 `AUTH_REQUIRED` "로그인이 필요해요" |
| 깨진 문자열 · 만료 · `anon` · `service_role` · HS256 위조 · `alg: none` · 모르는 `kid` · 다른 발급자 · 다른 대상 | 401 `AUTH_REQUIRED` "로그인이 만료됐어요…" |
| 공개 키 목록을 못 받고 캐시도 없음 | 503 `AUTH_UNAVAILABLE` (401이면 앱이 사용자를 로그아웃시키므로 503) |
| 공개 키 목록을 못 받지만 캐시 있음 | 낡은 캐시로 계속 검사 |

- 캐시: 10분(`JWKS_CACHE_TTL_SECONDS`)이 지나면 새로 받아 봄. 이때 새로 받는 요청 하나만 기다리고 나머지는 낡은 캐시로 바로 검사(JWKS가 느려도 요청이 줄줄이 늦어지지 않음).
- 모르는 `kid`는 키 교체일 수 있어 새로 받아 보되, 10초(`JWKS_MIN_REFETCH_SECONDS`) 안에서는 다시 안 받음(아무 `kid`나 보내 두드리는 것 방지). 새로 받아 봐도 없으면 401. **받기가 실패해서 가릴 수 없으면 503**(정상 사용자를 로그아웃시키지 않으려고). 키 교체 직후 새 `kid` 사용자는 최대 10초 401이 될 수 있으나 Supabase는 새 키를 서명에 쓰기 전에 JWKS에 먼저 올림.
- JWKS 응답이 깨졌거나 키 하나가 깨졌으면 그 항목만 건너뛰고, 쓸 수 있는 키가 하나도 없으면 실패로 취급(캐시가 있으면 계속 사용).
- 낡은 캐시에는 상한이 없음: JWKS가 오래 안 받아져도 마지막으로 받은 키로 검사를 이어 감(토큰 만료가 1시간이라 새 토큰은 계속 통과). 키가 폐기돼도 JWKS를 다시 받기 전에는 그 키로 서명한 토큰이 통과할 수 있음.
- **알려진 한계 (기록)**: DB · Auth에 안 물으므로 **탈퇴(삭제)한 계정의 토큰도 만료(기본 1시간)까지는 통과**함. 되돌릴 수 없는 쓰기(계정 삭제 등)는 그 함수에서 Supabase Auth에 한 번 더 확인할 것(설계 S4, 2차 이후). `tests/test_auth.py::test_deleted_account_token_passes_until_expiry`가 이 동작을 고정해 둠.

## 오류 모양 (`app/core/errors.py` · `http.py`)

본 레포 `supabase/functions/_shared/errors.ts`와 같음: `{"error": {"code", "message", "details"?}}`, 코드 14개 · 상태 번호 동일(`tests/test_errors_http.py`가 표를 대조). 서버에만 있는 코드는 `AUTH_UNAVAILABLE`(503) 하나.
- FastAPI 기본 404 · 405 · 422도 이 모양으로 바뀜. 422는 Edge와 같이 `VALIDATION_FAILED` 400이고, 입력값(좌표 · 검색어)은 되돌려 주지 않고 위치와 종류만 줌.
- 예상 못 한 오류는 500 `INTERNAL` "잠시 후 다시 시도해 주세요". 원문은 로그에만(요청 번호와 함께).
- 모든 응답(오류 포함)에 CORS 헤더와 `X-Request-Id`. 앱이 보낸 값은 `^[A-Za-z0-9_-]{1,64}$`일 때만 그대로 쓰고 아니면 새 UUID.
- Edge와 다른 점: `OPTIONS`가 200이 아니라 204, 허용 메서드에 `GET` 추가(`/me` · `/health`).

## 로그에 안 남기는 것

토큰 · 키 · 좌표 · 장소 검색어 · 푸시 토큰 · 요청 본문 · 쿼리 문자열. 로그 한 줄은 `request_id · method · path(퍼센트 인코딩) · status · ms`만.
- 예상 못 한 오류는 요청 번호 · 예외 종류 · 코드 위치(`파일:줄:함수`)만 남기고 **예외 메시지는 안 남김**(좌표 · 검색어 · 키가 섞일 수 있어서).
- `httpx`(나가는 요청 URL을 쿼리째 찍음)와 `httpcore` 로그는 WARNING 이상만. 설정이 틀려 기동이 실패해도 오류 글에 입력값이 안 나옴(`hide_input_in_errors`).
- uvicorn은 반드시 `--no-access-log`로 띄울 것(접근 로그에는 쿼리 문자열이 찍힘).
- `tests/`가 토큰 · 쿼리 문자열 · 입력값 · 예외 메시지가 로그와 응답에 없는지 확인함.

## 설정 (환경 변수)

값은 서버의 env 파일에만 둠(코드 · 레포 · 로그에 없음). **빈 문자열은 "없음"으로 읽음**(선택 변수가 비어도 기본값이 쓰이고, 필수 변수가 비면 기동 실패). 이름과 뜻:

| 이름 | 필수 | 뜻 |
|---|---|---|
| `SUPABASE_URL` | 예 | Supabase 프로젝트 주소. 로컬은 [`docs/local-dev.md`](../docs/local-dev.md)의 격리 스택 `http://127.0.0.1:28321` (본 레포 기본 로컬 스택 54321은 HS256 토큰이라 서버가 거절함) |
| `SUPABASE_JWT_ISSUER` | 아니오 | 토큰의 `iss`가 `SUPABASE_URL/auth/v1`과 다를 때만 |
| `SUPABASE_JWKS_URL` | 아니오 | 공개 키 목록 주소가 기본(`SUPABASE_URL/auth/v1/.well-known/jwks.json`)과 다를 때만 (로컬 compose: 컨테이너가 호스트의 Supabase를 부르는 주소) |
| `SUPABASE_SECRET_KEY` | scheduler는 예, api는 `/health/deep`에 | 서버 전용 Supabase 키(`sb_secret_…`). PostgREST로 `server_routes` · `server_heartbeat`를 읽고 `server_heartbeat`에 씀(scheduler), `/health/deep`이 `server_heartbeat`를 읽음(api). 없으면 **scheduler는 시작하지 않고 종료 코드 1**, api는 deep의 db 검사가 실패 |
| `JWT_AUDIENCE` | 아니오 | 기본 `authenticated` |
| `JWT_LEEWAY_SECONDS` | 아니오 | 시계 어긋남 허용, 기본 10 |
| `JWKS_CACHE_TTL_SECONDS` · `JWKS_MIN_REFETCH_SECONDS` · `HTTP_TIMEOUT_SECONDS` | 아니오 | 기본 600 · 10 · 3 |
| `HEALTH_REQUIRE_SCHEDULER` · `SCHEDULER_STALE_MINUTES` | 아니오 | 스케줄러(S3, 1-10)가 배포돼 `alive_at`이 올라오는 것을 확인한 뒤 `true`로. 켜면 `server_heartbeat.alive_at`이 15분 넘게 멈추면 deep 실패 |
| `APP_VERSION` | 아니오 | 배포 태그(`sha-…`). compose가 api · scheduler에 넣어 줌. `/health`의 `version`, 없으면 `dev`. `/health`는 토큰 없이 부를 수 있으므로 **배포 태그 말고 다른 값은 넣지 않음** |
| `HEALTH_DEEP_CACHE_SECONDS` | 아니오 | `/health/deep` 결과 재사용 시간(초), 기본 5, **0~60만 받음**. 0은 캐시만 끔(확인은 한 번에 하나씩 순서대로 함) |
| `LOG_LEVEL` | 아니오 | 기본 `INFO` (대소문자 무관, DEBUG · INFO · WARNING · ERROR · CRITICAL) |

## 스케줄러 (`app/scheduler.py` · `jobs.py` · `db.py`, S3 · 완결점 1-10)

예약 작업을 도는 프로세스(하나라 중복 없음). 설계 7절. 웹 비유: pg_cron(Supabase)이 "5분마다 Edge 함수를 부르는 알람"이라면, 이 프로세스는 같은 일을 서버가 대신 하되 **표(`server_routes`)가 허락한 작업만** 하는 일꾼임.
설계 7절은 APScheduler라고 했으나 **직접 만든 1초 틱 루프**임: 의존성이 늘지 않고, 가짜 시계로 정시 · 늦음 · 시계 역행을 결정적으로 시험할 수 있고, SIGTERM · Windows Ctrl+C로 바로 정상 종료됨. 정시는 5분 · 하루 한 번뿐이라 스케줄 문법이 필요 없음.

| 하는 일 | 내용 |
|---|---|
| 하트비트 | 1분마다 `server_heartbeat.alive_at`에 지금 시각을 씀(작업 줄 3개 전부, mode와 상관없이). `/health/deep`이 이것이 15분 넘게 멈추면 실패로 답함(`HEALTH_REQUIRE_SCHEDULER=true`일 때). `last_success_at`은 이 경로가 안 건드림. 표에는 마지막 값 하나만 남으므로 성공은 5분에 한 줄 `heartbeat ok at=…`(INFO)로 로그에 남김 |
| 작업 정시 | UTC 정시에 깨어남: `wake-check` · `alarm-roll` 5분마다(`:00 :05 …`, pg_cron `*/5`와 같은 시각), `suggestion-generate` 매일 09:00 UTC = 18:00 KST. 돌 때마다 `server_routes`(`kind = 'job'`)를 새로 읽음 |
| `edge` | 아무것도 안 함 (pg_cron이 Edge를 부름). 표에 줄이 없어도 같음 |
| `shadow` | 세기만 함(쓰기 · 푸시 없음). 지금 구현된 것은 `wake-check`뿐: `find_missed_wake_alarms(p_now := 다음 정시, 24시간)`의 행 수를 로그 한 줄로 — `shadow job=wake-check boundary=2026-11-15T03:05:00Z checked=2`. 알람 · 사용자 번호는 안 찍고 수만 |
| `server` | 서버가 실제로 함(지금 시각 기준으로 처리) — 틀만 있고 구현된 작업은 아직 없음(3차). 구현이 없는 작업이 `server`로 정해지면 `shadow`로 **낮추고 오류 로그**(`서버 구현 없는 작업이 server로 설정됨 → shadow로 낮춤`), `last_success_at`은 안 씀 → pg_cron이 계속 Edge를 부름 |
| 표를 못 읽음 · 모르는 값 | `edge`로 봄 (서버는 아무것도 안 함) |
| 되돌리기 | `update server_routes set mode = 'edge' where kind = 'job' and name = '…'` — 다음 회차부터 서버는 안 함. 서버가 멈춰도 pg_cron이 Edge로 이어받음 |

**shadow가 정시 30초 전부터 정시 전까지만 도는 이유**: pg_cron이 정시에 Edge `wake-check`를 부르면 Edge가 MISSED를 기록해서, 그 뒤에 서버가 세면 후보가 0이 됨(이미 기록된 것은 `find_missed_wake_alarms`가 뺌). 그래서 정시 30초 전에 **정시를 기준 시각으로** 세어 Edge가 정시에 보게 될 후보와 같은 수를 얻음. 틱이 막혔다 정시를 넘겨 깨어나면 그 회차는 건너뜀(`회차 놓침` 로그). 앞당김이 없는 작업은 정시 뒤 60초까지는 한 번 돎. 시계가 뒤로 가면(시간 맞춤) `시계가 뒤로 감` 로그와 함께 하트비트 · 정시를 지금 기준으로 다시 잡음.

**Edge와 대조하는 법 (24시간 shadow, 완결점 1-10 ①)** — 증거 셋:
1. **서버가 센 수 = 같은 정시에 Edge가 처리한 수**: 서버 쪽은 `docker logs`의 `shadow job=wake-check boundary=… checked=…` 줄, Edge 쪽은 Supabase SQL 편집기의 `net._http_response`. 응답 보관이 짧아(보통 6시간) 24시간 동안 6시간마다 한 번씩 결과를 파일로 저장해 모음(5분 정시라 하루 288건):
   ```sql
   select to_timestamp(floor(extract(epoch from created) / 300) * 300) at time zone 'UTC' as boundary,
          (content::jsonb->>'checked')::int as checked, (content::jsonb->>'recorded')::int as recorded
   from net._http_response where content::text like '{"checked"%' order by created desc limit 80;
   ```
   `boundary`(UTC, 서버 로그의 `boundary=…Z`와 같은 정시)로 맞춰 봄. 수가 다르면 이유를 적음(정시 직전 30초 안에 기상 확인이 들어옴 · Edge 시작이 늦음 등 ±1은 정상). 에러 응답은 `like`에 안 걸려 빠짐.
2. **`alive_at` 간격 6분 이하**: `docker logs`의 `heartbeat ok at=…` 줄(5분마다) 사이 간격이 6분 이하이고 `heartbeat 쓰기 실패` 줄이 없음. `HEALTH_REQUIRE_SCHEDULER=true`면 15분 넘게 멈출 때 감시 메일도 옴.
3. **기간 안 대상 1건 이상**: 위 `checked`가 0이 아닌 정시가 하나라도 있어야 함. 없으면 시험 계정에 알람 시각이 30분 넘게 지난 켜진 기상 알람을 만들어 두고(확인하지 않음) 그 정시를 봄.

대조하는 동안 `LOG_LEVEL`을 INFO로 둘 것(WARNING 이상이면 `shadow`와 `heartbeat ok` 줄이 안 남음). PostgREST `max-rows`(기본 1000)가 후보 수를 자를 수 있으나 Edge(supabase-js)도 같음.

실패는 루프를 안 멈춤: DB 호출 실패 · 작업의 예외는 로그(오류 종류 · 상태 번호만)를 남기고 다음 회차로 감. 하트비트 쓰기 실패는 1분 뒤 다시 시도(`alive_at`이 멈추면 감시가 알림). `last_success_at`은 서버 시계로 씀(EC2는 NTP 동기화 — `run_job`의 12분 창에 비해 오차가 작음).

**한계**: 키가 아예 없으면 스케줄러가 바로 종료(코드 1)해 배포 직후 점검에서 드러나지만, 키가 틀려(401) 쓰기가 계속 실패하거나 M1이 적용되지 않았을 땐 죽지 않고 1분마다 경고만 남김 — 이때는 `/health/deep`의 db 검사 · 감시가 잡음. 동기 단일 스레드라 한 작업이 오래 걸리면 같은 틱의 하트비트 · 다른 작업이 밀림(3차 핸들러에 시간 상한을 둘 것).

## 컨테이너 (`Dockerfile`)

api와 scheduler가 같은 이미지를 쓰고 시작 명령만 다름. 빌드 컨텍스트는 `api/`: `docker build -f api/Dockerfile api`.
- api: 기본 CMD `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log` (포트 8000)
- scheduler: compose가 `python -m app.scheduler`로 덮어씀. 아래 "스케줄러" 절. `SUPABASE_SECRET_KEY`가 없으면 바로 종료(코드 1) → 배포 직후 점검("running · 재시작 0")에서 드러남
- 비밀값은 이미지에 없음(실행할 때 env로), 비루트 사용자(uid 10001), 개발 도구 없음. 이미지 안 `python`이 PATH에 있음(compose 헬스체크가 씀)

## 개발

```bash
cd api
uv sync             # Python 3.12 + 라이브러리 (uv.lock 그대로)
uv run pytest       # 시험
uv run ruff check . && uv run ruff format --check .
```

로컬 Supabase · Edge 함수와 함께 띄우고 로컬 토큰으로 `/me`를 확인하는 순서(`compose.local.yml` 포함)는 [`docs/local-dev.md`](../docs/local-dev.md)에 있음.

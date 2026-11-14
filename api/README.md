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
| `SUPABASE_SECRET_KEY` | `/health/deep`에 | 서버 전용 Supabase 키(`sb_secret_…`). PostgREST로 `server_heartbeat`를 읽음. 없으면 deep의 db 검사가 실패 |
| `JWT_AUDIENCE` | 아니오 | 기본 `authenticated` |
| `JWT_LEEWAY_SECONDS` | 아니오 | 시계 어긋남 허용, 기본 10 |
| `JWKS_CACHE_TTL_SECONDS` · `JWKS_MIN_REFETCH_SECONDS` · `HTTP_TIMEOUT_SECONDS` | 아니오 | 기본 600 · 10 · 3 |
| `HEALTH_REQUIRE_SCHEDULER` · `SCHEDULER_STALE_MINUTES` | 아니오 | 스케줄러(S3) 배포 뒤 `true`로. 켜면 `server_heartbeat.alive_at`이 15분 넘게 멈추면 deep 실패 |
| `APP_VERSION` | 아니오 | 배포 태그(`sha-…`). compose가 api · scheduler에 넣어 줌. `/health`의 `version`, 없으면 `dev`. `/health`는 토큰 없이 부를 수 있으므로 **배포 태그 말고 다른 값은 넣지 않음** |
| `HEALTH_DEEP_CACHE_SECONDS` | 아니오 | `/health/deep` 결과 재사용 시간(초), 기본 5, **0~60만 받음**. 0은 캐시만 끔(확인은 한 번에 하나씩 순서대로 함) |
| `LOG_LEVEL` | 아니오 | 기본 `INFO` (대소문자 무관, DEBUG · INFO · WARNING · ERROR · CRITICAL) |

## 컨테이너 (`Dockerfile`)

api와 scheduler가 같은 이미지를 쓰고 시작 명령만 다름. 빌드 컨텍스트는 `api/`: `docker build -f api/Dockerfile api`.
- api: 기본 CMD `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log` (포트 8000)
- scheduler: compose가 `python -m app.scheduler`로 덮어씀. **지금은 자리표시자** — 작업 없이 떠 있다가 SIGTERM에 정상 종료. 실제 작업(`alive_at` 갱신 · wake-check · alarm-roll · 제안)은 S3(1-10)에서 구현
- 비밀값은 이미지에 없음(실행할 때 env로), 비루트 사용자(uid 10001), 개발 도구 없음. 이미지 안 `python`이 PATH에 있음(compose 헬스체크가 씀)

## 개발

```bash
cd api
uv sync             # Python 3.12 + 라이브러리 (uv.lock 그대로)
uv run pytest       # 시험
uv run ruff check . && uv run ruff format --check .
```

로컬 Supabase · Edge 함수와 함께 띄우고 로컬 토큰으로 `/me`를 확인하는 순서(`compose.local.yml` 포함)는 [`docs/local-dev.md`](../docs/local-dev.md)에 있음.

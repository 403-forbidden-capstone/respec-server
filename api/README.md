# api — 앱 요청 처리 서버 (FastAPI)

앱이 부르는 서버. 로그인 토큰을 직접 검사하고, 이후 Edge 함수를 하나씩 옮겨 받음. 구조 · 결정은 본 레포 `docs/서버_요구사항_설계_1112.md`.

## 지금 있는 것 (S1 · 완결점 1-4)

| 주소 | 하는 일 | 토큰 |
|---|---|---|
| `GET /health` | 프로세스가 떠 있는가만 | 없음 |
| `GET /health/deep` | 공개 키(JWKS) · DB(PostgREST) · 스케줄러 최근 실행. 하나라도 실패면 503 | 없음 |
| `GET /me` | 토큰 주인의 `{"id": ...}` | 필요 |
| `OPTIONS *` | CORS 사전 확인. 토큰 없이 204 | 없음 |

`/docs` · `/redoc` · `/openapi.json`은 꺼 둠(404).

## 토큰 검사 (`app/core/auth.py` · `jwks.py`)

웹 비유: 로그인 토큰 = 도장 찍힌 편지, 공개 키 목록(JWKS) = 도장 원본 모음. 서버가 목록을 받아 두고 요청마다 도장을 대조함 → Supabase Auth에 요청마다 물어보지 않아 빠름.

| 상황 | 결과 |
|---|---|
| 정상 토큰 (ES256 · 만료 전 · 발급자 · 대상 · `role=authenticated`) | 통과 |
| 헤더 없음 · Bearer 아님 | 401 `AUTH_REQUIRED` "로그인이 필요해요" |
| 깨진 문자열 · 만료 · `anon` · `service_role` · HS256 위조 · `alg: none` · 모르는 `kid` · 다른 발급자 · 다른 대상 | 401 `AUTH_REQUIRED` "로그인이 만료됐어요…" |
| 공개 키 목록을 못 받고 캐시도 없음 | 503 `AUTH_UNAVAILABLE` (401이면 앱이 사용자를 로그아웃시키므로 503) |
| 공개 키 목록을 못 받지만 캐시 있음 | 낡은 캐시로 계속 검사 |

- 캐시: 10분(`JWKS_CACHE_TTL_SECONDS`)이 지나면 새로 받아 봄. 모르는 `kid`는 키 교체일 수 있어 한 번 새로 받되, 30초(`JWKS_MIN_REFETCH_SECONDS`) 안에서는 다시 안 받음(아무 `kid`나 보내 두드리는 것 방지).
- **알려진 한계 (기록)**: DB · Auth에 안 물으므로 **탈퇴(삭제)한 계정의 토큰도 만료(기본 1시간)까지는 통과**함. 되돌릴 수 없는 쓰기(계정 삭제 등)는 그 함수에서 Supabase Auth에 한 번 더 확인할 것(설계 S4, 2차 이후). `tests/test_auth.py::test_deleted_account_token_passes_until_expiry`가 이 동작을 고정해 둠.

## 오류 모양 (`app/core/errors.py` · `http.py`)

본 레포 `supabase/functions/_shared/errors.ts`와 같음: `{"error": {"code", "message", "details"?}}`, 코드 14개 · 상태 번호 동일(`tests/test_errors_http.py`가 표를 대조). 서버에만 있는 코드는 `AUTH_UNAVAILABLE`(503) 하나.
- FastAPI 기본 404 · 405 · 422도 이 모양으로 바뀜. 422는 Edge와 같이 `VALIDATION_FAILED` 400이고, 입력값(좌표 · 검색어)은 되돌려 주지 않고 위치와 종류만 줌.
- 예상 못 한 오류는 500 `INTERNAL` "잠시 후 다시 시도해 주세요". 원문은 로그에만(요청 번호와 함께).
- 모든 응답(오류 포함)에 CORS 헤더와 `X-Request-Id`. 앱이 보낸 값은 `^[A-Za-z0-9_-]{1,64}$`일 때만 그대로 쓰고 아니면 새 UUID.
- Edge와 다른 점: `OPTIONS`가 200이 아니라 204, 허용 메서드에 `GET` 추가(`/me` · `/health`).

## 로그에 안 남기는 것

토큰 · 키 · 좌표 · 장소 검색어 · 푸시 토큰 · 요청 본문 · 쿼리 문자열. 로그 한 줄은 `request_id · method · path · status · ms`만. `tests/`가 토큰 · 쿼리 문자열 · 입력값이 로그와 응답에 없는지 확인함.

## 설정 (환경 변수)

값은 서버의 env 파일에만 둠(코드 · 레포 · 로그에 없음). 이름과 뜻:

| 이름 | 필수 | 뜻 |
|---|---|---|
| `SUPABASE_URL` | 예 | Supabase 프로젝트 주소. 로컬은 `http://127.0.0.1:54321` |
| `SUPABASE_JWT_ISSUER` | 아니오 | 토큰의 `iss`가 `SUPABASE_URL/auth/v1`과 다를 때만 |
| `SUPABASE_JWKS_URL` | 아니오 | 공개 키 목록 주소가 기본(`SUPABASE_URL/auth/v1/.well-known/jwks.json`)과 다를 때만 (로컬 compose: 컨테이너가 호스트의 Supabase를 부르는 주소) |
| `SUPABASE_SECRET_KEY` | `/health/deep`에 | 서버 전용 Supabase 키(`sb_secret_…`). PostgREST로 `server_heartbeat`를 읽음. 없으면 deep의 db 검사가 실패 |
| `JWT_AUDIENCE` | 아니오 | 기본 `authenticated` |
| `JWT_LEEWAY_SECONDS` | 아니오 | 시계 어긋남 허용, 기본 10 |
| `JWKS_CACHE_TTL_SECONDS` · `JWKS_MIN_REFETCH_SECONDS` · `HTTP_TIMEOUT_SECONDS` | 아니오 | 기본 600 · 30 · 3 |
| `HEALTH_REQUIRE_SCHEDULER` · `SCHEDULER_STALE_MINUTES` | 아니오 | 스케줄러(S3) 배포 뒤 `true`로. 켜면 `server_heartbeat.alive_at`이 15분 넘게 멈추면 deep 실패 |
| `LOG_LEVEL` | 아니오 | 기본 `INFO` |

## 개발

```bash
cd api
uv sync             # Python 3.12 + 라이브러리 (uv.lock 그대로)
uv run pytest       # 시험
uv run ruff check . && uv run ruff format --check .
SUPABASE_URL=http://127.0.0.1:54321 uv run uvicorn app.main:create_app --factory --port 8000 --no-access-log
```

로컬 Supabase와 함께 띄우는 법(compose · 로컬 토큰으로 `/me`)은 완결점 1-5 PR에서 추가함.

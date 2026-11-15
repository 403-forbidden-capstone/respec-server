# 로컬 개발 환경 (완결점 1-5)

개발 PC에서 **서버 + 로컬 Supabase + Edge 함수**를 처음부터 띄우고, 로컬 로그인 토큰으로 서버 `/me`가 200인 것까지 확인하는 순서임.
운영 배포(EC2 · compose · nginx)는 이 문서가 아니라 배포 길 문서(`deploy/`)를 봄. 이 문서의 어떤 단계도 운영 DB · 운영 키를 쓰지 않음.

## 뜨는 것

| 이름 | 어디서 | 호스트 포트 | 비고 |
|---|---|---|---|
| 서버(api) | `uv`로 직접, 또는 `compose.local.yml`(운영과 같은 이미지) | 28000 | 둘 중 하나만 (같은 포트) |
| scheduler | `compose.local.yml` | 없음 | 하트비트 · 스위치 표 읽기. 로컬 표는 전부 `edge`라 하트비트만 씀 (`api/README.md` "스케줄러") |
| 로컬 Supabase (DB · 로그인 · API 게이트웨이 · 실시간 · 스토리지) | Docker, 이 레포의 `dev/stack-config.toml` | API 28321 · DB 28322 | 본 레포 스택과 별개 |
| Edge 함수 | `supabase functions serve` | 28321(`/functions/v1/…`) | 본 레포 `supabase/functions` 복사본 |

본 레포(`respec-alarm-capstone`)의 로컬 스택(`project_id = respec-alarm`, 포트 54321 · 54322)과 **이름 · 포트가 안 겹쳐서 동시에 켜 둘 수 있음**. 이 문서의 명령은 본 레포 스택(컨테이너 이름 `…_respec-alarm`)을 건드리지 않음.

왜 따로 띄우는가
- Windows는 54236~54435 같은 TCP 구간을 예약해서 Supabase 기본 포트(54321 · 54322)가 안 열리는 PC가 있음 (`netsh interface ipv4 show excludedportrange protocol=tcp`로 확인)
- 운영 Supabase는 로그인 토큰을 **ES256**(공개 키 목록 JWKS)으로 서명함. 서버의 토큰 검사도 ES256만 받으므로, 로컬 Supabase도 ES256 키로 토큰을 만들게 설정함 (`dev/local_stack.py`가 키를 만듦. 기본 로컬 설정의 HS256 토큰은 서버가 거절함)

## 준비물

- Docker Desktop (켜 둔 상태). 켜면 이전에 쓰던 다른 컨테이너가 같이 켜질 수 있음
- Node.js (`npx`) — Supabase CLI를 `npx supabase@2.119.0`으로 부름 (본 레포 README와 같은 버전)
- `uv` (서버용 Python 3.12는 uv가 받아 줌)
- Python 3.10 이상 (`python` 또는 `python3`) — `dev/local_stack.py`를 돌림. 표준 라이브러리만 써서 설치할 것은 없음
- 본 레포(`respec-alarm-capstone`)를 **최신 main으로 받은** 폴더 — 마이그레이션 · 함수 복사에 씀. 오래된 상태면 `server_heartbeat` 같은 새 표가 없어 `/health/deep`이 실패함

명령 블록은 bash(Git Bash · macOS · Linux) 기준임. Windows PowerShell 5.1에서는 `&&`를 `;`로, `grep`을 `Select-String`으로 바꿔 씀(`python`을 못 찾으면 `py`).

## 순서 (처음부터)

아래는 서버 레포 루트에서 실행함. `<본 레포>`는 본 레포 폴더 경로(예: `../respec-alarm-capstone`).

**1. 서버 시험** — Docker 없이 되는 것부터 확인
```bash
cd api && uv sync && uv run pytest && cd ..
```

**2. 격리 Supabase 작업 폴더 만들기**
```bash
python dev/local_stack.py prepare --main-repo <본 레포>
```
`dev/.stack/`에 본 레포의 `supabase/migrations` · `supabase/functions`를 복사하고(`.env` 같은 비밀값 파일은 빼고), `dev/stack-config.toml`을 설정으로 복사한 뒤 **본 레포 `supabase/config.toml`의 `[functions.*]`(함수별 `verify_jwt`)를 읽어 끝에 붙이고**, ES256 서명 키(`signing_keys.json`)를 만듦. 끝나면 어느 본 레포 브랜치 · 커밋에서 복사했는지 한 줄로 알려 줌. `dev/.stack/`은 깃에 안 들어감(키가 들어 있음). 본 레포가 바뀌면 이 명령을 다시 실행해 복사본을 갱신함(키는 그대로 둠).

**3. 띄우기** — 이미지를 처음 받을 때는 몇 분, 받아 둔 뒤에는 30초 남짓(실측 34초)
```bash
npx supabase@2.119.0 --workdir dev/.stack start
```
끝나면 마이그레이션 전부가 적용된 상태로 `API_URL`이 `http://127.0.0.1:28321`로 나옴.

**4. 서버용 env 파일 만들기**
```bash
python dev/local_stack.py env
```
`dev/.stack/api.env`가 생김(로컬 키가 들어 있어 깃에 안 들어감, 값은 화면에 안 찍음). 서버가 이 파일로 로컬 Supabase를 가리킴.

**5. 서버 띄우기 — 방법 A (uv로 직접, 개발 중 기본)**
```bash
cd api
uv run --env-file ../dev/.stack/api.env uvicorn app.main:create_app --factory --host 127.0.0.1 --port 28000 --no-access-log
```

**5. 서버 띄우기 — 방법 B (운영과 같은 이미지, compose)** — A를 켜 뒀다면 먼저 끔
```bash
docker compose -f compose.local.yml up -d --build
docker compose -f compose.local.yml ps          # api: healthy, scheduler: Up
```
컨테이너 안의 서버는 호스트의 로컬 Supabase를 `host.docker.internal:28321`로 부르고, 토큰의 발급자(`iss`)는 호스트에서 본 주소(`http://127.0.0.1:28321/auth/v1`) 그대로 검사함(`compose.local.yml`의 주석 참고).

**6. 로컬 토큰으로 확인**
```bash
python dev/local_stack.py check --api-url http://127.0.0.1:28000
```
시험 계정(`server-dev@example.test`, 로컬 스택 안에서만 존재)으로 로컬 로그인해 얻은 토큰으로 서버를 부름. 통과 기준:

| 확인 | 기대 |
|---|---|
| `GET /health` | 200 |
| `GET /health/deep` | 200 — 서버 키로 로컬 DB의 `server_heartbeat`를 읽음 |
| `HEAD /health/deep` | 200 (감시가 HEAD로 부르는 경우) |
| `GET /me` (로컬 토큰) | 200, 응답의 `id` = 로그인한 사용자 id |
| `GET /me` (헤더 없음) | 401 |
| `GET /me` (서명 훼손) | 401 |
| `OPTIONS /me` (토큰 없이) | 204 |

토큰 · 키 값은 화면에 안 찍힘(상태 코드 · id 일치 여부, 토큰의 `alg` · `kid` 유무 · `iss`, `/health/deep` 응답 본문 앞부분만). `--api-url`은 로컬 주소(`127.0.0.1` · `localhost`)만 받음 — 로컬 시험 토큰을 다른 주소로 보내지 않으려는 것임.

**7. Edge 함수 띄우기** — 새 터미널에서 (계속 떠 있음, 끝낼 때 Ctrl-C)
```bash
npx supabase@2.119.0 --workdir dev/.stack functions serve
```
함수가 쓰는 비밀값(카카오 · Anthropic 키 등)은 본 레포 `supabase/functions/.env.example`을 보고 `--env-file <파일>`로 넘김(없으면 해당 함수만 `NOT_CONFIGURED`). 확인 예:
- 토큰 없이 `POST http://127.0.0.1:28321/functions/v1/place-search` → 401 `AUTH_REQUIRED`

서버가 Edge를 부르는 시험(스위치 `edge`/`shadow`/`server` 경우 시험 등)은 **이 단계를 먼저** 해 둘 것 — 꺼져 있으면 Edge 호출이 실패함. 로컬 토큰으로 함수 안까지 부르는 확인은 `dev/local_stack.py`의 `_local_user_token`(시험 계정 로그인)을 쓰는 코드로 함. 토큰을 화면에 찍는 명령은 일부러 만들지 않았고, 시험 계정 여러 명 · 시드가 필요한 계약 시험(Edge와 서버 결과 대조)은 `dev/contract/`에 있음 — 사용법은 `dev/contract/README.md`.

**8. 끝내기**
```bash
docker compose -f compose.local.yml down                       # B를 썼다면
npx supabase@2.119.0 --workdir dev/.stack stop                 # DB 데이터 유지 (다음에 start하면 이어짐)
npx supabase@2.119.0 --workdir dev/.stack stop --no-backup     # DB 데이터까지 삭제 (완전히 처음부터 하고 싶을 때)
```
`dev/.stack/`을 지우면 서명 키도 지워지며, 다시 `prepare`하면 **새 키**가 만들어짐(이전에 받은 로컬 토큰은 무효 — 다시 로그인하면 됨).

## 알아 둘 것

- 서버의 토큰 검사는 ES256 + JWKS(`/auth/v1/.well-known/jwks.json`)만 받음. 로컬도 같은 경로를 씀
- 로컬 토큰을 받는 `iss`가 서버 설정의 `SUPABASE_JWT_ISSUER`와 다르면 401임. `dev/.stack/api.env`가 `http://127.0.0.1:28321/auth/v1`로 맞춰 줌
- `/health/deep`은 GET과 HEAD 모두 받음(같은 5초 캐시). `/health`도 마찬가지. `/me`는 GET만
- Edge의 `OPTIONS`는 200 `ok`, 이 서버는 204임. 브라우저 사전 요청은 2xx면 통과하므로 앱에는 영향 없음
- 서버 로그에는 토큰 · 키 · 좌표 · 검색어 · 쿼리 문자열이 안 남음 (`api/README.md`의 "로그에 안 남기는 것"). 확인: `docker compose -f compose.local.yml logs | grep -E "eyJ|sb_secret|Bearer"` → 0건
- 이 문서의 로컬 스택은 비어 있는 DB로 시작함(시드 없음). 시드는 본 레포가 따로 관리함(`supabase/seed/`)

## 막혔을 때

| 증상 | 원인 · 처리 |
|---|---|
| `start`가 `port is already allocated` · `bind: An attempt was made to access a socket…` | 28321 · 28322 · 28320 · 28383 중 하나(또는 서버 포트 28000)를 다른 프로그램이 씀. 포트를 바꾸려면 `dev/stack-config.toml`의 해당 포트 · `dev/local_stack.py`의 `API_PORT`(API 게이트웨이) 또는 `SERVER_PORT`(서버) · `compose.local.yml`(`28321` 두 곳, `127.0.0.1:28000:8000`)을 **같이** 바꾸고, 이 문서의 `--port` · `--api-url`도 맞춘 뒤 `prepare` → `start` → `env`를 다시 실행. 하나라도 어긋나면 `uv run pytest`(`tests/test_dev_stack.py`)가 알려 줌 |
| `prepare`가 `…/migrations 가 없음` | `--main-repo`가 본 레포의 **최상위 폴더**가 아님 (`supabase/` 폴더가 그 안에 있어야 함) |
| `check`에서 `/health/deep`만 503 | 본 레포가 오래돼 `server_heartbeat` 표가 없음 → 본 레포를 `git pull`한 뒤 `prepare` → `stop --no-backup` → `start` |
| `check`에서 `/me`가 401 | 토큰의 `iss`가 서버 설정(`SUPABASE_JWT_ISSUER`)과 다름 — 포트를 바꿨다면 `python dev/local_stack.py env`를 다시 실행하고 서버를 다시 시작. 또는 서명 키를 새로 만든 직후: 서버가 새 키를 알아보는 데 최대 10초(공개 키 목록 재요청 간격)가 걸리고, 키를 만들기 전에 받은 옛 토큰은 무효이므로 다시 로그인(`check`는 매번 새로 로그인함) |
| `check`가 "로컬 스택이 안 떠 있음" | 3단계 `start`가 안 끝남. `docker ps --filter name=respec-server-dev`로 확인 |
| 컨테이너(B)에서 `/health/deep`만 실패 | 컨테이너 안에서 `host.docker.internal`이 안 풀림. Docker Desktop은 자동으로 풀지만 Linux 엔진은 `extra_hosts: host.docker.internal:host-gateway`가 있어야 함(`compose.local.yml`에 이미 있음). 그래도 안 되면 `docker compose -f compose.local.yml exec api python -c "import urllib.request as u; print(u.urlopen('http://host.docker.internal:28321/auth/v1/.well-known/jwks.json').status)"`로 컨테이너에서 직접 확인 |
| Docker가 느리고 디스크가 큼 | `docker system df`로 확인. 이 스택을 쓰지 않을 때는 `stop --no-backup` |

## 용어

- **ES256 · JWKS**: 로그인 토큰(JWT)을 서명하는 방식과 그 공개 키 목록 주소. 서버는 비밀 키 없이 공개 키로 "이 토큰이 Supabase가 발급한 게 맞나"만 검사함
- **`iss`(발급자)**: 토큰 안에 적힌 "누가 발급했는가". 서버 설정과 같아야 통과
- **격리 스택**: 본 레포 로컬 스택과 이름 · 포트가 다른 별도 Supabase. 둘을 동시에 켜도 안 부딪힘
- **`--workdir`**: Supabase CLI에게 "설정 · 마이그레이션이 이 폴더의 `supabase/` 안에 있다"고 알려 주는 옵션
- **`functions serve`**: 로컬에서 Edge 함수를 실행하는 명령(배포 없이 코드를 바로 시험)

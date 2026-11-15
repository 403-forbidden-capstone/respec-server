# 계약 시험 — 로컬 Edge vs 로컬 서버 대조

같은 시작 상태에서 같은 요청을 **본 레포 Edge 함수**와 **이 레포 서버(Python)**에 보내 응답과 DB 행이 같은지 견줍니다. 서버 전환(완결점 2-4 · 3-1 · 3-3 · 3-4 · 3-5)의 "Edge와 같은 결과" 증거용 도구이고, 로컬 격리 스택에서만 돕니다(운영 DB · 운영 키 · 진짜 Expo는 안 부름).

```text
경우 하나 = (시작 상태 SQL, 부르는 사람, 함수, 요청)
  [Edge 쪽] DB 초기화 → 시작 상태 → 호출 → (푸시 거둠) → DB 덤프
  [서버 쪽] 같은 순서 → 정규화한 (상태 · 본문 · DB 덤프 · 보낸 푸시)를 견줌
  Edge · 서버를 번갈아 먼저 돌림
```

## 안전장치

- 연결은 `127.0.0.1`과 격리 스택 컨테이너(`supabase_db_respec-server-dev`)뿐. 경우마다 데이터 표를 `truncate`하므로 **이 스택이 아닌 DB에 쓰지 말 것**.
- 시험 계정(`contract-{a,b,c}@example.test`)은 고정 id로 격리 스택 안에만 만듦. 비밀번호도 로컬 전용 값.
- Edge · 서버 코드는 안 고침. Expo 주소만 실행 때 가짜 서버로 바꿔 끼움(아래).

## 준비 (순서대로)

[docs/local-dev.md](../../docs/local-dev.md)의 `prepare` → `start` → `env`까지 한 뒤:

| 할 일 | 명령 (서버 레포 맨 위) |
|---|---|
| `env`가 `api.env`에 안 쓰는 키 더하기 | `dev/.stack/api.env`에 `SUPABASE_PUBLISHABLE_KEY=<로컬 anon 키>`를 한 줄 더함 (없으면 서버가 `NOT_CONFIGURED` 503) |
| 가짜 Expo 서버 (28999) | `python dev/contract/fake_expo.py` |
| Edge 사본의 Expo 주소를 가짜로 | `python dev/contract/patch_edge_expo.py` (`dev/.stack` 안 사본만 고침. `prepare`를 다시 하면 되돌아가니 그 뒤엔 다시 실행) |
| Edge 띄우기 | `npx supabase@2.119.0 --workdir dev/.stack functions serve` |
| 서버 띄우기 | `api/`에서 `.stack/api.env`를 환경 변수로 넣고 `python ../dev/contract/serve_server.py` (28000. uvicorn과 같고 Expo 주소만 가짜) |

Edge는 도커 안에서 돌아 가짜 서버를 `host.docker.internal:28999`로 부름.

## 실행

```bash
python dev/contract/run.py                          # 전부 (7그룹 · 230경우, 5분쯤)
python dev/contract/run.py --group wake,editpush    # 그룹만
python dev/contract/run.py --only "토큰 없음"        # 이름에 글자가 든 경우만
python dev/contract/run.py --out 결과.json           # 경우별 요약(응답 · 보낸 푸시)과 다른 곳을 저장
```

| 그룹 | 대상 | 경우 |
|---|---|---|
| `suggest` | alarm-suggest (미리보기) | 41 |
| `commit` | alarm-commit (저장) | 47 |
| `roll` | alarm-roll (본인 길 · 배치) | 24 |
| `edit` | appointment-edit (수정 · 삭제 · 복원 · 나가기) | 50 |
| `wake` | wake-check — 본인 미확인 · 친구 '깨워 볼까요?' · 후속 '일어났어요' | 30 |
| `editpush` | appointment-edit 공동 약속 푸시 — 바뀜 · 취소 · 조용한 푸시 · 확인 필요 | 20 |
| `suggestgen` | suggestion-generate (후보 · 만든 수 · 건너뛴 수 · 두 번째 실행) | 18 |

출력: `같음` / `다름` + `[Edge 상태/서버 상태 행:표별 행 수 푸시:보낸 수 응답:…]`. 다른 경우는 다른 곳을 `$.body.…` 경로로 최대 12줄 보여 줌.

## 무엇을 견주나

- 응답 상태 · 본문, DB 표(`appointments` · `appointment_participants` · `alarms` · `user_edit_logs` · `roll_attempts` · `groups` · `group_members` · `push_sends` · `alarm_events` · `push_tokens` · `push_tickets` · `alarm_suggestions`)의 행.
- 푸시: 가짜 Expo가 받은 메시지(받는 토큰 · 제목 · 본문 · 채널 · 데이터 · 조용한 푸시 여부)를 내용순으로. 호출마다 따로 거둠.
- 뺀 것: `request_id` · 행 id(새로 생긴 것은 `<새N>`으로 번호 매김) · 만든 · 고친 · 보낸 시각 · 초대 코드. 이 값들은 같은지 못 봄.
- 예약 작업(wake-check · suggestion-generate)은 서버에 HTTP 길이 없어 `server_job.py`가 스케줄러가 부르는 본문(`app/jobs_server.py`)을 지금 시각으로 한 번 돌리고 Edge 응답과 같은 모양의 JSON을 견줌. Edge는 서비스 키로 HTTP 호출.
- `then=[…]`: 같은 상태에서 이어 부름(두 번 돌림 · 그 사이 DB를 바꾸는 `("$sql", None, "SQL")`).

## 못 덮는 것

- **카카오 길찾기 경로**: Edge의 호출 주소가 코드에 박혀 있어 가짜로 못 바꿈. 이동시간은 직접 입력 · 근사 · 저장된 값만 시험함.
- **현재 시각을 못 바꿈**: 시작 상태를 지금 기준 상대 시각으로 만들고 경계(30분 · 10분 · 5분)에서 충분히 떨어진 값을 씀. 자정 가까이(23:50 이후)는 친구 알림 경우가 안 맞을 수 있음. 5분 지남 허용 경계 · 푸시 영수증(15분 뒤)은 시험 안 함.
- 진짜 Expo · FCM 전달, 운영 게이트웨이의 응답 본문 모양, 동시 호출, 실행 시간.
- `suggestion-generate`의 `skipped`(동시에 겹쳐 돌 때만 생김).

## 알려진 차이

- `토큰 없음(401)`: `verify_jwt = true`인 함수(alarm-suggest · alarm-commit · appointment-edit)는 Edge 게이트웨이가 본문(`{code: UNAUTHORIZED_NO_AUTH_HEADER, message, msg}`)을 만들고, 서버는 `{error: {code: AUTH_REQUIRED, message}}`. 상태는 둘 다 401이고 앱(`mobile/src/lib/edge.ts`)은 상태로 `AUTH_REQUIRED`를 골라 영향 없음.

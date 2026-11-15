# 팀원 서버 공유 안내

우리 팀 서버 `api.respecalarm.com`(AWS EC2 `respec-server`, IP 3.38.123.6)을 팀원이 보는 길은 네 가지. 위 셋은 지금 바로 되고, 서버 안(SSH)은 서버가 안정된 뒤 공용 열쇠를 카톡방에 올리면 열림.

| 무엇을 | 어떻게 | 권한 |
|---|---|---|
| 서버가 살아 있나 | 브라우저로 https://api.respecalarm.com/health/deep — 로그인 없음, 계정 · 키 값은 안 나옴(허용 계정은 개수만) | 누구나 |
| 서버 코드 · 배포 | 이 레포(`respec-server`). main에 머지하면 자동 배포, 실패하면 직전 판으로 자동 되돌림. 구조 · 되돌리기 · 측정값은 [README](../README.md) | 팀원 4명 모두 관리자(조직 멤버) |
| 기능을 서버 ↔ Edge로 돌리기 | Supabase SQL 편집기에서 표 `server_routes` 한 줄. SQL은 [`runbook.md`](runbook.md) | 팀원 4명 모두 Supabase Owner |
| 서버 안(로그 · 컨테이너 상태) | 팀 공용 열쇠 **`respec-share`**로 SSH — 아래 1 ~ 2절 | 열쇠를 받은 사람(관리자 계정 `ubuntu`, sudo 가능) |

---

## 1. 열쇠 파일 받기 (1분)

- 팀 카톡방에 올라온 **`respec-share`**(확장자 없음) 파일을 내 PC에 저장
- 이 파일이 서버 문을 여는 열쇠 자체 → **카톡방 밖으로 보내지 않음** · 깃 · 드라이브 · 노션에 올리지 않음
- 비밀번호(passphrase)는 없음. 연결할 때 묻지 않음

## 2. 연결 — 둘 중 편한 것 하나

| 방법 | 설치 | 좋은 점 |
|---|---|---|
| **A. Termius** | Termius 앱 | 버튼으로 연결 · 서버 목록 저장 |
| **B. 터미널** | 없음(Windows 10 이상 · Mac 기본 포함) | 명령 한 줄 |

### 2-A. Termius (3분)

1. 열쇠 넣기: 왼쪽 **Keychain** → **New key** → 받은 `respec-share` 파일을 창에 끌어다 놓기(또는 가져오기 버튼으로 파일 고르기) → 이름(Label) `respec-share` · 비밀번호(Passphrase) 칸은 **비워 둠** → 저장
   - Termius 공식 문서: [SSH keys and certificates](https://docs.termius.com/keychain/ssh-keys-and-certificates) (버튼 글자는 판마다 조금 다를 수 있음)
2. 서버 넣기: **Hosts** → **New Host** → 주소 `3.38.123.6` · 포트 `22` · 사용자 `ubuntu` · 열쇠 고르는 칸(Key)에서 `respec-share` → 저장
3. **Connect** → 처음 한 번 서버 지문을 물음 → 아래 [지문](#서버-지문)과 **같으면** 계속
- 끝: 검은 화면에 `ubuntu@ip-…:~$`

### 2-B. 터미널 (2분)

**Windows (PowerShell)**
1. 받은 파일을 `C:\Users\<내 이름>\.ssh\` 폴더로 옮김(폴더가 없으면 만듦)
2. 연결
   ```bash
   ssh -i $HOME\.ssh\respec-share ubuntu@3.38.123.6
   ```

**Mac (터미널)**
1. 받은 파일을 `~/.ssh/` 폴더로 옮긴 뒤 나만 읽게 바꿈(남도 읽을 수 있는 열쇠는 ssh가 거부함)
   ```bash
   chmod 600 ~/.ssh/respec-share
   ```
2. 연결
   ```bash
   ssh -i ~/.ssh/respec-share ubuntu@3.38.123.6
   ```

- 처음 한 번 `Are you sure you want to continue connecting?` → 화면의 `SHA256:…`이 아래 [지문](#서버-지문)과 같으면 `yes`
- 끝: `ubuntu@ip-…:~$` · 나가기: `exit`

## 서버 지문

처음 연결 때 나오는 값이 이것과 **다르면 접속하지 말고** 카톡방에 알림(가짜 서버 방지).
```
SHA256:J9UTl/dmixmzXqM1uGDKV3vIbiRQWNz+9K+GjAialIo
```

## 들어가서 볼 것 (읽기만)

| 보고 싶은 것 | 명령 |
|---|---|
| 컨테이너가 다 떠 있나 | `sudo docker ps` |
| api 최근 로그 50줄 | `sudo docker logs --tail 50 respec-api-1` |
| 스케줄러 최근 로그 50줄 | `sudo docker logs --tail 50 respec-scheduler-1` |
| 메모리 · 디스크 | `free -h` · `df -h /` |

컨테이너 이름이 다르면 `sudo docker ps`의 NAMES 칸 이름을 씀. 로그에는 토큰 · 키 값이 안 남게 만들어 둠.

---

## 관리자 계정이라 조심할 것

- **배포는 이 레포 main 머지로만**. 서버에서 컨테이너를 직접 끄거나 다시 켜지 않음(자동 배포 · 자동 되돌림이 꼬임)
- `/srv/respec/*.env`(서버 비밀값)는 열거나 복사하지 않음
- 패키지 설치 · 업그레이드 X (docker 판을 일부러 고정해 둠)
- 이상하면 그 화면을 카톡방에
- 모델 파일 올리기는 따로 안내

## 막히면

| 보이는 것 | 할 일 |
|---|---|
| `Permission denied (publickey)` | 사용자 이름이 `ubuntu`인지 · 열쇠 `respec-share`를 골랐는지(B는 `-i` 뒤 경로) 확인 |
| `UNPROTECTED PRIVATE KEY FILE` · `bad permissions` | Mac은 `chmod 600` 다시, Windows는 파일을 `C:\Users\<내 이름>\.ssh\`로 옮겨서 다시 |
| 지문이 위 값과 다름 | 접속 중단 → 카톡방에 그 화면 |
| `Connection timed out` | 학교 · 회사 와이파이가 22번 포트를 막는 경우 → 휴대폰 핫스팟으로 다시 |

## 열쇠 관리 (지형)

- 만들기: `ssh-keygen -t ed25519 -C "respec-share" -f "$HOME\.ssh\respec-share"` (passphrase 비움 — 같은 카톡방으로 나눠야 해서 효과 없음)
- 서버 등록: 공개 키 `respec-share.pub` 한 줄을 서버 `ubuntu` 열쇠 목록(`~/.ssh/authorized_keys`)에 추가
- 회수: 그 줄 하나를 지우면 팀원 접속이 전부 끊김. 원래 `.pem` 열쇠와 배포 열쇠(`deploy`)는 그대로

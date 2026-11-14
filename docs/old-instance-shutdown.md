# 옛 빵모아 인스턴스 종료 체크리스트

기록용 · 실행용. 순서를 지키고 줄마다 **[되돌림 가능]** / **[되돌릴 수 없음]** 을 확인한 뒤 지형님 OK 를 받아서 진행함.
옛 인스턴스는 고쳐 쓰지 않음(11/14 결정): 새 `respec-server` 를 처음부터 만들었고, 옛 것은 빵모아 사이트가 끊기는 날 통째로 종료함. **AMI 는 찍지 않음**(기록을 남기지 않는 방침).

옛 인스턴스: `i-0c8c4deb633c31df5` (t4g.small, 서울 ap-northeast-2a, Ubuntu 24.04), 컨테이너 3개(api · db · redis).
이 서버의 DB 는 비어 있었음(표 `flyway_schema_history` 하나, 행 0건) — 종료해도 잃는 사용자 데이터 없음. redis 는 키 4개짜리 캐시.

## 시작 조건 (전부 참이어야 함)

- [ ] 빵모아 사이트(`breadmoa.com`)가 서버 없이 도는 상태: Vercel 에서 `VITE_API_BASE` 삭제 + Redeploy 끝
- [ ] 배포된 화면 코드에 `api.breadmoa.com` 없음 (배포 JS 에서 문자열 검색)
- [ ] `breadmoa.com` 에서 관광 목록 · 지도 검색 · 길찾기 동작
- [ ] 로컬 백업이 있음: `C:\Users\user\backups\bbangmoa_1112\` (`MANIFEST.txt` 의 SHA-256 과 현재 파일이 같음)
- [ ] 지형님이 종료 날짜를 정하고 OK

## 순서

| # | 할 일 | 누가 | 되돌림 | 확인 |
|---|---|---|---|---|
| 1 | 빵모아 서버 레포의 배포 워크플로 끄기 (`gh workflow disable`, `vmgfh878-art/bbangmoa-server`) | 세션 | 가능 (enable) | 워크플로 상태 disabled |
| 2 | Cloudflare `breadmoa.com` → DNS → `api` 레코드 삭제 | 지형님 | 가능(레코드 다시 추가, 단 옛 IP 는 8 이후 못 돌려받음) | `dig api.breadmoa.com` 응답 없음 |
| 3 | (선택) 인스턴스 **중지(Stop)** 로 며칠 두기 — 중지 중에도 EBS 요금은 나감 | 지형님 | 가능(시작) | 상태 stopped, 사이트는 정상 |
| 4 | 인스턴스 **종료(Terminate)** — 종료 전에 루트 볼륨의 "종료 시 삭제"가 켜져 있는지 확인(기본 켜짐). 꺼져 있으면 볼륨을 따로 삭제 | 지형님 | **되돌릴 수 없음** (인스턴스 안 데이터 · docker 이미지 · 인증서 비밀키가 사라짐) | 상태 terminated, 볼륨 목록에 옛 것 없음 |
| 5 | 옛 **탄력적 IP 해제**(Release). 인스턴스에 붙은 탄력적 IP 가 있었으면 연결 해제 → 해제. 자동 할당 IP 였으면 종료 때 같이 사라짐 | 지형님 | **되돌릴 수 없음** (같은 IP 를 다시 못 받음) | 탄력적 IP 목록에 옛 IP 없음 |
| 6 | 옛 **보안 그룹** 삭제 (연결된 인스턴스가 없어야 지워짐, default 는 삭제 불가) | 지형님 | 되돌릴 수 없음 (규칙은 다시 만들면 됨) | 보안 그룹 목록에 옛 것 없음 |
| 7 | 옛 **키 페어** 삭제(콘솔) + PC 정리: `ssh-keygen -R 54.116.206.145`, `C:\Users\user\.ssh\bbangmoa-key.pem` 삭제 | 지형님 (PC 정리는 세션) | **되돌릴 수 없음** | 키 페어 목록에 옛 것 없음 |
| 8 | **AMI**: 지형님이 U5 에서 이미 `bbangmoa-final-1113` 을 만들었다면 등록 취소(Deregister) + 연결된 **스냅샷도 삭제**(둘 다 해야 요금이 멈춤). 만들지 않았다면 건너뜀 | 지형님 | **되돌릴 수 없음** | AMI · 스냅샷 목록 비어 있음 |
| 9 | 빵모아 레포 정리 (아래 "레포 정리" 순서) | 세션 · 지형님 | 보관만 가능 | |
| 10 | 로컬 정리: Docker Desktop 의 `bbangmoa-*` 컨테이너 · 볼륨 삭제, `bbangmoa-server/.env` 는 백업 폴더로 이동 후 삭제 | 지형님 · 세션 | 볼륨 삭제는 되돌릴 수 없음 | `docker ps -a`, `docker volume ls` |

종료 뒤 다음날 확인: AWS 결제 → Cost Explorer 에서 옛 인스턴스 · EBS · 스냅샷 · 탄력적 IP 항목이 더 안 늘어남.

## 레포 정리 (`vmgfh878-art/bbangmoa-server`) — 순서 지킴

보관(archive)하면 설정 변경이 막히므로 **마지막**.

1. README 맨 위에 운영 종료 기록 PR(B1): 기간(2026-09-03 ~ 종료일) · 구조 그림 · 배포 방식 · 배운 것 → 머지
2. GitHub Secrets 3개(`SSH_HOST` · `SSH_USER` · `SSH_KEY`) 삭제
3. GHCR `bbangmoa-server` 이미지(패키지) 삭제
4. 레포 보관(Settings → Archive this repository)

## 안 건드리는 것 (사이트가 계속 씀)

`breadmoa.com` 도메인 · Vercel 프로젝트 · 빵모아 Supabase · 빵모아 외부 API 키(관광공사 · 카카오 · ODSAY · TMAP). 사이트를 내릴 때 따로 정함.

## 끝 확인 (증거)

- `dig api.breadmoa.com` 응답 없음
- 콘솔 인스턴스 목록에서 옛 인스턴스 `terminated` (이후 목록에서 사라짐)
- 탄력적 IP · 보안 그룹 · 키 페어 · AMI · 스냅샷 목록에 옛 것 없음
- `breadmoa.com` 관광 목록 · 지도 검색 · 길찾기 동작
- 빵모아 서버 레포: README 종료 기록 머지 · Secrets 삭제 · 보관됨

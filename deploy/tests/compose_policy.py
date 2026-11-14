"""compose.yml 정책 점검. `docker compose config --format json` 출력을 표준입력으로 받음.

  python compose_policy.py <ml 프로필 켠 상태면 with-ml, 아니면 without-ml>
어기면 줄마다 설명을 출력하고 종료 코드 1.
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")  # 윈도우 콘솔 기본 인코딩(cp949)으로 한글이 깨지지 않게
mode = sys.argv[1] if len(sys.argv) > 1 else "with-ml"
cfg = json.load(sys.stdin)
services = cfg.get("services", {})
problems = []


def bad(msg):
    problems.append(msg)


expected = {"api", "scheduler"} | ({"prepare-ml"} if mode == "with-ml" else set())
if set(services) != expected:
    bad(f"서비스 목록이 {sorted(expected)} 이어야 함 (지금 {sorted(services)})")

MIB = 1024 * 1024
total = 0
for name, svc in services.items():
    for p in svc.get("ports", []):
        if p.get("host_ip") != "127.0.0.1":
            bad(f"{name}: 포트 {p.get('published')} 가 127.0.0.1 에만 열려 있지 않음 (host_ip={p.get('host_ip')!r})")
    for key in ("privileged", "network_mode", "cap_add", "pid", "ipc", "devices", "userns_mode"):
        if svc.get(key):
            bad(f"{name}: 허용하지 않는 옵션 {key}")
    if svc.get("restart") != "unless-stopped":
        bad(f"{name}: restart 는 unless-stopped 이어야 함")
    if "no-new-privileges:true" not in (svc.get("security_opt") or []):
        bad(f"{name}: security_opt no-new-privileges:true 없음")
    opts = (svc.get("logging") or {}).get("options") or {}
    if opts.get("max-size") != "10m" or opts.get("max-file") != "3":
        bad(f"{name}: 로그 크기 제한(10m x 3) 없음")
    mem = svc.get("mem_limit")
    if not mem:
        bad(f"{name}: mem_limit 없음")
    else:
        total += int(mem)
    image = svc.get("image", "")
    if not image.startswith("ghcr.io/403-forbidden-capstone/respec-"):
        bad(f"{name}: 이미지가 조직 GHCR 의 respec-* 가 아님 ({image})")
    if image.endswith(":latest") or ":" not in image.rsplit("/", 1)[-1]:
        bad(f"{name}: 이미지 태그를 고정해야 함 (latest 금지) ({image})")
    # 마운트는 prepare-ml 의 모델 폴더(읽기 전용)뿐
    for v in svc.get("volumes", []):
        if name != "prepare-ml":
            bad(f"{name}: 마운트 금지 ({v.get('target')})")
        elif not (v.get("read_only") and v.get("target") == "/app/models/v4_age_only"
                  and (v.get("bind") or {}).get("create_host_path") is False):
            bad(f"{name}: 마운트는 모델 폴더 읽기 전용(create_host_path: false) 하나뿐이어야 함 ({v})")
    # 컨테이너에 비밀을 직접 넣지 않음: environment 에는 정해진 이름만
    allowed_env = {"APP_VERSION", "CATBOOST_MODEL_DIR", "PORT"}
    extra = set(svc.get("environment") or {}) - allowed_env
    if extra:
        bad(f"{name}: environment 에 허용 목록 밖 이름 {sorted(extra)} (비밀은 env_file 로만)")

if total > 1126 * MIB:  # 1.1GiB
    bad(f"mem_limit 합계 {total // MIB}MiB 가 1.1GiB(1126MiB) 를 넘음")

if mode == "with-ml":
    ml = services.get("prepare-ml", {})
    if "ml" not in (ml.get("profiles") or []):
        bad("prepare-ml 은 profile ml 이어야 함")

if problems:
    print("compose 정책 위반:")
    for p in problems:
        print(" -", p)
    sys.exit(1)
print(f"compose 정책 통과 ({mode}, mem_limit 합계 {total // MIB}MiB)")

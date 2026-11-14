"""compose.yml 정책 점검. 서버에서 배포 직전(deploy.sh)과 CI 시험이 같이 씀.

  docker compose ... --profile '*' config --no-env-resolution --format json | python3 compose-policy.py --app-dir /srv/respec

왜 필요한가: compose 파일은 번들 이미지에서 꺼내 오는데, 배포 열쇠로 이미지를 올릴 수 있는 사람이 compose 를 바꿔
호스트 디스크를 마운트하거나 특권 컨테이너를 띄우면 서버 root 를 얻을 수 있음. 그래서 "허용 목록에 있는 모양"만 통과시킴.
(이미지 안의 코드는 이 점검이 못 막음 — api 이미지는 api.env 의 비밀을 읽을 수 있음. docs/server-setup.md 10절 참고)

--no-env-resolution 을 쓰는 이유: env_file 의 값을 풀어 JSON 에 넣지 않게(비밀 값이 이 점검기까지 오지 않게) 파일 경로만 남김.
어기면 줄마다 이유를 출력하고 종료 코드 1.
"""
import argparse
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")  # 윈도우 콘솔 기본 인코딩(cp949)으로 한글이 깨지지 않게

MIB = 1024 * 1024
REGISTRY = "ghcr.io/403-forbidden-capstone"

# 서비스마다 허용하는 것 (그 밖의 키 · 값은 전부 위반)
SERVICES = {
    "api": {
        "image": "respec-api",
        "env_file": "api.env",
        "environment": {"APP_VERSION"},
        "ports": {8000: 8000},
        "command": None,
        "profiles": None,
        "volumes": False,
    },
    "scheduler": {
        "image": "respec-api",
        "env_file": "api.env",
        "environment": {"APP_VERSION"},
        "ports": {},
        "command": ["python", "-m", "app.scheduler"],
        "profiles": None,
        "volumes": False,
    },
    "prepare-ml": {
        "image": "respec-prepare-ml",
        "env_file": "ml.env",
        "environment": {"CATBOOST_MODEL_DIR", "PORT"},
        "ports": {8091: 8091},
        "command": None,
        "profiles": ["ml"],
        "volumes": True,
    },
}

ALLOWED_KEYS = {
    "image", "restart", "env_file", "environment", "ports", "mem_limit", "stop_grace_period",
    "security_opt", "healthcheck", "logging", "command", "entrypoint", "profiles", "volumes",
    "networks", "init",
}
MAX_TOTAL_MEM = 1126 * MIB  # 1.1GiB (OS · Docker · nginx 몫 약 0.4GB 를 남김)


def norm(path):
    return os.path.normcase(os.path.abspath(path))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dir", required=True)
    args = ap.parse_args()
    app_dir = norm(args.app_dir)

    try:
        cfg = json.load(sys.stdin)
    except ValueError:
        print("compose 정책 위반:\n - config 출력을 JSON 으로 읽지 못함")
        return 1

    problems = []

    def bad(msg):
        problems.append(msg)

    # 최상위: name · services · networks(default) 와 x- 확장만
    for k in cfg:
        if k not in ("name", "services", "networks") and not k.startswith("x-"):
            bad(f"최상위 키 {k} 는 허용하지 않음 (volumes · secrets · configs 금지)")
    if cfg.get("name") != "respec":
        bad(f"프로젝트 이름이 respec 이어야 함 ({cfg.get('name')!r})")
    nets = cfg.get("networks") or {}
    if set(nets) - {"default"}:
        bad("네트워크는 compose 기본(default)뿐이어야 함")
    # compose 가 정규화한 기본 네트워크는 {"name": "respec_default", "ipam": {}} 하나. driver · driver_opts · ipam 내용 ·
    # external · internal 같은 다른 키가 붙으면 호스트 네트워크 쪽을 건드릴 수 있으므로 허용 목록(name · 빈 ipam)에 맞춤
    default_net = nets.get("default") or {}
    extra_keys = sorted(set(default_net) - {"name", "ipam"})
    if extra_keys:
        bad(f"기본 네트워크에 허용하지 않는 설정이 있음: {', '.join(extra_keys)}")
    if default_net.get("ipam"):
        bad("기본 네트워크의 ipam 은 비어 있어야 함")
    if default_net.get("name") not in (None, "respec_default"):
        bad(f"기본 네트워크 이름이 respec_default 이어야 함 ({default_net.get('name')!r})")

    services = cfg.get("services", {})
    if set(services) != set(SERVICES):
        bad(f"서비스 목록이 {sorted(SERVICES)} 이어야 함 (지금 {sorted(services)})")

    total = 0
    for name, svc in services.items():
        spec = SERVICES.get(name)
        if spec is None:
            continue
        svc = {k: v for k, v in svc.items() if v is not None}

        extra = set(svc) - ALLOWED_KEYS
        if extra:
            bad(f"{name}: 허용하지 않는 옵션 {sorted(extra)}")
        if "entrypoint" in svc:
            bad(f"{name}: entrypoint 재정의 금지")
        if svc.get("command") != spec["command"] and not (spec["command"] is None and "command" not in svc):
            bad(f"{name}: command 는 {spec['command']!r} 이어야 함 (지금 {svc.get('command')!r})")
        if svc.get("init") not in (None, True):
            bad(f"{name}: init 은 true 만 허용")

        # 이미지: 우리 GHCR 의 정해진 저장소 + 고정 태그
        image = svc.get("image", "")
        m = re.fullmatch(re.escape(REGISTRY) + r"/([a-z-]+):([A-Za-z0-9._-]+)", image)
        if not m or m.group(1) != spec["image"]:
            bad(f"{name}: 이미지는 {REGISTRY}/{spec['image']}:<태그> 이어야 함 ({image})")
        elif m.group(2) == "latest":
            bad(f"{name}: 이미지 태그 latest 금지")

        if svc.get("restart") != "unless-stopped":
            bad(f"{name}: restart 는 unless-stopped 이어야 함")
        if "no-new-privileges:true" not in (svc.get("security_opt") or []):
            bad(f"{name}: security_opt no-new-privileges:true 가 없음")
        if set(svc.get("security_opt") or []) - {"no-new-privileges:true"}:
            bad(f"{name}: security_opt 는 no-new-privileges:true 하나뿐이어야 함")
        opts = (svc.get("logging") or {}).get("options") or {}
        if (svc.get("logging") or {}).get("driver") != "json-file" or opts.get("max-size") != "10m" or opts.get("max-file") != "3":
            bad(f"{name}: 로그는 json-file 10m x 3 이어야 함")
        if set((svc.get("networks") or {}).keys()) - {"default"}:
            bad(f"{name}: 네트워크는 default 뿐이어야 함")

        # 메모리 상한
        mem = svc.get("mem_limit")
        if not mem:
            bad(f"{name}: mem_limit 없음")
        else:
            total += int(mem)

        # env_file: 서버 폴더의 정해진 파일 하나뿐
        env_files = svc.get("env_file") or []
        paths = [norm(e["path"] if isinstance(e, dict) else e) for e in env_files]
        if paths != [os.path.join(app_dir, spec["env_file"])]:
            bad(f"{name}: env_file 은 {spec['env_file']} 하나뿐이어야 함 (지금 {len(paths)}개)")

        # environment: 허용한 이름만 (비밀은 env_file 로만)
        env_names = set((svc.get("environment") or {}).keys())
        if env_names - spec["environment"]:
            bad(f"{name}: environment 에 허용 목록 밖 이름 {sorted(env_names - spec['environment'])}")

        # 포트: 127.0.0.1 에만, 정해진 포트 하나뿐
        want_ports = dict(spec["ports"])
        for p in svc.get("ports") or []:
            pub, tgt = int(p.get("published", 0)), int(p.get("target", 0))
            if p.get("host_ip") != "127.0.0.1":
                bad(f"{name}: 포트 {pub} 가 127.0.0.1 에만 열려 있지 않음 (host_ip={p.get('host_ip')!r})")
            if p.get("protocol", "tcp") != "tcp" or want_ports.get(pub) != tgt:
                bad(f"{name}: 허용하지 않는 포트 {pub}:{tgt}")
            want_ports.pop(pub, None)

        # 프로필
        if svc.get("profiles") != spec["profiles"] and not (spec["profiles"] is None and "profiles" not in svc):
            bad(f"{name}: profiles 는 {spec['profiles']!r} 이어야 함")

        # 마운트: prepare-ml 의 모델 폴더(읽기 전용)뿐
        vols = svc.get("volumes") or []
        if not spec["volumes"] and vols:
            bad(f"{name}: 마운트 금지")
        if spec["volumes"]:
            ok = (
                len(vols) == 1
                and vols[0].get("type") == "bind"
                and norm(vols[0].get("source", "")) == os.path.join(app_dir, "models", "v4_age_only")
                and vols[0].get("target") == "/app/models/v4_age_only"
                and vols[0].get("read_only") is True
                and (vols[0].get("bind") or {}).get("create_host_path") is False
            )
            if not ok:
                bad(f"{name}: 마운트는 {app_dir}/models/v4_age_only 를 /app/models/v4_age_only 에 읽기 전용(create_host_path: false)으로 붙이는 하나뿐이어야 함")

    if total > MAX_TOTAL_MEM:
        bad(f"mem_limit 합계 {total // MIB}MiB 가 1.1GiB(1126MiB) 를 넘음")

    if problems:
        print("compose 정책 위반:")
        for p in problems:
            print(" -", p)
        return 1
    print(f"compose 정책 통과 (mem_limit 합계 {total // MIB}MiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

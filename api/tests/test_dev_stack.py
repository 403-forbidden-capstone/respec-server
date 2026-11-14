"""로컬 개발 환경 파일들(dev/ · compose.local.yml · docs/local-dev.md)이 서로 어긋나지 않는지 — Docker 없이 파일만 읽어 확인"""

import importlib.util
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_local_stack():
    spec = importlib.util.spec_from_file_location("local_stack", REPO / "dev" / "local_stack.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config() -> dict:
    return tomllib.loads((REPO / "dev" / "stack-config.toml").read_text(encoding="utf-8"))


def test_script_port_matches_stack_config():
    assert _config()["api"]["port"] == _load_local_stack().API_PORT


def test_stack_ports_avoid_windows_reserved_ranges_and_other_stacks():
    config = _config()
    ports = [
        config["api"]["port"],
        config["db"]["port"],
        config["db"]["shadow_port"],
        config["studio"]["port"],
        config["local_smtp"]["port"],
        config["analytics"]["port"],
        config["edge_runtime"]["inspector_port"],
    ]
    assert len(set(ports)) == len(ports)  # 서로 안 겹침
    # Windows가 예약하는 54236~54435 · 54564~54763 구간과 본 레포 로컬 스택(5432x)을 피함
    assert all(port >= 20000 and not 54000 <= port < 55000 for port in ports)
    assert config["project_id"] != "respec-alarm"  # 본 레포 스택과 이름이 달라야 동시에 뜸


def test_stack_uses_es256_signing_keys_like_production():
    assert _config()["auth"]["signing_keys_path"] == "./signing_keys.json"


def test_compose_local_points_at_the_isolated_stack_port():
    text = (REPO / "compose.local.yml").read_text(encoding="utf-8")
    port = _config()["api"]["port"]
    assert f"http://host.docker.internal:{port}" in text
    assert "dev/.stack/api.env" in text
    assert "app.scheduler" in text  # scheduler도 같은 이미지에서 시작 명령만 바꿔 뜸


def test_generated_stack_folder_is_never_committed():
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "dev/.stack/" in ignored  # 서명 키 · 로컬 키가 들어 있음


def test_local_dev_doc_mentions_every_port_it_relies_on():
    doc = (REPO / "docs" / "local-dev.md").read_text(encoding="utf-8")
    config = _config()
    for port in (config["api"]["port"], config["db"]["port"], 28000):
        assert str(port) in doc

"""서버 쪽 프로세스에 넘기는 환경 변수를 허용 목록으로 만듦 (계약 시험 전용).

셸에 운영 값(KAKAO_REST_API_KEY · PREPARE_ML_* · SUPABASE_* 등)이 남아 있어도 서버 쪽만 그 값을 받아 진짜 카카오 · 모델 서버를 부르거나
운영 키가 섞이지 않게: 셸에서는 실행에 꼭 필요한 값(PATH · 임시 폴더 · 사용자 폴더 등)만 넘기고, 서버 설정은 로컬 스택 api.env에서만 가져옴.
api.env에 SUPABASE_PUBLISHABLE_KEY가 없으면(local_stack.py env가 안 씀) 로컬 스택 status에서 읽어 채움(파일은 안 고침).
"""

from __future__ import annotations

import functools
import os
from collections.abc import Mapping
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_ENV = HERE.parent / ".stack" / "api.env"

# 셸에서 그대로 넘기는 이름 (대문자 비교). 실행 · 임시 폴더 · 인코딩에만 쓰이고 비밀 값이 아님
ALLOWED_FROM_SHELL = frozenset(
    {
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
        "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "USERNAME", "USER",
        "LANG", "LANGUAGE", "TZ", "PYTHONIOENCODING", "PYTHONUTF8", "UV_CACHE_DIR",
        "CONTRACT_EXPO_BASE",  # 가짜 Expo 주소를 바꿀 때만 (기본 127.0.0.1:28999)
    }
)  # fmt: skip
PUBLISHABLE = "SUPABASE_PUBLISHABLE_KEY"


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


@functools.lru_cache(maxsize=1)
def _publishable_from_stack() -> str | None:
    """로컬 스택 `supabase status -o env`의 anon(publishable) 키 — 로컬 기본 키라 화면에 안 찍기만 하면 됨"""
    import sys

    sys.path.insert(0, str(HERE.parent))
    import local_stack as ls  # noqa: PLC0415

    status = ls._status()
    return status.get("PUBLISHABLE_KEY") or status.get("ANON_KEY")


def server_env(base: Mapping[str, str] | None = None, env_file: Path | None = None) -> dict[str, str]:
    """서버 프로세스용 환경: 셸의 허용 목록 값 + api.env. 그 밖의 셸 값은 모두 버림"""
    base = os.environ if base is None else base
    env = {k: v for k, v in base.items() if k.upper() in ALLOWED_FROM_SHELL or k.upper().startswith("LC_")}
    path = env_file or API_ENV
    if path.is_file():
        env.update(load_env_file(path))
    if not env.get(PUBLISHABLE):
        key = _publishable_from_stack()
        if key:
            env[PUBLISHABLE] = key
    return env


def scrub_process_env() -> None:
    """현재 프로세스(`uv run --env-file`로 뜬 서버 실행기)의 환경을 server_env로 바꿈 — 서버 코드를 불러오기 전에 부를 것"""
    clean = server_env()
    os.environ.clear()
    os.environ.update(clean)

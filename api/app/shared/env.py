"""본 레포 supabase/functions/_shared/env.ts의 서버판 (기준 b140f70) — 환경 변수 · 키 읽기

Edge는 Deno.env에서 SUPABASE_URL · SUPABASE_ANON_KEY · SUPABASE_SERVICE_ROLE_KEY(+ 새 키 JSON 사전)를 읽었지만,
서버는 `app/core/config.py` Settings(SUPABASE_URL · SUPABASE_PUBLISHABLE_KEY · SUPABASE_SECRET_KEY)가 이미 같은 일을 함 → 얇게 이어 줌.
  · get_env / require_env — Settings에 없는 선택 값(EXPO_ACCESS_TOKEN 등)을 os.environ에서 (Edge getEnv와 같게 앞뒤 공백 지움, 빈 값은 없음)
  · supabase_url / anon_key / service_keys / service_key — 설정에서
설정은 `configure_env(settings)`(앱 시작 때 supabase.configure가 부름)로 넣고, 안 넣었으면 처음 쓸 때 환경 변수에서 만듦.
"""

import os

from ..core.config import Settings
from ..core.errors import AppError

_settings: Settings | None = None


def configure_env(settings: Settings | None) -> None:
    global _settings
    _settings = settings


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # 환경 변수에서 (SUPABASE_URL이 없으면 pydantic 오류 — 서버 기동 시 이미 걸림)
    return _settings


def get_env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def require_env(name: str) -> str:
    value = get_env(name)
    if not value:
        raise AppError("NOT_CONFIGURED", f"서버 설정이 아직 안 됐어요 ({name})")
    return value


def supabase_url() -> str:
    url = get_settings().supabase_url.strip()
    if not url:
        raise AppError("NOT_CONFIGURED", "서버 설정이 아직 안 됐어요 (SUPABASE_URL)")
    return url


def anon_key() -> str:
    """앱 쪽 키(RLS를 따름) — 서버는 publishable 키 하나 (SUPABASE_PUBLISHABLE_KEY)"""
    secret = get_settings().supabase_publishable_key
    key = secret.get_secret_value().strip() if secret is not None else ""
    if not key:
        raise AppError("NOT_CONFIGURED", "서버 설정이 아직 안 됐어요 (SUPABASE_PUBLISHABLE_KEY)")
    return key


def service_keys() -> list[str]:
    """서버 키(RLS를 건너뜀) 목록 — 서버는 SUPABASE_SECRET_KEY 하나"""
    secret = get_settings().supabase_secret_key
    key = secret.get_secret_value().strip() if secret is not None else ""
    return [key] if key else []


def service_key() -> str:
    keys = service_keys()
    if not keys:
        raise AppError("NOT_CONFIGURED", "서버 설정이 아직 안 됐어요 (SUPABASE_SECRET_KEY)")
    return keys[0]

"""설정 — 환경 변수에서만 읽음 (값은 코드 · 로그에 안 남김).

env 이름 목록은 PR 설명에 있음 (`.env.example`은 AWS 세션이 이 목록에 맞춤).
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # Supabase 프로젝트 주소 (예: https://<ref>.supabase.co, 로컬은 http://127.0.0.1:54321)
    supabase_url: str
    # 토큰의 iss · JWKS 주소를 supabase_url과 다르게 줘야 할 때만 (로컬 compose: 컨테이너가 부르는 주소 ≠ 토큰에 찍힌 주소)
    supabase_jwt_issuer: str | None = None
    supabase_jwks_url: str | None = None
    # 서버 전용 Supabase 키 (PostgREST로 server_heartbeat 등을 읽는 /health/deep, 이후 DB 접근). 없으면 deep의 db 검사가 실패
    supabase_secret_key: SecretStr | None = None

    jwt_audience: str = "authenticated"
    jwt_leeway_seconds: int = 10  # 서버 · Supabase 시계 어긋남 허용

    jwks_cache_ttl_seconds: int = 600  # 이 시간이 지나면 새로 받아 봄 (못 받으면 낡은 캐시로 계속 검사)
    jwks_min_refetch_seconds: int = (
        30  # 모르는 kid가 와도 이 간격 안에서는 다시 안 받음 (아무 kid나 보내 JWKS를 두드리는 것 방지)
    )
    http_timeout_seconds: float = 3.0

    # 스케줄러(S3) 구현 전에는 끔 — 켜면 /health/deep이 server_heartbeat.alive_at 정체(기본 15분)를 실패로 봄
    health_require_scheduler: bool = False
    scheduler_stale_minutes: int = 15

    log_level: str = "INFO"

    @property
    def issuer(self) -> str:
        return self.supabase_jwt_issuer or f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def jwks_url(self) -> str:
        return self.supabase_jwks_url or f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

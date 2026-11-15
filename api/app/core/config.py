"""설정 — 환경 변수에서만 읽음 (값은 코드 · 로그에 안 남김).

env 이름 목록은 PR 설명에 있음 (`.env.example`은 AWS 세션이 이 목록에 맞춤).
"""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # hide_input_in_errors: 설정이 틀려 기동이 실패해도 오류 글에 입력값(키 일부 등)이 안 찍히게
    # env_ignore_empty: 빈 문자열 env는 "없음"으로 봄 (compose가 값이 빈 선택 변수를 빈 문자열로 넣어도 기본값이 쓰이고, 숫자 칸이 파싱 오류로 죽지 않게)
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", hide_input_in_errors=True, env_ignore_empty=True
    )

    # Supabase 프로젝트 주소 (예: https://<ref>.supabase.co, 로컬은 docs/local-dev.md의 격리 스택 http://127.0.0.1:28321)
    supabase_url: str
    # 토큰의 iss · JWKS 주소를 supabase_url과 다르게 줘야 할 때만 (로컬 compose: 컨테이너가 부르는 주소 ≠ 토큰에 찍힌 주소)
    supabase_jwt_issuer: str | None = None
    supabase_jwks_url: str | None = None
    # 서버 전용 Supabase 키 (PostgREST로 server_heartbeat 등을 읽는 /health/deep, 이후 DB 접근). 없으면 deep의 db 검사가 실패
    supabase_secret_key: SecretStr | None = None
    # 앱 쪽 공개 키 (사용자 토큰으로 PostgREST를 부를 때 apikey 머리글 — RLS를 따름). 없으면 사용자 DB 클라이언트가 NOT_CONFIGURED (W0 db-base)
    supabase_publishable_key: SecretStr | None = None
    # 카카오 REST API 키 (장소 검색 · 좌표 → 주소, 카카오 로컬). 없으면 /fn/place-*가 NOT_CONFIGURED(503) —
    # 앱은 읽기 함수의 이 오류를 Edge로 한 번 더 보내므로 키를 서버에 넣기 전에 켜도 안전함 (본 레포 M2)
    kakao_rest_api_key: SecretStr | None = None

    # 준비시간 예측 서버(CatBoost, 실험) — 원본 Edge의 PREPARE_ML_* 비밀값. 서버 설정 표(2-3)로 옮기기 전까지 env로.
    # 켜는 조건 6가지(플래그 "1" · UUID 허용 목록 · URL · 토큰 · 코드북 버전 · 검증된 프로필)가 다 맞아야 호출 — 기본은 전부 꺼짐
    prepare_ml_enabled: str | None = None
    prepare_ml_allowed_user_ids: str | None = None
    prepare_ml_url: str | None = None
    prepare_ml_api_token: SecretStr | None = None
    prepare_ml_codebook_version: str | None = None

    jwt_audience: str = "authenticated"
    jwt_leeway_seconds: int = 10  # 서버 · Supabase 시계 어긋남 허용

    jwks_cache_ttl_seconds: int = 600  # 이 시간이 지나면 새로 받아 봄 (못 받으면 낡은 캐시로 계속 검사)
    # 모르는 kid가 와도 이 간격 안에서는 다시 안 받음 (아무 kid나 보내 JWKS를 두드리는 것 방지).
    # 키 교체 직후 새 kid 사용자가 최대 이 시간 401이 될 수 있으나, Supabase는 새 키를 서명에 쓰기 전에 JWKS에 먼저 올림
    jwks_min_refetch_seconds: int = 10
    http_timeout_seconds: float = 3.0

    # 스케줄러(S3)가 배포돼 alive_at이 올라오는 것을 확인한 뒤에 켬 — 켜면 /health/deep이 server_heartbeat.alive_at 정체(기본 15분)를 실패로 봄
    health_require_scheduler: bool = False
    scheduler_stale_minutes: int = 15

    # 배포 태그(sha-…). compose가 api · scheduler 컨테이너에 APP_VERSION으로 넣어 줌 → /health 응답의 version (배포 직후 점검이 읽음)
    app_version: str = "dev"
    # /health/deep 결과를 이 시간(초) 동안 재사용 (토큰 없이 부를 때마다 서버 키로 DB를 부르는 것을 줄임). 0이면 캐시만 끔
    # (확인 자체는 한 번에 하나씩 순서대로 함). inf면 장애를 못 보고 nan · 음수는 캐시가 조용히 꺼지므로 0~60초만 받음
    health_deep_cache_seconds: float = Field(5.0, ge=0, le=60, allow_inf_nan=False)

    log_level: str = "INFO"

    @field_validator("app_version")
    @classmethod
    def _app_version(cls, value: str) -> str:
        return value.strip()[:64] or "dev"

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL은 DEBUG · INFO · WARNING · ERROR · CRITICAL 중 하나")
        return level

    @property
    def issuer(self) -> str:
        return self.supabase_jwt_issuer or f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def jwks_url(self) -> str:
        return self.supabase_jwks_url or f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

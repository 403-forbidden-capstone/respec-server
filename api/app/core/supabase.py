"""Supabase(PostgREST) 호출 도구 — /health/deep과 scheduler가 같이 씀 (서버 전용 키로 service_role 권한)"""


def rest_url(supabase_url: str, path: str) -> str:
    return f"{supabase_url.rstrip('/')}/rest/v1/{path.lstrip('/')}"


def rest_headers(secret: str) -> dict[str, str]:
    """옛 service_role JWT 키(eyJ…)는 Authorization에도 싣고, 새 sb_secret_ 키는 apikey만"""
    headers = {"apikey": secret}
    if secret.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {secret}"
    return headers

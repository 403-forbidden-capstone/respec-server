"""본 레포 supabase/functions/_shared/keys.ts의 서버판 (기준 b140f70) — 서비스 키 비교 도구 (키 사전 해석 · 일정 시간 비교 · 요청이 실은 키 후보)

서비스 키 확인: 키 형식이 둘이라 둘 다 받음 (legacy service_role JWT · 새 secret 키 sb_secret_…).
배치는 Authorization: Bearer 와 apikey 머리글 둘 다에 키를 싣고, 새 키는 apikey로만 보내는 게 맞음 → 두 머리글을 다 봄
"""

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

# `Authorization: Bearer …` — keys.ts와 같은 정규식 (대소문자 무시, 앞뒤 공백 제거한 값에 적용)
_BEARER = re.compile(r"^Bearer\s+(.+)$", re.IGNORECASE)


def _reject_constant(name: str) -> Any:
    raise ValueError(name)  # NaN · Infinity는 JSON이 아님 (JS JSON.parse도 거절 → 단일 키 문자열로 봄)


def parse_key_dictionary(raw: str | None) -> list[str]:
    """{"default": "sb_secret_…"} 같은 JSON 사전에서 값만. 깨졌거나 비었으면 빈 목록 (JSON이 아니면 단일 키 문자열)"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        # JSON이 아니면 단일 키 문자열로 본다
        return [raw]
    # JS `typeof parsed === 'object'` — 배열도 Object.values로 값을 꺼냄 (null · 숫자 · 글자는 빈 목록)
    if isinstance(parsed, dict):
        values: Iterable[Any] = parsed.values()
    elif isinstance(parsed, list):
        values = parsed
    else:
        return []
    return [v for v in values if isinstance(v, str) and len(v) > 0]


def _js_length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def timing_safe_equal(a: str, b: str) -> bool:
    """길이가 같을 때 모든 글자를 끝까지 비교 (일찍 끝나는 비교로 키를 한 글자씩 알아내지 못하게). 길이는 JS(UTF-16 단위)로 셈"""
    if _js_length(a) != _js_length(b):
        return False
    ua = a.encode("utf-16-le", errors="surrogatepass")
    ub = b.encode("utf-16-le", errors="surrogatepass")
    diff = 0
    for x, y in zip(ua, ub, strict=True):
        diff |= x ^ y
    return diff == 0


def key_candidates(headers: Mapping[str, str]) -> list[str]:
    """요청이 실은 키 후보 — apikey 머리글, Authorization: Bearer (머리글 이름은 대소문자 안 가림: Starlette Headers · httpx Headers)"""
    out: list[str] = []
    apikey = headers.get("apikey")
    if apikey:
        out.append(apikey.strip())
    auth = headers.get("authorization")
    match = _BEARER.match(auth.strip()) if auth else None
    if match:
        out.append(match.group(1).strip())
    return out


def matches_any_key(headers: Mapping[str, str], valid_keys: Iterable[str]) -> bool:
    valid = [k for k in valid_keys if len(k) > 0]
    if not valid:
        return False
    ok = False
    for candidate in key_candidates(headers):
        for key in valid:
            if timing_safe_equal(candidate, key):
                ok = True
    return ok

"""요청 본문 읽기 · 검사 — 본 레포 `supabase/functions/_shared/read.ts`(Reader · readJson)의 서버판.

모양이 틀리면 VALIDATION_FAILED(400) + details.issues[{path, message}] — 문구 · 순서 · 경계가 Edge와 같아야 앱이 같은 오류를 봄.
JS와 달라지기 쉬운 곳을 맞춤: 글자 수는 UTF-16 단위(이모지 하나 = 2) · trim은 JS가 지우는 글자 · bool은 숫자가 아님 ·
NaN · Infinity 글자는 JSON이 아님 · 5.0은 정수 · 1e400은 숫자가 아님. 입력값은 문제 목록에 안 실음(위치 · 문구만).
"""

import json
import math
from typing import Any

from .errors import AppError

MAX_BODY_CHARS = 100_000  # UTF-16 글자 수 (Edge 기본값)

# JS String.prototype.trim이 지우는 글자: WhiteSpace(탭 · 수직탭 · 폼피드 · 스페이스 · NBSP · BOM · 유니코드 공백) + LineTerminator
JS_WHITESPACE = "".join(
    chr(c)
    for c in (
        0x09,
        0x0A,
        0x0B,
        0x0C,
        0x0D,
        0x20,
        0xA0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    )
)


def js_trim(value: str) -> str:
    return value.strip(JS_WHITESPACE)


def js_length(value: str) -> int:
    """JS `string.length` — UTF-16 코드 단위 수 (BMP 밖 글자 · 이모지는 2)"""
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def clean_text(value: str) -> str:
    """짝 없는 서로게이트(`"\\ud800"` · 이모지 반쪽)를 U+FFFD로 — JS `URLSearchParams`가 하는 일. 안 바꾸면 UTF-8로 못 만들어 500"""
    return value.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")


def _reject_constant(name: str) -> Any:
    raise ValueError(name)  # NaN · Infinity · -Infinity는 JSON이 아님 (JS JSON.parse도 거절)


def read_json(raw: bytes, max_chars: int = MAX_BODY_CHARS) -> Any:
    """Edge `readJson`: 너무 큼 · 비어 있음 · JSON 아님을 같은 문구의 VALIDATION_FAILED로"""
    # Fetch 표준의 `req.text()`는 본문 맨 앞 UTF-8 BOM을 지움 — utf-8-sig가 같은 일을 함 (본문 중간의 BOM은 그대로 두어 JSON 오류)
    text = raw.decode("utf-8-sig", errors="replace")
    if js_length(text) > max_chars:
        raise AppError("VALIDATION_FAILED", "요청이 너무 커요")
    if not js_trim(text):
        raise AppError("VALIDATION_FAILED", "요청 본문(JSON)이 비어 있어요")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise AppError("VALIDATION_FAILED", "요청 본문이 JSON 형식이 아니에요") from None


def _as_number(value: Any) -> int | float | None:
    """JS `typeof v === 'number' && Number.isFinite(v)` — bool은 숫자가 아님, 1e400 같은 무한대는 숫자가 아님"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return value if math.isfinite(float(value)) else None
    except OverflowError:  # 아주 큰 정수
        return None


def _js_str(value: int | float) -> str:
    """JS `${n}` — 90 · -90 · 0.5 처럼 (문구에 들어가는 경계값용)"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class Reader:
    def __init__(self, value: dict[str, Any], path: str, issues: list[dict[str, str]]) -> None:
        self.path = path
        self.value = value
        self.issues = issues

    @classmethod
    def root(cls, raw: Any) -> "Reader":
        if not isinstance(raw, dict):
            raise AppError("VALIDATION_FAILED", "요청 본문은 JSON 객체여야 해요")
        return cls(raw, "", [])

    def _at(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def _problem(self, key: str, message: str) -> None:
        self.issues.append({"path": self._at(key), "message": message})

    def _present(self, key: str, required: bool) -> Any:
        """null · 없음을 같게 보고 "없음"으로. 필수인데 없으면 문제로 기록"""
        value = self.value.get(key)
        if value is None:
            if required:
                self._problem(key, "필수 값이에요")
            return None
        return value

    def string(
        self, key: str, *, required: bool = False, min: int | None = None, max: int | None = None
    ) -> str | None:
        value = self._present(key, required)
        if value is None:
            return None
        if not isinstance(value, str):
            self._problem(key, "글자여야 해요")
            return None
        text = js_trim(value)
        length = js_length(text)
        if min is not None and length < min:
            self._problem(key, f"{min}자 이상이어야 해요")
            return None
        if max is not None and length > max:
            self._problem(key, f"{max}자 이하여야 해요")
            return None
        return text

    def number(
        self,
        key: str,
        *,
        required: bool = False,
        min: int | float | None = None,
        max: int | float | None = None,
    ) -> int | float | None:
        value = self._present(key, required)
        if value is None:
            return None
        number = _as_number(value)
        if number is None:
            self._problem(key, "숫자여야 해요")
            return None
        if min is not None and number < min:
            self._problem(key, f"{_js_str(min)} 이상이어야 해요")
            return None
        if max is not None and number > max:
            self._problem(key, f"{_js_str(max)} 이하여야 해요")
            return None
        return number

    def int(
        self, key: str, *, required: bool = False, min: int | None = None, max: int | None = None
    ) -> int | None:
        number = self.number(key, required=required, min=min, max=max)
        if number is None:
            return None
        if isinstance(number, float):
            if not number.is_integer():  # JS: 5.0은 정수, 5.5는 아님
                self._problem(key, "정수여야 해요")
                return None
            return int(number)
        return number

    def problem_at(self, key: str, message: str) -> None:
        self._problem(key, message)

    def finish(self) -> None:
        """모은 문제가 있으면 던짐 — 마지막에 한 번만 부름"""
        if self.issues:
            raise AppError("VALIDATION_FAILED", "입력값을 확인해 주세요", {"issues": self.issues})

"""JS와 Python의 계산 차이를 한 곳에 모은 도우미 — 본 레포 Edge(TypeScript) 코드를 1:1로 옮기는 모든 모듈이 import해서 씀.

맞춘 곳: Math.round(.5는 +∞ 쪽) · Math.floor/ceil(무한 · NaN 그대로) · Date(epoch 밀리초, 소수는 0 쪽으로 버림) ·
Date.UTC(월 · 일 넘침 허용, 연도 0~99는 1900년대) · Date.parse(오프셋 · Z, 밀리초 아래 버림) · toISOString(밀리초 3자리 Z) ·
`%`(음수는 나눠지는 수의 부호) · Number()(공백 · 빈 글자 0 · 16진수 · Infinity).
시각은 전부 UTC aware `datetime` 또는 epoch 밀리초 int. naive datetime은 오류로 막음.
"""

import json as _json
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ..core.read import js_length, js_trim  # noqa: F401  (다른 묶음이 여기서 함께 import)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DAY_MS = 86_400_000

# JS 정규식 \s가 잡는 글자 (Python re의 \s와 다름 — \x1c~\x1f는 빼고 BOM은 넣음). 문자 클래스 안에 쓰는 조각
JS_WS = "\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"


class RangeError(ValueError):
    """JS RangeError 대응 (원본이 `throw new RangeError`로 던지는 곳)"""


# ---------------------------------------------------------------------------
# 숫자
# ---------------------------------------------------------------------------


def js_round(x: float) -> int:
    """Math.round — .5는 +∞ 쪽 (-2.5 → -2, 2.5 → 3). Python round는 짝수 쪽이라 쓰면 안 됨.
    NaN · ±Infinity는 JS처럼 그대로 돌려줌(그 경우만 float)."""
    if isinstance(x, int):
        return x
    if not math.isfinite(x):
        return x  # type: ignore[return-value]
    f = math.floor(x)
    return f + 1 if x - f >= 0.5 else f


def js_floor(x: float) -> int:
    """Math.floor — NaN · ±Infinity는 그대로"""
    if isinstance(x, int):
        return x
    return math.floor(x) if math.isfinite(x) else x  # type: ignore[return-value]


def js_ceil(x: float) -> int:
    """Math.ceil — NaN · ±Infinity는 그대로"""
    if isinstance(x, int):
        return x
    return math.ceil(x) if math.isfinite(x) else x  # type: ignore[return-value]


def js_mod(a: float, b: float) -> float:
    """JS `a % b` — 결과 부호는 a를 따름 (Python은 b를 따름). -1 % 7 → -1"""
    if isinstance(a, int) and isinstance(b, int) and b != 0:
        r = abs(a) % abs(b)
        return -r if a < 0 else r
    if b == 0 or not math.isfinite(a):
        return math.nan
    if not math.isfinite(b):
        return a
    return math.fmod(a, b)


def js_max(*values: float) -> float:
    """Math.max — NaN이 하나라도 있으면 NaN (Python max는 순서에 따라 NaN을 무시함). 인자가 없으면 -Infinity"""
    if not values:
        return -math.inf
    if any(isinstance(v, float) and v != v for v in values):
        return math.nan
    return max(values)


def js_min(*values: float) -> float:
    """Math.min — NaN이 하나라도 있으면 NaN. 인자가 없으면 Infinity"""
    if not values:
        return math.inf
    if any(isinstance(v, float) and v != v for v in values):
        return math.nan
    return min(values)


def js_number_to_string(n: int | float) -> str:
    """String(n) / `${n}` — 정수꼴은 소수점 없이(1e21 미만), 1e21 이상 · 1e-7 이하는 지수 표기(1e+21 · 1e-7), -0은 0, NaN · Infinity 글자"""
    if isinstance(n, bool):
        return "true" if n else "false"
    if isinstance(n, int):
        if abs(n) < 2**53:
            return str(n)
        n = float(n)  # JS 숫자는 실수라 큰 정수는 가까운 실수 값으로
    if n != n:
        return "NaN"
    if n == math.inf:
        return "Infinity"
    if n == -math.inf:
        return "-Infinity"
    if n == 0:
        return "0"
    sign = "-" if n < 0 else ""
    t = Decimal(repr(abs(n))).as_tuple()
    digits = "".join(str(d) for d in t.digits).rstrip("0") or "0"
    # 값 = 0.digits × 10^point
    point = len("".join(str(d) for d in t.digits)) + t.exponent  # type: ignore[operator]
    k = len(digits)
    if k <= point <= 21:
        body = digits + "0" * (point - k)
    elif 0 < point <= 21:
        body = digits[:point] + "." + digits[point:]
    elif -6 < point <= 0:
        body = "0." + "0" * (-point) + digits
    else:
        e = point - 1
        exp = f"e{'+' if e >= 0 else '-'}{abs(e)}"
        body = digits[0] + ("." + digits[1:] if k > 1 else "") + exp
    return sign + body


_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*(?:[eE][+-]?[0-9]+)?|\.[0-9]+(?:[eE][+-]?[0-9]+)?)\Z")
_RADIX = re.compile(r"0[xX][0-9a-fA-F]+\Z|0[oO][0-7]+\Z|0[bB][01]+\Z")


def js_number(value: Any) -> int | float:
    """Number(value) — 숫자는 그대로, 글자는 앞뒤 공백을 지우고 읽음(빈 글자 0 · 읽을 수 없으면 NaN), None은 0, bool은 0 · 1.
    정수로 딱 떨어지는 값은 int로 돌려줌(JSON에서 60.0이 아니라 60)."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        t = js_trim(value)
        if t == "":
            return 0
        if _RADIX.match(t):
            return int(t, 0)
        if t in ("Infinity", "+Infinity"):
            return math.inf
        if t == "-Infinity":
            return -math.inf
        if _DECIMAL.match(t):
            f = float(t)
            return int(f) if math.isfinite(f) and f == int(f) and abs(f) < 2**53 else f
        return math.nan
    return math.nan


# ---------------------------------------------------------------------------
# 시각 (Date)
# ---------------------------------------------------------------------------


def json_safe(value: Any) -> Any:
    """JSON.stringify와 같게 유한하지 않은 숫자(NaN · ±Infinity)를 null로 — Python json.dumps는 `Infinity` 글자를 내보내 PostgREST가 거절함"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    return value


def js_json(value: Any) -> str:
    """JSON.stringify(value) — 공백 없음 · 한글 그대로 · NaN/Infinity는 null · 숫자는 JS 글자(1e+21 · 1e-7 · 5)"""
    if value is None or isinstance(value, bool):
        return "null" if value is None else ("true" if value else "false")
    if isinstance(value, int | float):
        return "null" if isinstance(value, float) and not math.isfinite(value) else js_number_to_string(value)
    if isinstance(value, str):
        return _json_str(value)
    if isinstance(value, list | tuple):
        return "[" + ",".join("null" if v is None else js_json(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(_json_str(str(k)) + ":" + js_json(v) for k, v in value.items()) + "}"
    raise TypeError(f"JSON으로 만들 수 없는 값: {type(value).__name__}")


def _json_str(text: str) -> str:
    return _json.dumps(text, ensure_ascii=False)


def ms(dt: datetime) -> int:
    """getTime() — epoch 밀리초 (밀리초 아래는 버림)"""
    if dt.tzinfo is None:
        raise ValueError("naive datetime은 쓰지 않아요 (UTC aware만)")
    td = dt - EPOCH
    return td.days * DAY_MS + td.seconds * 1000 + td.microseconds // 1000


def from_ms(n: float) -> datetime:
    """new Date(n) — 소수는 0 쪽으로 버림(TimeClip), NaN · 무한은 Invalid Date라 ValueError"""
    if isinstance(n, float) and not math.isfinite(n):
        raise ValueError("Invalid Date")
    return EPOCH + timedelta(milliseconds=int(n))


def to_iso(dt: datetime) -> str:
    """Date.toISOString — 'YYYY-MM-DDTHH:MM:SS.sssZ' (UTC, 밀리초 3자리)"""
    if dt.tzinfo is None:
        raise ValueError("naive datetime은 쓰지 않아요 (UTC aware만)")
    u = dt.astimezone(UTC)
    return f"{u.year:04d}-{u.month:02d}-{u.day:02d}T{u.hour:02d}:{u.minute:02d}:{u.second:02d}.{u.microsecond // 1000:03d}Z"


_HOUR_24 = re.compile(r"T24:00(?::00(?:\.0+)?)?(?=Z|[+-][0-9]{2}:?[0-9]{2}|\Z)")


def parse_iso(s: str) -> datetime:
    """Date.parse / new Date(s) — 오프셋 · Z 처리, 오프셋이 없으면 UTC로 읽음. 결과는 밀리초 단위 UTC aware.
    읽을 수 없으면 ValueError (JS는 Invalid Date)."""
    text = s.strip()
    next_day = False
    if _HOUR_24.search(text):  # JS는 24:00을 다음 날 0시로 받음
        text = _HOUR_24.sub("T00:00", text, count=1)
        next_day = True
    dt = datetime.fromisoformat(text)
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    dt = dt.replace(microsecond=dt.microsecond // 1000 * 1000)
    return dt + timedelta(days=1) if next_day else dt


# ---------------------------------------------------------------------------
# Date.UTC · getUTC* (밀리초 정수 위에서 — datetime이 못 받는 넘침 · 범위를 JS처럼)
# ---------------------------------------------------------------------------


def _days_from_civil(y: int, m: int, d: int) -> int:
    y -= m <= 2
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _civil_from_days(z: int) -> tuple[int, int, int]:
    z += 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return y + (m <= 2), m, d


def utc_ms(
    year: float,
    month0: float,
    day: float = 1,
    hour: float = 0,
    minute: float = 0,
    second: float = 0,
    millisecond: float = 0,
) -> int:
    """Date.UTC(year, monthIndex, day, hour, minute, second, ms) — 월 · 일 · 시 · 분이 넘쳐도 이어서 셈(13월 → 다음 해 1월),
    각 인자는 0 쪽으로 버림, 연도 0~99는 1900년대. 달은 0부터(1월 = 0)."""
    y, mo, d, h, mi, s, milli = (int(v) for v in (year, month0, day, hour, minute, second, millisecond))
    if 0 <= y <= 99:
        y += 1900
    ym = y * 12 + mo
    days = _days_from_civil(ym // 12, ym % 12 + 1, 1) + (d - 1)
    return (((days * 24 + h) * 60 + mi) * 60 + s) * 1000 + milli


def utc_ymd(ms_value: int) -> tuple[int, int, int]:
    """getUTCFullYear · getUTCMonth()+1 · getUTCDate — (연, 월 1~12, 일)"""
    return _civil_from_days(ms_value // DAY_MS)


def utc_weekday(ms_value: int) -> int:
    """getUTCDay — 일 0 · 월 1 … 토 6 (월 0이 아님)"""
    return (ms_value // DAY_MS + 4) % 7


def pad_start(value: int | str, width: int, fill: str = "0") -> str:
    """String(value).padStart(width, fill) — 음수 부호도 글자로 보고 앞에 채움"""
    return str(value).rjust(width, fill)

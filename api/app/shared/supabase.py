"""본 레포 supabase/functions/_shared/supabase.ts의 서버판 (기준 b140f70) — supabase-js(2.117.2) 쿼리 체인을 PostgREST 호출로 옮긴 Db

Edge는 npm:@supabase/supabase-js를 썼음. 서버는 같은 체인(`db.from_('t').select(...).eq(...)`)을 httpx로 PostgREST에 보냄 (정할 일 0-7).
  create_user_client(authorization) — 부른 사람의 토큰을 실어서 RLS를 그대로 따름 (읽기 · 권한 확인에 우선 씀). apikey = 공개 키
  create_service_client()           — 서버 키라 RLS를 건너뜀. 쓰기 전에 꼭 직접 권한을 확인하고, 사용자가 보낸 값으로 쿼리를 만들지 않음
await 한 결과는 Result(data, error, count) — DB 오류여도 예외를 안 던짐 (supabase-js와 같음). 호출한 쪽이 db.must(...)로 던짐.
요청 모양은 postgrest-js가 만드는 것과 같게:
  · select → GET `select=열` (`head=True`면 HEAD) · `count='exact'`면 `Prefer: count=exact`, 개수는 Content-Range의 `/` 뒤
  · insert/upsert → POST, update → PATCH, delete → DELETE. `.select()`가 뒤따를 때만 `Prefer: return=representation`
  · upsert → `on_conflict=a,b` · `Prefer: resolution=merge-duplicates | ignore-duplicates`
  · 여러 줄 insert/upsert → `columns=` (넣는 줄의 칼럼 합집합)
  · single → `Accept: application/vnd.pgrst.object+json`, maybe_single → 읽기는 배열을 받아 0줄이면 None · 2줄 이상이면 PGRST116,
    쓰기는 object+json으로 받고 "0 rows" 오류는 오류 없음 + None
  · rpc → POST `/rest/v1/rpc/<이름>` 본문 = 인자 JSON
네트워크 실패는 예외 대신 error(`{"message": "ConnectError: …", "code": ""}`)로 돌려줌 (supabase-js와 같음).
토큰 · 키는 로그 · 오류에 안 넣음.
"""

import asyncio
import json
import re
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, Self

import httpx

from ..core.config import Settings
from ..core.supabase import rest_headers, rest_url
from .env import anon_key, configure_env, get_settings, service_key, supabase_url
from .js import js_json

# supabase-js가 실는 X-Client-Info (버전은 본 레포 supabase.ts가 고정한 것)
CLIENT_INFO = "supabase-js-node/2.117.2"
SCHEMA = "public"
OBJECT_ACCEPT = "application/vnd.pgrst.object+json"

# client: 공용 httpx 클라이언트 · lazy: _http()가 직접 만든 것(그때의 이벤트 루프 `loop`에 묶임) · configure()로 넣은 것은 lazy=False
_state: dict[str, Any] = {"client": None, "lazy": False, "loop": None}


def configure(settings: Settings | None, client: httpx.AsyncClient | None = None) -> None:
    """앱 시작 때 (main lifespan) 설정과 공용 httpx 클라이언트를 넣음. 시험은 MockTransport를 넣은 클라이언트를 넣음. 둘 다 None이면 되돌림"""
    configure_env(settings)
    _state["client"] = client
    _state["lazy"] = False
    _state["loop"] = None


def _http() -> httpx.AsyncClient:
    """공용 클라이언트. configure()로 넣은 것이 정상 길 (서버: main lifespan, 스케줄러: async_runtime).
    안 넣었으면 처음 쓸 때 만들되, 만든 루프가 아닌 곳(`asyncio.run()`을 여러 번 부르는 경우)에서 쓰이면 새로 만듦 —
    옛 클라이언트의 연결은 닫힌 루프에 묶여 `Event loop is closed`로 죽기 때문"""
    running = asyncio.get_running_loop()  # 루프 밖이면 RuntimeError (httpx 호출은 어차피 루프 안에서만)
    client: httpx.AsyncClient | None = _state["client"]
    if client is not None and _state["lazy"] and _state["loop"] is not running:
        client = None  # 만든 루프가 아님(닫혔거나 다른 루프) — 버리고 다시 만듦
    if client is None:
        client = httpx.AsyncClient(timeout=get_settings().http_timeout_seconds)
        _state["client"] = client
        _state["lazy"] = True
        _state["loop"] = running
    return client


@dataclass
class Result:
    data: Any = None
    error: dict[str, Any] | None = None  # None 또는 {"message", "code", "details", "hint"}
    count: int | None = None
    status: int = 0


def _js_str(value: Any) -> str:
    """JS 템플릿 `${value}` — 불리언은 true/false, 정수꼴 실수는 소수점 없이, None은 null"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    if isinstance(value, dict | list):
        return js_json(value)
    return str(value)


def _clean_columns(columns: str) -> str:
    """postgrest-js select: 따옴표 밖의 공백을 지움"""
    quoted = False
    out: list[str] = []
    for c in columns:
        if c.isspace() and not quoted:
            continue
        if c == '"':
            quoted = not quoted
        out.append(c)
    return "".join(out)


def _in_list(values: list[Any]) -> str:
    # postgrest-js: , ( ) 가 든 글자는 따옴표로 감쌈
    cleaned = [f'"{v}"' if isinstance(v, str) and re.search(r"[,()]", v) else _js_str(v) for v in values]
    return f"({','.join(cleaned)})"


class Query:
    """supabase-js 쿼리 빌더 한 개 — 메서드마다 자기 자신을 돌려주는 체인, await 하면 Result"""

    def __init__(self, db: "Db", method: str, path: str, body: Any = None) -> None:
        self._db = db
        self._method = method
        self._path = path
        self._body = body
        self._params: list[tuple[str, str]] = []
        self._prefer: list[str] = []
        self._accept: str | None = None
        self._maybe_single = False

    # --- 쿼리 만들기 (postgrest-js PostgrestQueryBuilder) ---

    def _set(self, key: str, value: str) -> None:
        self._params = [(k, v) for k, v in self._params if k != key]
        self._params.append((key, value))

    def select(self, columns: str = "*", *, count: str | None = None, head: bool = False) -> Self:
        if self._method in ("GET", "HEAD"):
            # 처음 select(): 읽기. head면 HEAD
            self._method = "HEAD" if head else "GET"
            if count:
                self._prefer.append(f"count={count}")
        else:
            # insert/update/upsert/delete 뒤 select(): 바뀐 줄을 돌려받음
            self._prefer.append("return=representation")
        self._set("select", _clean_columns(columns))
        return self

    def _columns_of(self, rows: Any) -> None:
        if isinstance(rows, list):
            names: list[str] = []
            for row in rows:
                for key in row:
                    if key not in names:
                        names.append(key)
            if names:
                self._set("columns", ",".join(f'"{n}"' for n in names))

    def insert(self, rows: Any) -> Self:
        self._method = "POST"
        self._body = rows
        self._columns_of(rows)
        return self

    def upsert(self, rows: Any, *, on_conflict: str | None = None, ignore_duplicates: bool = False) -> Self:
        self._method = "POST"
        self._body = rows
        self._prefer.append(
            "resolution=ignore-duplicates" if ignore_duplicates else "resolution=merge-duplicates"
        )
        if on_conflict is not None:
            self._set("on_conflict", on_conflict)
        self._columns_of(rows)
        return self

    def update(self, values: Any) -> Self:
        self._method = "PATCH"
        self._body = values
        return self

    def delete(self) -> Self:
        self._method = "DELETE"
        return self

    # --- 거르기 (postgrest-js PostgrestFilterBuilder) ---

    def _filter(self, column: str, operator: str, value: str) -> Self:
        self._params.append((column, f"{operator}.{value}"))
        return self

    def eq(self, column: str, value: Any) -> Self:
        return self._filter(column, "eq", _js_str(value))

    def neq(self, column: str, value: Any) -> Self:
        return self._filter(column, "neq", _js_str(value))

    def gt(self, column: str, value: Any) -> Self:
        return self._filter(column, "gt", _js_str(value))

    def gte(self, column: str, value: Any) -> Self:
        return self._filter(column, "gte", _js_str(value))

    def lt(self, column: str, value: Any) -> Self:
        return self._filter(column, "lt", _js_str(value))

    def lte(self, column: str, value: Any) -> Self:
        return self._filter(column, "lte", _js_str(value))

    def is_(self, column: str, value: Any) -> Self:
        return self._filter(column, "is", _js_str(value))

    def in_(self, column: str, values: list[Any]) -> Self:
        return self._filter(column, "in", _in_list(list(values)))

    def not_(self, column: str, operator: str, value: Any) -> Self:
        is_list = operator == "in" and isinstance(value, list | tuple)
        return self._filter(column, f"not.{operator}", _in_list(list(value)) if is_list else _js_str(value))

    # --- 모양 바꾸기 (postgrest-js PostgrestTransformBuilder) ---

    def order(self, column: str, *, ascending: bool = True) -> Self:
        existing = next((v for k, v in self._params if k == "order"), None)
        self._set("order", f"{existing + ',' if existing else ''}{column}.{'asc' if ascending else 'desc'}")
        return self

    def limit(self, n: int) -> Self:
        self._set("limit", str(n))
        return self

    def single(self) -> Self:
        self._accept = OBJECT_ACCEPT
        return self

    def maybe_single(self) -> Self:
        # 읽기는 배열로 받아 0줄 · 1줄을 직접 가름. 쓰기는 object+json으로 받고 "0 rows" 오류를 지움
        self._accept = "application/json" if self._method == "GET" else OBJECT_ACCEPT
        self._maybe_single = True
        return self

    # --- 보내기 ---

    def _headers(self) -> dict[str, str]:
        headers = dict(self._db.headers)
        headers["Accept-Profile" if self._method in ("GET", "HEAD") else "Content-Profile"] = SCHEMA
        if self._method not in ("GET", "HEAD"):
            headers["Content-Type"] = "application/json"
        if self._prefer:
            headers["Prefer"] = ",".join(self._prefer)
        if self._accept:
            headers["Accept"] = self._accept
        return headers

    async def _execute(self) -> Result:
        content = (
            js_json(self._body).encode("utf-8")  # supabase-js는 JSON.stringify — NaN · Infinity는 null
            if self._body is not None and self._method not in ("GET", "HEAD")
            else None
        )
        try:
            response = await _http().request(
                self._method,
                rest_url(self._db.base_url, self._path),
                params=self._params,
                headers=self._headers(),
                content=content,
            )
        except httpx.HTTPError as exc:
            # supabase-js: 네트워크 실패도 예외가 아니라 error로
            return Result(
                error={"message": f"{type(exc).__name__}: {exc}", "details": "", "hint": "", "code": ""}
            )
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> Result:
        status = response.status_code
        data: Any = None
        error: dict[str, Any] | None = None
        count: int | None = None
        if 200 <= status < 300:
            if self._method != "HEAD":
                body = response.text
                if body != "":
                    try:
                        data = json.loads(body)
                    except ValueError:
                        error = {
                            "message": "invalid JSON from PostgREST",
                            "code": "",
                            "details": None,
                            "hint": None,
                        }
            ranged = response.headers.get("content-range")
            wants_count = any(p.startswith("count=") for p in self._prefer)
            if error is None and wants_count and ranged:
                parts = ranged.split("/")
                matched = re.match(r"^\d+", parts[1]) if len(parts) > 1 else None
                if matched:
                    count = int(matched.group(0))
            if error is None and self._maybe_single and self._method == "GET" and isinstance(data, list):
                if len(data) > 1:
                    error = {
                        "code": "PGRST116",
                        "details": f"Results contain {len(data)} rows, application/vnd.pgrst.object+json requires 1 row",
                        "hint": None,
                        "message": "JSON object requested, multiple (or no) rows returned",
                    }
                    data = None
                    count = None
                    status = 406
                else:
                    data = data[0] if len(data) == 1 else None
        else:
            text = response.text
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                error = {
                    "message": parsed.get("message"),
                    "code": parsed.get("code"),
                    "details": parsed.get("details"),
                    "hint": parsed.get("hint"),
                }
            else:
                error = {
                    "message": text or response.reason_phrase,
                    "code": None,
                    "details": None,
                    "hint": None,
                }
            if self._maybe_single and "0 rows" in (error.get("details") or ""):
                error = None
                status = 200
        return Result(data=data, error=error, count=count, status=status)

    def __await__(self) -> Generator[Any, None, Result]:
        return self._execute().__await__()


class Db:
    """supabase-js SupabaseClient 대응 — 키 · 주소 · 머리글을 쥐고 있고, 요청은 공용 httpx 클라이언트로 보냄"""

    def __init__(self, base_url: str, headers: dict[str, str]) -> None:
        self.base_url = base_url
        self.headers = {**headers, "X-Client-Info": CLIENT_INFO}

    def from_(self, table: str) -> Query:
        return Query(self, "GET", table)

    def rpc(self, name: str, args: dict[str, Any] | None = None) -> Query:
        return Query(self, "POST", f"rpc/{name}", body=args if args is not None else {})


def create_user_client(authorization: str) -> Db:
    """부른 사람의 토큰을 실어서 RLS를 그대로 따름 (읽기 · 권한 확인에 우선 씀)"""
    return Db(supabase_url(), {"apikey": anon_key(), "Authorization": authorization})


def create_service_client() -> Db:
    """서버 키라 RLS를 건너뜀. 쓰기 전에 꼭 직접 권한을 확인하고, 사용자가 보낸 값으로 쿼리를 만들지 않음"""
    # 옛 service_role JWT(eyJ…)는 Authorization에도 싣고, 새 sb_secret_ 키는 apikey만 (core.supabase.rest_headers와 같음)
    return Db(supabase_url(), rest_headers(service_key()))

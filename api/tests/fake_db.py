"""본 레포 supabase/functions/_shared/fake-db.ts의 서버판 (기준 b140f70) — 시험용 가짜 DB (메모리 표). 배포되는 코드가 아님

supabase-js 쿼리 빌더 중 서버 함수가 쓰는 모양만 흉내 냄. 인터페이스는 `app.shared.supabase.Db`와 같음 (from_ · rpc · await → Result).
  from_(t).select/insert/update/delete/upsert + eq · neq · is_ · in_ · not_(is null) · gt · gte · lt · lte · order · limit + single · maybe_single + 개수(count · head)
  rpc(이름, 인자) — 시험이 rpc_handlers에 이름별로 답을 등록함 (호출은 rpc_calls에 남음). with_roll_rpc()는 alarm-roll이 쓰는 DB 함수 둘을 등록해 줌
  기본키 만들기, alarms 유일 제약(참여 조건 + 종류), CASCADE 삭제
  (L1 PR 4b) appointments.exception_kind(이번만 고친 · 지운 회차, 기본 NULL). roll_due_series 흉내는 기준 회차에서 예외 회차를 뺌
  반복 회차 칸 (alarm_roll.sql): appointments · alarms(일정 없는 알람)는 insert 때 series_id · occurrence_on을 채우고(fill_series 트리거),
    (만든 사람, 묶음, 날짜)가 겹치면 23505. 날짜는 만든 사람 profiles.timezone(없으면 Asia/Seoul)으로 셈
  fail_on으로 특정 표 · 동작을 실패시켜 되돌리기를 시험함
  upsert(…, on_conflict=…, ignore_duplicates=True)는 PostgREST처럼 새로 들어간 행만 돌려줌 (ON CONFLICT DO NOTHING … RETURNING). on_conflict는 'a,b,c'처럼 여러 칸도 됨
  임베드: appointment_participants를 읽을 때 `appointments!inner(…)` · `alarms!inner(…)`와 'alarms.alarm_type' 같은 점 붙은 거르기만 흉내 (그날 첫 일정 · 이동 기록 읽기용)
  (L4) jsonb 칸 거르기: eq('server_state->>rev', '3') 같은 `->` · `->>` 경로 (낙관적 잠금 비교용). alarm_events를 읽을 때 `alarms(…, appointment_participants(…))` 왼쪽 임베드(앞 출발 읽기용).
  (S8) with_commit_rpc()는 alarm_commit_apply(저장 DB 함수, 한 트랜잭션 · 요청 키)를 같은 규칙으로 흉내 — 한 번에 적용, 어디서든 실패(fail_on 포함)하면 표 전부 되돌림
  (L4) 표: conversation_sessions · ai_calls · user_questions — 기본키 · 시각 기본값. 서버 키만 쓰는 두 칸 트리거(turns · server_state)는 흉내 안 냄

TS와 이름이 달라진 곳: failOn → fail_on(dict {"table", "op"}의 목록) · rpcHandlers → rpc_handlers · rpcCalls → rpc_calls({"name", "args"}) ·
  withRollRpc → with_roll_rpc · timezoneOf → timezone_of · newId → new_id · now는 UTC aware datetime.
시각 계산(Date.parse · toISOString · localDateKey)은 js.py · tz.py 대신 이 파일 안의 작은 도우미로 함 (시험 도구가 다른 묶음 모듈에 안 기대게).
"""

import copy
import re
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from functools import cmp_to_key
from typing import Any
from zoneinfo import ZoneInfo

from app.shared.supabase import Result

Row = dict[str, Any]

PK: dict[str, str] = {
    "appointments": "appointment_id",
    "appointment_participants": "participant_id",
    "alarms": "alarm_id",
    "user_edit_logs": "edit_id",
    "alarm_events": "event_id",
    "todos": "todo_id",
    "conversation_sessions": "session_id",
    "ai_calls": "call_id",
    "user_questions": "question_id",
}

DEFAULTS: dict[str, Row] = {
    "alarms": {
        "routine_id": None,
        "step_order": None,
        "snooze_limit": 3,
        "recurrence_rule": None,
        "vibration": None,
    },
    "appointments": {"memo": None, "exception_kind": None},
    "daily_journals": {"edited_summary": None, "is_edited": False},
    "todos": {"is_done": False},  # init_schema 기본값 (앱 · voice-commit은 is_done을 안 보냄)
}

DAY_MS = 86_400_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ISO_START = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_NAN = float("nan")


# --- JS 흉내 도우미 (이 파일 안에서만) ---


def _parse_ms(value: str) -> float:
    """Date.parse — ISO 8601 글자 → epoch 밀리초. 못 읽으면 NaN (오프셋 없는 글자는 UTC로 봄)"""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return _NAN
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def _iso(dt: datetime) -> str:
    """Date.toISOString — 'YYYY-MM-DDTHH:MM:SS.sssZ'"""
    u = dt.astimezone(UTC)
    return f"{u:%Y-%m-%dT%H:%M:%S}.{u.microsecond // 1000:03d}Z"


def _local_date_key(ms: float, tz: str) -> str:
    """tz.ts localDateKey — 그 시간대의 'YYYY-MM-DD'"""
    local = (_EPOCH + timedelta(milliseconds=ms)).astimezone(ZoneInfo(tz))
    return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"


def _str(v: Any) -> str:
    """JS String(v)"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _eq(a: Any, b: Any) -> bool:
    """JS `===` — 불리언은 숫자와 안 같고, 숫자끼리는 1 === 1.0"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, int | float) != isinstance(b, int | float):
        return False
    return a == b


def _comparable(v: Any) -> Any:
    return _parse_ms(v) if isinstance(v, str) and _ISO_START.match(v) else v


def _compare(a: Any, b: Any) -> float | None:
    """시각 글자(ISO 8601)끼리는 시각으로, 그 밖에는 값 그대로 비교. 비교할 수 없으면 None"""
    x, y = _comparable(a), _comparable(b)
    if x is None or y is None:
        return None
    if (
        isinstance(x, int | float)
        and isinstance(y, int | float)
        and not isinstance(x, bool)
        and not isinstance(y, bool)
    ):
        return x - y
    sx, sy = _str(x), _str(y)
    return -1 if sx < sy else (1 if sx > sy else 0)


def _first(*vals: float) -> float:
    """JS `a || b || c` (숫자) — 0 · NaN이면 다음 값"""
    for v in vals:
        if v and v == v:
            return v
    return vals[-1]


def _d(row: Row, key: str, default: Any = None) -> Any:
    """JS `row.key ?? default`"""
    v = row.get(key)
    return default if v is None else v


class FakeDb:
    def __init__(self) -> None:
        self.tables: dict[str, list[Row]] = {}
        self.fail_on: list[
            dict[str, str]
        ] = []  # {"table": ..., "op": 'select' | 'insert' | 'update' | 'delete' | 'upsert'}
        self.calls: list[str] = []
        self.rpc_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self.rpc_calls: list[dict[str, Any]] = []  # {"name": ..., "args": ...}
        self.now: datetime = datetime.now(
            UTC
        )  # DB 함수 흉내가 created_at 같은 칸에 찍는 "지금" — 시험이 시계를 옮기며 바꿈
        self._seq = 0

    def rows(self, table: str) -> list[Row]:
        return self.tables.setdefault(table, [])

    def new_id(self) -> str:
        self._seq += 1
        return f"00000000-0000-4000-8000-{self._seq:012d}"

    def from_(self, table: str) -> "FakeQuery":
        return FakeQuery(self, table)

    def rpc(self, name: str, args: dict[str, Any] | None = None) -> "FakeRpc":
        """시험이 등록한 handler의 값을 돌려줌. 등록이 없으면 "없는 함수" 오류, handler가 던지면 DB 오류로. 호출은 부른 순간 기록 (await 전)"""
        args = {} if args is None else args
        self.calls.append(f"rpc {name}")
        self.rpc_calls.append({"name": name, "args": args})
        return FakeRpc(self, name, args)

    def with_roll_rpc(self) -> "FakeDb":
        """alarm_roll.sql의 DB 함수 둘 (roll_insert_occurrence · roll_due_series)을 같은 규칙으로 흉내 내서 등록. 이미 등록한 handler는 덮지 않음"""
        self.rpc_handlers.setdefault(
            "roll_insert_occurrence", lambda args: roll_insert_occurrence(self, args)
        )
        self.rpc_handlers.setdefault("roll_due_series", lambda args: roll_due_series(self, args))
        return self

    def with_commit_rpc(self) -> "FakeDb":
        """alarm_commit_apply.sql(저장 DB 함수 — 약속 · 참여 조건 · 알람 · 고친 값 기록을 한 트랜잭션으로, 요청 키로 다시 보내도 첫 결과)를 같은 규칙으로 흉내 내서 등록.
        이미 등록한 handler는 덮지 않음. 요청 키 표는 tables["private.request_keys"]"""
        self.rpc_handlers.setdefault("alarm_commit_apply", lambda args: commit_apply(self, args))
        return self

    def timezone_of(self, user_id: Any) -> str:
        profile = next((p for p in self.rows("profiles") if p.get("user_id") == user_id), None)
        tz = profile.get("timezone") if profile else None
        return tz if isinstance(tz, str) else "Asia/Seoul"

    def cascade(self, table: str, removed: list[Row]) -> None:
        ids = {r.get(PK.get(table, "")) for r in removed}  # TS: r[PK[table]] — 기본키가 없는 표는 undefined
        if table == "appointments":
            parts = [p for p in self.rows("appointment_participants") if p.get("appointment_id") in ids]
            self.tables["appointment_participants"] = [
                p for p in self.rows("appointment_participants") if p.get("appointment_id") not in ids
            ]
            self.cascade("appointment_participants", parts)
        if table == "appointment_participants":
            self.tables["alarms"] = [a for a in self.rows("alarms") if a.get("participant_id") not in ids]


class RpcError(Exception):
    """DB 함수 안에서 errcode를 달아 던진 오류 (P0001 · P0002 …)"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FakeRpc:
    """rpc() 결과 — await 하면 Result"""

    def __init__(self, db: FakeDb, name: str, args: dict[str, Any]) -> None:
        self._db = db
        self._name = name
        self._args = args

    async def _run(self) -> Result:
        handler = self._db.rpc_handlers.get(self._name)
        if handler is None:
            return Result(error={"message": f"function {self._name} does not exist", "code": "42883"})
        try:
            return Result(data=handler(self._args))
        except RpcError as exc:  # DB 함수의 raise exception ... using errcode
            return Result(error={"message": str(exc), "code": exc.code})
        except Exception as exc:  # noqa: BLE001 — handler가 던지면 DB 오류로 (TS와 같음)
            return Result(error={"message": str(exc), "code": "XX000"})

    def __await__(self) -> Generator[Any, None, Result]:
        return self._run().__await__()


class FakeQuery:
    def __init__(self, db: FakeDb, table: str) -> None:
        self._db = db
        self._table = table
        self._op = "select"
        self._payload: Any = None
        self._filters: list[Callable[[Row], bool]] = []
        self._embed_filters: list[tuple[str, Callable[[Row], bool]]] = []
        self._columns = ""
        self._returning = False
        self._one: str | None = None
        self._count_mode = False
        self._head_only = False
        self._ordering: tuple[str, bool] | None = None
        self._max: int | None = None
        self._upsert_on_conflict: str | None = None
        self._upsert_ignore = False

    def select(self, columns: str = "", *, count: str | None = None, head: bool = False) -> "FakeQuery":
        self._returning = True
        self._columns = columns
        self._count_mode = count is not None
        self._head_only = head is True
        return self

    def insert(self, rows: Any) -> "FakeQuery":
        self._op = "insert"
        self._payload = rows
        return self

    def upsert(
        self, rows: Any, *, on_conflict: str | None = None, ignore_duplicates: bool = False
    ) -> "FakeQuery":
        self._op = "upsert"
        self._payload = rows
        self._upsert_on_conflict = on_conflict
        self._upsert_ignore = ignore_duplicates
        return self

    def update(self, patch: Row) -> "FakeQuery":
        self._op = "update"
        self._payload = patch
        return self

    def delete(self) -> "FakeQuery":
        self._op = "delete"
        return self

    # 'alarms.alarm_type'처럼 점이 붙은 칸은 임베드한 쪽의 칸 (PostgREST 임베드 거르기)
    def _where(self, col: str, test: Callable[[Any], bool]) -> "FakeQuery":
        dot = col.find(".")
        if "->" in col:
            # jsonb 경로: `a->b->>c` (->> 는 글자로 꺼냄, 없으면 null)
            parts = re.split(r"->>?", col)
            as_text = "->>" in col

            def jsonb(r: Row) -> bool:
                v: Any = r.get(parts[0])
                for key in parts[1:]:
                    v = v.get(key) if isinstance(v, dict) else None
                return test((None if v is None else _str(v)) if as_text else v)

            self._filters.append(jsonb)
        elif dot < 0:
            self._filters.append(lambda r: test(r.get(col)))
        else:
            field = col[dot + 1 :]
            self._embed_filters.append((col[:dot], lambda embedded: test(embedded.get(field))))
        return self

    def eq(self, col: str, val: Any) -> "FakeQuery":
        return self._where(col, lambda v: _eq(v, val))

    def neq(self, col: str, val: Any) -> "FakeQuery":
        return self._where(col, lambda v: not _eq(v, val))

    def is_(self, col: str, val: Any) -> "FakeQuery":
        return self._where(col, lambda v: _eq(v, None) if val is None else _eq(v, val))

    def not_(self, col: str, operator: str, val: Any) -> "FakeQuery":
        if operator != "is" or val is not None:
            raise RuntimeError(f"FakeDb: not({col}, {operator}, …)은 아직 흉내 내지 않아요")
        return self._where(col, lambda v: v is not None)

    def in_(self, col: str, vals: list[Any]) -> "FakeQuery":
        return self._where(col, lambda v: any(_eq(v, x) for x in vals))

    def gt(self, col: str, val: Any) -> "FakeQuery":
        return self._where(col, lambda v: _cmp_or(v, val, -1) > 0)

    def gte(self, col: str, val: Any) -> "FakeQuery":
        return self._where(col, lambda v: _cmp_or(v, val, -1) >= 0)

    def lte(self, col: str, val: Any) -> "FakeQuery":
        return self._where(col, lambda v: _cmp_or(v, val, 1) <= 0)

    def lt(self, col: str, val: Any) -> "FakeQuery":
        return self._where(col, lambda v: _cmp_or(v, val, 1) < 0)

    def order(self, col: str, *, ascending: bool = True) -> "FakeQuery":
        self._ordering = (col, ascending)
        return self

    def limit(self, n: int) -> "FakeQuery":
        self._max = n
        return self

    def single(self) -> "FakeQuery":
        self._one = "single"
        return self

    def maybe_single(self) -> "FakeQuery":
        self._one = "maybe"
        return self

    async def _run_async(self) -> Result:
        return self._run()

    def __await__(self) -> Generator[Any, None, Result]:
        return self._run_async().__await__()

    # 임베드(`…!inner(…)`)를 붙인 참여 한 줄. 안쪽에 거를 조건에 맞는 행이 없으면 None (inner join)
    #   appointments는 참여 → 약속(하나), alarms는 참여 → 알람(여러 개, 거른 것만 남김)
    def _with_embeds(self, parent: Row, rels: list[str]) -> Row | None:
        out: Row = dict(parent)
        for rel in rels:

            def passes(embedded: Row, rel: str = rel) -> bool:
                return all(test(embedded) for r, test in self._embed_filters if r == rel)

            if self._table == "appointment_participants" and rel == "appointments":
                found = next(
                    (
                        a
                        for a in self._db.rows("appointments")
                        if _eq(a.get("appointment_id"), parent.get("appointment_id"))
                    ),
                    None,
                )
                if found is None or not passes(found):
                    return None
                out[rel] = dict(found)
            elif self._table == "appointment_participants" and rel == "alarms":
                alarms = [
                    a
                    for a in self._db.rows("alarms")
                    if _eq(a.get("participant_id"), parent.get("participant_id")) and passes(a)
                ]
                if not alarms:
                    return None
                out[rel] = [dict(a) for a in alarms]
            else:
                raise RuntimeError(f"FakeDb: {self._table}의 임베드 {rel}!inner는 아직 흉내 내지 않아요")
        return out

    # alarm_roll.sql의 fill_series 트리거 — series_id가 없으면 자기 id, 날짜는 만든 사람 시간대의 약속(알람) 날짜. 알람은 약속 · 루틴 알람이 아닐 때만
    def _fill_series(self, row: Row) -> None:
        db, table = self._db, self._table
        if table == "appointments":
            if row.get("series_id") is None:
                row["series_id"] = row.get("appointment_id")
            if row.get("occurrence_on") is None and isinstance(row.get("appointment_at"), str):
                row["occurrence_on"] = _local_date_key(
                    _parse_ms(row["appointment_at"]), db.timezone_of(row.get("creator_id"))
                )
        elif table == "alarms" and row.get("participant_id") is None and row.get("routine_id") is None:
            if row.get("series_id") is None:
                row["series_id"] = row.get("alarm_id")
            if row.get("occurrence_on") is None and isinstance(row.get("scheduled_at"), str):
                row["occurrence_on"] = _local_date_key(
                    _parse_ms(row["scheduled_at"]), db.timezone_of(row.get("user_id"))
                )

    # L4 표의 칸 기본값 (init_schema · ai_conversation_state.sql) — 시각은 db.now
    def _fill_defaults(self, row: Row) -> None:
        stamp = _iso(self._db.now)
        if self._table == "conversation_sessions":
            for key, value in (
                ("started_at", stamp),
                ("created_at", stamp),
                ("ended_at", None),
                ("apply_status", "PENDING"),
                ("turns", []),
                ("server_state", {}),
            ):
                if row.get(key) is None:
                    row[key] = value
        elif self._table == "ai_calls":
            for key, value in (("created_at", stamp), ("flags", []), ("io", None)):
                if row.get(key) is None:
                    row[key] = value
        elif self._table == "user_questions":
            for key, value in (("asked_at", stamp), ("answered_at", None)):
                if row.get(key) is None:
                    row[key] = value

    # alarm_events를 읽을 때 `alarms(…, appointment_participants(…))` 왼쪽 임베드 — 없는 쪽은 None (PostgREST 왼쪽 조인). 거른 뒤에 붙임
    def _with_left_embeds(self, rows: list[Row]) -> list[Row]:
        if self._table != "alarm_events" or not re.search(r"(^|[,\s])alarms\(", self._columns):
            return rows
        nested = "appointment_participants(" in self._columns
        out: list[Row] = []
        for r in rows:
            alarm = next(
                (a for a in self._db.rows("alarms") if _eq(a.get("alarm_id"), r.get("alarm_id"))), None
            )
            if alarm is None:
                out.append({**r, "alarms": None})
                continue
            part = None
            if nested:
                part = next(
                    (
                        p
                        for p in self._db.rows("appointment_participants")
                        if _eq(p.get("participant_id"), alarm.get("participant_id"))
                    ),
                    None,
                )
            embedded = {**alarm}
            if nested:
                embedded["appointment_participants"] = dict(part) if part else None
            out.append({**r, "alarms": embedded})
        return out

    # 기본키 · 기본값을 채워 넣음. alarms는 (참여 조건, 종류) 유일 제약을 지킴. 반복 회차는 (만든 사람, 묶음, 날짜) 유일 제약.
    # 어기면 이번에 넣은 것을 모두 되돌리고 오류 결과
    def _insert_rows(self, incoming: list[Row], made: list[Row]) -> Result | None:
        db, table = self._db, self._table
        all_rows = db.rows(table)

        def duplicate(name: str) -> Result:
            for done in made:
                all_rows.remove(done)
            made.clear()
            return Result(
                error={"message": f'duplicate key value violates unique constraint "{name}"', "code": "23505"}
            )

        for r in incoming:
            row: Row = {
                **DEFAULTS.get(table, {}),
                **copy.deepcopy(r),
            }  # jsonb 칸을 DB처럼 복사해 담음 (넣은 쪽이 나중에 고쳐도 표는 안 변함)
            if table in PK and row.get(PK[table]) is None and PK[table] not in row:
                row[PK[table]] = db.new_id()
            self._fill_defaults(row)
            self._fill_series(row)
            if (
                table == "alarms"
                and row.get("participant_id") is not None
                and any(
                    _eq(a.get("participant_id"), row.get("participant_id"))
                    and _eq(a.get("alarm_type"), row.get("alarm_type"))
                    for a in all_rows
                )
            ):
                return duplicate("alarms_participant_type_key")
            if table == "appointments" and any(
                _eq(a.get("creator_id"), row.get("creator_id"))
                and _eq(a.get("series_id"), row.get("series_id"))
                and _eq(a.get("occurrence_on"), row.get("occurrence_on"))
                for a in all_rows
            ):
                return duplicate("appointments_series_occurrence_key")
            if (
                table == "alarms"
                and row.get("series_id") is not None
                and any(
                    _eq(a.get("user_id"), row.get("user_id"))
                    and _eq(a.get("series_id"), row.get("series_id"))
                    and _eq(a.get("occurrence_on"), row.get("occurrence_on"))
                    for a in all_rows
                )
            ):
                return duplicate("alarms_series_occurrence_key")
            made.append(row)
            all_rows.append(row)
        return None

    def _run(self) -> Result:
        db, table = self._db, self._table
        db.calls.append(f"{self._op} {table}")
        if any(f["table"] == table and f["op"] == self._op for f in db.fail_on):
            return Result(error={"message": f"injected failure: {self._op} {table}", "code": "XX000"})
        all_rows = db.rows(table)
        if self._payload is None:
            incoming: list[Row] = []
        elif isinstance(self._payload, list):
            incoming = self._payload
        else:
            incoming = [self._payload]
        data: list[Row] = []

        if self._op == "insert":
            made: list[Row] = []
            failed = self._insert_rows(incoming, made)
            if failed:
                return failed
            data = made
        elif self._op == "upsert":
            keys = (
                [k.strip() for k in self._upsert_on_conflict.split(",")] if self._upsert_on_conflict else []
            )
            made = []
            for r in incoming:
                existing = (
                    next(
                        (a for a in all_rows if all(k in a and k in r and _eq(a[k], r[k]) for k in keys)),
                        None,
                    )
                    if keys
                    else None
                )
                if existing is not None:
                    if not self._upsert_ignore:
                        existing.update(r)
                        made.append(existing)
                    continue  # ignore_duplicates면 건너뜀 — 돌려주는 행에도 없음 (ON CONFLICT DO NOTHING … RETURNING)
                failed = self._insert_rows([r], made)
                if failed:
                    return failed
            data = made
        elif self._op == "update":
            data = [r for r in all_rows if all(f(r) for f in self._filters)]
            for r in data:
                r.update(copy.deepcopy(self._payload))
        elif self._op == "delete":
            gone = [r for r in all_rows if all(f(r) for f in self._filters)]
            gone_ids = {id(r) for r in gone}
            db.tables[table] = [r for r in all_rows if id(r) not in gone_ids]
            db.cascade(table, gone)
            data = gone
        else:
            rels = re.findall(r"(\w+)!inner\(", self._columns)
            for rel, _test in self._embed_filters:
                if rel not in rels:
                    raise RuntimeError(f"FakeDb: {rel}.… 거르기는 select에 {rel}!inner(…)가 있어야 해요")
            if rels:
                source: list[Row] = []
                for r in all_rows:
                    joined = self._with_embeds(r, rels)
                    if joined is not None:
                        source.append(joined)
            else:
                source = all_rows
            data = self._with_left_embeds([r for r in source if all(f(r) for f in self._filters)])
            if self._ordering:
                col, ascending = self._ordering
                sign = 1 if ascending else -1

                def by(x: Row, y: Row) -> float:
                    c = _compare(x.get(col), y.get(col))
                    return 0 if c is None or c != c else c * sign

                data = sorted(data, key=cmp_to_key(by))
            if self._max is not None:
                data = data[: self._max]

        out = [copy.deepcopy(r) for r in data]  # 읽은 쪽이 jsonb 칸을 고쳐도 표는 안 변함
        count = len(out) if self._count_mode else None
        if self._op != "select" and not self._returning:
            return Result()
        if self._head_only:
            return Result(count=count)
        if self._one == "single":
            if len(out) == 1:
                return Result(data=out[0])
            return Result(error={"message": f"expected 1 row, got {len(out)}", "code": "PGRST116"})
        if self._one == "maybe":
            if len(out) <= 1:
                return Result(data=out[0] if out else None)
            return Result(error={"message": "multiple rows", "code": "PGRST116"})
        return Result(data=out, count=count)


ALARM_COLUMNS = (
    "alarm_id",
    "participant_id",
    "user_id",
    "alarm_type",
    "scheduled_at",
    "intensity",
    "voice_tone",
    "sound_key",
    "vibration",
    "is_enabled",
    "snooze_limit",
    "time_source",
    "intensity_source",
    "recurrence_rule",
)
REQUEST_KEYS = "private.request_keys"


def commit_apply(db: FakeDb, args: dict[str, Any]) -> Any:
    """public.alarm_commit_apply 흉내 — 한 번에 적용하고 어디서든 실패하면 표 전부(요청 키 줄 포함)를 부르기 전으로 되돌림"""
    saved = {t: [(r, copy.deepcopy(r)) for r in rows] for t, rows in db.tables.items()}
    try:
        return _commit_apply(db, args)
    except BaseException:
        for t in list(db.tables):
            if t not in saved:
                db.tables[t] = []
        for t, pairs in saved.items():
            for (
                row,
                original,
            ) in pairs:  # 행 객체는 그대로 두고 값만 되돌림 (시험이 들고 있는 참조가 계속 맞게)
                row.clear()
                row.update(original)
            db.tables[t] = [row for row, _ in pairs]
        raise


def _ok(result: Result) -> Any:
    if result.error:
        raise RpcError(result.error.get("code", "XX000"), result.error.get("message", ""))
    return result.data


def _alarm_columns(row: Row) -> Row:
    return {k: copy.deepcopy(row[k]) for k in ALARM_COLUMNS if k in row}


def _commit_apply(db: FakeDb, a: dict[str, Any]) -> dict[str, Any]:
    uid, key, mode = a["p_user_id"], a.get("p_key"), a["p_mode"]
    key_row: Row | None = None
    if key:
        if not a.get("p_request_hash"):
            raise RpcError("P0001", "request_hash required")
        found = next(
            (
                r
                for r in db.rows(REQUEST_KEYS)
                if r["user_id"] == uid and r["fn"] == "alarm-commit" and r["key"] == key
            ),
            None,
        )
        if found is not None:
            if found["request_hash"] != a["p_request_hash"]:
                raise RpcError("P0001", "IDEMPOTENCY_KEY_REUSED")
            if found["result"] is not None:
                return {**copy.deepcopy(found["result"]), "replayed": True}
        key_row = {
            "user_id": uid,
            "fn": "alarm-commit",
            "key": key,
            "request_hash": a["p_request_hash"],
            "result": None,
        }
        db.rows(REQUEST_KEYS).append(key_row)

    # 약속
    if a.get("p_appointment") is not None:
        inserted = _ok(
            db.from_("appointments")
            .insert({**a["p_appointment"], "creator_id": uid})
            .select("appointment_id, title, appointment_at")
            .single()
            ._run()
        )
        appointment: Row | None = inserted
    else:
        appointment = copy.deepcopy(a.get("p_appointment_ref"))

    # 참여 조건
    participant_id: Any = None
    p = a.get("p_participant")
    if mode == "APPOINTMENT" and p is not None:
        participant_id = p["participant_id"]
        if p["is_new"]:
            _ok(
                db.from_("appointment_participants")
                .insert(
                    {
                        **p["fields"],
                        "participant_id": participant_id,
                        "appointment_id": (appointment or {}).get("appointment_id"),
                        "user_id": uid,
                    }
                )
                .select("participant_id")
                ._run()
            )
        else:
            done = _ok(
                db.from_("appointment_participants")
                .update(p["fields"])
                .eq("participant_id", participant_id)
                .eq("user_id", uid)
                .select("participant_id")
                ._run()
            )
            if not done:
                raise RpcError("P0002", "participant not found")

    # 알람 — 고치기 → 만들기 → 지우기
    updated: list[Row] = []
    for u in a.get("p_alarm_updates") or []:
        rows = _ok(
            db.from_("alarms")
            .update(u["patch"])
            .eq("alarm_id", u["alarm_id"])
            .eq("user_id", uid)
            .select(", ".join(ALARM_COLUMNS))
            ._run()
        )
        if len(rows) != 1:
            raise RpcError("P0002", "alarm not found")
        updated.append(_alarm_columns(rows[0]))
    inserts = [
        {
            **w,
            "user_id": uid,
            "participant_id": participant_id if mode == "APPOINTMENT" else None,
            **({"routine_id": None} if mode == "STANDALONE" else {}),
        }
        for w in a.get("p_alarm_inserts") or []
    ]
    made: list[Row] = []
    if inserts:
        made = [
            _alarm_columns(r)
            for r in _ok(db.from_("alarms").insert(inserts).select(", ".join(ALARM_COLUMNS))._run())
        ]
    removes = a.get("p_alarm_removes") or []
    if removes:
        _ok(db.from_("alarms").delete().in_("alarm_id", removes).eq("user_id", uid)._run())

    # 고친 값 기록
    if a.get("p_edit_logs"):
        _ok(db.from_("user_edit_logs").insert(a["p_edit_logs"])._run())

    before = copy.deepcopy(a.get("p_before") or [])
    gone = set(removes) | {r["alarm_id"] for r in updated}
    result = {
        "replayed": False,
        "appointment": appointment,
        "participant_id": participant_id,
        "after": [*updated, *made, *[r for r in before if r["alarm_id"] not in gone]],
        "before": before,
    }
    if key_row is not None:
        key_row["result"] = copy.deepcopy(result)
    return result


def _cmp_or(v: Any, val: Any, fallback: float) -> float:
    c = _compare(v, val)
    return fallback if c is None else c


# ---------------------------------------------------------------------------
# alarm_roll.sql의 DB 함수 흉내
# ---------------------------------------------------------------------------


def _text(v: Any) -> str | None:
    return None if v is None else _str(v)


# roll_insert_occurrence — 같은 (만든 사람, 묶음, 날짜)가 있으면 None, 없으면 약속 · 참여 · 알람 · 준비물을 쓰고 약속 id (함수가 한 트랜잭션이라 중간 실패는 흉내 안 냄)
def roll_insert_occurrence(db: FakeDb, args: dict[str, Any]) -> str | None:
    ap: Row = _d(args, "p_appointment", {})
    pt: Row = _d(args, "p_participant", {})
    alarms: list[Row] = _d(args, "p_alarms", [])
    items: list[Row] = _d(args, "p_items", [])
    if not ap.get("creator_id") or not ap.get("series_id") or not ap.get("occurrence_on"):
        raise RuntimeError("creator_id · series_id · occurrence_on이 필요함")
    if any(
        _eq(a.get("creator_id"), ap.get("creator_id"))
        and _eq(a.get("series_id"), ap.get("series_id"))
        and _eq(a.get("occurrence_on"), ap.get("occurrence_on"))
        for a in db.rows("appointments")
    ):
        return None

    appointment_id = db.new_id()
    db.rows("appointments").append(
        {
            "appointment_id": appointment_id,
            "creator_id": ap.get("creator_id"),
            "group_id": ap.get("group_id"),
            "series_id": ap.get("series_id"),
            "occurrence_on": ap.get("occurrence_on"),
            "title": ap.get("title"),
            "appointment_at": ap.get("appointment_at"),
            "dest_name": ap.get("dest_name"),
            "dest_lat": ap.get("dest_lat"),
            "dest_lng": ap.get("dest_lng"),
            "dest_radius_m": ap.get("dest_radius_m"),
            "importance": _d(ap, "importance", "NORMAL"),
            "recurrence_rule": ap.get("recurrence_rule"),
            "memo": ap.get("memo"),
            "exception_kind": None,
            "created_at": _iso(db.now),
        }
    )
    participant_id = db.new_id()
    db.rows("appointment_participants").append(
        {
            "participant_id": participant_id,
            "appointment_id": appointment_id,
            "user_id": ap.get("creator_id"),
            "origin_name": pt.get("origin_name"),
            "origin_lat": pt.get("origin_lat"),
            "origin_lng": pt.get("origin_lng"),
            "transport_type": _d(pt, "transport_type", "TRANSIT"),
            "est_travel_minutes": pt.get("est_travel_minutes"),
            "travel_source": _d(pt, "travel_source", "API"),
            "prepare_minutes": _d(pt, "prepare_minutes", 60),
            "buffer_minutes": _d(pt, "buffer_minutes", 10),
            "actual_travel_minutes": None,
            "departed_at": None,
            "arrived_at": None,
        }
    )
    for a in alarms:
        db.rows("alarms").append(
            {
                **DEFAULTS["alarms"],
                "alarm_id": db.new_id(),
                "user_id": ap.get("creator_id"),
                "participant_id": participant_id,
                "alarm_type": a.get("alarm_type"),
                "scheduled_at": a.get("scheduled_at"),
                "intensity": _d(a, "intensity", "NORMAL"),
                "voice_tone": _d(a, "voice_tone", "NEUTRAL"),
                "sound_key": a.get("sound_key"),
                "vibration": a.get("vibration"),
                "is_enabled": _d(a, "is_enabled", True),
                "time_source": _d(a, "time_source", "AUTO"),
                "intensity_source": _d(a, "intensity_source", "AUTO"),
                "last_calculated_at": _d(a, "last_calculated_at", _iso(db.now)),
            }
        )
    for i in items:
        title = (_text(i.get("title")) or "").strip()[:100]
        if not title:
            continue
        db.rows("todos").append(
            {
                "todo_id": db.new_id(),
                "user_id": ap.get("creator_id"),
                "appointment_id": appointment_id,
                "title": title,
                "todo_type": "ITEM",
                "due_date": _d(i, "due_date", ap.get("occurrence_on")),
                "is_done": False,
            }
        )
    return appointment_id


# roll_due_series — 같은 조건으로 거르고 급한 순(due_at, user_id, series_id)으로 p_limit개
#   APPOINTMENT: 묶음의 가장 늦은 회차에 규칙이 있고 개인 약속 · 대기 중 아님 · (약속 시각 < 지금 + 14일 또는 재시도 때가 지난 날짜가 있음)
#   STANDALONE: 일정 없는 알람 묶음의 가장 늦은 행에 규칙 · 시각 < 지금 + 14일. ROUTINE: 켜 둔 루틴 · 요일 있음 · 켜진 단계 알람 중 시각이 지난 것
def roll_due_series(db: FakeDb, args: dict[str, Any]) -> list[Row]:
    now = (
        _parse_ms(str(args["p_now"])) if args.get("p_now") else (db.now - _EPOCH) // timedelta(milliseconds=1)
    )
    limit = 50 if args.get("p_limit") is None else int(args["p_limit"])
    horizon = now + 14 * DAY_MS
    utc_yesterday = _iso(_EPOCH + timedelta(milliseconds=now - DAY_MS))[:10]
    due: list[dict[str, Any]] = []

    def at(r: Row, col: str) -> float:
        return _parse_ms(_str(r.get(col)))

    def newest(rows: list[Row], user: str, id_col: str, at_col: str) -> list[Row]:
        groups: dict[str, Row] = {}
        for r in rows:
            if r.get("series_id") is None:
                continue
            key = f"{_str(r.get(user))}|{_str(r.get('series_id'))}"
            cur = groups.get(key)
            better = cur is None or (
                _str(r.get("occurrence_on")) > _str(cur.get("occurrence_on"))
                or (
                    _eq(r.get("occurrence_on"), cur.get("occurrence_on"))
                    and (
                        at(r, at_col) > at(cur, at_col)
                        or (at(r, at_col) == at(cur, at_col) and _str(r.get(id_col)) < _str(cur.get(id_col)))
                    )
                )
            )
            if better:
                groups[key] = r
        return list(groups.values())

    attempts = [a for a in db.rows("roll_attempts") if a.get("skipped") is not True]

    def pending_of(user: Any, series: Any) -> dict[str, bool]:
        mine = [a for a in attempts if _eq(a.get("user_id"), user) and _eq(a.get("series_id"), series)]
        return {
            "waiting": any(at(a, "next_try_at") > now for a in mine),
            "retry_due": any(
                at(a, "next_try_at") <= now and _str(a.get("occurrence_on")) >= utc_yesterday for a in mine
            ),
        }

    # 기준 회차(template_id · 규칙 · 공동 약속)는 예외 아닌 행 중 가장 늦은 것 → 없으면 EDITED 중 가장 늦은 것, CANCELED는 기준이 아님 (20261030150000_appointment_edit.sql의 base_appointment)
    #   다음 회차 시각(due_at)을 세는 기준은 예외까지 포함한 가장 늦은 행
    def base_of(latest: Row) -> Row | None:
        mine = [
            x
            for x in db.rows("appointments")
            if _eq(x.get("creator_id"), latest.get("creator_id"))
            and _eq(x.get("series_id"), latest.get("series_id"))
            and x.get("exception_kind") != "CANCELED"
        ]

        def rank(x: Row) -> int:
            return 0 if x.get("exception_kind") is None else 1

        def occ_desc(x: Row, y: Row) -> int:
            sx, sy = _str(x.get("occurrence_on")), _str(y.get("occurrence_on"))
            return 1 if sx < sy else (-1 if sx > sy else 0)

        def by(x: Row, y: Row) -> float:
            return _first(
                rank(x) - rank(y),
                occ_desc(x, y),
                at(y, "appointment_at") - at(x, "appointment_at"),
                -1 if _str(x.get("appointment_id")) < _str(y.get("appointment_id")) else 1,
            )

        ordered = sorted(mine, key=cmp_to_key(by))
        return ordered[0] if ordered else None

    for a in newest(db.rows("appointments"), "creator_id", "appointment_id", "appointment_at"):
        base = base_of(a)
        if base is None or base.get("recurrence_rule") is None or base.get("group_id") is not None:
            continue
        p = pending_of(a.get("creator_id"), a.get("series_id"))
        when = at(a, "appointment_at")
        if p["waiting"] or not (when < horizon or p["retry_due"]):
            continue
        due.append(
            {
                "user_id": _str(a.get("creator_id")),
                "series_id": _str(a.get("series_id")),
                "kind": "APPOINTMENT",
                "template_id": _str(base.get("appointment_id")),
                "due_at": when,
            }
        )
    standalone = [
        r for r in db.rows("alarms") if r.get("participant_id") is None and r.get("routine_id") is None
    ]
    for s in newest(standalone, "user_id", "alarm_id", "scheduled_at"):
        when = at(s, "scheduled_at")
        if s.get("recurrence_rule") is None or not (when < horizon):
            continue
        due.append(
            {
                "user_id": _str(s.get("user_id")),
                "series_id": _str(s.get("series_id")),
                "kind": "STANDALONE",
                "template_id": _str(s.get("alarm_id")),
                "due_at": when,
            }
        )
    for r in db.rows("routines"):
        if r.get("is_active") is not True or not _str(_d(r, "repeat_days", "")).strip():
            continue
        past = [
            a
            for a in db.rows("alarms")
            if _eq(a.get("routine_id"), r.get("routine_id"))
            and a.get("is_enabled") is True
            and at(a, "scheduled_at") < now
        ]
        if not past:
            continue
        due.append(
            {
                "user_id": _str(r.get("user_id")),
                "series_id": _str(r.get("routine_id")),
                "kind": "ROUTINE",
                "template_id": _str(r.get("routine_id")),
                "due_at": min(at(a, "scheduled_at") for a in past),
            }
        )

    due.sort(key=lambda x: (x["due_at"], x["user_id"], x["series_id"]))
    return [{k: v for k, v in d.items() if k != "due_at"} for d in due[:limit]]

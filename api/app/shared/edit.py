"""본 레포 supabase/functions/_shared/edit.ts의 서버판 (기준 b140f70) — 일정 수정 · 삭제의 순수 계산 (appointment-edit이 DB에 쓸 내용을 정하는 부분)

  · 어느 범위(이번만 / 이후 모두)가 무엇을 하는가 (scope_effect)
  · 고친 값을 약속 행에 반영 (apply_patch) — 바뀐 것만 찾아냄
  · 알람 시각이 어떻게 바뀌는지 (alarm_changes) · 확인이 먼저인지 (needs_confirm, D67)
  · 이후 모두 고치기 · 지우기에서 지울 뒤 회차 (later_rows_to_delete)
  · 앞 일정이 바뀌거나 지워질 때 같은 날 다음 일정의 기상 알람을 다시 판정 (wake_action, 7장 7.1.3 — has_earlier_wake 그대로)
  · 만든 사람만 고치기 · 지우기 (editor_verdict)
읽고 쓰는 쪽은 edit_run.py, 요청 읽기는 edit_request.py.

JS와 달라서 맞춘 곳: Date → UTC aware datetime(ms 정수는 js.ms) · Math.round → js_round · Number(문자열) → float, 정수면 int(JSON에 37이 37.0으로 안 나가게) ·
`undefined` vs `null`은 dict에서 키가 없음 vs None으로 (AppointmentPatch의 recurrence_rule).
값 모양: AppointmentPatch · SeriesRow · EditRequest 등 JSON 닮은 값은 dict(TypedDict), DayWakeRow는 dataclass.
"""

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cmp_to_key
from typing import Any, Literal, TypedDict

from ..core.read import js_trim
from .calc import SameDayWake, has_earlier_wake
from .draft import (
    UNSET,  # TS의 `undefined` — 요청에 그 칸이 없음 (None은 TS의 null = 반복 끔). 한 곳(draft)에서만 만듦
)
from .js import js_number, js_round, ms, parse_iso, to_iso

# ---------------------------------------------------------------------------
# 범위 · 예외 표시
# ---------------------------------------------------------------------------

# 앱 EditScope와 같은 글자 (mobile/src/features/alarm/types.ts) — this = 이번 회차만, following = 이번 회차와 그 뒤 모두. 반복이 아니면 둘 다 그 일정 하나
EditScope = Literal["this", "following"]
EDIT_SCOPES: tuple[str, ...] = ("this", "following")

# appointments.exception_kind (마이그레이션 20261030150000_appointment_edit.sql)
ExceptionKind = Literal["EDITED", "CANCELED"]

# 알람이 이만큼(분) 이상 늦춰지면 저장 전에 확인 (앱 schedule.ts CONFIRM_MOVE_MINUTES와 같은 값 · D67). 앞당김은 묻지 않음
CONFIRM_MOVE_MINUTES = 5


class ScopeEffect(TypedDict):
    span: Literal["ROW", "FROM_HERE"]  # ROW = 이 행만, FROM_HERE = 이 행 + 뒤 회차 정리
    mark: Literal[
        "KEEP", "EDITED", "CLEAR"
    ]  # 이 행의 예외 표시: 그대로 / 이번만 고침으로 / 지움(이 행이 새 기준이 됨)
    deleteLater: bool  # 뒤 회차(기록 없는 것)를 지워 alarm-roll이 새 기준으로 다시 만들게 함
    rule: dict[
        str, Any
    ]  # 반복 규칙: {kind: KEEP} / {kind: SET, value} / {kind: CLEAR_ALL}(묶음 모든 행의 규칙을 비움 = 반복 끔)


def scope_effect(
    *, recurring: bool, group: bool, scope: str, patch_rule: str | None | Any, current_rule: str | None
) -> ScopeEffect:
    """고치기의 범위 효과. patch_rule: UNSET(요청에 반복 칸 없음) / None(반복 끔) / 규칙 글자

    · 공동 약속 · 반복 아닌 일정 = 그 행 하나 (반복 아닌 일정에 반복을 켜는 것은 이 행에 규칙을 넣음 → alarm-roll이 다음 회차를 만듦)
    · 반복 + 이번만: 이 행만 고치고 예외(EDITED) 표시. 반복 요일 변경은 무시 (앱 가짜 모드와 같음 — L6 안건후보: 이번만 고칠 때 반복 요일 기본안 = 무시)
    · 반복 + 이후 모두: 이 행이 새 기준(예외 표시 지움) + 뒤 회차 지움. 규칙을 비우면(None) 묶음 모든 행의 규칙을 비움 — 안 그러면 앞 회차가 기준이 되어 roll이 다시 만듦
    """
    if group:
        return {"span": "ROW", "mark": "KEEP", "deleteLater": False, "rule": {"kind": "KEEP"}}
    if not recurring:
        rule = {"kind": "SET", "value": patch_rule} if isinstance(patch_rule, str) else {"kind": "KEEP"}
        return {"span": "ROW", "mark": "KEEP", "deleteLater": False, "rule": rule}
    if scope == "this":
        return {"span": "ROW", "mark": "EDITED", "deleteLater": False, "rule": {"kind": "KEEP"}}
    if patch_rule is UNSET or patch_rule == current_rule:
        rule = {"kind": "KEEP"}
    elif patch_rule is None:
        rule = {"kind": "CLEAR_ALL"}
    else:
        rule = {"kind": "SET", "value": patch_rule}
    return {"span": "FROM_HERE", "mark": "CLEAR", "deleteLater": True, "rule": rule}


# 지우기의 범위 효과 — 반복이고 공동 약속이 아니어야 범위를 가림
#   · this: 행은 두고 알람만 지움 + CANCELED 표시 (행을 지우면 alarm-roll이 그 날짜를 다시 만듦)
#   · following: 뒤 회차 + 이 행을 지우고 남은 행의 규칙을 비움 (alarm-roll 대상에서 빠짐, 지난 행은 기록으로)
#   · ROW: 한 번짜리(반복 아님) · 공동 약속 = 약속 + 참여 + 알람을 지움
DeleteMode = Literal["ROW", "CANCEL_THIS", "FROM_HERE"]


def delete_mode(*, recurring: bool, group: bool, scope: str) -> DeleteMode:
    if group or not recurring:
        return "ROW"
    return "CANCEL_THIS" if scope == "this" else "FROM_HERE"


# ---------------------------------------------------------------------------
# 만든 사람만
# ---------------------------------------------------------------------------


def editor_verdict(
    *, row: dict[str, Any] | None, user_id: str, participating: bool | None = None
) -> Literal["OK", "NOT_VISIBLE", "NOT_CREATOR"]:
    """약속을 고치거나 지울 수 있는 사람. 개인 약속은 RLS가 이미 만든 사람에게만 보여 주고, 공동 약속은 참여 중인 그룹원도 RLS로 고칠 수 있어서 코드로 막음

    NOT_VISIBLE = 못 찾음(없거나 남의 개인 약속), NOT_CREATOR = 공동 약속의 참여만 한 사람(빠지기만 가능)
    만든 사람이 비어 있는(탈퇴한) 공동 약속은 그 약속에 참여한 사람이 고치고 지움 (9장 9-아 · 9-카, DB 정책 appointments_update · appointments_delete_creator와 같은 규칙 — 지우기는 탐색 시험 X-APPT-01 뒤에 열림)
    """
    if row is None:
        return "NOT_VISIBLE"
    if row.get("creator_id") == user_id:
        return "OK"
    if row.get("creator_id") is None and row.get("group_id") is not None and participating:
        return "OK"
    return "NOT_CREATOR"


# ---------------------------------------------------------------------------
# 고친 값 반영
# ---------------------------------------------------------------------------

# 요청에서 읽은 고칠 값 (dict) — 안 넣은 칸은 키가 없음(그대로)
#   키: title(글자) · at(datetime) · destination({name, lat?, lng?}) · importance · recurrence_rule(None = 반복 끔, 키 없음 = 그대로)
AppointmentPatch = dict[str, Any]


def _same_coord(a: Any, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(_number(a) - b) < 1e-6


def _number(value: Any) -> int | float:
    """JS `Number(v)` — 글자 · 숫자 어느 쪽이든 숫자로. 정수 값은 int로 (JSON에 37 → 37.0이 되지 않게)"""
    number = js_number(value)
    return (
        int(number) if isinstance(number, float) and number.is_integer() and abs(number) < 2**53 else number
    )


class PatchResult(TypedDict):
    next: dict[str, Any]  # title · appointment_at · dest_name · dest_lat · dest_lng · importance
    changed: dict[str, bool]  # title · at · place · importance
    any: bool  # 약속 행의 칸이 하나라도 바뀜 (반복 규칙은 따로 — scope_effect)
    affects_alarms: bool  # 알람 시각 · 강도가 달라질 수 있음 (시각 · 장소 · 중요도). 제목만이면 False


def apply_patch(row: dict[str, Any], patch: AppointmentPatch) -> PatchResult:
    title = js_trim(patch["title"]) if "title" in patch else row["title"]
    at: datetime = patch["at"] if patch.get("at") is not None else parse_iso(row["appointment_at"])
    place = patch.get("destination")
    dest_name = js_trim(place["name"]) if place is not None else row["dest_name"]
    # 이름이 바뀌었는데 좌표가 없으면 옛 좌표는 다른 곳이므로 비움 (길찾기를 새로 구하려면 좌표가 있어야 함)
    # 이름이 그대로면 옛 좌표를 유지 (편집 시트가 장소 문자열을 매번 보내도 제목만 고친 요청이 막히지 않게)
    keep_coord = place is None or (
        place.get("lat") is None and place.get("lng") is None and dest_name == row["dest_name"]
    )
    if keep_coord:
        dest_lat = None if row.get("dest_lat") is None else _number(row["dest_lat"])
        dest_lng = None if row.get("dest_lng") is None else _number(row["dest_lng"])
    else:
        dest_lat = place.get("lat") if place is not None else None
        dest_lng = place.get("lng") if place is not None else None
    importance = patch["importance"] if patch.get("importance") is not None else row["importance"]
    changed = {
        "title": title != row["title"],
        "at": ms(at) != ms(parse_iso(row["appointment_at"])),
        "place": dest_name != row["dest_name"]
        or not _same_coord(row.get("dest_lat"), dest_lat)
        or not _same_coord(row.get("dest_lng"), dest_lng),
        "importance": importance != row["importance"],
    }
    any_changed = changed["title"] or changed["at"] or changed["place"] or changed["importance"]
    return {
        "next": {
            "title": title,
            "appointment_at": to_iso(at),
            "dest_name": dest_name,
            "dest_lat": dest_lat,
            "dest_lng": dest_lng,
            "importance": importance,
        },
        "changed": changed,
        "any": any_changed,
        "affects_alarms": changed["at"] or changed["place"] or changed["importance"],
    }


# ---------------------------------------------------------------------------
# 바뀌는 알람 · 확인 (D67)
# ---------------------------------------------------------------------------

CHANGE_KINDS: tuple[str, ...] = ("WAKE", "PREPARE", "DEPARTURE")

# 앱 AlarmChange({ kind, from, to })의 서버 쪽 모양 — 시각은 ISO 글자 (dict: kind · from · to)
AlarmChangeLine = dict[str, str]


def _minutes_between(frm: str, to: str) -> int:
    return js_round((ms(parse_iso(to)) - ms(parse_iso(frm))) / 60_000)


def alarm_changes(before: Sequence[dict[str, Any]], after: Sequence[dict[str, Any]]) -> list[AlarmChangeLine]:
    """저장하면 시각이 바뀌는 기상 · 준비 · 출발 (앱 schedule.ts changedAlarms와 같은 의미 — 도착 목표는 안 셈).

    고치기 전 알람(before)과 같은 종류가 고친 뒤에도 켜진 채 있고(after) 분 단위로 시각이 다른 것만. 새로 생기거나 사라지는 알람(그날 첫 일정이 바뀌어 기상이 붙고 떨어짐)은 줄에 넣지 않음
    """
    out: list[AlarmChangeLine] = []
    for kind in CHANGE_KINDS:
        prev = next((x for x in before if x["alarm_type"] == kind), None)
        nxt = next((x for x in after if x["alarm_type"] == kind and x["alarm_type"] in CHANGE_KINDS), None)
        if (
            prev
            and nxt
            and nxt["is_enabled"]
            and _minutes_between(prev["scheduled_at"], nxt["scheduled_at"]) != 0
        ):
            out.append({"kind": kind, "from": prev["scheduled_at"], "to": nxt["scheduled_at"]})
    return out


def needs_confirm(changes: Sequence[AlarmChangeLine]) -> bool:
    """늦춰지는 알람이 있어 확인이 먼저인가 (앞당김 · 5분 미만 늦춤은 바로 저장). 앱 가짜 모드 updateAppointment의 needsConfirm과 같은 조건

    L6 안건후보: 서버 needs_confirm 경계 기본안 = 고치는 일정 자신의 알람이 5분 이상 늦춰질 때 (앱 CONFIRM_MOVE_MINUTES와 같은 값). 다른 일정의 기상 재판정은 묻지 않음
    """
    return any(_minutes_between(c["from"], c["to"]) >= CONFIRM_MOVE_MINUTES for c in changes)


# ---------------------------------------------------------------------------
# 이후 모두 — 지울 뒤 회차
# ---------------------------------------------------------------------------

# SeriesRow (dict): appointment_id · occurrence_on · appointment_at · exception_kind
SeriesRow = dict[str, Any]


def later_rows_to_delete(
    *,
    rows: Sequence[SeriesRow],
    target: dict[str, Any],
    now: datetime,
    record_ids: Collection[str],
    mode: Literal["EDIT", "DELETE"],
) -> list[str]:
    """이 회차보다 뒤(occurrence_on)의 회차 중 지울 것. 지난 회차(약속 시각이 지금 이전)와 출발 · 도착 기록이 있는 회차는 기록으로 남김

    · 이후 모두 고치기(mode EDIT)는 이번만 고친 · 지운 회차(예외)도 남김 — 사용자가 따로 손댄 회차이고 날짜 자리도 그대로여야 roll이 다시 안 만듦
    · 이후 모두 지우기(mode DELETE)는 예외 회차도 지움 (이 회차부터 반복이 끝남)
    L6 안건후보: 이후 모두 고치기에서 예외 회차 기본안 = 남김 (이번만 고친 값이 이후 모두에 덮이지 않음)
    """
    now_ms = ms(now)
    return [
        r["appointment_id"]
        for r in rows
        if r["appointment_id"] != target["appointment_id"]
        and r["occurrence_on"] > target["occurrence_on"]
        and ms(parse_iso(r["appointment_at"])) > now_ms
        and r["appointment_id"] not in record_ids
        and (mode == "DELETE" or r.get("exception_kind") is None)
    ]


# ---------------------------------------------------------------------------
# 7.1.3 재평가 — 그날 첫 일정만 기상
# ---------------------------------------------------------------------------


@dataclass
class DayWakeRow:
    """그날 내 약속 한 줄이 가진 기상 알람 (wake_enabled: None = 기상 알람이 없음)"""

    id: str
    at: datetime
    wake_enabled: bool | None


def wakes_of_rows(rows: Sequence[DayWakeRow]) -> list[SameDayWake]:
    """같은 날 약속들을 has_earlier_wake(calc/wake.py — 서버 plan.py · 앱 목업과 같은 판정)에 넘길 모양으로. 기상 알람이 없는 약속은 기상을 안 가짐"""
    return [
        SameDayWake(appointmentId=r.id, appointmentAt=r.at, isEnabled=r.wake_enabled)
        for r in rows
        if r.wake_enabled is not None
    ]


def wake_action(
    *,
    id: str,
    at: datetime,
    has_wake: bool,
    wake_rung: bool = False,
    before_wakes: Sequence[SameDayWake],
    sim: Sequence[SameDayWake],
) -> dict[str, Any]:
    """고치기 · 지우기 전(before_wakes)과 지금까지 다시 판정한 결과(sim) 기준으로, 이 약속의 기상을 어떻게 할지 → {action, earlierAfter}

    · 전에는 앞 기상에 밀려 기상이 없었는데(displaced) 이제 앞 기상이 없고 기상 알람도 없음 → ADD (첫 일정이 지워지면 둘째 일정에 기상)
    · 전에는 첫 일정이라 기상이 있었는데 이제 앞 기상이 생김 → REMOVE (앞 일정이 새로 생기거나 시각이 앞으로 옮겨짐)
    · 그 밖은 NONE — 사용자가 일부러 기상을 안 만든 약속(밀려서가 아님)을 멋대로 되살리지 않음
    · 이미 울린 기상 알람(wake_rung = 울릴 시각이 지남)은 떼지 않음 → NONE. 울린 기록(미루기 · 끄기)이 학습 · 미루기 판정의 근거라, 알람을 지우면 그 기록도 사라짐 (헤드 확정)
    earlierAfter = 지금 기준으로 앞 기상이 있는지 (다시 계산할 때 same_day_wake로 넘김)
    """
    displaced_before = has_earlier_wake(at, before_wakes, id)
    earlier_after = has_earlier_wake(at, sim, id)
    if displaced_before and not earlier_after and not has_wake:
        return {"action": "ADD", "earlierAfter": earlier_after}
    if not displaced_before and earlier_after and has_wake and not wake_rung:
        return {"action": "REMOVE", "earlierAfter": earlier_after}
    return {"action": "NONE", "earlierAfter": earlier_after}


def _u16(s: Any) -> Any:
    """JS 글자 비교는 UTF-16 코드 단위 순 (Python은 코드포인트 순 — 이모지 등이 U+E000~FFFF 글자와 뒤바뀜)"""
    return s.encode("utf-16-be", "surrogatepass") if isinstance(s, str) else s


def _compare(a: Any, b: Any) -> int:
    a, b = _u16(a), _u16(b)
    return -1 if a < b else 1 if a > b else 0


def by_time_then_id(a: DayWakeRow, b: DayWakeRow) -> int:
    """같은 날 약속을 시각 순(같으면 id 순)으로 — 비교 함수 (정렬은 cmp_to_key)"""
    return (ms(a.at) - ms(b.at)) or _compare(a.id, b.id)


# ---------------------------------------------------------------------------
# 기준 회차 고르기 (alarm-roll 본인 길이 씀 — DB 함수 roll_due_series의 base_appointment와 같은 규칙)
# ---------------------------------------------------------------------------


def pick_base_row(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """묶음의 기준 회차 = 예외 아닌 회차 중 occurrence_on이 가장 늦은 행. 없으면 이번만 고친(EDITED) 행 중 가장 늦은 것(예외뿐인 묶음이 멈추지 않게).
    지운(CANCELED) 회차는 기준이 아님(알람이 없어 이어받을 것이 없음)"""
    usable = [r for r in rows if r.get("exception_kind") != "CANCELED"]

    def rank(r: dict[str, Any]) -> int:
        return 0 if r.get("exception_kind") is None else 1

    def compare(a: dict[str, Any], b: dict[str, Any]) -> int:
        return (
            rank(a) - rank(b)
            or (-_compare(a["occurrence_on"], b["occurrence_on"]))
            or (ms(parse_iso(b["appointment_at"])) - ms(parse_iso(a["appointment_at"])))
            or _compare(a["appointment_id"], b["appointment_id"])
        )

    key: Callable[[dict[str, Any]], Any] = cmp_to_key(compare)
    ordered = sorted(usable, key=key)
    return ordered[0] if ordered else None

"""본 레포 supabase/functions/_shared/edit-request.ts의 서버판 (기준 b140f70) — appointment-edit 요청 읽기 (순수)

모양이 틀리면 VALIDATION_FAILED(400) + details.issues (core/read.py Reader).
앱 가짜 모드(mobile/src/features/alarm/api.ts)의 네 함수와 하나씩 맞음. 이름은 서버 규칙(snake_case · DB와 같은 값 이름)이고,
앱 AppointmentPatch(title · at · place · placeLat · placeLng · importance · repeatDays)는 4c(realEdit.ts)가 옮겨 담음:
at → appointment_at, place · placeLat · placeLng → destination, repeatDays → repeatToRrule(…) = recurrence_rule

  { action: 'UPDATE', appointment_id, scope?, patch: { title?, appointment_at?, destination?: { name, lat?, lng? }, importance?, recurrence_rule? (null = 반복 끔) }, confirmed? }
  { action: 'DELETE', appointment_id, scope? }
  { action: 'RESTORE', appointment_id }     이미 지운 뒤의 되돌리기 (이번만 지운 회차만 — edit_run.py)
  { action: 'LEAVE', appointment_id }       공동 약속에서 나만 빠지기
  scope: 'this' | 'following' (앱 EditScope와 같은 글자, 안 보내면 'this'). 반복이 아니면 둘 다 그 일정 하나
  confirmed: 확인 시트에서 "바꾸기"를 눌렀거나 되돌리기처럼 확인 없이 바로 저장 (앱 UpdateOptions.confirmed) — 아니면 늦춰지는 알람이 있을 때 저장하지 않고 needs_confirm으로 돌려줌

값 모양(TS의 camelCase 객체 → dict, 키는 snake_case): { action, appointment_id, scope, patch, confirmed }.
patch는 안 넣은 칸의 키가 없음(TS의 undefined), recurrence_rule만 None = 반복 끔 (edit.py AppointmentPatch).
"""

from typing import Any

from ..core.read import Reader
from .draft import IMPORTANCES, UNSET, read_recurrence
from .edit import EDIT_SCOPES, AppointmentPatch

EDIT_ACTIONS: tuple[str, ...] = ("UPDATE", "DELETE", "RESTORE", "LEAVE")

# EditRequest = dict — action별 키: UPDATE {action, appointment_id, scope, patch, confirmed} · DELETE {action, appointment_id, scope} · RESTORE · LEAVE {action, appointment_id}
EditRequest = dict[str, Any]


def _read_destination(parent: Reader) -> dict[str, Any] | None:
    """장소는 이름이 있어야 함 — readPlace(draft)는 공백 이름을 조용히 빼서 "고쳤다고 응답했는데 안 바뀜"이 되므로 제목처럼 min 1을 검, 좌표는 둘이 같이"""
    r = parent.object("destination")
    if r is None:
        return None
    name = r.string("name", min=1, max=200, required=True)
    lat = r.number("lat", min=-90, max=90)
    lng = r.number("lng", min=-180, max=180)
    if (lat is None) != (lng is None):
        r.problem_at("lat" if lat is None else "lng", "lat · lng는 같이 있어야 해요")
    if not name:
        return None
    return {"name": name, **({"lat": lat, "lng": lng} if lat is not None and lng is not None else {})}


def _read_patch(r: Reader) -> AppointmentPatch:
    p = r.object("patch", required=True)
    if p is None:
        return {}
    title = p.string("title", min=1, max=100)
    at = p.date("appointment_at")
    destination = _read_destination(p)
    importance = p.one_of("importance", IMPORTANCES)
    recurrence_rule = read_recurrence(p)
    patch: AppointmentPatch = {}
    if title is not None:
        patch["title"] = title
    if at is not None:
        patch["at"] = at
    if destination is not None:
        patch["destination"] = destination
    if importance is not None:
        patch["importance"] = importance
    if recurrence_rule is not UNSET:  # 값이 있거나 명시적 null(= 반복 끔)일 때만 키가 생김
        patch["recurrence_rule"] = recurrence_rule
    return patch


def read_edit_request(r: Reader) -> EditRequest:
    action = r.one_of("action", EDIT_ACTIONS, required=True)
    appointment_id = r.uuid("appointment_id", required=True)
    scope = r.one_of("scope", EDIT_SCOPES) or "this"
    # 이 함수는 읽기만 하고, 필수 값이 없으면 부르는 쪽(r.finish())에서 던짐. 아래 id는 그때 안 쓰이는 자리표시
    id_ = appointment_id or ""
    if action == "UPDATE":
        return {
            "action": action,
            "appointment_id": id_,
            "scope": scope,
            "patch": _read_patch(r),
            "confirmed": r.bool("confirmed") or False,
        }
    if action == "DELETE":
        return {"action": action, "appointment_id": id_, "scope": scope}
    if action == "RESTORE":
        return {"action": action, "appointment_id": id_}
    # LEAVE, 또는 action이 틀렸거나 없음 — 문제는 이미 기록됨
    return {"action": "LEAVE", "appointment_id": id_}

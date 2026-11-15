"""본 레포 supabase/functions/_shared/commit-core.ts의 서버판 (기준 b140f70) — alarm-commit이 DB에 쓸 내용을 정하는 순수 부분

어떤 알람 행을 새로 만들고 · 고치고 · 지울지, 고친 값 기록 행. 실제 쓰기는 라우터(alarm-commit)에서.
읽어 온 행(dict)과 계획(Plan)만 받아 계산함.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.shared.calc.time import add_minutes
from app.shared.calc.validate import PAST_GRACE_MINUTES
from app.shared.js import ms, parse_iso, to_iso

# alarms 행에 쓸 칸 (user_id · participant_id는 만들 때만 붙음 — 고칠 때는 keep_keys 트리거가 못 바꾸게 함). 모양은 dict:
#   alarm_type · scheduled_at · intensity · voice_tone · sound_key · vibration(직접 고른 진동만 값, 자동이면 None) · is_enabled
#   · time_source('AUTO'|'MANUAL') · intensity_source('AUTO'|'MANUAL') · last_calculated_at
AlarmWrite = dict[str, Any]


@dataclass
class AlarmDiff:
    insert: list[AlarmWrite] = field(default_factory=list)
    update: list[dict[str, Any]] = field(default_factory=list)  # {"alarm_id", "patch": AlarmWrite}
    remove: list[str] = field(default_factory=list)  # 지울 alarm_id


LIFECYCLE_TYPES = ("WAKE", "PREPARE", "DEPARTURE", "ARRIVE")


def resolve_enabled(
    *,
    checked_enabled: bool,
    now: datetime,
    old: Mapping[str, Any] | None = None,
    requested: bool | None = None,
) -> bool:
    """알람이 켜진 채로 저장되는지 — 미리 보기(to_suggestion)와 저장(diff_alarms)이 같은 규칙을 씀

    checked_enabled: 지난 알림 검사(8.2.1)를 통과했는지. 통과 못 하면 꺼서 저장
    requested: 요청의 enabled(H03 종류별 켜기 · 끄기). 없으면 지금 상태 → 새 알람이면 켬
    지금 꺼져 있어도 옛 시각이 이미 지났다면 지나서 꺼진 것으로 보고 새 시각으로 다시 켬 (사용자가 H02에서 끈 것과 구분)
    """
    grace_end = ms(add_minutes(now, PAST_GRACE_MINUTES))
    was_on = (
        True if old is None else bool(old["is_enabled"]) or ms(parse_iso(old["scheduled_at"])) < grace_end
    )
    return checked_enabled and (was_on if requested is None else requested)


def diff_alarms(
    plan: Any,
    existing: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    replace_existing: bool,
    enabled: Mapping[str, bool] | None = None,
) -> AlarmDiff:
    """계획(plan.check가 ok인 것)을 DB 행으로. existing = 이 참여 조건의 지금 알람

    · 켜짐: 지난 알림이면 꺼서 저장(8.2.1). 아니면 요청의 enabled → 지금 상태 → 켬 순.
    · 시각 출처: 직접 고른 시각이면 MANUAL, 아니면 AUTO (기상을 직접 옮겨서 따라온 준비는 AUTO)
    · 강도 4칸(전체 세기 · 배경음 · 말소리 · 진동)은 같은 값을 모든 종류에 씀. 진동은 직접 고를 때만 값, 자동이면 None
    """
    if not plan.check["ok"]:
        raise ValueError("검증을 통과하지 못한 계획은 저장할 수 없어요")
    now_iso = to_iso(now)
    fields = plan.intensity["fields"]
    manual = plan.intensity["manual"]
    diff = AlarmDiff()
    wanted: set[str] = set()

    for a in plan.check["alarms"]:
        wanted.add(a["kind"])
        old = next((x for x in existing if x["alarm_type"] == a["kind"]), None)
        requested = None if a["kind"] == "ARRIVE" or enabled is None else enabled.get(a["kind"])
        write: AlarmWrite = {
            "alarm_type": a["kind"],
            "scheduled_at": to_iso(a["at"]),
            "intensity": fields["intensity"],
            "voice_tone": fields["voiceTone"],
            "sound_key": fields["soundKey"],
            "vibration": fields["vibration"],
            "is_enabled": resolve_enabled(
                checked_enabled=a["enabled"], old=old, requested=requested, now=now
            ),
            "time_source": "MANUAL" if a.get("auto") is not None else "AUTO",
            "intensity_source": "MANUAL" if manual else "AUTO",
            "last_calculated_at": now_iso,
        }
        if old:
            diff.update.append({"alarm_id": old["alarm_id"], "patch": write})
        else:
            diff.insert.append(write)

    if replace_existing:
        for old in existing:
            if old["alarm_type"] in LIFECYCLE_TYPES and old["alarm_type"] not in wanted:
                diff.remove.append(old["alarm_id"])
    return diff


# ---- 고친 값 기록 (user_edit_logs) ----


@dataclass
class EditInput:
    screen: str  # 'H03' | 'H05' | 'S01'
    field: str
    alarm_type: str | None = None
    auto_value: Any = None
    edited_value: Any = None  # None = 자동으로 되돌림 (D42 C)


def build_edit_log_rows(
    user_id: str,
    appointment_id: str | None,
    alarms: Sequence[Mapping[str, Any]],
    edits: Sequence[EditInput],
) -> list[dict[str, Any]]:
    """앱은 새 알람의 id를 저장 전에 모르므로 alarm_type으로 가리킴 → 저장된 알람에서 id를 찾아 붙임"""

    def alarm_id_of(e: EditInput) -> str | None:
        if not e.alarm_type:
            return None
        found = next((a for a in alarms if a["alarm_type"] == e.alarm_type), None)
        return found["alarm_id"] if found else None

    return [
        {
            "user_id": user_id,
            "appointment_id": appointment_id,
            "alarm_id": alarm_id_of(e),
            "screen": e.screen,
            "field": e.field,
            "alarm_type": e.alarm_type,
            "auto_value": e.auto_value,
            "edited_value": e.edited_value,
        }
        for e in edits
    ]

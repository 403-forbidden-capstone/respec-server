"""본 레포 supabase/functions/_shared/suggest-run.ts의 서버판 (기준 b140f70) — 알람 조정 제안 만들기 (suggestion-generate 본문)

재료: DB 함수 prepare_suggestion_inputs(p_now) — 후보(앞으로 36시간 안의 반복 일정 준비 알람)와 근거 잔차(실제 준비 − 계획 준비)
규칙: calc/suggest.py suggest_prepare_advance (최근 3번 모두 넘치고 평균 5분 이상 → 5분 단위 내림, 상한 안)
결과: alarm_suggestions에 PREPARE_ADVANCE · PENDING (current_value = 지금 당김 분, suggested_value = 당긴 뒤 분, evidence = 근거 잔차)
· 당김 분 합은 120분을 넘지 않게 자름 (appointment_participants.prepare_advance_minutes 0 ~ 120)
· 같은 준비 알람에 대기 중 제안이 이미 있으면(배치가 겹침) 그 줄만 건너뜀 — 유일 색인이 막음(23505)
"""

import math
from datetime import datetime
from typing import Any

from .calc.suggest import SUGGEST_ROUND_TO, suggest_prepare_advance
from .db import db_fail, must
from .js import to_iso
from .supabase import Db

MAX_ADVANCE_MINUTES = 120

# SuggestInputRow = prepare_suggestion_inputs가 돌려주는 행 (dict, JSON 키 그대로):
#   user_id · participant_id · target_alarm_id · current_advance · before_prepare_minutes(None 가능) · cap_minutes · residuals
SuggestInputRow = dict[str, Any]
# SuggestionInsert = {user_id, target_alarm_id, suggestion_type: 'PREPARE_ADVANCE', current_value, suggested_value, evidence}
SuggestionInsert = dict[str, Any]


def _num_str(value: int | float) -> str:
    """JS `String(n)` — 정수로 떨어지는 실수는 소수점 없이 (5.0 → '5')"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def plan_suggestions(rows: list[SuggestInputRow] | tuple[SuggestInputRow, ...]) -> list[SuggestionInsert]:
    """후보 → 넣을 제안 (순수). 규칙에 안 걸리거나 120분 상한에 닿아 당길 게 없으면 뺌"""
    out: list[SuggestionInsert] = []
    for row in rows:
        advance = row.get("current_advance")
        current = max(0, 0 if advance is None else advance)
        room = math.floor((MAX_ADVANCE_MINUTES - current) / SUGGEST_ROUND_TO) * SUGGEST_ROUND_TO
        before = row.get("before_prepare_minutes")
        s = suggest_prepare_advance(
            {
                "residuals": row["residuals"],
                "beforePrepareMinutes": math.inf if before is None else before,
                "capMinutes": min(row["cap_minutes"], room),
            }
        )
        if not s:
            continue
        out.append(
            {
                "user_id": row["user_id"],
                "target_alarm_id": row["target_alarm_id"],
                "suggestion_type": "PREPARE_ADVANCE",
                "current_value": _num_str(current),
                "suggested_value": _num_str(current + s["minutes"]),
                "evidence": s["evidence"],
            }
        )
    return out


async def run_suggest(db: Db, now: datetime) -> dict[str, int]:
    """{candidates, created, skipped}"""
    rows = must(
        "prepare_suggestion_inputs", await db.rpc("prepare_suggestion_inputs", {"p_now": to_iso(now)})
    )
    plans = plan_suggestions(rows or [])
    created = 0
    skipped = 0
    # 한 줄씩 — 대기 중 제안이 이미 있는 줄(23505)만 건너뛰고 나머지는 넣음
    for plan in plans:
        error = (await db.from_("alarm_suggestions").insert(plan)).error
        if not error:
            created += 1
        elif error.get("code") == "23505":
            skipped += 1
        else:
            db_fail("alarm_suggestions insert", error)
    return {"candidates": len(rows) if rows else 0, "created": created, "skipped": skipped}

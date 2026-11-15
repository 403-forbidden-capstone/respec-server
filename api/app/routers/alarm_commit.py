"""alarm-commit — 본 레포 Edge `alarm-commit`의 서버판 (`POST /fn/alarm-commit`) — 약속 · 참여 조건 · 알람을 한 번에 저장 (상세설계서 5장 5.4.2, 화면 H05 · H03 · S01)

약속 알람:   새 약속(appointment) 또는 기존 약속(appointment_id)에 내 참여 조건과 알람을 만들거나 고침.
            기존 약속의 제목 · 시각 · 장소 · 중요도는 이 함수가 고치지 않음 (D41). 공동 약속은 멤버가 각자 appointment_id로 불러 자기 몫을 계산 (#46)
일정 없는 알람(type STANDALONE): 시각 + 반복(RRULE)만으로 기상 알람 하나 (D46 · Q10 B). 약속 · 루틴과 연결하지 않음
응답: 저장된 알람 + ring_packages(폰에 걸 예약 꾸러미, 알람 코어 RingPackage) + canceled_alarm_ids(폰에서 지울 예약, 반드시 같이 줌)
오류: VALIDATION_FAILED · NOT_FOUND · FORBIDDEN · UNPROCESSABLE(출발 시각까지 지남 · 알람 순서 어긋남) · ROUTE_UNAVAILABLE(이동시간을 정할 수 없음)
쓰기는 service_role로 하되, 읽기(약속 · 내 기록)는 부른 사람의 RLS로 해서 남의 일정에는 닿지 못함. 쓰기는 DB 함수 alarm_commit_apply 하나(한 트랜잭션)로 — app/shared/commit.py. Idempotency-Key 헤더(8~100자)가 있으면 같은 요청을 다시 보내도 첫 결과를 돌려줌
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from ..core.auth import CurrentUser
from ..core.errors import AppError
from ..core.read import Reader, read_json
from ..shared.auth import bearer_of
from ..shared.commit import CommitCtx, assert_persistable, commit_standalone, persist_appointment
from ..shared.db import must
from ..shared.plan import PlanContext, build_plan, load_profile
from ..shared.requests import read_commit_request
from ..shared.supabase import create_service_client, create_user_client
from ..shared.tz import safe_time_zone
from .alarm_suggest import kakao_key_of

router = APIRouter()


def _now() -> datetime:
    return datetime.now(UTC)


def _field(obj: Any, *names: str) -> Any:
    """읽은 요청이 dict로 오든 객체로 오든 같은 칸을 읽음 (snake_case · camelCase 둘 다)"""
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    return None


_KEY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{8,100}")


def idempotency_key_of(request: Request) -> str | None:
    """Idempotency-Key 헤더 — 8 ~ 100자, [A-Za-z0-9_.:-]만. 아니면 없는 것으로 (오류 내지 않음)"""
    value = request.headers.get("idempotency-key")
    return value if value is not None and _KEY_PATTERN.fullmatch(value) else None


def request_hash_of(value: Any) -> str:
    """read_json이 읽은 요청 값 → 키 정렬 · 공백 없는 JSON 글자 → sha256 hex (같은 키로 다른 내용을 보냈는지 DB 함수가 비교).
    본문을 다시 파싱하지 않음 — read_json이 받아 준 본문(깨진 UTF-8 · 짝 없는 반쪽 글자 포함)이 키를 붙였다고 500이 되지 않게"""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


@router.post("/fn/alarm-commit")
async def alarm_commit(request: Request, user: CurrentUser) -> dict[str, Any]:
    raw = read_json(await request.body())
    root = Reader.root(raw)
    parsed = read_commit_request(root)
    root.finish()
    key = idempotency_key_of(request)
    request_hash = request_hash_of(raw) if key else None

    user_db = create_user_client(bearer_of(request))
    ctx = CommitCtx(user_id=user.user_id, user_db=user_db, service=create_service_client(), now=_now())

    if _field(parsed, "type") == "STANDALONE":
        time_zone = safe_time_zone((await load_profile(user_db, user.user_id))["timezone"])
        return await commit_standalone(
            ctx,
            alarm=_field(parsed, "alarm"),
            edits=_field(parsed, "edits"),
            time_zone=time_zone,
            key=key,
            request_hash=request_hash,
        )

    draft = _field(parsed, "draft")
    created = _field(parsed, "appointment")

    # 새 공동 약속이면 내가 그 그룹에 참여 중이어야 함 (appointments RLS의 insert 조건과 같음)
    group_id = _field(created, "group_id", "groupId") if created else None
    if group_id:
        member = must(
            "group_members",
            await user_db.from_("group_members")
            .select("group_id")
            .eq("group_id", group_id)
            .eq("user_id", user.user_id)
            .eq("invite_status", "JOINED")
            .maybe_single(),
        )
        if not member:
            raise AppError("FORBIDDEN", "이 그룹에 참여 중이 아니라서 공동 약속을 만들 수 없어요")

    # 계산 + 검증 (alarm-suggest와 같은 계산). 기존 약속이 내게 안 보이면 NOT_FOUND
    built = await build_plan(
        PlanContext(
            user_id=user.user_id,
            db=user_db,
            now=ctx.now,
            kakao_key=kakao_key_of(request),
            http=request.app.state.http,
            settings=request.app.state.settings,
        ),
        draft,
    )
    assert_persistable(built.plan)

    return await persist_appointment(
        ctx,
        plan=built.plan,
        inputs=built.inputs,
        created=created,
        draft=draft,
        replace_existing=_field(parsed, "replace_existing", "replaceExisting"),
        edits=_field(parsed, "edits"),
        time_zone=built.time_zone,
        key=key,
        request_hash=request_hash,
    )

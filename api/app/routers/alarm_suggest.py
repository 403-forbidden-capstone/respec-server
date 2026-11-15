"""alarm-suggest — 본 레포 Edge `alarm-suggest`의 서버판 (`POST /fn/alarm-suggest`) — 알람 시각 · 강도 미리 보기 (상세설계서 5장 5.4.1, 화면 H05 · H03). DB에 쓰지 않음

입력: alarm-commit과 같은 H05 초안 모양 (appointment_id 또는 appointment{…} + participant{…} + kinds · enabled · manual_times · levels)
     앱은 place-search로 고른 장소의 좌표(lat · lng)를 destination · origin에 담아 보냄 (좌표가 있어야 길찾기 · 근사 · 실측 중앙값이 됨)
처리: 이동시간(카카오 → 같은 경로 실측 중앙값 → 직선거리 근사 → 직접 입력) → 준비 P80 → 역산 → 강도 → 지난 알림 검사
응답: 알람 시각 · 이동 { minutes, source } · 준비 근거 · 강도 · 경고. 계산 규칙은 app/shared/calc (앱 목업과 같은 숫자)
     이동시간을 구할 길이 없으면(좌표 없음 · 직접 입력 없음) 오류 대신 200 + travel.manual_required true (check.reason TRAVEL_REQUIRED) — 직접 입력해 다시 부름
오류: UNPROCESSABLE(출발 시각까지 지남)
순서는 Edge와 같음: 토큰 확인(401) → 입력 검사(400) → 계획 계산 → 오류 번역. 카카오 키가 없어도 오류가 아님 (길찾기 단계만 건너뜀, F10).
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from ..core.auth import CurrentUser
from ..core.errors import AppError
from ..core.read import Reader, read_json
from ..shared.auth import bearer_of
from ..shared.plan import BuiltPlan, PlanContext, TravelRequiredError, build_plan
from ..shared.plan_core import to_suggestion, to_travel_required_suggestion
from ..shared.requests import read_suggest_request
from ..shared.supabase import create_user_client

router = APIRouter()


def _now() -> datetime:
    return datetime.now(UTC)


def kakao_key_of(request: Request) -> str | None:
    """원본 getEnv('KAKAO_REST_API_KEY') — 앞뒤 공백을 지우고, 비면 없는 것으로 (alarm-suggest · alarm-commit은 없어도 오류가 아님)"""
    secret = request.app.state.settings.kakao_rest_api_key
    key = secret.get_secret_value().strip() if secret is not None else ""
    return key or None


@router.post("/fn/alarm-suggest")
async def alarm_suggest(request: Request, user: CurrentUser) -> dict[str, Any]:
    root = Reader.root(read_json(await request.body()))
    draft = read_suggest_request(root)
    root.finish()

    ctx = PlanContext(
        user_id=user.user_id,
        db=create_user_client(bearer_of(request)),
        now=_now(),
        kakao_key=kakao_key_of(request),
        http=request.app.state.http,
        settings=request.app.state.settings,
    )
    built: BuiltPlan
    try:
        built = await build_plan(ctx, draft, want_auto_travel=True, mode="preview")
    except TravelRequiredError as error:
        # 좌표가 없어 이동시간을 못 구함 → 오류 대신 "직접 입력해 주세요"(travel.manual_required)를 알리는 200 (alarm-commit은 502 그대로)
        return to_travel_required_suggestion(error.inputs, error.travel)
    plan = built.plan
    suggestion = to_suggestion(plan, built.inputs)

    # 출발 시각까지 지났으면 저장도 안 되는 입력 — 화면이 이유를 보일 수 있게 계산 결과를 같이 줌 (순서 어긋남은 200 + check.ok false)
    if not plan.check["ok"] and plan.check["reason"] == "DEPARTURE_PASSED":
        raise AppError(
            "UNPROCESSABLE", plan.check["message"], {"reason": plan.check["reason"], "suggestion": suggestion}
        )
    return suggestion

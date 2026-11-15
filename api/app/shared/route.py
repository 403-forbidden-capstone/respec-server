"""본 레포 supabase/functions/_shared/route.ts의 서버판 (기준 b140f70) — 이동시간 정하기 (상세설계서 7장 7.1.2 폴백 순서)

직접 입력(MANUAL)이 있으면 그 값 → 1 카카오 길찾기(API) → 2 같은 경로 실측 3건 이상의 중앙값(CACHED) → 3 좌표 직선거리 근사값 → 없음
  · alarm-commit 길은 CACHED를 계산해서 "보여 주기만" 함. DB에 travel_source = CACHED로 쓰지 않음 (alarm-commit이 막음, D5 열림)
    단 alarm-roll(roll.ts)은 길찾기가 안 되고 첫 알람이 12시간 안이면 내 실측 중앙값을 CACHED로 저장함
  · 근사값(ESTIMATE)은 사용자 확인 필수 — 저장은 확인한 값을 MANUAL로 (7.1.2 단계 4)
  · 카카오 키가 없으면(F10: 키는 받는다고 보고 만듦) 경고만 남기고 다음 단계로
부르는 곳을 deps로 받아서 카카오 · DB 없이 시험함 (tests/shared/test_route.py)

좌표(LatLng)는 `{"lat", "lng"}` dict. Warning은 응답에 그대로 실리는 JSON 모양이라 dict(TypedDict, detail은 없으면 키를 뺌),
TravelChoice · ResolvedTravel · AutoTravel · TravelDeps는 서버 안에서만 쓰는 값이라 dataclass.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

from ..kakao import KakaoError
from .calc.travel import estimate_travel_minutes, median_same_route

TravelSourceKind = Literal["API", "CACHED", "ESTIMATE", "MANUAL"]


@dataclass
class TravelChoice:
    minutes: int
    source: TravelSourceKind
    needs_confirmation: bool  # ESTIMATE: 사용자가 확인해야 저장 가능
    note: str  # 화면에 보일 근거 한 줄 (7.1.2 표 "화면 표시")
    sample_count: int | None = None  # CACHED: 중앙값에 쓴 실측 횟수


class Warning(TypedDict):  # noqa: A001 — Edge의 타입 이름 그대로
    # code: KAKAO_NOT_CONFIGURED · KAKAO_FAILED · KAKAO_PARTIAL · NO_COORDINATES · PAST_ALARM_DISABLED
    code: str
    message: str
    detail: NotRequired[str]


@dataclass
class TravelDeps:
    # 카카오로 분 단위 이동시간 → {minutes, partial}. 키가 없으면 None
    kakao: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = (
        None  # kakao({origin, destination, transport})
    )
    # 내 지난 이동 기록 (출발 · 도착이 모두 찍힌 것). 안 주면 2단계를 건너뜀
    samples: Callable[[], Awaitable[list[Any]]] | None = None


@dataclass
class AutoTravel:
    choice: TravelChoice | None
    warnings: list[Warning]


async def resolve_auto_travel(args: dict[str, Any], deps: TravelDeps) -> AutoTravel:
    """args = {origin?, destination?, transport} (원본의 인자 객체와 같은 키)"""
    warnings: list[Warning] = []
    origin = args.get("origin")
    destination = args.get("destination")
    transport = args["transport"]
    if not origin or not destination:
        warnings.append(
            {
                "code": "NO_COORDINATES",
                "message": "출발지 · 목적지 좌표가 없어서 길찾기를 못 했어요 (장소를 검색해서 골라 주세요)",
            }
        )
        return AutoTravel(None, warnings)

    # 1. 카카오 길찾기
    if deps.kakao:
        try:
            r = await deps.kakao({"origin": origin, "destination": destination, "transport": transport})
            if r["partial"]:
                warnings.append(
                    {"code": "KAKAO_PARTIAL", "message": "출발지 · 목적지 쪽 도보 구간은 빠져 있을 수 있어요"}
                )
            return AutoTravel(TravelChoice(r["minutes"], "API", False, "길찾기 기준"), warnings)
        except Exception as e:  # 원본 catch (e): 어떤 오류든 다음 단계로
            kind = e.kind if isinstance(e, KakaoError) else "UNKNOWN"
            warnings.append({"code": "KAKAO_FAILED", "message": "길찾기를 못 불러왔어요", "detail": kind})
    else:
        warnings.append({"code": "KAKAO_NOT_CONFIGURED", "message": "길찾기 키가 아직 설정되지 않았어요"})

    # 2. 같은 경로 실측 3건 이상 → 중앙값 (카카오 응답이 아니라 내 출발 · 도착 기록이라 약관 제약 없음)
    if deps.samples:
        try:
            median = median_same_route(await deps.samples(), origin, destination, transport)
            if median:
                return AutoTravel(
                    TravelChoice(
                        median["minutes"],
                        "CACHED",
                        False,
                        "최근 이동 기록을 기준으로 했어요",
                        sample_count=median["sampleCount"],
                    ),
                    warnings,
                )
        except Exception:
            pass  # 기록을 못 읽어도 다음 단계로

    # 3. 직선거리 근사값 — 좌표가 있을 때만, 사용자 확인 필수
    minutes = estimate_travel_minutes(origin, destination, transport)
    return AutoTravel(
        TravelChoice(minutes, "ESTIMATE", True, "대략적인 값이에요 · 확인이 필요해요"), warnings
    )


@dataclass
class ResolvedTravel:
    chosen: TravelChoice | None  # 적용되는 값. None이면 이동시간을 구할 길이 없음(좌표도 직접 입력도 없음)
    auto: TravelChoice | None  # 직접 입력이 없을 때의 자동 값 (직접 입력이어도 미리 보기용으로 구해 둠)
    warnings: list[Warning] = field(default_factory=list)


async def resolve_travel(
    args: dict[str, Any], deps: TravelDeps, *, want_auto: bool = False
) -> ResolvedTravel:
    """args = {origin?, destination?, transport, override?}. override = 사용자가 직접 입력한 이동시간 → MANUAL.
    want_auto(원본 options.wantAuto)가 거짓이면 직접 입력일 때 자동 값은 구하지 않음(호출 절약)"""
    override = args.get("override")
    if override is not None:
        chosen = TravelChoice(override, "MANUAL", False, "직접 입력")
        if not want_auto:
            return ResolvedTravel(chosen, None, [])
        auto = await resolve_auto_travel(args, deps)
        return ResolvedTravel(chosen, auto.choice, auto.warnings)
    auto = await resolve_auto_travel(args, deps)
    return ResolvedTravel(auto.choice, auto.choice, auto.warnings)

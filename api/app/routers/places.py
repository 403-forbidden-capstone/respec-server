"""장소 함수 두 개 — 본 레포 Edge `place-search` · `place-reverse`의 서버판 (완결점 1-12, 서버 설계 S3: `POST /fn/<Edge 함수 이름>`)

요청 · 응답 · 오류 모양이 Edge와 같음 — 앱 `callEdge()`가 표(`server_routes`)에 따라 주소만 바꿔 부름.
  place-search   { query, lat?, lng?, limit? } → { places: [{ id, name, address, lat, lng, category? }] } (결과 없으면 빈 배열)
  place-reverse  { lat, lng }                  → { place: { address, roadAddress, buildingName } | null }
순서도 Edge와 같음: 토큰 확인(401) → 입력 검사(400) → 키 확인(NOT_CONFIGURED 503) → 카카오 호출 → 오류 번역.
KAKAO_REST_API_KEY가 서버 env에 없으면 NOT_CONFIGURED(503) — 앱은 읽기 함수의 이 오류를 Edge로 한 번 더 보냄(M2).
좌표 · 검색어는 로그 · 오류 본문에 안 남김. 응답은 저장하지 않음 (카카오 약관).
"""

from typing import Any

from fastapi import APIRouter, Request

from ..core.auth import CurrentUser
from ..core.errors import AppError
from ..core.read import Reader, clean_text, js_trim, read_json
from ..kakao import KakaoError, reverse_address, search_places

router = APIRouter()


def _kakao_key(request: Request, not_configured: str) -> str:
    secret = request.app.state.settings.kakao_rest_api_key
    # 앞뒤 공백 · 줄바꿈이 섞인 키(env 파일을 손으로 고칠 때)는 헤더 오류가 되므로 Edge `getEnv`처럼 JS trim으로 지우고, 비면 없는 것으로
    key = js_trim(secret.get_secret_value()) if secret is not None else ""
    if not key:
        raise AppError("NOT_CONFIGURED", not_configured)
    return key


def _translate(error: KakaoError, *, auth: str, rate_limited: str, unavailable: str) -> AppError:
    # 401/403 = 키가 틀렸거나 카카오맵(로컬) 사용 설정이 꺼져 있음 → 설정 문제
    if error.kind == "AUTH":
        return AppError("NOT_CONFIGURED", auth)
    if error.kind == "RATE_LIMIT":
        return AppError("RATE_LIMITED", rate_limited)
    return AppError("PLACE_UNAVAILABLE", unavailable)


@router.post("/fn/place-search")
async def place_search(request: Request, user: CurrentUser) -> dict[str, Any]:
    root = Reader.root(read_json(await request.body()))
    query = root.string("query", required=True, min=1, max=100)
    lat = root.number("lat", min=-90, max=90)
    lng = root.number("lng", min=-180, max=180)
    limit = root.int("limit", min=1, max=15)
    if (lat is None) != (lng is None):
        root.problem_at("lat" if lat is None else "lng", "lat · lng는 같이 있어야 해요")
    root.finish()

    key = _kakao_key(request, "장소 검색 키가 아직 설정되지 않았어요")
    try:
        places = await search_places(
            request.app.state.http,
            key,
            query=clean_text(
                query or ""
            ),  # 짝 없는 서로게이트는 U+FFFD로 (JS URLSearchParams와 같음, 안 바꾸면 500)
            near={"lat": lat, "lng": lng} if lat is not None and lng is not None else None,
            size=limit or 10,
        )
    except KakaoError as error:
        raise _translate(
            error,
            auth="장소 검색 키가 맞지 않거나 카카오 로컬 사용 설정이 꺼져 있어요",
            rate_limited="잠시 후 다시 검색해 주세요",
            unavailable="장소 검색을 지금은 쓸 수 없어요. 잠시 후 다시 시도해 주세요",
        ) from None
    return {"places": places}


@router.post("/fn/place-reverse")
async def place_reverse(request: Request, user: CurrentUser) -> dict[str, Any]:
    root = Reader.root(read_json(await request.body()))
    lat = root.number("lat", required=True, min=-90, max=90)
    lng = root.number("lng", required=True, min=-180, max=180)
    root.finish()

    key = _kakao_key(request, "주소 조회 키가 아직 설정되지 않았어요")
    try:
        place = await reverse_address(request.app.state.http, key, lat=lat or 0, lng=lng or 0)
    except KakaoError as error:
        raise _translate(
            error,
            auth="주소 조회 키가 맞지 않거나 카카오 로컬 사용 설정이 꺼져 있어요",
            rate_limited="잠시 후 다시 시도해 주세요",
            unavailable="주소를 지금은 불러올 수 없어요. 잠시 후 다시 시도해 주세요",
        ) from None
    return {"place": place}

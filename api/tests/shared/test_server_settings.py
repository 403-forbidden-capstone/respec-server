"""서버 설정 표 읽기 (shared/server_settings.py) — 캐시 만료 · 읽기 실패 때 직전 값 · 처음부터 실패하면 꺼짐 · 표가 이김"""

import asyncio

import pytest

from app.core.config import Settings
from app.shared import plan
from app.shared.plan import PlanContext
from app.shared.server_settings import (
    CACHE_SECONDS,
    OFF,
    PredictSettings,
    SettingsCache,
    SettingsRead,
    SettingsReadError,
    parse_rows,
)
from app.shared.supabase import Result
from tests.conftest import USER_ID

UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Source:
    """표 읽기 흉내 — 호출 수를 세고, values를 바꾸거나 fail을 켜서 DB 상태를 흉내 냄"""

    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = rows
        self.calls = 0
        self.fail: str | None = None
        self.delay = 0.0

    async def __call__(self) -> dict[str, str]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise SettingsReadError(self.fail)
        return dict(self.rows)


ON = {
    "prepare_ml_enabled": "1",
    "prepare_ml_allowed_user_ids": UUID_A,
    "prepare_ml_codebook_version": "validated-v1",
}


def make(rows: dict[str, str] | None = None, *, ttl: float = 30.0, retry: float = 10.0):
    clock, source = Clock(), Source(ON if rows is None else rows)
    return SettingsCache(source, ttl=ttl, retry=retry, clock=clock), source, clock


# ---------- 값 읽기 ----------
def test_parse_rows_reads_the_three_values():
    settings = parse_rows(ON)
    assert settings == PredictSettings(enabled=True, allowed_user_ids=UUID_A, codebook_version="validated-v1")
    assert settings.allowed_count == 1


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "2", "01"])
def test_enabled_only_when_value_is_exactly_1(value):
    assert parse_rows({**ON, "prepare_ml_enabled": value}).enabled is False


def test_enabled_trims_spaces_like_the_edge_secret():
    assert parse_rows({**ON, "prepare_ml_enabled": " 1 "}).enabled is True


def test_missing_rows_or_empty_values_are_off_fail_closed():
    assert parse_rows({}) == OFF
    empty = parse_rows(
        {"prepare_ml_enabled": "", "prepare_ml_allowed_user_ids": "  ", "prepare_ml_codebook_version": " "}
    )
    assert empty == OFF and empty.allowed_count == 0


def test_codebook_version_is_trimmed_and_empty_means_none():
    assert parse_rows({**ON, "prepare_ml_codebook_version": " v2 "}).codebook_version == "v2"
    assert parse_rows({**ON, "prepare_ml_codebook_version": ""}).codebook_version is None


def test_allowed_count_counts_distinct_valid_uuids_only():
    raw = f"{UUID_A}, {UUID_A.upper()} ,{UUID_B},not-a-uuid,,"
    assert parse_rows({**ON, "prepare_ml_allowed_user_ids": raw}).allowed_count == 2


# ---------- 캐시 ----------
def test_first_read_goes_to_the_table_and_is_reused_within_the_ttl():
    cache, source, clock = make()
    first = asyncio.run(cache.get())
    assert first == SettingsRead(parse_rows(ON), "table") and source.calls == 1
    clock.now += 29.9
    assert asyncio.run(cache.get()).source == "table" and source.calls == 1  # 요청마다 DB를 안 부름


def test_value_changed_in_the_table_shows_after_the_ttl():
    cache, source, clock = make()
    asyncio.run(cache.get())
    source.rows = {**ON, "prepare_ml_allowed_user_ids": f"{UUID_A},{UUID_B}"}
    clock.now += 29.0
    assert asyncio.run(cache.get()).settings.allowed_count == 1  # 아직 캐시
    clock.now += 1.1
    after = asyncio.run(cache.get())
    assert after.settings.allowed_count == 2 and source.calls == 2


def test_default_ttl_plus_health_cache_is_within_one_minute():
    # 완결점 2-3: SQL 편집기로 바꾸면 1분 안에 /health/deep에 반영 — 설정 캐시 + /health/deep 결과 캐시(기본 5초)가 1분 안
    assert CACHE_SECONDS + Settings.model_fields["health_deep_cache_seconds"].default <= 60


def test_failure_after_a_success_keeps_the_previous_values_as_stale():
    cache, source, clock = make()
    asyncio.run(cache.get())
    source.fail = "XX000"
    clock.now += 31
    stale = asyncio.run(cache.get())
    assert stale.source == "stale" and stale.settings == parse_rows(ON)  # 직전 값을 계속 씀


def test_failures_retry_after_the_short_wait_not_on_every_call():
    cache, source, clock = make()
    asyncio.run(cache.get())
    source.fail = "XX000"
    clock.now += 31
    asyncio.run(cache.get())
    calls = source.calls
    clock.now += 9.9
    asyncio.run(cache.get())
    assert source.calls == calls  # 10초 안에는 다시 안 부름 (장애 중 DB를 두드리지 않음)
    clock.now += 0.2
    asyncio.run(cache.get())
    assert source.calls == calls + 1


def test_recovery_after_a_failure_reads_the_new_values():
    cache, source, clock = make()
    asyncio.run(cache.get())
    source.fail = "XX000"
    clock.now += 31
    asyncio.run(cache.get())
    source.fail = None
    source.rows = {**ON, "prepare_ml_enabled": "0"}
    clock.now += 11
    recovered = asyncio.run(cache.get())
    assert recovered.source == "table" and recovered.settings.enabled is False


def test_first_failure_means_off_and_unavailable():
    cache, source, _ = make()
    source.fail = "PGRST205"
    result = asyncio.run(cache.get())
    assert result == SettingsRead(OFF, "unavailable")  # 처음부터 못 읽으면 예측 꺼짐


def test_first_failure_recovers_when_the_table_becomes_readable():
    cache, source, clock = make()
    source.fail = "PGRST205"
    asyncio.run(cache.get())
    source.fail = None
    clock.now += 10.1
    assert asyncio.run(cache.get()).source == "table"


def test_unexpected_exceptions_are_also_survived(caplog):
    cache, source, clock = make()

    async def boom() -> dict[str, str]:
        raise RuntimeError("secret-looking message sb_secret_x")

    cache = SettingsCache(boom, clock=clock)
    with caplog.at_level("WARNING"):
        result = asyncio.run(cache.get())
    assert result == SettingsRead(OFF, "unavailable")
    assert "RuntimeError" in caplog.text and "sb_secret_x" not in caplog.text  # 로그는 코드(종류)만


def test_failure_log_has_only_the_error_code(caplog):
    cache, source, _ = make()
    source.fail = "XX000"
    with caplog.at_level("WARNING"):
        asyncio.run(cache.get())
    assert "XX000" in caplog.text and "validated-v1" not in caplog.text and UUID_A not in caplog.text


def test_concurrent_requests_at_expiry_read_the_table_once():
    cache, source, _ = make()
    source.delay = 0.05

    async def burst() -> list[SettingsRead]:
        return await asyncio.gather(*(cache.get() for _ in range(8)))

    results = asyncio.run(burst())
    assert source.calls == 1 and all(r.source == "table" for r in results)


# ---------- 표가 항상 이김: env 3개는 없어짐 ----------
def test_settings_has_no_ml_flag_fields_and_old_env_values_are_ignored(monkeypatch):
    for name in ("PREPARE_ML_ENABLED", "PREPARE_ML_ALLOWED_USER_IDS", "PREPARE_ML_CODEBOOK_VERSION"):
        monkeypatch.setenv(name, "1")
    settings = Settings(supabase_url="http://supabase.test")
    for field in ("prepare_ml_enabled", "prepare_ml_allowed_user_ids", "prepare_ml_codebook_version"):
        assert field not in Settings.model_fields and not hasattr(settings, field)
    # 주소 · 토큰은 비밀이라 env 그대로
    assert "prepare_ml_url" in Settings.model_fields and "prepare_ml_api_token" in Settings.model_fields


# ---------- plan.py가 표 값으로 켜고 끔 ----------
class _Profile:
    """서비스 클라이언트 흉내 — prepare_ml_verified_profiles 한 줄"""

    def __init__(self, row: dict | None, log: list[str]) -> None:
        self.row, self.log = row, log

    def from_(self, table: str):
        self.log.append(table)
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def maybe_single(self):
        return self

    def __await__(self):
        async def run():
            return Result(data=self.row)

        return run().__await__()


class _NoDaily(_Profile):
    pass


def _ctx(**settings) -> PlanContext:
    values = {
        "supabase_url": "http://supabase.test",
        "prepare_ml_url": "http://127.0.0.1:8091",
        "prepare_ml_api_token": "tok",
        **settings,
    }
    return PlanContext(user_id=USER_ID, db=_NoDaily(None, []), now=plan_now(), settings=Settings(**values))


def plan_now():
    from datetime import UTC, datetime

    return datetime(2025, 5, 22, 0, 0, tzinfo=UTC)


AT = plan_now()


def _patch(monkeypatch, read: SettingsRead | None, profile: dict | None = None):
    calls = {"table": 0, "service": []}

    async def fake_read() -> SettingsRead:
        calls["table"] += 1
        assert read is not None, "표를 읽으면 안 되는 경우인데 읽음"
        return read

    monkeypatch.setattr(plan, "predict_settings", fake_read)
    monkeypatch.setattr(plan, "create_service_client", lambda: _Profile(profile, calls["service"]))
    return calls


def run(ctx: PlanContext, allow: bool = True):
    return asyncio.run(plan._optional_ml_prediction(ctx, AT, "Asia/Seoul", allow))


ALLOWED_ON = SettingsRead(PredictSettings(True, USER_ID, "validated-v1"), "table")
VERIFIED = {
    "ml_age_code": "2",
    "ml_holiday_code": "1",
    "codebook_version": "validated-v1",
    "consent_at": "2025-01-01T00:00:00Z",
    "verified_at": "2025-01-01T00:00:00Z",
}


def test_plan_does_not_read_the_table_when_the_plan_is_not_allowed(monkeypatch):
    calls = _patch(monkeypatch, None)
    assert run(_ctx(), allow=False) is None and calls["table"] == 0


def test_plan_does_not_read_the_table_without_ml_url_or_token(monkeypatch):
    calls = _patch(monkeypatch, None)
    assert run(_ctx(prepare_ml_url="")) is None
    assert run(_ctx(prepare_ml_api_token="")) is None
    assert calls["table"] == 0 and calls["service"] == []


def test_plan_stays_off_when_the_table_says_disabled_even_if_env_says_1(monkeypatch):
    monkeypatch.setenv("PREPARE_ML_ENABLED", "1")  # 옛 env가 남아 있어도 무시
    calls = _patch(monkeypatch, SettingsRead(PredictSettings(False, USER_ID, "validated-v1"), "table"))
    assert run(_ctx()) is None and calls["table"] == 1 and calls["service"] == []  # 프로필도 안 읽음


def test_plan_stays_off_for_a_user_not_in_the_table_allowlist(monkeypatch):
    other = SettingsRead(PredictSettings(True, UUID_B, "validated-v1"), "table")
    calls = _patch(monkeypatch, other)
    assert run(_ctx()) is None and calls["service"] == []


def test_plan_stays_off_when_the_table_could_not_be_read_at_all(monkeypatch):
    calls = _patch(monkeypatch, SettingsRead(OFF, "unavailable"))
    assert run(_ctx()) is None and calls["service"] == []


def test_plan_reads_the_profile_only_after_the_table_allows_it_and_codebook_must_match(monkeypatch):
    calls = _patch(monkeypatch, ALLOWED_ON, {**VERIFIED, "codebook_version": "older-v0"})
    assert run(_ctx()) is None and calls["service"] == ["prepare_ml_verified_profiles"]  # 판이 다르면 안 부름


def test_plan_calls_the_ml_server_when_every_condition_holds(monkeypatch):
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        import json

        model = json.loads(request.content)["model"]
        return httpx.Response(200, json={"model": model, "prediction_type": "point", "minutes": 55})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctx = _ctx()
            ctx.http = client
            return await plan._optional_ml_prediction(ctx, AT, "Asia/Seoul", True)

    _patch(monkeypatch, ALLOWED_ON, VERIFIED)
    result = asyncio.run(go())
    assert result is not None and result["minutes"] == 55 and len(seen) == 1
    assert seen[0].headers["x-api-key"] == "tok"


# New pure-preparation rollout uses separate, explicit UUID allowlists.
# Legacy C1/W4 authorization must not implicitly allow these users.
def test_pure_prep_multi_allowlist_is_independent_and_defaults_off():
    from app.shared.prepare_ml import may_use_ml

    ids = f"{UUID_A},{UUID_B}"
    settings = parse_rows(
        {
            **ON,
            "pure_prep_apply_enabled": "1",
            "pure_prep_allowed_user_ids": ids,
            "pure_prep_survey_enabled": "1",
            "pure_prep_survey_allowed_user_ids": ids,
        }
    )
    assert settings.pure_prep_apply_enabled
    assert settings.pure_prep_survey_enabled
    for user in (UUID_A, UUID_B):
        assert may_use_ml(
            enabled=settings.pure_prep_apply_enabled,
            allow_for_plan=True,
            user_id=user,
            allowed_user_ids=settings.pure_prep_allowed_user_ids,
        )
        assert may_use_ml(
            enabled=settings.pure_prep_survey_enabled,
            allow_for_plan=True,
            user_id=user,
            allowed_user_ids=settings.pure_prep_survey_allowed_user_ids,
        )
    assert not may_use_ml(
        enabled=settings.pure_prep_apply_enabled,
        allow_for_plan=True,
        user_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        allowed_user_ids=settings.pure_prep_allowed_user_ids,
    )
    assert not parse_rows(ON).pure_prep_apply_enabled
    assert not parse_rows(ON).pure_prep_survey_enabled
    assert not parse_rows(
        {
            "pure_prep_apply_enabled": "1",
            "pure_prep_allowed_user_ids": "",
        }
    ).pure_prep_allowed_user_ids

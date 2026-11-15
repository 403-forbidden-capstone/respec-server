"""/health · /health/deep"""

from datetime import UTC, datetime, timedelta

from app.shared import server_settings
from tests.conftest import bearer


def _iso(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def test_health_is_200_without_token_and_calls_nothing_external(client, fake):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "version": "dev"}
    assert fake.jwks_calls == 0 and fake.rest_requests == []  # 프로세스만 확인


def test_deep_ok_when_auth_and_db_reachable_scheduler_skipped(client, fake):
    res = client.get("/health/deep")
    assert res.status_code == 200
    assert res.json() == {
        "status": "ok",
        "checks": {"auth": "ok", "db": "ok", "scheduler": "skipped"},
        # 서버 설정 표를 못 읽는 상태(이 가짜에는 표가 없음) → 예측 꺼짐. 정보라 status에는 안 들어감
        "ml": {"enabled": False, "codebook_version": None, "allowed_count": 0, "source": "unavailable"},
    }
    assert len(fake.rest_requests) == 1
    request = fake.rest_requests[0]
    assert request.headers["apikey"] == "sb_secret_test_value"
    assert "authorization" not in request.headers  # 새 sb_secret_ 키는 apikey만


def test_deep_sends_authorization_too_for_legacy_jwt_key(make_client, fake):
    client = make_client(supabase_secret_key="eyJhbGciOiJIUzI1NiJ9.e30.sig")
    assert client.get("/health/deep").status_code == 200
    assert fake.rest_requests[0].headers["authorization"].startswith("Bearer eyJ")


def test_deep_fails_with_503_when_db_down(client, fake):
    fake.rest_down = True
    res = client.get("/health/deep")
    assert res.status_code == 503
    assert res.json()["checks"]["db"] == "fail" and res.json()["status"] == "fail"


def test_deep_fails_when_no_secret_key_configured(make_client):
    res = make_client(supabase_secret_key=None).get("/health/deep")
    assert res.status_code == 503
    assert res.json()["checks"]["db"] == "fail"


def test_deep_fails_when_jwks_unavailable_and_no_cache(client, fake):
    fake.jwks_down = True
    res = client.get("/health/deep")
    assert res.status_code == 503
    assert res.json()["checks"]["auth"] == "fail"


def test_deep_scheduler_check_when_required(make_client, fake):
    client = make_client(health_require_scheduler=True)
    fake.heartbeat = [{"alive_at": _iso(1)}]
    assert client.get("/health/deep").json()["checks"]["scheduler"] == "ok"

    fake.heartbeat = [{"alive_at": _iso(20)}]  # 15분 넘게 멈춤
    res = client.get("/health/deep")
    assert res.status_code == 503 and res.json()["checks"]["scheduler"] == "fail"

    fake.heartbeat = [{"alive_at": None}]  # 한 번도 안 돎
    assert client.get("/health/deep").json()["checks"]["scheduler"] == "fail"

    fake.heartbeat = []
    assert client.get("/health/deep").json()["checks"]["scheduler"] == "fail"


def test_deep_response_has_no_urls_keys_or_errors(client, fake):
    fake.rest_down = True
    text = client.get("/health/deep").text
    assert "supabase.test" not in text and "sb_secret" not in text and "boom" not in text


def test_deep_and_health_do_not_need_token_but_me_does(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health/deep").status_code in (200, 503)
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers=bearer("x")).status_code == 401


# ---------- 예측 설정 항목 (ml) — 서버 설정 표의 현재 값 ----------
UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _rows(enabled="1", allowed=f"{UUID_A},{UUID_B}", codebook="validated-v1"):
    return [
        {"key": "prepare_ml_enabled", "value": enabled},
        {"key": "prepare_ml_allowed_user_ids", "value": allowed},
        {"key": "prepare_ml_codebook_version", "value": codebook},
    ]


class _Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


def _use_clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(server_settings._cache, "_clock", clock)
    return clock


def test_deep_shows_ml_settings_from_the_table_with_count_only(client, fake):
    fake.settings_rows = _rows()
    res = client.get("/health/deep")
    assert res.status_code == 200
    assert res.json()["ml"] == {
        "enabled": True,
        "codebook_version": "validated-v1",
        "allowed_count": 2,
        "source": "table",
    }
    assert UUID_A not in res.text and UUID_B not in res.text  # 계정 id는 안 보임 — 수만
    request = fake.settings_requests[0]
    assert request.headers["apikey"] == "sb_secret_test_value"  # 서비스 키로 읽음
    assert request.url.params["select"] == "key,value"


def test_deep_reflects_a_sql_editor_change_after_the_cache_expires(client, fake, monkeypatch):
    clock = _use_clock(monkeypatch)
    fake.settings_rows = _rows(allowed=UUID_A)
    assert client.get("/health/deep").json()["ml"]["allowed_count"] == 1

    fake.settings_rows = _rows(
        allowed=f"{UUID_A},{UUID_B}", enabled="0", codebook="validated-v2"
    )  # SQL 편집기로 바꿈
    clock.now += 20
    assert client.get("/health/deep").json()["ml"]["allowed_count"] == 1  # 캐시 안 — DB를 다시 안 부름
    assert len(fake.settings_requests) == 1

    clock.now += 11  # 30초가 지남 (완결점 2-3: 1분 안)
    ml = client.get("/health/deep").json()["ml"]
    assert ml == {"enabled": False, "codebook_version": "validated-v2", "allowed_count": 2, "source": "table"}
    assert len(fake.settings_requests) == 2


def test_deep_keeps_the_previous_ml_values_when_the_table_read_fails(client, fake, monkeypatch):
    clock = _use_clock(monkeypatch)
    fake.settings_rows = _rows()
    client.get("/health/deep")
    fake.settings_down = True
    clock.now += 31
    res = client.get("/health/deep")
    assert res.status_code == 200  # 예측 설정은 정보 — 읽기 실패가 상태를 503으로 만들지 않음
    assert res.json()["ml"] == {
        "enabled": True,
        "codebook_version": "validated-v1",
        "allowed_count": 2,
        "source": "stale",  # 직전 값을 계속 씀
    }


def test_deep_ml_is_off_and_unavailable_when_the_table_never_read(client, fake):
    fake.settings_down = True
    ml = client.get("/health/deep").json()["ml"]
    assert ml == {"enabled": False, "codebook_version": None, "allowed_count": 0, "source": "unavailable"}


def test_deep_ml_handles_a_broken_table_response_as_a_read_failure(client, fake):
    fake.settings_body = {"not": "a list"}
    assert client.get("/health/deep").json()["ml"]["source"] == "unavailable"


def test_deep_ml_missing_rows_or_empty_values_mean_off(client, fake):
    fake.settings_rows = [{"key": "prepare_ml_enabled", "value": "1"}]  # 허용 계정 · 판 줄이 없음
    ml = client.get("/health/deep").json()["ml"]
    assert ml["enabled"] is True and ml["allowed_count"] == 0 and ml["codebook_version"] is None


def test_deep_ml_log_and_response_never_carry_values_on_failure(client, fake, caplog):
    fake.settings_rows = None  # 표 없음(404)
    with caplog.at_level("WARNING"):
        text = client.get("/health/deep").text
    assert "PGRST205" in caplog.text and "no table" not in caplog.text and "sb_secret" not in text


def test_health_plain_does_not_read_the_settings_table(client, fake):
    client.get("/health")
    assert fake.settings_requests == []

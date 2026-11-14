"""/health · /health/deep"""

from datetime import UTC, datetime, timedelta

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
    assert res.json() == {"status": "ok", "checks": {"auth": "ok", "db": "ok", "scheduler": "skipped"}}
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

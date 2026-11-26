"""End-to-end synthetic pure-prep session records, with no production DB writes.

The FakeDb stores the *same named columns* used by the runtime query.
The model response in these tests is a MOCK of the user's separately observed
25.916 min / 0.86387 response to a 30 min baseline; these tests DO NOT
measure real-model accuracy or retrain CatBoost.
"""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr

from app.shared.plan_core import compute_plan, to_suggestion
from app.shared.survey_pure_prep import (
    BASELINE,
    MODEL_STORED,
    pure_prep_candidate,
)
from tests.fake_db import FakeDb
from tests.shared.plan_helpers import PROFILE, api
from tests.shared.test_commit import commit_args, inputs_of, new_draft, persist_appointment, setup

KST = ZoneInfo("Asia/Seoul")
USER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"  # synthetic, never inserted into Supabase
APPOINTMENT = datetime(2026, 11, 27, 10, tzinfo=KST)
NOW = datetime(2026, 11, 26, 12, tzinfo=KST)
UPDATED = datetime(2026, 11, 15, 0, tzinfo=KST)
CONSENT = datetime(2026, 11, 14, 0, tzinfo=KST)
BOOK = "2024-time-use-age-holiday-v1"


def history_rows(*, n: int, prior_day: bool = True, bad_record: bool = False) -> list[dict]:
    """Insert 30-minute confirmed sessions, one per day, newest 26 Nov (or 25 Nov)."""
    newest = datetime(2026, 11, 26 if prior_day else 25, 8, tzinfo=KST)
    out = []
    for j in range(n):
        departed = newest - timedelta(days=j)
        started = departed - timedelta(minutes=30)
        out.append(
            {
                "user_id": USER,
                "prepare_started_at": started.isoformat(),
                "departed_at": departed.isoformat(),
                "prepare_started_source": "PREPARE_ALARM",
                "departed_source": "LOCATION",
            }
        )
    if bad_record:
        departed = newest - timedelta(days=n)
        out.append(
            {
                "user_id": USER,
                "prepare_started_at": (departed - timedelta(minutes=30)).isoformat(),
                "departed_at": departed.isoformat(),
                "prepare_started_source": "ROUTINE_STEP",
                "departed_source": "LOCATION",
            }
        )
    return out


def fake_env(
    monkeypatch,
    n=10,
    *,
    prior_day=True,
    consent=True,
    verified=True,
    bad_record=False,
    since=UPDATED,
    model_status=200,
    response_target="survey_start_cluster_proxy",
):
    db = FakeDb()
    db.rows("appointment_participants").extend(history_rows(n=n, prior_day=prior_day, bad_record=bad_record))
    db.rows("prepare_ml_verified_profiles").append(
        {
            "user_id": USER,
            "ml_age_code": "2",
            "ml_holiday_code": None,
            "codebook_version": BOOK,
            "consent_at": CONSENT.isoformat() if verified else None,
            "verified_at": CONSENT.isoformat() if verified else None,
        }
    )
    profile = {
        "default_outing_prep_minutes": 35,
        "default_outing_prep_updated_at": since.isoformat(),
        "ml_profile_consented_at": CONSENT.isoformat() if consent else None,
    }

    async def settings_lookup():
        return SimpleNamespace(settings=SimpleNamespace(codebook_version=BOOK))

    monkeypatch.setattr("app.shared.survey_pure_prep.predict_settings", settings_lookup)
    monkeypatch.setattr("app.shared.survey_pure_prep.create_service_client", lambda: db)
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        assert request.url.host == "prepare-ml"
        assert request.headers["x-api-key"] == "synthetic-token"
        assert payload["model"] == "prep_start_ratio_v2"
        assert payload["baseline_definition"] == BASELINE
        assert payload["features"]["baseline_observed"] == 30
        assert payload["features"]["연령코드"] == "2"
        if model_status != 200:
            return httpx.Response(model_status, json={"detail": "synthetic model unavailable"})
        return httpx.Response(
            200,
            json={
                "model": "prep_start_ratio_v2",
                "training_target": response_target,
                "baseline_definition": BASELINE,
                "prediction_type": "ratio",
                "minutes": 25.916,
                "ratio": 0.86387,
            },
        )

    settings = SimpleNamespace(
        prepare_ml_url="http://prepare-ml:8091",
        prepare_ml_api_token=SecretStr("synthetic-token"),
    )
    return db, profile, settings, captured, handler


def candidate_for(monkeypatch, n=10, *, allow_model=True, **kwargs):
    db, profile, settings, captured, handler = fake_env(monkeypatch, n, **kwargs)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await pure_prep_candidate(
                db=db,
                user_id=USER,
                profile=profile,
                now=NOW,
                appointment_at=APPOINTMENT,
                timezone="Asia/Seoul",
                settings=settings,
                http=client,
                allow_model=allow_model,
            )

    result = asyncio.run(run())
    return result, captured, db


@pytest.mark.parametrize("n", [0, 8, 9])
def test_synthetic_fewer_than_ten_confirmed_sessions_never_calls_model(monkeypatch, n):
    choice, calls, db = candidate_for(monkeypatch, n)
    assert len(db.rows("appointment_participants")) == n
    assert choice == {"source": "PROFILE", "minutes": 35}
    assert calls == []


def test_synthetic_exactly_ten_previous_day_records_calls_model_and_preserves_target(monkeypatch):
    choice, calls, db = candidate_for(monkeypatch, 10)
    assert len(db.rows("appointment_participants")) == 10
    assert len(calls) == 1
    assert calls[0]["history_count"] == 10
    # 25.916 is the user's observed real-server response, intentionally mocked.
    assert choice == {
        "source": "MODEL",
        "model": MODEL_STORED,
        "minutes": 26,
        "sampleCount": 10,
    }


@pytest.mark.parametrize(
    ("scenario", "options"),
    [
        ("ten_but_no_yesterday", {"prior_day": False}),
        ("nine_valid_plus_one_bad_source", {"n": 9, "bad_record": True}),
        ("ten_but_consent_missing", {"consent": False}),
        ("ten_but_verified_age_missing", {"verified": False}),
        (
            "ten_but_profile_changed_after_last_record",
            {"since": datetime(2026, 11, 26, 9, tzinfo=KST)},
        ),
        ("ml_pilot_off", {"allow_model": False}),
        ("ml_http_unavailable", {"model_status": 503}),
        ("wrong_training_target", {"response_target": "actual_start_depart"}),
    ],
)
def test_synthetic_candidate_falls_back_without_false_model_claim(monkeypatch, scenario, options):
    n = options.get("n", 10)
    kwargs = {k: v for k, v in options.items() if k not in ("n", "allow_model")}
    choice, calls, _ = candidate_for(monkeypatch, n, allow_model=options.get("allow_model", True), **kwargs)
    assert choice == {"source": "PROFILE", "minutes": 35}, scenario
    if scenario in {
        "ten_but_no_yesterday",
        "nine_valid_plus_one_bad_source",
        "ten_but_consent_missing",
        "ten_but_profile_changed_after_last_record",
        "ml_pilot_off",
    }:
        assert calls == [], scenario


@pytest.mark.parametrize(
    ("records", "expect_model", "expect_pure_minutes"),
    [
        (9, False, 35),
        (10, True, 26),
    ],
)
def test_synthetic_history_through_plan_preview_commit_and_persist(
    monkeypatch, records, expect_model, expect_pure_minutes
):
    choice, calls, _ = candidate_for(monkeypatch, records)
    assert (choice["source"] == "MODEL") == expect_model
    assert len(calls) == int(expect_model)

    env = setup()  # FakeDb.with_commit_rpc(), not a real Supabase connection
    env.ctx.user_id = USER
    draft = new_draft(
        at=APPOINTMENT,
        travel_minutes=20,
        prepare_minutes=None,
        before_prepare_minutes=None,  # must NOT silently add 20 minutes
        with_wake=True,
    )
    profile = {**PROFILE, "default_buffer_minutes": 0}
    inputs = inputs_of(draft, pure_prep_candidate=choice, profile=profile)
    preview = compute_plan(inputs, api(20), "preview")
    saved_plan = compute_plan(inputs, api(20), "save")
    assert preview.prepare_target == saved_plan.prepare_target == "PREPARE_START_TO_DEPART"
    assert [x["at"] for x in preview.chain] == [x["at"] for x in saved_plan.chain]
    assert to_suggestion(preview)["prepare"]["pure_minutes"] == expect_pure_minutes
    # No arbitrary wake-to-preparation 20-minute padding in a new pure-prep plan.
    assert preview.before_prepare_minutes == 0
    assert preview.prepare["minutes"] == expect_pure_minutes

    actual = {a["kind"]: a["at"].astimezone(KST).strftime("%H:%M") for a in preview.chain}
    assert actual["DEPARTURE"] == "09:40"
    assert actual["WAKE"] == ("09:05" if records == 9 else "09:14")

    args = commit_args(
        draft,
        {"plan": saved_plan, "inputs": inputs, "created": {"title": "Synthetic E2E"}},
    )
    response = asyncio.run(persist_appointment(env.ctx, **args))
    participant = next(
        p
        for p in env.db.rows("appointment_participants")
        if p["participant_id"] == response["participant_id"]
    )
    assert participant["user_id"] == USER
    assert participant["prepare_minutes"] == expect_pure_minutes
    assert participant["outing_prep_minutes"] == expect_pure_minutes
    assert participant["outing_prep_source"] == ("MODEL" if expect_model else "PROFILE")
    assert participant["outing_prep_model"] == (MODEL_STORED if expect_model else None)
    actual_saved = {
        a["alarm_type"]: a["scheduled_at"]
        for a in env.db.rows("alarms")
        if a["participant_id"] == response["participant_id"]
    }
    for alarm in saved_plan.chain:
        assert actual_saved[alarm["kind"]] == alarm["at"].astimezone(ZoneInfo("UTC")).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

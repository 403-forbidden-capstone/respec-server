"""Pure preparation v2 integration: distinct units, safe gates and DB provenance."""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from app.shared.plan_core import PlanDraft, compute_plan, to_suggestion
from app.shared.survey_pure_prep import (
    BASELINE, MIN_HISTORY, completed_history, pure_profile_candidate, request_v2,
)
from tests.shared.test_plan_core import base, dest
from tests.shared.plan_helpers import api, kst
from app.shared.commit import CommitCtx, persist_appointment
from tests.fake_db import FakeDb
from app.shared.calc.prepare import prepare_stats_of
from app.shared.plan_core import PlanInputs


def candidate(minutes=35):
    return {"source":"MODEL","model":"prep_start_ratio_v2_survey_pilot",
            "minutes":minutes, "sampleCount":10}


def test_pure_model_changes_only_preparation_interval():
    i=base(
        draft=PlanDraft(at=kst(22,600),destination=dest(),before_prepare_minutes=20),
        pure_prep_candidate=candidate(35),
        ml_prediction={"model":"w4_mae","minutes":90},
    )
    plan=compute_plan(i,api(50))
    assert plan.prepare_target=="PREPARE_START_TO_DEPART"
    assert plan.prepare["source"]=="MODEL"
    assert plan.prepare["mlModel"]=="prep_start_ratio_v2_survey_pilot"
    assert plan.prepare["minutes"]==55  # pure 35 + pre-prep 20, once
    dto=to_suggestion(plan)["prepare"]
    assert dto["target"]=="PREPARE_START_TO_DEPART"
    assert dto["pure_minutes"]==35
    assert dto["minutes"]==55


def test_manual_prepare_duration_preempts_pure_model():
    i=base(
        draft=PlanDraft(at=kst(22,600),destination=dest(),
                        prepare_minutes=47,before_prepare_minutes=20),
        pure_prep_candidate=candidate(),
    )
    plan=compute_plan(i,api(50))
    assert plan.prepare_target=="WAKE_TO_DEPART"
    assert plan.prepare["minutes"]==47
    assert plan.prepare["source"]=="MANUAL"


def test_non_wake_schedule_never_adds_morning_activity_to_pure_minutes():
    i=base(
        draft=PlanDraft(at=kst(22,1080),destination=dest()),
        same_day_wake=True,
        pure_prep_candidate=candidate(35),
    )
    plan=compute_plan(i,api(20))
    assert plan.with_wake is False
    assert plan.prepare["minutes"]==35
    assert to_suggestion(plan)["prepare"]["pure_minutes"]==35


def test_survey_model_needs_previous_day_record_and_ten_valid_history():
    cutoff=datetime(2026,11,25,10,tzinfo=UTC)
    def row(n,source="PREPARE_ALARM"):
        ended=cutoff-timedelta(hours=n)
        return {
            "prepare_started_at":(ended-timedelta(minutes=30)).isoformat(),
            "departed_at":ended.isoformat(),
            "prepare_started_source":source,
            "departed_source":"LOCATION",
        }
    assert len(completed_history([row(n) for n in range(1,11)],cutoff,None))==10
    assert len(completed_history([row(n) for n in range(1,10)],cutoff,None))<MIN_HISTORY
    assert not completed_history([row(1,"ROUTINE_STEP")],cutoff,None)
    assert pure_profile_candidate({"default_outing_prep_minutes":40})=={"source":"PROFILE","minutes":40}
    assert pure_profile_candidate({"default_prepare_minutes":40}) is None


def test_request_v2_provenance_and_fallback():
    payload={"model":"prep_start_ratio_v2","baseline_definition":BASELINE,
             "history_count":10,
             "features":{"weekday_code":"2","평토일구분코드":"1",
                         "휴일종류코드":"__MISSING__","연령코드":"2",
                         "baseline_observed":30.0}}
    def handler(request):
        assert request.headers["x-api-key"]=="testing-only"
        return httpx.Response(200,json={
            "model":"prep_start_ratio_v2", "training_target":"survey_start_cluster_proxy",
            "baseline_definition":BASELINE,"prediction_type":"ratio",
            "minutes":27,"ratio":.9,
        })
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            good=await request_v2(http,"https://model.example.org/ml","testing-only",payload)
            blocked=await request_v2(http,"http://public.example.org/ml","testing-only",payload)
            return good,blocked
    assert asyncio.run(run())==(27,None)

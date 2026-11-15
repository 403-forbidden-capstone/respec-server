"""plan-core.ts → plan_core.py 시험 (원본 plan-core.test.ts의 모든 경우) — 앱 목업(estimateAppointment → alarmChain)과 같은 결과가 나오는지, 저장된 값을 어떻게 이어받는지"""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.shared.calc.prepare import PrepareBasis, PrepareStats, prepare_source_label, prepare_stats_of
from app.shared.draft import PlaceInput
from app.shared.plan_core import (
    PlanDraft,
    PlanInputs,
    clamp_prepare_to_bound,
    compute_plan,
    first_prepare_minutes,
    infer_before_prepare,
    resolve_basics,
    resolve_overrides,
    to_suggestion,
)
from app.shared.route import ResolvedTravel, TravelChoice
from tests.shared.plan_helpers import PROFILE as profile
from tests.shared.plan_helpers import alarm_row as row
from tests.shared.plan_helpers import api, hm, iso_of, kst


def dest(name: str = "세종대학교", **kw: Any) -> PlaceInput:
    return PlaceInput(name=name, **kw)


def base(**over: Any) -> PlanInputs:
    """목업 기본 상태 — 새 일정: 5월 22일 10시 팀 주간회의 (목업 a1 — 이동 50분 · 여유 10분)

    stats: 목업 estimate n 10 · 공통 50 · 개인 오차 P80 20 → 보정 10 → 예측 60. 기록은 이미 기상~출발이라 P80 70 그대로
    """
    args: dict[str, Any] = {
        "now": kst(22, 400),
        "draft": PlanDraft(at=kst(22, 600), destination=dest(lat=37.55, lng=127.07)),
        "profile": profile,
        "existing_alarms": [],
        "stats": PrepareStats(sampleCount=10, p80=70),
        "late_count4w": 1,
        "same_day_wake": False,
    }
    args.update(over)
    return PlanInputs(**args)


def text(plan) -> list[str]:
    return [f"{x['kind']} {hm(iso_of(x['at']))}{'*' if x.get('auto') else ''}" for x in plan.chain]


NO_STATS = PrepareStats(sampleCount=0, p80=None)


def test_mockup_a1_same_numbers():
    """목업 a1과 같은 숫자: 기상 8:00 · 준비 8:30 · 출발 9:00 · 도착 목표 9:50, 준비 60 = 공통 50 + 보정 10"""
    plan = compute_plan(
        base(draft=PlanDraft(at=kst(22, 600), destination=dest(), before_prepare_minutes=30)), api(50)
    )
    assert text(plan) == ["WAKE 08:00", "PREPARE 08:30", "DEPARTURE 09:00", "ARRIVE 09:50"]
    assert [plan.prepare["commonP80"], plan.prepare["correction"], plan.prepare["minutes"]] == [50, 10, 60]
    assert plan.intensity["fields"]["intensity"] == "NORMAL"  # NORMAL 일정 + 늦잠 1회 → 중
    assert plan.check["ok"]


def test_c1_prediction_applies_and_model_shown():
    """C1 예측이 준비시간과 기상 알람 계산에 적용되고 응답에 모델이 표시됨"""
    plan = compute_plan(base(stats=NO_STATS, ml_prediction={"model": "c1_mae", "minutes": 80}), api(50))
    assert plan.prepare["minutes"] == 80
    assert plan.prepare["autoMinutes"] == 80
    assert plan.prepare["source"] == "MODEL"
    assert plan.prepare["confidence"] == "LOW"
    assert text(plan) == ["WAKE 07:40", "PREPARE 08:00", "DEPARTURE 09:00", "ARRIVE 09:50"]
    dto = to_suggestion(plan)
    assert dto["prepare"]["model"] == "c1_mae"
    assert dto["prepare"]["source"] == "MODEL"
    assert dto["prepare"]["auto_minutes"] == 80


def test_w4_manual_prepare_wins():
    """W4 예측이 적용돼도 직접 준비시간은 수동값 우선"""
    ml = {"model": "w4_mae", "minutes": 90}
    automatic = compute_plan(base(ml_prediction=ml), api(50))
    assert automatic.prepare["minutes"] == 90
    assert automatic.prepare["mlModel"] == "w4_mae"
    assert to_suggestion(automatic)["prepare"]["model"] == "w4_mae"
    manual = compute_plan(
        base(ml_prediction=ml, draft=PlanDraft(at=kst(22, 600), destination=dest(), prepare_minutes=30)),
        api(50),
    )
    assert manual.prepare["source"] == "MANUAL"
    assert manual.prepare["minutes"] == 30
    assert manual.prepare.get("mlModel") is None
    assert "model" not in to_suggestion(manual)["prepare"]


def test_no_model_for_extra_same_day_schedule_without_wake():
    """기상 없는 당일 추가 일정에는 모델 예측을 사용하지 않음"""
    plan = compute_plan(
        base(
            draft=PlanDraft(at=kst(22, 1080), destination=dest("강남역"), buffer_minutes=5),
            same_day_wake=True,
            ml_prediction={"model": "w4_mae", "minutes": 90},
        ),
        api(20),
    )
    assert plan.with_wake is False
    assert plan.prepare["minutes"] == 15
    assert plan.prepare["source"] != "MODEL"


def test_no_records_common_p80_plus_default_before_prepare():
    """기록이 없는 사용자는 공통 P80 그대로 (50분) + 기본 준비 전 활동 20분"""
    plan = compute_plan(base(stats=NO_STATS), api(50))
    assert plan.prepare["minutes"] == 50
    assert plan.prepare["source"] == "DEFAULT"
    assert plan.before_prepare_minutes == 20
    assert text(plan) == ["WAKE 08:10", "PREPARE 08:30", "DEPARTURE 09:00", "ARRIVE 09:50"]


def test_second_schedule_same_day_no_wake_common_15():
    """같은 날 앞선 일정에 기상 알람이 있으면 기상 없이 준비부터, 공통 15분 (목업 a2)"""
    plan = compute_plan(
        base(
            draft=PlanDraft(at=kst(22, 1080), destination=dest("강남역"), buffer_minutes=5),
            same_day_wake=True,
            stats=NO_STATS,
            now=kst(22, 600),
        ),
        api(20),
    )
    assert plan.with_wake is False
    assert plan.prepare["minutes"] == 15
    assert text(plan) == ["PREPARE 17:20", "DEPARTURE 17:35", "ARRIVE 17:55"]


def test_manual_prepare_travel_time_before_auto():
    """직접 고른 준비 · 이동 · 시각이 자동보다 먼저 (D42 C)"""
    travel = ResolvedTravel(
        chosen=TravelChoice(minutes=70, source="MANUAL", needs_confirmation=False, note="직접 입력"),
        auto=TravelChoice(minutes=50, source="API", needs_confirmation=False, note="길찾기 기준"),
        warnings=[],
    )
    plan = compute_plan(
        base(
            draft=PlanDraft(
                at=kst(22, 600),
                destination=dest(),
                prepare_minutes=40,
                before_prepare_minutes=10,
                manual_times={"PREPARE": kst(22, 500)},
            )
        ),
        travel,
    )
    assert text(plan) == ["WAKE 08:00", "PREPARE 08:20*", "DEPARTURE 08:40", "ARRIVE 09:50"]
    dto = to_suggestion(plan)
    assert dto["travel"]["minutes"] == 70
    assert dto["travel"]["auto_minutes"] == 50
    assert dto["prepare"]["source"] == "MANUAL"
    assert dto["prepare"]["auto_minutes"] == 60  # 내 기록 P80 70(이미 기상~출발) → 공통 50 + 보정 10


def test_chosen_kinds_and_no_alarm_schedule():
    """고른 종류만 · 알람 없는 일정"""
    some = compute_plan(
        base(draft=PlanDraft(at=kst(22, 600), destination=dest("x"), kinds=["WAKE"])), api(50)
    )
    assert [x["kind"] for x in some.chain] == ["WAKE", "ARRIVE"]
    assert len(some.full_chain) == 4  # 미리 보기용 전체는 그대로
    none = compute_plan(base(draft=PlanDraft(at=kst(22, 600), destination=dest("x"), kinds=[])), api(50))
    assert none.chain == []
    assert none.check["ok"] is True and none.check["alarms"] == []


def test_passed_wake_off_with_warning_and_departure_passed_blocks():
    """지난 기상은 끄고 저장 + 경고, 출발까지 지나면 막힘 (8.2.1)"""
    late = compute_plan(
        base(now=kst(22, 490)), api(50)
    )  # 기상 < 8:10 + 5분 (이 입력의 예측 준비는 60분이라 기상 8:00)
    assert late.check["ok"]
    assert late.check["ok"] and late.check["alarms"][0]["enabled"] is False
    assert [[w["code"], w["detail"]] for w in late.warnings] == [["PAST_ALARM_DISABLED", "WAKE"]]
    dto = to_suggestion(late)
    assert dto["alarms"][0]["disabled_reason"] == "PASSED"
    assert dto["alarms"][0]["is_enabled"] is False

    # 경고 문구: 저장한 뒤(alarm-commit)는 "끄고 저장했어요", 미리 보기(alarm-suggest)는 아직 저장하지 않았으니 "저장할 때 꺼져요"
    assert late.warnings[0]["message"] == "이미 지난 알림은 끄고 저장했어요"
    preview = compute_plan(base(now=kst(22, 490)), api(50), "preview")
    assert [[w["code"], w["detail"], w["message"]] for w in preview.warnings] == [
        ["PAST_ALARM_DISABLED", "WAKE", "이미 지난 알림은 저장할 때 꺼져요"]
    ]
    assert (
        compute_plan(base(now=kst(22, 490)), api(50), "save").warnings[0]["message"]
        == late.warnings[0]["message"]
    )

    gone = compute_plan(base(now=kst(22, 545)), api(50))
    assert gone.check["ok"] is False
    assert (not gone.check["ok"] and gone.check["reason"]) == "DEPARTURE_PASSED"


# ---- 이미 저장된 값 이어받기 ----

APPOINTMENT = {
    "appointment_id": "ap1",
    "creator_id": "u1",
    "group_id": None,
    "title": "팀 주간회의",
    "appointment_at": iso_of(kst(22, 600)),
    "dest_name": "세종대학교",
    "dest_lat": 37.55,
    "dest_lng": 127.07,
    "importance": "NORMAL",
    "recurrence_rule": None,
}
PARTICIPANT = {
    "participant_id": "pt1",
    "appointment_id": "ap1",
    "user_id": "u1",
    "origin_name": "학교",
    "origin_lat": 37.5,
    "origin_lng": 127.0,
    "transport_type": "CAR",
    "est_travel_minutes": 30,
    "travel_source": "API",
    "prepare_minutes": 60,
    "buffer_minutes": 5,
}


def existing_draft(**kw: Any) -> PlanDraft:
    return PlanDraft(appointment_id="ap1", **kw)


def test_existing_appointment_uses_db_values_not_request():
    """기존 약속은 DB의 시각 · 장소 · 중요도를 씀 (D41: 요청 값은 무시) · 출발지 · 수단 · 여유는 저장된 값"""
    b = resolve_basics(
        draft=PlanDraft(at=kst(25, 0), destination=dest("딴 곳"), importance="LOW", appointment_id="ap1"),
        profile=profile,
        appointment=APPOINTMENT,
        participant=PARTICIPANT,
    )
    assert b.at == kst(22, 600)
    assert (b.destination.name, b.destination.lat, b.destination.lng) == ("세종대학교", 37.55, 127.07)
    assert b.importance == "NORMAL"
    assert (b.origin.name, b.origin.lat, b.origin.lng) == ("학교", 37.5, 127)
    assert b.transport == "CAR"
    assert b.buffer_minutes == 5


def test_profile_defaults_when_nothing_stored():
    """저장된 값이 없으면 프로필 기본값 (집 · 기본 이동 수단 · 기본 여유)"""
    b = resolve_basics(draft=PlanDraft(at=kst(22, 600), destination=dest("x")), profile=profile)
    assert (b.origin.name, b.origin.lat, b.origin.lng) == ("우리 집", 37.5547, 126.9707)
    assert b.transport == "TRANSIT"
    assert b.buffer_minutes == 10
    assert b.importance == "LOW"  # 새 일정 기본 중요도는 약(LOW) — 알람 정책 변경


def test_origin_name_only_uses_matching_home_or_stored_coordinates():
    """출발지 이름만 오면 같은 이름의 집 · 저장된 출발지 좌표를 씀 (목업의 "집")"""
    o = resolve_basics(
        draft=PlanDraft(at=kst(22, 600), destination=dest("x"), origin=PlaceInput(name="집")), profile=profile
    ).origin
    assert (o.name, o.lat, o.lng) == ("집", 37.5547, 126.9707)
    o = resolve_basics(
        draft=PlanDraft(at=kst(22, 600), destination=dest("x"), origin=PlaceInput(name="강남역")),
        profile=profile,
    ).origin
    assert (o.name, o.lat, o.lng) == ("강남역", None, None)
    o = resolve_basics(
        draft=PlanDraft(appointment_id="ap1", origin=PlaceInput(name="학교")),
        profile=profile,
        appointment=APPOINTMENT,
        participant=PARTICIPANT,
    ).origin
    assert (o.name, o.lat, o.lng) == ("학교", 37.5, 127)


def test_no_manual_times_field_keeps_stored_present_field_is_everything():
    """칸이 없으면 저장된 직접 시각을 그대로, 칸이 있으면 그 칸이 전부 (null = 자동으로 되돌림)"""
    alarms = [
        row("WAKE", 450, time_source="MANUAL"),
        row("PREPARE", 480),
        row("DEPARTURE", 540),
        row("ARRIVE", 590),
    ]
    keep = compute_plan(
        base(
            draft=existing_draft(),
            appointment=APPOINTMENT,
            participant=PARTICIPANT,
            existing_alarms=alarms,
            stats=NO_STATS,
        ),
        api(30),
    )
    assert keep.manual_times["WAKE"] == kst(22, 450)
    reset = compute_plan(
        base(
            draft=existing_draft(manual_times={"WAKE": None}),
            appointment=APPOINTMENT,
            participant=PARTICIPANT,
            existing_alarms=alarms,
            stats=NO_STATS,
        ),
        api(30),
    )
    assert reset.manual_times == {}


def test_partial_alarm_schedule_does_not_grow():
    """부분 알람 일정(기상만)을 다시 저장해도 종류가 늘지 않음, 알람 없는 일정은 계산된 전부"""
    plan = compute_plan(
        base(
            draft=existing_draft(),
            appointment=APPOINTMENT,
            participant=PARTICIPANT,
            existing_alarms=[row("WAKE", 450), row("ARRIVE", 590)],
        ),
        api(30),
    )
    assert [x["kind"] for x in plan.chain] == ["WAKE", "ARRIVE"]
    fresh = compute_plan(
        base(draft=existing_draft(), appointment=APPOINTMENT, participant=PARTICIPANT, existing_alarms=[]),
        api(30),
    )
    assert [x["kind"] for x in fresh.chain] == ["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]


def test_stored_manual_intensity_kept_equal_to_auto_becomes_auto():
    """저장된 직접 강도는 유지, 자동과 같은 값이면 자동으로 (자동 값이 바뀌면 같이 바뀜)"""
    # 중요도 NORMAL · 늦잠 1회 → 자동 중(1). 저장: 배경음 약(0) 직접, 말소리 중(= 자동), 진동 강(2) 직접
    stored = row(
        "WAKE",
        480,
        intensity="NORMAL",
        voice_tone="NEUTRAL",
        sound_key="theme_calm",
        vibration="STRONG_CONTINUOUS",
        intensity_source="MANUAL",
    )
    keep = compute_plan(
        base(
            draft=existing_draft(), appointment=APPOINTMENT, participant=PARTICIPANT, existing_alarms=[stored]
        ),
        api(30),
    )
    assert keep.intensity["axes"] == {
        "sound": {"level": 0, "manual": True},
        "voice": {"level": 1, "manual": False},
        "vibration": {"level": 2, "manual": True},
    }
    # 요청이 levels를 주면 그 칸이 전부 — null은 자동으로 되돌림
    reset = compute_plan(
        base(
            draft=existing_draft(levels={"sound": None, "voice": None, "vibration": None}),
            appointment=APPOINTMENT,
            participant=PARTICIPANT,
            existing_alarms=[stored],
        ),
        api(30),
    )
    assert reset.intensity["manual"] is False
    assert reset.intensity["fields"]["vibration"] is None  # 진동도 "자동으로" → vibration None
    assert keep.intensity["fields"]["vibration"] == "STRONG_CONTINUOUS"
    assert (
        keep.intensity["fields"]["intensity"] == "NORMAL"
    )  # 전체 세기는 직접 고른 진동과 상관없이 자동 단계
    # 자동으로 저장된 강도는 늦잠 횟수가 바뀌면 같이 바뀜
    auto_stored = row("WAKE", 480)
    assert (
        compute_plan(
            base(
                draft=existing_draft(),
                appointment=APPOINTMENT,
                participant=PARTICIPANT,
                existing_alarms=[auto_stored],
                late_count4w=3,
            ),
            api(30),
        ).intensity["fields"]["intensity"]
        == "STRONG"
    )


def test_infer_before_prepare_from_gap():
    """준비가 자동이면 준비 − 기상 간격을 준비 전 활동으로 이어받음 (기상을 직접 옮겼어도), 준비가 직접이면 읽지 않음"""
    assert infer_before_prepare([row("WAKE", 450), row("PREPARE", 480)]) == 30
    assert (
        infer_before_prepare([row("WAKE", 450, time_source="MANUAL"), row("PREPARE", 480)]) == 30
    )  # 기상을 옮기면 자동 준비가 같은 간격으로 따라옴
    assert infer_before_prepare([row("WAKE", 450), row("PREPARE", 480, time_source="MANUAL")]) is None
    assert (
        infer_before_prepare(
            [row("WAKE", 450, time_source="MANUAL"), row("PREPARE", 480, time_source="MANUAL")]
        )
        is None
    )
    assert infer_before_prepare([row("WAKE", 450)]) is None
    plan = compute_plan(
        base(
            draft=existing_draft(),
            appointment=APPOINTMENT,
            participant=PARTICIPANT,
            existing_alarms=[row("WAKE", 450), row("PREPARE", 480)],
        ),
        api(30),
    )
    assert plan.before_prepare_minutes == 30


def test_moved_wake_resave_keeps_prepare_gap_30():
    """기상을 직접 옮긴 약속을 다시 저장해도 준비 알람 간격은 20분으로 돌아가지 않고 준비 전 활동(30분) 그대로"""
    # 자동 기상 8:00이던 걸 7:30으로 옮김 → 자동 준비는 7:30 + 30 = 8:00. 요청에 칸이 없으면 저장된 직접 기상 시각 · 준비 전 활동을 둘 다 이어받음
    stored = [
        row("WAKE", 450, time_source="MANUAL"),
        row("PREPARE", 480),
        row("DEPARTURE", 540),
        row("ARRIVE", 590),
    ]
    plan = compute_plan(
        base(
            draft=existing_draft(),
            appointment=APPOINTMENT,
            participant=PARTICIPANT,
            existing_alarms=stored,
            stats=NO_STATS,
        ),
        api(30),
    )
    assert plan.before_prepare_minutes == 30
    assert text(plan) == ["WAKE 07:30*", "PREPARE 08:00", "DEPARTURE 09:25", "ARRIVE 09:55"]


def test_response_shape_alarm_suggestion_dto_fields():
    """응답 모양: 5.4.1 AlarmSuggestionDTO 칸"""
    dto = to_suggestion(compute_plan(base(), api(50)))
    assert list(dto.keys()) == [
        "appointment_at",
        "wake_at",
        "prepare_at",
        "departure_at",
        "arrive_at",
        "alarms",
        "check",
        "travel",
        "prepare",
        "intensity",
        "with_wake",
        "same_day_wake",
        "confidence",
        "warnings",
    ]
    assert dto["intensity"]["level"] == "NORMAL"
    assert dto["intensity"]["reason"] == "평소 일정 → 중"
    assert [a["alarm_type"] for a in dto["alarms"]] == ["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]
    assert dto["confidence"] == "MEDIUM"


def test_late_count_in_reason():
    """늦잠 지각이 2회 이상이면 이유 문구에 나옴"""
    dto = to_suggestion(compute_plan(base(late_count4w=3), api(50)))
    assert dto["intensity"]["level"] == "STRONG"
    assert dto["intensity"]["reason"] == "평소 일정 + 최근 4주 늦잠 지각 3회 → 강"


# ---- 학습 루프 기초 a (이슈 #266) — 5절 시험 표 ----


def test_s5_no_records_onboarding_70():
    """[5절] 기록 0건 · 온보딩 70분: 기상~출발 70분, 출처 첫 값(DEFAULT), "배우는 중 0/5\""""
    onboarded = {**profile, "default_prepare_minutes": 70}
    plan = compute_plan(base(profile=onboarded, stats=NO_STATS), api(50))
    assert [
        plan.prepare["minutes"],
        plan.prepare["source"],
        plan.prepare["commonP80"],
        plan.prepare["sampleCount"],
    ] == [70, "DEFAULT", 70, 0]
    assert prepare_source_label(plan.prepare["sampleCount"]) == "배우는 중 0/5"
    assert to_suggestion(plan)["prepare"]["common_p80"] == 70  # 앱이 받는 "공통(첫 값)"도 70
    # 기상 = 약속 10:00 − 여유 10 − 이동 50 − 준비 70 = 7:50 (온보딩을 안 읽던 때는 50분 기준이라 8:10)
    assert text(plan)[0] == "WAKE 07:50"


def test_first_prepare_minutes_ranges():
    """온보딩 값이 없거나 범위 밖이면 50 (프로필 없음 · null · 5분 · 400분)"""
    assert first_prepare_minutes({}) == 50
    assert first_prepare_minutes({"default_prepare_minutes": None}) == 50
    assert first_prepare_minutes({"default_prepare_minutes": 4}) == 50
    assert first_prepare_minutes({"default_prepare_minutes": 5}) == 5
    assert first_prepare_minutes({"default_prepare_minutes": 400}) == 50
    assert first_prepare_minutes({"default_prepare_minutes": 10}) == 10
    assert first_prepare_minutes({"default_prepare_minutes": 300}) == 300


def test_s5_second_schedule_prepare_15_regardless_of_records_and_onboarding():
    """[5절] 그날 두 번째 일정(기상 없음): 준비 15분 그대로 — 기록이 0건이든 30건이든 같음, 온보딩 값과도 무관"""

    def second(stats):
        return compute_plan(
            base(
                draft=PlanDraft(at=kst(22, 1080), destination=dest("강남역"), buffer_minutes=5),
                same_day_wake=True,
                profile={**profile, "default_prepare_minutes": 90},
                stats=stats,
                now=kst(22, 600),
            ),
            api(20),
        )

    for stats in [
        PrepareStats(sampleCount=0, p80=None),
        PrepareStats(sampleCount=5, p80=60),
        PrepareStats(sampleCount=30, p80=120),
    ]:
        plan = second(stats)
        assert [
            plan.with_wake,
            plan.prepare["minutes"],
            plan.prepare["correction"],
            plan.prepare["source"],
        ] == [
            False,
            15,
            0,
            "DEFAULT",
        ], f"기록 {stats['sampleCount']}건"
    # 요청에서 기상을 빼도(withWake false) 같음 — 기록 수와 상관없음
    asked = compute_plan(
        base(
            draft=PlanDraft(at=kst(22, 1080), destination=dest("강남역"), with_wake=False),
            same_day_wake=False,
            stats=PrepareStats(sampleCount=30, p80=120),
        ),
        api(20),
    )
    assert asked.prepare["minutes"] == 15
    # 반대로 첫 일정(기상 있음)은 같은 기록으로 보정됨
    first = compute_plan(base(stats=PrepareStats(sampleCount=30, p80=120)), api(50))
    assert first.prepare["minutes"] > 50


def test_s5_no_double_20_minutes():
    """[5절] 20분 중복 없음: 기록 60분 · n=10이면 기상~출발 = 공통 50 + 보정 5 = 55 (준비 전 활동이 얼마든 안 변함)"""
    for before in [0, 10, 20, 30]:
        plan = compute_plan(
            base(
                draft=PlanDraft(at=kst(22, 600), destination=dest("x"), before_prepare_minutes=before),
                stats=PrepareStats(sampleCount=10, p80=60),
            ),
            api(50),
        )
        assert [plan.prepare["correction"], plan.prepare["minutes"]] == [5, 55], f"준비 전 활동 {before}분"
    # 예전 식(+20분)이었다면 개인 P80이 80이 되어 보정 15 · 65분이었음
    plan = compute_plan(base(stats=PrepareStats(sampleCount=10, p80=60)), api(50))
    assert plan.prepare["minutes"] != 65


def test_s5_source_label_changes_as_records_grow():
    """[5절] 기록이 쌓이면 출처 문구가 바뀜: 4건까지 배우는 중, 5건부터 내 기록 기반 (출처 DEFAULT → PERSONAL)"""

    def at(n):
        return compute_plan(base(stats=PrepareStats(sampleCount=n, p80=80 if n else None)), api(50)).prepare

    assert [[at(n)["source"], prepare_source_label(at(n)["sampleCount"])] for n in (4, 5)] == [
        ["DEFAULT", "배우는 중 4/5"],
        ["PERSONAL", "내 기록 기반 5건"],
    ]


def test_clamp_prepare_to_bound():
    """clampPrepareToBound: 자동 값만 기준 ±범위로, 직접 고른 값(MANUAL)과 범위 안 값은 그대로, 보정 칸은 합이 맞게"""

    def auto(minutes):
        return PrepareBasis(
            minutes=minutes,
            autoMinutes=minutes,
            commonP80=50,
            correction=minutes - 50,
            sampleCount=30,
            prior=10,
            weight=0.75,
            residualP80=70,
            source="PERSONAL",
            confidence="HIGH",
        )

    bound = {"around": 70, "within": 10}
    up = clamp_prepare_to_bound(auto(103), bound)
    assert [up["minutes"], up["autoMinutes"], up["correction"]] == [80, 80, 30]
    assert clamp_prepare_to_bound(auto(30), bound)["minutes"] == 60
    assert clamp_prepare_to_bound(auto(80), bound)["minutes"] == 80  # 경계 포함
    inside = auto(75)
    assert clamp_prepare_to_bound(inside, bound) is inside
    manual = {**auto(200), "source": "MANUAL"}
    assert clamp_prepare_to_bound(manual, bound)["minutes"] == 200


def test_compute_plan_prepare_bound():
    """computePlan의 prepareBound: 직접 고른 준비시간은 안 자름, 기상 없는 일정(15분)에는 걸지 않음"""
    big = PrepareStats(sampleCount=30, p80=150)  # 자동 예측 약 105
    auto = compute_plan(base(stats=big, prepare_bound={"around": 60, "within": 10}), api(50))
    assert [auto.prepare["minutes"], auto.prepare["autoMinutes"]] == [70, 70]
    manual = compute_plan(
        base(
            stats=big,
            prepare_bound={"around": 60, "within": 10},
            draft=PlanDraft(at=kst(22, 600), destination=dest("x"), prepare_minutes=120),
        ),
        api(50),
    )
    assert [manual.prepare["minutes"], manual.prepare["source"]] == [120, "MANUAL"]
    second = compute_plan(
        base(stats=big, same_day_wake=True, prepare_bound={"around": 60, "within": 10}), api(50)
    )
    assert second.prepare["minutes"] == 15


# D69: 기본 보통 — 중요도 낮음(앱 라벨 '평소')이어도 약이 아니고, 매우 중요(HIGH)만 한 단계 올림
def test_intensity_reason_low_is_normal_high_goes_up():
    """강도 이유: 중요도 낮음은 보통(중), 매우 중요는 한 단계 올려 강 — 약이라는 문구는 안 나옴"""

    def draft(importance):
        return PlanDraft(at=kst(22, 600), destination=dest(lat=37.55, lng=127.07), importance=importance)

    low = to_suggestion(compute_plan(base(draft=draft("LOW"), late_count4w=0), api(50)))
    assert low["intensity"]["level"] == "NORMAL"
    assert low["intensity"]["reason"] == "평소 일정 → 중"
    assert [
        low["intensity"]["auto_level"],
        low["intensity"]["importance_point"],
        low["intensity"]["late_point"],
    ] == [1, 0, 0]

    high = to_suggestion(compute_plan(base(draft=draft("HIGH"), late_count4w=0), api(50)))
    assert high["intensity"]["level"] == "STRONG"
    assert high["intensity"]["reason"] == "매우 중요 일정 (한 단계 올림) → 강"
    assert [high["intensity"]["auto_level"], high["intensity"]["importance_point"]] == [2, 1]

    low_late = to_suggestion(compute_plan(base(draft=draft("LOW"), late_count4w=2), api(50)))
    assert low_late["intensity"]["level"] == "STRONG"  # 낮음 + 늦잠 지각 2회 = 보통에서 한 단계 올림
    assert low_late["intensity"]["reason"] == "평소 일정 + 최근 4주 늦잠 지각 2회 → 강"


def test_legacy_alarm_without_vibration_is_not_read_as_manual_vibration():
    """옛 알람(vibration 칸이 비어 있고 intensity만 STRONG인 직접 알람)은 진동을 직접 고른 것으로 안 읽음 — 강도를 따르는 칸이라 자동"""
    legacy = row("WAKE", 480, intensity="STRONG", vibration=None, intensity_source="MANUAL")
    plan = compute_plan(
        base(
            draft=existing_draft(), appointment=APPOINTMENT, participant=PARTICIPANT, existing_alarms=[legacy]
        ),
        api(30),
    )
    assert plan.intensity["axes"]["vibration"]["manual"] is False
    assert plan.intensity["fields"]["vibration"] is None


def test_preview_intensity_block_has_vibration():
    """미리 보기 응답의 intensity 블록에 vibration이 실림 (직접 고른 진동만 값, 자동이면 null)"""
    auto = to_suggestion(compute_plan(base(draft=PlanDraft(at=kst(22, 600), destination=dest())), api(50)))
    assert auto["intensity"]["vibration"] is None
    picked = to_suggestion(
        compute_plan(
            base(draft=PlanDraft(at=kst(22, 600), destination=dest(), levels={"vibration": 0})), api(50)
        )
    )
    assert picked["intensity"]["vibration"] == "NONE"
    assert picked["intensity"]["level"] == "NORMAL"


# ---- 학습 루프 기초 b (이슈 #272) — 잠귀 · 깨우는 여유 · 최근 5번 ----

TEN = [70, 100, 40, 80, 45, 75, 55, 60, 50, 65]  # 최신순. 정렬하면 0.7 → 71.5, 0.8 → 76, 0.9 → 82


def with_margin(wake_margin):
    return {**profile, "wake_margin": wake_margin}


def prepare_of(margin, importance="NORMAL", stats=None):
    stats = prepare_stats_of(TEN) if stats is None else stats
    return compute_plan(
        base(
            profile=with_margin(margin),
            stats=stats,
            draft=PlanDraft(at=kst(22, 600), destination=dest("x"), importance=importance),
        ),
        api(50),
    ).prepare


def test_s5_same_records_tight_normal_loose():
    """[5절] 같은 기록, 빠듯 / 보통 / 넉넉: 넉넉이 더 이름 (0.7 < 0.8 < 0.9)"""
    tight, normal, loose = (prepare_of(m) for m in ("TIGHT", "NORMAL", "LOOSE"))
    assert tight["minutes"] < normal["minutes"] < loose["minutes"], (
        f"{tight['minutes']} < {normal['minutes']} < {loose['minutes']}"
    )
    # 공통 50 + round(0.5 × (분위수 − 50)) — 71.5 → 61, 76 → 63, 82 → 66
    assert [tight["minutes"], normal["minutes"], loose["minutes"]] == [61, 63, 66]
    assert [tight["sampleCount"], normal["sampleCount"], loose["sampleCount"]] == [
        10,
        10,
        10,
    ]  # 기록 수는 여유와 무관


def test_normal_margin_equals_old_value():
    """깨우는 여유 보통은 예전 값과 정확히 같음: 칸이 없거나 모르는 값이어도, 옛 모양 stats(p80만)여도"""
    old = compute_plan(
        base(stats=PrepareStats(sampleCount=10, p80=76)), api(50)
    ).prepare  # wake_margin 칸 없는 프로필 + 옛 모양
    for margin in ["NORMAL", None, None, "x"]:
        assert prepare_of(margin) == old, str(margin)
    assert old["minutes"] == 63


def test_s5_high_importance_one_step_up():
    """[5절] 매우 중요 일정은 여유가 한 단계 위: 빠듯 → 보통, 보통 → 넉넉, 넉넉은 그대로 · 중요 / 보통 일정은 그대로"""
    assert prepare_of("TIGHT", "HIGH")["minutes"] == prepare_of("NORMAL")["minutes"]
    assert prepare_of("NORMAL", "HIGH")["minutes"] == prepare_of("LOOSE")["minutes"]
    assert prepare_of("LOOSE", "HIGH")["minutes"] == prepare_of("LOOSE")["minutes"]
    for importance in ("LOW", "NORMAL"):
        assert prepare_of("TIGHT", importance)["minutes"] == prepare_of("TIGHT")["minutes"]


def test_fewer_than_5_records_same_rule():
    """기록이 5건 미만이어도 같은 규칙 (섞는 가중치 n:(n+10) 그대로): 3건 → 빠듯 < 보통 < 넉넉, 매우 중요는 한 단계 위"""
    three = prepare_stats_of([90, 70, 50])  # 정렬 50 70 90: 0.7 → 78, 0.8 → 82, 0.9 → 86

    def at(m, imp="NORMAL"):
        return prepare_of(m, imp, three)

    # 가중치 3/13: 보정 = round(3/13 × (분위수 − 50)) → 78 → 6, 82 → 7, 86 → 8
    assert [at(m)["minutes"] for m in ("TIGHT", "NORMAL", "LOOSE")] == [56, 57, 58]
    assert at("NORMAL")["source"] == "DEFAULT"  # 5건 미만이라 출처는 첫 값
    assert at("TIGHT", "HIGH")["minutes"] == at("NORMAL")["minutes"]
    assert at("NORMAL", "HIGH")["minutes"] == at("LOOSE")["minutes"]


def test_zero_records_first_value_regardless_of_margin():
    """기록이 0건이면 여유와 상관없이 첫 값 그대로"""
    for m in ("TIGHT", "NORMAL", "LOOSE"):
        for imp in ("NORMAL", "HIGH"):
            p = prepare_of(m, imp, prepare_stats_of([]))
            assert [p["minutes"], p["source"]] == [50, "DEFAULT"]


def test_s5_second_schedule_ignores_margin_and_importance():
    """[5절] 그날 두 번째 일정(기상 없음)은 여유 · 중요도와 상관없이 준비 15분 그대로"""
    for m in ("TIGHT", "NORMAL", "LOOSE"):
        for importance in ("NORMAL", "HIGH"):
            plan = compute_plan(
                base(
                    draft=PlanDraft(
                        at=kst(22, 1080), destination=dest("강남역"), buffer_minutes=5, importance=importance
                    ),
                    same_day_wake=True,
                    profile=with_margin(m),
                    stats=prepare_stats_of(TEN),
                    now=kst(22, 600),
                ),
                api(20),
            )
            assert [
                plan.with_wake,
                plan.prepare["minutes"],
                plan.prepare["correction"],
                plan.recent_prepare,
            ] == [
                False,
                15,
                0,
                [],
            ], f"{m} {importance}"


def test_manual_prepare_ignores_margin():
    """직접 고른 준비 시간은 여유와 상관없이 그 값"""
    plan = compute_plan(
        base(
            profile=with_margin("LOOSE"),
            stats=prepare_stats_of(TEN),
            draft=PlanDraft(at=kst(22, 600), destination=dest("x"), prepare_minutes=40),
        ),
        api(50),
    )
    assert [plan.prepare["minutes"], plan.prepare["source"]] == [40, "MANUAL"]


def test_margin_reaches_alarm_times():
    """깨우는 여유가 알람 시각에 닿음: 같은 기록에서 넉넉하면 기상이 더 이름"""

    def wake(m):
        plan = compute_plan(
            base(
                profile=with_margin(m),
                stats=prepare_stats_of(TEN),
                draft=PlanDraft(at=kst(22, 600), destination=dest("x"), before_prepare_minutes=30),
            ),
            api(50),
        )
        return next(x for x in plan.chain if x["kind"] == "WAKE")["at"]

    assert wake("LOOSE") < wake("NORMAL") < wake("TIGHT")


# 잠귀 — 강도 (D69: 약은 직접 고를 때만)
def level_of(wake_sensitivity, importance, late_count4w=0):
    return compute_plan(
        base(
            profile={**profile, "wake_sensitivity": wake_sensitivity},
            late_count4w=late_count4w,
            draft=PlanDraft(at=kst(22, 600), destination=dest("x"), importance=importance),
        ),
        api(50),
    ).intensity


def test_s5_heavy_sleeper_is_strong():
    """[5절] 잠귀 깊음(HEAVY): 보통 일정이어도 강 — 매우 중요 · 지각 2번이어도 최대 강에서 멈춤"""
    assert level_of("HEAVY", "NORMAL")["auto"] == 2
    assert level_of("HEAVY", "LOW")["auto"] == 2
    mx = level_of("HEAVY", "HIGH", 2)
    assert [mx["auto"], mx["fields"]["intensity"], mx["heavySleeper"]] == [2, "STRONG", True]


def test_s5_light_normal_or_missing_sensitivity_is_normal():
    """[5절] 잠귀 가벼움(LIGHT) · 보통(NORMAL) · 칸 없음 + 보통 일정: 기본 보통 (약 아님)"""
    for sensitivity in ["LIGHT", "NORMAL", None, None, "x"]:
        for importance in ("LOW", "NORMAL"):
            it = level_of(sensitivity, importance)
            assert [it["auto"], it["fields"]["intensity"], it["heavySleeper"]] == [1, "NORMAL", False], (
                f"{sensitivity} {importance}"
            )
    assert level_of("LIGHT", "HIGH")["auto"] == 2  # 매우 중요는 한 단계 올림 그대로


def test_heavy_sleeper_reason_and_manual_before_sensitivity():
    """잠귀가 무거우면 이유 문구에 나오고, 직접 고른 칸은 잠귀보다 먼저"""
    dto = to_suggestion(
        compute_plan(base(profile={**profile, "wake_sensitivity": "HEAVY"}, late_count4w=0), api(50))
    )
    assert dto["intensity"]["reason"] == "평소 일정 + 잠귀가 무거움 → 강"
    manual = compute_plan(
        base(
            profile={**profile, "wake_sensitivity": "HEAVY"},
            draft=PlanDraft(
                at=kst(22, 600), destination=dest("x"), levels={"sound": 0, "voice": None, "vibration": None}
            ),
        ),
        api(50),
    )
    assert [manual.intensity["axes"]["sound"], manual.intensity["axes"]["voice"]] == [
        {"level": 0, "manual": True},
        {"level": 2, "manual": False},
    ]


def test_stored_manual_comparison_uses_auto_with_sensitivity():
    """저장된 직접 고른 강도 비교는 잠귀를 반영한 자동 값 기준: HEAVY인 사람의 강 칸은 직접 고름이 아님"""
    stored = [
        {
            "alarm_id": "a1",
            "participant_id": "p1",
            "user_id": "u",
            "alarm_type": "WAKE",
            "scheduled_at": "2025-05-22T07:00:00+09:00",
            "intensity": "STRONG",
            "voice_tone": "DIRECT",
            "sound_key": "theme_urgent",
            "is_enabled": True,
            "snooze_limit": 3,
            "time_source": "AUTO",
            "intensity_source": "MANUAL",
            "recurrence_rule": None,
        }
    ]
    plan = compute_plan(
        base(
            profile={**profile, "wake_sensitivity": "HEAVY"},
            existing_alarms=stored,
            draft=PlanDraft(at=kst(22, 600), destination=dest("x")),
        ),
        api(50),
    )
    assert plan.intensity["manual"] is False


# 최근 5번
def test_response_recent_up_to_5_newest_first():
    """응답의 prepare.recent: 최신순 최대 5개 — 0건 빈 배열 · 3건 3개 · 7건 최신 5개"""

    def recent(values):
        return to_suggestion(compute_plan(base(stats=prepare_stats_of(values)), api(50)))["prepare"]["recent"]

    assert recent([]) == []
    assert recent([62, 55, 48]) == [62, 55, 48]
    assert recent([75, 70, 62, 55, 48, 90, 91]) == [75, 70, 62, 55, 48]
    assert (
        to_suggestion(compute_plan(base(stats=PrepareStats(sampleCount=10, p80=70)), api(50)))["prepare"][
            "recent"
        ]
        == []
    )  # 옛 모양 stats


def test_response_recent_empty_for_second_schedule():
    """응답의 prepare.recent: 그날 두 번째 일정은 기록을 안 쓰므로 빈 배열"""
    dto = to_suggestion(compute_plan(base(same_day_wake=True, stats=prepare_stats_of([62, 55, 48])), api(20)))
    assert [dto["prepare"]["recent"], dto["prepare"]["sample_count"]] == [[], 0]


# ---- 저녁에만 일정이 있는 날은 기상 알람 없음 (#320, 헤드 10/30) ----


def test_evening_single_schedule_has_no_wake():
    """저녁 단일 일정 18:00(이동 30 · 여유 10): 기상 없음 · 준비 · 출발 · 도착만, 준비 15분"""
    plan = compute_plan(
        base(draft=PlanDraft(at=kst(22, 1080), destination=dest("강남역")), now=kst(22, 600)), api(30)
    )
    assert plan.with_wake is False
    assert plan.prepare["minutes"] == 15
    assert plan.before_prepare_minutes == 0
    assert text(plan) == ["PREPARE 17:05", "DEPARTURE 17:20", "ARRIVE 17:50"]
    s = to_suggestion(plan)
    assert s["with_wake"] is False
    assert s["wake_at"] is None


def test_evening_boundary_1159_vs_1200():
    """경계: 계산된 기상이 11:59면 기상 있음, 12:00이면 기상 없음"""

    # 기록 없음: 준비 50 + 이동 30 + 여유 10 = 90분 전이 기상 → 약속 13:29면 기상 11:59, 13:30이면 12:00
    def at(minutes):
        return compute_plan(
            base(
                stats=NO_STATS,
                draft=PlanDraft(at=kst(22, minutes), destination=dest("강남역")),
                now=kst(22, 400),
            ),
            api(30),
        )

    before = at(13 * 60 + 29)
    assert before.with_wake is True
    assert hm(iso_of(before.chain[0]["at"])) == "11:59"
    exact = at(13 * 60 + 30)
    assert exact.with_wake is False
    assert text(exact) == [
        "PREPARE 12:35",
        "DEPARTURE 12:50",
        "ARRIVE 13:20",
    ]  # 기상 없이 준비 15분으로 다시 계산


def test_daytime_14_wake_morning_has_wake():
    """낮 일정 14:00에서 계산된 기상이 오전이면 기상 있음"""
    # 첫 값(온보딩) 90분 · 이동 30 · 여유 10 → 기상 14:00 − 10 − 30 − 90 = 11:50. 기본 50분이면 12:30이라 기상 없음
    draft = PlanDraft(at=kst(22, 14 * 60), destination=dest("강남역"))
    long = compute_plan(
        base(
            profile={**profile, "default_prepare_minutes": 90}, stats=NO_STATS, draft=draft, now=kst(22, 400)
        ),
        api(30),
    )
    assert long.with_wake is True
    assert hm(iso_of(long.chain[0]["at"])) == "11:50"
    short = compute_plan(base(stats=NO_STATS, draft=draft, now=kst(22, 400)), api(30))
    assert short.with_wake is False


def test_explicit_choices_are_respected():
    """직접 지정은 존중: with_wake true · 직접 고른 기상 시각은 12시 이후여도 기상 있음"""
    forced = compute_plan(
        base(draft=PlanDraft(at=kst(22, 1080), destination=dest("강남역"), with_wake=True), now=kst(22, 400)),
        api(30),
    )
    assert forced.with_wake is True
    assert forced.chain[0]["kind"] == "WAKE"
    manual = compute_plan(
        base(
            draft=PlanDraft(
                at=kst(22, 1080), destination=dest("강남역"), manual_times={"WAKE": kst(22, 15 * 60)}
            ),
            now=kst(22, 400),
        ),
        api(30),
    )
    assert manual.with_wake is True
    assert text(manual)[:1] == ["WAKE 15:00*"]
    # with_wake false는 원래대로 기상 없음 (오전 일정이어도)
    off = compute_plan(base(draft=PlanDraft(at=kst(22, 600), destination=dest(), with_wake=False)), api(50))
    assert off.with_wake is False


def test_kinds_with_wake_does_not_create_wake_in_evening():
    """kinds에 WAKE가 있어도 저녁이면 기상은 안 만들어짐 (kinds는 거르기만, L6 안건후보 기본안)"""
    plan = compute_plan(
        base(
            draft=PlanDraft(
                at=kst(22, 1080), destination=dest("강남역"), kinds=["WAKE", "PREPARE", "DEPARTURE"]
            ),
            now=kst(22, 400),
        ),
        api(30),
    )
    assert plan.with_wake is False
    assert [x["kind"] for x in plan.chain] == ["PREPARE", "DEPARTURE", "ARRIVE"]


def test_timezone_decides_day():
    """시간대: 같은 순간도 profile.timezone의 낮으로 판정 (서울이면 오후 → 기상 없음, 뉴욕이면 새벽 → 기상 있음)"""
    # 약속 UTC 08:00 (서울 17:00 · 뉴욕 04:00), 기록 없음 · 이동 30 · 여유 10 · 준비 50 → 기상 UTC 06:30 (서울 15:30 · 뉴욕 02:30)
    at = datetime(2025, 5, 22, 8, 0, tzinfo=UTC)

    def inputs(timezone):
        return base(
            profile={**profile, "timezone": timezone},
            stats=NO_STATS,
            draft=PlanDraft(at=at, destination=dest("강남역")),
            now=datetime(2025, 5, 21, tzinfo=UTC),
        )

    assert compute_plan(inputs("Asia/Seoul"), api(30)).with_wake is False
    assert compute_plan(inputs("America/New_York"), api(30)).with_wake is True
    # 시간대가 비었거나 틀리면 서울
    assert compute_plan(inputs(""), api(30)).with_wake is False
    assert compute_plan(inputs("No/Such_Zone"), api(30)).with_wake is False


def test_axes_disabled_ignores_request_and_stored_axes():
    """축별 선택을 끄면(axesEnabled false) 요청 · 저장된 축 값을 모두 무시하고 자동(강도 따름) — 켜면 요청 값이 우선 (D1 A flag, 헤드 확정 10/30)"""
    stored = row("WAKE", 480, intensity_source="MANUAL", sound_key="theme_urgent", vibration="NONE")
    assert resolve_overrides({"sound": 2}, [stored], 1, True) == {"sound": 2}
    assert resolve_overrides({"sound": 2}, [stored], 1, False) == {}
    assert resolve_overrides(None, [stored], 1, False) == {}
    assert resolve_overrides(None, [stored], 1, True) != {}  # 켜면 저장된 직접 값이 살아 있음


def test_9_05_each_person_gets_own_alarms():
    """9-05: 같은 약속(11:00)에 사람마다 이동 · 기상~출발이 다르면 알람도 각자 따로 — 서로의 값이 섞이지 않고 한쪽만 바꿔도 다른 쪽은 그대로"""
    at = kst(22, 660)  # 11:00 약속, 여유 0

    def mine(travel, prepare):
        t = text(
            compute_plan(
                base(
                    draft=PlanDraft(
                        at=at,
                        destination=dest(),
                        buffer_minutes=0,
                        prepare_minutes=prepare,
                        before_prepare_minutes=5,
                    )
                ),
                api(travel),
            )
        )
        return [x for x in t if x.startswith("WAKE") or x.startswith("DEPARTURE")]

    a = mine(30, 20)  # A: 이동 30 · 기상~출발 20 → 출발 10:30, 기상 10:10
    b = mine(60, 40)  # B: 이동 60 · 기상~출발 40 → 출발 10:00, 기상 9:20
    assert a == ["WAKE 10:10", "DEPARTURE 10:30"]
    assert b == ["WAKE 09:20", "DEPARTURE 10:00"]
    assert mine(30, 20) == a  # 같은 입력은 같은 결과 — B를 계산해도 A는 안 변함
    assert mine(60, 20) == [
        "WAKE 09:40",
        "DEPARTURE 10:00",
    ]  # 이동만 늘면 출발이 그만큼 일러지고 기상도 같이 감


def test_c1_w4_preview_and_save_same_timestamps():
    """C1/W4 preview and save calculate exactly the same timestamps before database writes"""
    for model in ("c1_mae", "w4_mae"):
        inputs = base(
            ml_prediction={"model": model, "minutes": 66 if model == "c1_mae" else 58},
            draft=PlanDraft(
                at=kst(22, 600),
                destination=dest(),
                travel_minutes=30,
                travel_source="MANUAL",
                buffer_minutes=10,
                before_prepare_minutes=20,
                with_wake=True,
            ),
        )
        preview = to_suggestion(compute_plan(inputs, api(30), "preview"), inputs)
        save = to_suggestion(compute_plan(inputs, api(30), "save"), inputs)
        assert preview["prepare"]["source"] == "MODEL"
        assert preview["prepare"]["model"] == model
        assert preview["prepare"]["minutes"] == (66 if model == "c1_mae" else 58)
        assert [preview["wake_at"], preview["prepare_at"], preview["departure_at"], preview["arrive_at"]] == [
            save["wake_at"],
            save["prepare_at"],
            save["departure_at"],
            save["arrive_at"],
        ]
        assert preview["check"]["ok"] == save["check"]["ok"]


def test_missing_prediction_falls_back_to_personal_p80_manual_first():
    """missing prediction falls back to existing personal P80 while keeping manual override first"""
    args = base()
    fallback = compute_plan(replace(args, ml_prediction=None), api(50), "preview")
    assert fallback.prepare["source"] == "PERSONAL"
    assert fallback.prepare["minutes"] == 60
    assert to_suggestion(fallback)["prepare"].get("model") is None
    manual = compute_plan(
        replace(
            args,
            ml_prediction={"model": "w4_mae", "minutes": 58},
            draft=replace(args.draft, prepare_minutes=75),
        ),
        api(50),
        "preview",
    )
    assert manual.prepare["source"] == "MANUAL"
    assert manual.prepare["minutes"] == 75
    assert manual.prepare.get("mlModel") is None


# ---- 준비 알람 당김 (#484 · 안건 D111, B안: 준비 전 활동은 저장하지 않고 되찾음) ----
# 저장된 일정: 출발 9:25(약속 10:00 − 여유 5 − 이동 30), 준비 시간 60 → 기상 8:25, 준비 전 활동 20 → 준비 알람 8:45


def saved(adv, alarms, **over):
    return compute_plan(
        base(
            draft=PlanDraft(),
            appointment=APPOINTMENT,
            participant={**PARTICIPANT, "prepare_advance_minutes": adv},
            existing_alarms=alarms,
            **over,
        ),
        api(30),
    )


def at_of(plan):
    return [x for x in text(plan) if not x.startswith("ARRIVE")]


def test_infer_before_prepare_with_advance():
    """준비 전 활동 되찾기: 당김 10(간격 10)이면 20, 당김 25(기상도 5분 당김 · 간격 0)이면 출발 · 준비 시간으로 20, 당김 없으면 간격 그대로"""
    assert infer_before_prepare([row("WAKE", 505), row("PREPARE", 515), row("DEPARTURE", 565)], 10, 60) == 20
    assert infer_before_prepare([row("WAKE", 500), row("PREPARE", 500), row("DEPARTURE", 565)], 25, 60) == 20
    assert infer_before_prepare([row("WAKE", 505), row("PREPARE", 525), row("DEPARTURE", 565)]) == 20
    assert infer_before_prepare([row("WAKE", 505), row("PREPARE", 525, time_source="MANUAL")], 10, 60) is None


def test_recalculating_advanced_schedule_gives_same_times():
    """당김을 받은 일정을 다시 계산해도 같은 시각 — 같은 당김이 두 번 들어가지 않음 (10분 · 25분)"""
    ten = saved(10, [row("WAKE", 505), row("PREPARE", 515), row("DEPARTURE", 565)])
    assert at_of(ten) == ["WAKE 08:25", "PREPARE 08:35", "DEPARTURE 09:25"]
    assert ten.before_prepare_minutes == 20  # 계획의 준비 전 활동 · 준비 시간은 원래 값
    assert ten.prepare["minutes"] == 60
    assert at_of(saved(25, [row("WAKE", 500), row("PREPARE", 500), row("DEPARTURE", 565)])) == [
        "WAKE 08:20",
        "PREPARE 08:20",
        "DEPARTURE 09:25",
    ]


def test_first_application_with_existing_advance_minutes():
    """당김을 처음 적용할 때(저장된 알람은 당김 0) — existingAdvanceMinutes로 옛 값을 알려 주면 준비 알람만 10분 당겨짐"""
    applied = saved(
        10, [row("WAKE", 505), row("PREPARE", 525), row("DEPARTURE", 565)], existing_advance_minutes=0
    )
    assert at_of(applied) == ["WAKE 08:25", "PREPARE 08:35", "DEPARTURE 09:25"]


def test_manual_prepare_alarm_not_advanced():
    """준비 알람을 직접 고른 일정은 당기지 않음 — 직접 고른 시각 그대로, 기상도 그대로"""
    plan = saved(10, [row("WAKE", 505), row("PREPARE", 520, time_source="MANUAL"), row("DEPARTURE", 565)])
    assert at_of(plan) == ["WAKE 08:25", "PREPARE 08:40*", "DEPARTURE 09:25"]  # * = 직접 고른 시각

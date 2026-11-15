"""본 레포 supabase/functions/_shared/plan.test.ts의 서버판 — buildPlan(DB에서 읽기 + 이동시간 정하기)을 가짜 DB로. 좌표가 없을 때의 폴백, 미리 보기 = 저장 대조"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.errors import AppError
from app.core.read import Reader
from app.shared.commit import CommitCtx, assert_persistable, persist_appointment
from app.shared.plan import (
    PlanContext,
    TravelRequiredError,
    build_plan,
    load_prepare_stats,
    load_profile,
    load_same_day_wake,
)
from app.shared.plan_core import to_suggestion, to_travel_required_suggestion
from app.shared.requests import read_commit_request, read_suggest_request
from tests.fake_db import FakeDb

from .w2_helpers import get, hm, iso, iso_z, js, kst, sync

USER = "u-me"


def setup(now=None):
    now = now or kst(22, 400)
    db = FakeDb().with_commit_rpc()
    db.rows("profiles").append(
        {
            "user_id": USER,
            "timezone": "Asia/Seoul",
            "default_buffer_minutes": 10,
            "default_transport": "TRANSIT",
            "home_name": "집",
            "home_lat": 37.5547,
            "home_lng": 126.9707,
        }
    )
    ctx = PlanContext(user_id=USER, db=db, now=now)
    commit_ctx = CommitCtx(user_id=USER, user_db=db, service=db, now=now)
    return SimpleNamespace(db=db, ctx=ctx, commit_ctx=commit_ctx)


def draft_of(body):
    r = Reader.root(body)
    draft = read_suggest_request(r)
    r.finish()
    return draft


def chain_of(plan):
    return [f"{a['kind']} {hm(iso_z(a['at']))}" for a in plan.chain]


def stats_of(stats):
    """PrepareStats dict를 (sampleCount, p80, values)로"""
    return (stats["sampleCount"], stats["p80"], list(stats["values"]))


# ---- 좌표가 없을 때 (이동시간 폴백의 마지막 단계) ----

NO_COORDS = {"appointment": {"appointment_at": iso(22, 600), "destination": {"name": "세종대학교"}}}


@sync
async def test_no_coordinates_and_no_manual_input_raises_travel_required_error():
    """좌표가 없고 직접 입력도 없으면 TravelRequiredError (502 ROUTE_UNAVAILABLE + manual_required) — alarm-commit은 이 오류를 그대로 돌려줌"""
    env = setup()
    with pytest.raises(TravelRequiredError) as info:
        await build_plan(env.ctx, draft_of(NO_COORDS))
    e = info.value
    assert isinstance(e, AppError)
    assert e.status == 502
    assert e.code == "ROUTE_UNAVAILABLE"
    assert e.details["manual_required"] is True
    assert any(w["code"] == "NO_COORDINATES" for w in e.details["warnings"])


@sync
async def test_alarm_suggest_turns_that_error_into_a_200_that_only_empties_travel():
    """alarm-suggest는 그 오류를 200 응답으로: 이동시간만 비우고 직접 입력을 안내, 준비 · 강도는 그대로"""
    env = setup()
    with pytest.raises(TravelRequiredError) as info:
        await build_plan(env.ctx, draft_of(NO_COORDS))
    err = info.value
    s = to_travel_required_suggestion(err.inputs, err.travel)

    # 보통 응답과 같은 칸 이름 (앱이 같은 타입으로 읽을 수 있게)
    normal = await build_plan(
        setup().ctx,
        draft_of({**NO_COORDS, "participant": {"est_travel_minutes": 50, "travel_source": "MANUAL"}}),
    )
    assert list(s) == list(to_suggestion(normal.plan))
    assert list(s["travel"]) == list(to_suggestion(normal.plan)["travel"])

    travel = s["travel"]
    assert [travel["minutes"], travel["source"], travel["manual_required"], travel["needs_confirmation"]] == [
        None,
        None,
        True,
        True,
    ]
    assert [s["alarms"], s["wake_at"], s["departure_at"], s["arrive_at"]] == [[], None, None, None]
    assert [s["check"]["ok"], s["check"]["reason"]] == [False, "TRAVEL_REQUIRED"]
    # 이동시간과 상관없는 칸
    assert [s["prepare"]["minutes"], s["prepare"]["common_p80"], s["intensity"]["level"], s["with_wake"]] == [
        50,
        50,
        "NORMAL",
        True,
    ]
    assert [w["code"] for w in s["warnings"]] == ["NO_COORDINATES"]  # 자리표시 계산의 경고가 새어 나오지 않음


@sync
async def test_manual_travel_minutes_compute_times_even_without_coordinates():
    """직접 입력한 이동시간(MANUAL)이 있으면 좌표가 없어도 시각까지 계산됨"""
    env = setup()
    built = await build_plan(
        env.ctx,
        draft_of(
            {
                **NO_COORDS,
                "participant": {
                    "est_travel_minutes": 40,
                    "travel_source": "MANUAL",
                    "before_prepare_minutes": 30,
                },
            }
        ),
    )
    plan = built.plan
    assert [plan.travel.chosen.source, plan.travel_minutes] == ["MANUAL", 40]
    assert chain_of(plan) == ["WAKE 08:20", "PREPARE 08:50", "DEPARTURE 09:10", "ARRIVE 09:50"]


@sync
async def test_coordinates_without_a_kakao_key_or_records_give_a_straight_line_estimate():
    """좌표가 있고 카카오 키 · 기록이 없으면 직선거리 근사(ESTIMATE) — 사용자 확인이 필요하다고 알림"""
    env = setup()
    body = {
        "appointment": {
            "appointment_at": iso(22, 600),
            "destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07},
        },
        "participant": {"origin": {"name": "집", "lat": 37.5547, "lng": 126.9707}},
    }
    built = await build_plan(env.ctx, draft_of(body), want_auto_travel=True)
    assert built.plan.travel.chosen.source == "ESTIMATE"
    s = to_suggestion(built.plan)
    assert [s["travel"]["source"], s["travel"]["needs_confirmation"], s["travel"]["manual_required"]] == [
        "ESTIMATE",
        True,
        False,
    ]
    assert any(w["code"] == "KAKAO_NOT_CONFIGURED" for w in s["warnings"])


# ---- 미리 보기(alarm-suggest)와 저장(alarm-commit)이 같은 초안에서 같은 시각을 냄 ----

FULL = {
    "appointment": {
        "title": "팀 주간회의",
        "appointment_at": iso(22, 600),
        "destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07},
        "importance": "NORMAL",
    },
    "participant": {
        "origin": {"name": "집", "lat": 37.5547, "lng": 126.9707},
        "transport_type": "TRANSIT",
        "est_travel_minutes": 50,
        "travel_source": "API",
        "buffer_minutes": 10,
        "before_prepare_minutes": 30,
    },
}

SCENARIOS = [
    ("기본", FULL, None),
    ("직접 고른 기상 시각", {**FULL, "manual_times": {"WAKE": iso(22, 450)}}, None),
    ("고른 종류만", {**FULL, "kinds": ["WAKE", "DEPARTURE"]}, None),
    ("알람 하나 끄기", {**FULL, "enabled": {"PREPARE": False}}, None),
    ("기상이 이미 지난 때(그 알림만 끄고 저장)", FULL, kst(22, 478)),
    (
        "직접 고른 이동 · 준비",
        {
            **FULL,
            "participant": {
                **FULL["participant"],
                "est_travel_minutes": 35,
                "travel_source": "MANUAL",
                "prepare_minutes": 45,
            },
        },
        None,
    ),
]

ORDER = ["WAKE", "PREPARE", "DEPARTURE", "ARRIVE"]


def line(a):
    return f"{a['alarm_type']} {a['scheduled_at']} {js(a['is_enabled'])} {a['time_source']}"


# 앱은 같은 초안 하나를 alarm-suggest와 alarm-commit에 그대로 보냄 → 미리 보여 준 알람과 저장된 알람
async def preview_and_save(env, body):
    preview = await build_plan(env.ctx, draft_of(body), want_auto_travel=True, mode="preview")
    shown = [line(a) for a in to_suggestion(preview.plan, preview.inputs)["alarms"]]

    request = read_commit_request(Reader.root(body))
    assert get(request, "type") == "APPOINTMENT"
    draft = get(request, "draft")
    built = await build_plan(env.ctx, draft)
    assert_persistable(built.plan)
    body2 = await persist_appointment(
        env.commit_ctx,
        plan=built.plan,
        inputs=built.inputs,
        created=get(request, "appointment"),
        draft=draft,
        replace_existing=get(request, "replace_existing", "replaceExisting"),
        edits=get(request, "edits"),
        time_zone=built.time_zone,
    )
    saved = [
        line(a)
        for a in sorted(
            (a for a in env.db.rows("alarms") if a["participant_id"] == body2["participant_id"]),
            key=lambda a: ORDER.index(a["alarm_type"]),
        )
    ]
    return SimpleNamespace(shown=shown, saved=saved, appointment_id=body2["appointment_id"])


@pytest.mark.parametrize(("name", "body", "now"), SCENARIOS, ids=[s[0] for s in SCENARIOS])
@sync
async def test_preview_equals_save(name, body, now):
    """미리 보기 = 저장: {name}"""
    result = await preview_and_save(setup(now), body)
    assert result.saved == result.shown
    assert len(result.saved) > 0


@sync
async def test_preview_equals_save_when_the_user_turned_an_alarm_off_it_stays_off_and_request_enabled_wins():
    """미리 보기 = 저장: 사용자가 끈 알람은 다시 저장해도 꺼진 채, 요청의 enabled가 먼저 (기존 약속)"""
    env = setup()
    first = await preview_and_save(env, FULL)
    prepare = next(a for a in env.db.rows("alarms") if a["alarm_type"] == "PREPARE")
    prepare["is_enabled"] = False  # H02에서 끔

    again = {
        "appointment_id": first.appointment_id,
        "participant": {"est_travel_minutes": 50, "travel_source": "API"},
    }
    kept = await preview_and_save(env, again)
    assert kept.saved == kept.shown
    assert kept.saved[1].startswith("PREPARE ") and " false " in kept.saved[1]  # 꺼진 채로

    turned_on = await preview_and_save(env, {**again, "enabled": {"PREPARE": True}})
    assert turned_on.saved == turned_on.shown
    assert turned_on.saved[1].startswith("PREPARE ") and " true " in turned_on.saved[1]


# ---- 그날 첫 일정 판정: 내 참여 → 약속 시각 + 기상 알람을 읽고 약속 시각이 앞선 것만 셈 ----
# 가짜 DB는 PostgREST 임베드(`appointments!inner`)를 흉내 못 내서, 부른 쿼리 모양은 기록 도우미로 확인하고 판정은 읽어 온 행으로 시험함


class RecordingQuery:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self._calls.append([name, *args])
            return self

        return record

    def __await__(self):
        async def result():
            return SimpleNamespace(data=self._rows, error=None, count=None)

        return result().__await__()


class RecordingDb:
    """쿼리 빌더 호출을 기록하고 준비해 둔 행을 돌려주는 도우미"""

    def __init__(self, rows):
        self.calls: list[list] = []
        self._rows = rows

    def from_(self, table):
        self.calls.append(["from_", table])
        return RecordingQuery(self._rows, self.calls)


def recording_db(rows):
    return RecordingDb(rows)


def wake_row(appointment_id, appointment_minutes, is_enabled=True):
    """그날 내 참여 한 줄: 약속 시각 + 기상 알람(켜짐 여부)"""
    return {
        "appointment_id": appointment_id,
        "appointments": {"appointment_at": iso(22, appointment_minutes)},
        "alarms": [{"is_enabled": is_enabled}],
    }


@sync
async def test_first_event_of_the_day_ten_oclock_with_wake_means_830_has_wake_and_eight_means_ten_has_none():
    """그날 첫 일정: 10시 약속(기상 8:00)이 있는 날 8:30 새 약속은 기상 있음, 8:00 약속이 있는 날 10:00 새 약속은 기상 없음"""
    with_ten = recording_db([wake_row("ap-ten", 600)])
    assert await load_same_day_wake(with_ten, USER, kst(22, 510), "Asia/Seoul") is False

    with_eight = recording_db([wake_row("ap-eight", 480)])
    assert await load_same_day_wake(with_eight, USER, kst(22, 600), "Asia/Seoul") is True


@sync
async def test_first_event_of_the_day_same_time_is_both_wake_and_off_or_own_wake_does_not_count():
    """그날 첫 일정: 같은 시각 약속 둘은 둘 다 기상, 꺼 둔 기상 · 자기 자신의 기상은 안 셈"""
    at = kst(22, 600)
    assert (
        await load_same_day_wake(recording_db([wake_row("ap-other", 600)]), USER, at, "Asia/Seoul") is False
    )
    assert (
        await load_same_day_wake(recording_db([wake_row("ap-other", 480, False)]), USER, at, "Asia/Seoul")
        is False
    )
    assert (
        await load_same_day_wake(recording_db([wake_row("ap-self", 480)]), USER, at, "Asia/Seoul", "ap-self")
        is False
    )
    both = recording_db([wake_row("ap-self", 480), wake_row("ap-other", 540)])
    assert await load_same_day_wake(both, USER, at, "Asia/Seoul", "ap-self") is True


@sync
async def test_first_event_of_the_day_reads_object_or_list_embeds_and_skips_unreadable_times():
    """그날 첫 일정: 임베드가 객체로 와도 배열로 와도 읽음, 약속 시각을 못 읽는 행은 건너뜀"""
    mixed_shapes = {
        "appointment_id": "a",
        "appointments": [{"appointment_at": iso(22, 480)}],
        "alarms": {"is_enabled": True},
    }
    assert await load_same_day_wake(recording_db([mixed_shapes]), USER, kst(22, 600), "Asia/Seoul") is True
    broken = [
        {"appointment_id": "b", "appointments": None, "alarms": [{"is_enabled": True}]},
        {"appointment_id": "c", "appointments": {"appointment_at": "x"}, "alarms": [{"is_enabled": True}]},
    ]
    assert await load_same_day_wake(recording_db(broken), USER, kst(22, 600), "Asia/Seoul") is False


@sync
async def test_first_event_of_the_day_reads_only_my_participation_wake_alarms_within_my_local_day():
    """그날 첫 일정: 내 참여만, 기상 알람만, 내 시간대 하루(0시 ~ 다음 0시)의 약속 시각으로 읽음"""
    db = recording_db([])
    await load_same_day_wake(db, USER, kst(22, 600), "Asia/Seoul")
    calls = db.calls
    assert calls[0] == ["from_", "appointment_participants"]
    select = next(c for c in calls if c[0] == "select")
    assert "appointments!inner(appointment_at)" in str(select[1])
    assert "alarms!inner(is_enabled)" in str(select[1])
    filters = [" ".join(str(x) for x in c) for c in calls if c[0] not in ("from_", "select")]
    assert sorted(filters) == sorted(
        [
            "eq alarms.alarm_type WAKE",
            f"eq user_id {USER}",
            f"gte appointments.appointment_at {iso_z(kst(22, 0))}",
            f"lt appointments.appointment_at {iso_z(kst(23, 0))}",
        ]
    )


# ---- 기존 약속을 다시 저장할 때 직접 입력한 이동시간 이어받기 (시각 · 강도의 직접 값과 같은 규칙) ----

AP = "00000000-0000-4000-8000-0000000000a1"


def counting_client():
    """카카오로 나가는 호출을 세는 가짜 네트워크. 부르면 실패 응답을 줘서 다음 단계(기록 → 근사)로 넘어감"""
    state = SimpleNamespace(calls=0)

    def handler(request: httpx.Request) -> httpx.Response:
        state.calls += 1
        return httpx.Response(500, text="{}")

    return state, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def stored_appointment(db, source, minutes):
    """저장된 약속 하나 (10시 · 좌표 있음 · 저장된 이동시간은 source로)"""
    db.rows("appointments").append(
        {
            "appointment_id": AP,
            "creator_id": USER,
            "group_id": None,
            "title": "팀 주간회의",
            "appointment_at": iso(22, 600),
            "dest_name": "세종대학교",
            "dest_lat": 37.55,
            "dest_lng": 127.07,
            "importance": "NORMAL",
            "recurrence_rule": None,
        }
    )
    db.rows("appointment_participants").append(
        {
            "participant_id": "pt1",
            "appointment_id": AP,
            "user_id": USER,
            "origin_name": "집",
            "origin_lat": 37.5547,
            "origin_lng": 126.9707,
            "transport_type": "TRANSIT",
            "est_travel_minutes": minutes,
            "travel_source": source,
            "prepare_minutes": 60,
            "buffer_minutes": 10,
        }
    )


def with_kakao(ctx, client):
    return PlanContext(user_id=ctx.user_id, db=ctx.db, now=ctx.now, kakao_key="k", http=client)


@sync
async def test_inherit_travel_manual_stored_minutes_are_kept_and_the_route_api_is_not_called():
    """이동시간 이어받기: 요청에 이동시간이 없고 저장된 게 MANUAL이면 저장된 분을 그대로, 길찾기는 안 부름"""
    env = setup()
    stored_appointment(env.db, "MANUAL", 70)
    state, client = counting_client()
    async with client:
        built = await build_plan(with_kakao(env.ctx, client), draft_of({"appointment_id": AP}))
    plan = built.plan
    assert [plan.travel.chosen.source, plan.travel_minutes, plan.travel.chosen.needs_confirmation] == [
        "MANUAL",
        70,
        False,
    ]
    assert state.calls == 0
    # 출발 = 10:00 − 여유 10 − 이동 70
    assert chain_of(plan) == ["WAKE 07:50", "PREPARE 08:10", "DEPARTURE 08:40", "ARRIVE 09:50"]


@sync
async def test_inherit_travel_preview_also_gets_the_auto_value_separately():
    """이동시간 이어받기: 미리 보기도 같은 값(MANUAL 70분)에 "자동으로 되돌리기" 값만 따로 구함"""
    env = setup()
    stored_appointment(env.db, "MANUAL", 70)
    state, client = counting_client()
    async with client:
        built = await build_plan(
            with_kakao(env.ctx, client), draft_of({"appointment_id": AP}), want_auto_travel=True
        )
    assert [built.plan.travel.chosen.source, built.plan.travel_minutes] == ["MANUAL", 70]
    assert built.plan.travel.auto  # 되돌리기 후보
    assert state.calls > 0


@sync
async def test_inherit_travel_request_minutes_come_first_for_manual_and_api():
    """이동시간 이어받기: 요청에 이동시간이 있으면 그 값이 먼저 (MANUAL · API 둘 다)"""
    env = setup()
    stored_appointment(env.db, "MANUAL", 70)
    manual = await build_plan(
        env.ctx,
        draft_of(
            {"appointment_id": AP, "participant": {"est_travel_minutes": 35, "travel_source": "MANUAL"}}
        ),
    )
    assert [manual.plan.travel.chosen.source, manual.plan.travel_minutes] == ["MANUAL", 35]
    api = await build_plan(
        env.ctx,
        draft_of({"appointment_id": AP, "participant": {"est_travel_minutes": 45, "travel_source": "API"}}),
    )
    assert [api.plan.travel.chosen.source, api.plan.travel_minutes] == ["API", 45]


@sync
async def test_inherit_travel_stored_api_or_cached_is_looked_up_again_and_manual_without_minutes_has_nothing_to_inherit():
    """이동시간 이어받기: 저장된 게 API · CACHED면 이어받지 않고 다시 구함 (D5), MANUAL인데 분이 비면 이어받을 게 없음"""
    for source in ("API", "CACHED"):
        env = setup()
        stored_appointment(env.db, source, 70)
        state, client = counting_client()
        async with client:
            built = await build_plan(with_kakao(env.ctx, client), draft_of({"appointment_id": AP}))
        assert built.plan.travel.chosen.source != "MANUAL"
        assert built.plan.travel_minutes != 70
        assert state.calls > 0  # 길찾기를 다시 부름
    env = setup()
    stored_appointment(env.db, "MANUAL", None)
    built = await build_plan(env.ctx, draft_of({"appointment_id": AP}))
    assert built.plan.travel.chosen.source == "ESTIMATE"


@sync
async def test_inherit_travel_resaving_without_key_or_records_keeps_manual():
    """이동시간 이어받기: 키 · 기록이 없어도 다시 저장되고 직접 입력은 MANUAL 그대로 남음 (예전에는 502)"""
    env = setup()
    first = await preview_and_save(
        env,
        {**FULL, "participant": {**FULL["participant"], "est_travel_minutes": 70, "travel_source": "MANUAL"}},
    )
    again = await preview_and_save(env, {"appointment_id": first.appointment_id})
    assert again.saved == again.shown
    row = next(
        p for p in env.db.rows("appointment_participants") if p["appointment_id"] == first.appointment_id
    )
    assert [row["travel_source"], row["est_travel_minutes"]] == ["MANUAL", 70]


@sync
async def test_past_alarm_warning_text_differs_by_mode_but_the_code_is_the_same():
    """지난 알림 경고 문구: alarm-suggest(preview)는 "저장할 때 꺼져요", alarm-commit(기본 save)은 "끄고 저장했어요", 코드는 같음"""
    now = kst(22, 490)  # 기상 8:10이 곧 지나는 때 (지금 + 5분보다 앞)
    preview = await build_plan(setup(now).ctx, draft_of(FULL), want_auto_travel=True, mode="preview")
    save = await build_plan(setup(now).ctx, draft_of(FULL))
    assert [[w["code"], w["message"]] for w in preview.plan.warnings] == [
        ["PAST_ALARM_DISABLED", "이미 지난 알림은 저장할 때 꺼져요"]
    ]
    assert [[w["code"], w["message"]] for w in save.plan.warnings] == [
        ["PAST_ALARM_DISABLED", "이미 지난 알림은 끄고 저장했어요"]
    ]
    # 앱이 받는 미리 보기 응답에 그 문구
    assert to_suggestion(preview.plan, preview.inputs)["warnings"] == preview.plan.warnings


# ---- 준비 통계: 내 기상~출발 기록 최근 30건 · 10~240분만 (학습 루프 기초 ③) ----


def record(user, days_ago, minutes):
    """날짜(record_date)만 다른 기록 줄들 — days_ago가 클수록 오래됨"""
    return {
        "record_id": f"r-{user}-{days_ago}",
        "user_id": user,
        "record_date": (datetime(2025, 5, 22, tzinfo=UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d"),
        "actual_prepare_minutes": minutes,
    }


@sync
async def test_prepare_stats_use_only_my_user_id_rows():
    """준비 통계는 내 user_id 줄만: 다른 사람 기록이 같이 보여도 오류 없이 내 값 (친구 기록 조회 정책이 생겨도)"""
    env = setup()
    env.db.rows("daily_records").extend(
        [record("u-friend", 1, 100), *[record(USER, i, m) for i, m in enumerate([40, 41, 42, 43, 44])]]
    )
    mine = await load_prepare_stats(env.db, USER)
    count, p80, values = stats_of(mine)
    assert count == 5
    assert p80 == pytest.approx(43.2)  # 40 41 42 43 44의 0.8 분위수 (선형 보간)
    assert stats_of(await load_prepare_stats(env.db, "u-nobody")) == (0, None, [])
    assert values == [40, 41, 42, 43, 44]  # 최신순 (days_ago 0이 40)
    built = await build_plan(env.ctx, draft_of(FULL))
    assert built.plan.prepare["sampleCount"] == 5


@sync
async def test_prepare_stats_use_only_the_latest_30_and_10_to_240_minutes():
    """준비 통계는 최근 30건만, 10~240분만: 31번째 오래된 기록과 5분 · 400분 · 빈 값은 무시"""
    env = setup()
    # 최근 30일은 60분, 그 전 31번째 날(days_ago 30)은 200분 — 30건 창 밖이라 무시돼야 함
    for d in range(30):
        env.db.rows("daily_records").append(record(USER, d, 60))
    env.db.rows("daily_records").append(record(USER, 30, 200))
    # 범위 밖 · 빈 값 — 가장 최근 날짜(-1 ~ -5일 전)에 있어도 30건 창을 차지하지 않고 무시
    env.db.rows("daily_records").extend(
        [
            record(USER, -1, 5),
            record(USER, -2, 400),
            record(USER, -3, None),
            record(USER, -4, 9),
            record(USER, -5, 241),
        ]
    )
    assert stats_of(await load_prepare_stats(env.db, USER)) == (30, 60, [60] * 30)
    # 경계값 10 · 240은 쓴다
    edge = setup().db
    edge.rows("daily_records").extend([record(USER, 0, 10), record(USER, 1, 240)])
    assert stats_of(await load_prepare_stats(edge, USER)) == (2, 194, [10, 240])


@sync
async def test_the_view_v_prepare_time_stats_is_no_longer_read():
    """뷰 v_prepare_time_stats는 더 읽지 않음: 뷰에 값이 있어도 기록이 없으면 0건"""
    env = setup()
    env.db.rows("v_prepare_time_stats").append({"user_id": USER, "sample_count": 12, "p80_minutes": 41.5})
    assert stats_of(await load_prepare_stats(env.db, USER)) == (0, None, [])


# ---- 학습 루프 기초 b (이슈 #272): 프로필의 잠귀 · 깨우는 여유 ----


@sync
async def test_load_profile_reads_wake_sensitivity_and_wake_margin_and_defaults_to_normal_without_a_profile():
    """loadProfile이 잠귀(wake_sensitivity)와 깨우는 여유(wake_margin) 두 칸을 읽음 — 프로필이 없으면 둘 다 보통"""
    env = setup()
    env.db.rows("profiles")[0].update({"wake_sensitivity": "HEAVY", "wake_margin": "LOOSE"})
    mine = await load_profile(env.db, USER)
    assert [mine["wake_sensitivity"], mine["wake_margin"]] == ["HEAVY", "LOOSE"]
    none = await load_profile(env.db, "u-nobody")
    assert [none["wake_sensitivity"], none["wake_margin"]] == ["NORMAL", "NORMAL"]


@sync
async def test_build_plan_passes_the_two_profile_fields_into_the_calculation():
    """buildPlan: 프로필의 두 칸이 계산에 닿음 — 잠귀 깊음 → 강, 넉넉 → 같은 기록에서 준비가 더 김, 응답에 최근 값"""
    body = {
        "appointment": {
            "appointment_at": iso(22, 600),
            "destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07},
        },
        "participant": {"est_travel_minutes": 50, "travel_source": "MANUAL"},
    }

    async def run(profile):
        env = setup()
        env.db.rows("profiles")[0].update(profile)
        # 최근 10일 기록 (최신이 앞): 70 100 40 80 45 75 55 60 50 65
        for d, m in enumerate([70, 100, 40, 80, 45, 75, 55, 60, 50, 65]):
            env.db.rows("daily_records").append(record(USER, d, m))
        built = await build_plan(env.ctx, draft_of(body))
        return to_suggestion(built.plan)

    normal = await run({})
    tuned = await run({"wake_sensitivity": "HEAVY", "wake_margin": "LOOSE"})
    assert [normal["intensity"]["level"], normal["prepare"]["minutes"]] == ["NORMAL", 63]
    assert [tuned["intensity"]["level"], tuned["prepare"]["minutes"]] == ["STRONG", 66]
    assert tuned["prepare"]["recent"] == [70, 100, 40, 80, 45]
    assert (await run({"wake_margin": "TIGHT"}))["prepare"]["minutes"] == 61


# ---- 서버판에서 더한 확인 (원본 시험에 없는 경계) ----


@sync
async def test_server_version_unknown_appointment_is_not_found():
    """(서버판 확인) 기존 약속이 내게 안 보이면 NOT_FOUND (RLS가 거른 것과 같음)"""
    env = setup()
    with pytest.raises(AppError) as info:
        await build_plan(env.ctx, draft_of({"appointment_id": AP}))
    assert info.value.code == "NOT_FOUND"


@sync
async def test_server_version_late_count_counts_only_my_late_arrivals_in_the_last_28_days():
    """(서버판 확인) 최근 4주 지각 횟수는 내 is_late 줄 중 28일 안의 도착만"""
    from app.shared.plan import load_late_count

    env = setup()
    now = kst(22, 400)
    mine = lambda days, late=True, user=USER: {  # noqa: E731
        "participant_id": f"p-{user}-{days}-{late}",
        "user_id": user,
        "is_late": late,
        "arrived_at": (now - timedelta(days=days)).isoformat(),
    }
    env.db.rows("appointment_participants").extend(
        [mine(1), mine(27), mine(29), mine(3, late=False), mine(2, user="u-friend")]
    )
    assert await load_late_count(env.db, USER, now) == 2

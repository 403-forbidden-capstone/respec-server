"""본 레포 supabase/functions/_shared/morning-flow.test.ts의 서버판 — 하루 재생 (이슈 #230 PR 1a)

약속 하나를 저장한 날 서버에서 볼 수 있는 단계를 가짜 시계 · 가짜 카카오 · 가짜 DB로 차례로 확인
  저장(길찾기 → commit) → 꾸러미 문구(준비 · 출발 · 기상 voiceText) → 기상 시각별 여유 → 출발 시각 → 늦게 일어난 날 → 새벽 1시 약속
서버에서 안 보이는 단계는 PR 1b: 앱의 기상 확정(H08) · 아침 흐름 화면 · 홈 카드와 getMorningPlan · 폰이 알림을 띄우는 모습

"""

from types import SimpleNamespace

import httpx
import pytest

from app.core.errors import AppError
from app.core.read import Reader
from app.shared.calc.depart import depart_now, depart_slack
from app.shared.commit import CommitCtx, assert_persistable, persist_appointment
from app.shared.js import from_ms
from app.shared.plan import PlanContext, build_plan
from app.shared.requests import read_commit_request
from app.shared.tz import format_ko_time
from tests.fake_db import FakeDb

from .w2_helpers import get, iso, iso_z, kst, sync

USER = "u-me"
TZ = "Asia/Seoul"


def ko(value):
    return format_ko_time(from_ms(value) if isinstance(value, int) else value, TZ)


def transit(seconds):
    """가짜 카카오 대중교통: 앞뒤가 도보인 한 경로(도보 API를 더 안 부름), 총 seconds초"""
    return {
        "status": "OK",
        "routes": [
            {
                "properties": {"type": "SUBWAY", "totalTime": seconds},
                "steps": [
                    {"properties": {"type": "WALKING"}, "path": {"points": [[127.0, 37.5], [127.01, 37.51]]}},
                    {
                        "properties": {"type": "SUBWAY"},
                        "path": {"points": [[127.01, 37.51], [127.05, 37.54]]},
                    },
                    {
                        "properties": {"type": "WALKING"},
                        "path": {"points": [[127.05, 37.54], [127.07, 37.55]]},
                    },
                ],
            }
        ],
    }


def fake_kakao(seconds):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/publictraffic"):
            return httpx.Response(200, json=transit(seconds))
        return httpx.Response(404, text="not found")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def setup(now, travel_seconds):
    db = FakeDb().with_commit_rpc()
    db.rows("profiles").append(
        {
            "user_id": USER,
            "timezone": TZ,
            "default_buffer_minutes": 10,
            "default_transport": "TRANSIT",
            "home_name": "집",
            "home_lat": 37.5,
            "home_lng": 127.0,
        }
    )
    client = fake_kakao(travel_seconds)
    ctx = PlanContext(user_id=USER, db=db, now=now, kakao_key="KEY", http=client)
    commit_ctx = CommitCtx(user_id=USER, user_db=db, service=db, now=now)
    return SimpleNamespace(db=db, ctx=ctx, commit_ctx=commit_ctx, client=client)


def request(appointment_at, before_prepare_minutes):
    """앱이 보내는 요청 그대로: 이동은 안 보내서 가짜 카카오가 구함, 준비 60분 · 준비 전 활동 · 여유 10분"""
    return {
        "appointment": {
            "title": "전공 수업",
            "appointment_at": appointment_at,
            "destination": {"name": "광개토관", "lat": 37.55, "lng": 127.07},
            "importance": "NORMAL",
        },
        "participant": {
            "origin": {"name": "집", "lat": 37.5, "lng": 127.0},
            "transport_type": "TRANSIT",
            "buffer_minutes": 10,
            "prepare_minutes": 60,
            "before_prepare_minutes": before_prepare_minutes,
        },
    }


async def commit(env, body):
    req = read_commit_request(Reader.root(body))
    assert get(req, "type") == "APPOINTMENT"
    draft = get(req, "draft")
    built = await build_plan(env.ctx, draft)
    assert_persistable(built.plan)
    saved = await persist_appointment(
        env.commit_ctx,
        plan=built.plan,
        inputs=built.inputs,
        created=get(req, "appointment"),
        draft=draft,
        replace_existing=get(req, "replace_existing", "replaceExisting"),
        edits=get(req, "edits"),
        time_zone=built.time_zone,
    )
    return built.plan, {p["type"]: p for p in saved["ring_packages"]}


def slack_of(plan, wake):
    r = depart_slack(
        {
            "appointmentAt": plan.basics.at,
            "travelMinutes": plan.travel_minutes,
            "now": wake,
            "remainingMinutes": plan.prepare["minutes"],
        }
    )
    return r["slackMinutes"], r["status"]


@sync
async def test_a_day_replay_ten_oclock_class_saved_then_texts_then_slack_by_wake_time_then_departure_then_oversleep():
    """하루 재생: 10시 수업 저장 → 문구 → 기상 시각별 여유 → 출발 시각 → 늦잠"""
    # 오전 6:40 저장. 가짜 카카오가 대중교통 50분을 줌
    env = setup(kst(22, 400), 3000)
    async with env.client:
        plan, packages = await commit(env, request(iso(22, 600), 30))

    # 1. 저장 — 알람 시각은 기존 계산 그대로 (기상 8:00 · 준비 8:30 · 출발 9:00 · 도착 9:50)
    assert plan.travel_minutes == 50
    assert [f"{p['type']} {ko(p['atMillis'])}" for p in packages.values()] == [
        "WAKE 오전 8:00",
        "PREPARE 오전 8:30",
        "DEPARTURE 오전 9:00",
        "ARRIVE 오전 9:50",
    ]

    # 2. 꾸러미 문구 — 새 알림을 늘리지 않고 준비 · 출발 알림에 합침, 기상은 voiceText만
    assert packages["PREPARE"]["notice"] == "오전 9:00 출발까지 30분"
    assert packages["DEPARTURE"]["notice"] == "지금 나가면 오전 9:50 도착 · 여유 10분"
    assert (
        packages["WAKE"]["voiceText"]
        == "좋은 아침이에요. 오늘 첫 일정은 오전 10시 전공 수업이에요. 오전 9시에 나가면 돼요."
    )
    assert packages["WAKE"].get("notice") is None
    assert [packages["ARRIVE"].get("notice"), packages["ARRIVE"].get("voiceText")] == [None, None]
    assert packages["WAKE"]["subtitle"] == "오전 10:00 · 광개토관"  # 부제는 그대로

    # 3. 기상 시각별 여유 (상세설계서 7.5.2 표) — 앱이 기상을 확정한 시각(PR 1b)을 넣을 자리
    assert slack_of(plan, kst(22, 480)) == (10, "COMFORT")  # 정시 기상
    assert slack_of(plan, kst(22, 485)) == (5, "TIGHT")  # 5분 미룸
    assert slack_of(plan, kst(22, 505)) == (-15, "LATE_RISK")  # 늦잠

    # 4. 출발 시각 — 출발 알림 글자의 도착 · 여유와 departNow가 같은 값
    leave = depart_now(
        {
            "appointmentAt": plan.basics.at,
            "travelMinutes": plan.travel_minutes,
            "now": from_ms(packages["DEPARTURE"]["atMillis"]),
        }
    )
    assert [
        ko(leave["arriveAt"]),
        leave["slackMinutes"],
        leave["status"],
    ] == [
        "오전 9:50",
        10,
        "COMFORT",
    ]

    # 5. 늦게 일어난 날 — 출발 시각(9:00)이 지난 9:20에 일어남: 준비를 다 하면 지각 위험, 바로 나가도 10분 늦음
    assert slack_of(plan, kst(22, 560)) == (-70, "LATE_RISK")
    late_leave = depart_now(
        {"appointmentAt": plan.basics.at, "travelMinutes": plan.travel_minutes, "now": kst(22, 560)}
    )
    assert [
        ko(late_leave["arriveAt"]),
        late_leave["slackMinutes"],
    ] == [
        "오전 10:10",
        -10,
    ]
    # 서버는 출발 시각이 지난 뒤의 다시 저장을 지금도 막음 (문구는 저장할 때 정해진 채 그대로, 다시 계산하지 않음)
    after = setup(kst(22, 545), 3000)
    req = read_commit_request(Reader.root(request(iso(22, 600), 30)))
    assert get(req, "type") == "APPOINTMENT"
    async with after.client:
        built = await build_plan(after.ctx, get(req, "draft"))
    with pytest.raises(AppError) as info:
        assert_persistable(built.plan)
    assert info.value.code == "UNPROCESSABLE"
    assert info.value.details["reason"] == "DEPARTURE_PASSED"


@sync
async def test_one_am_appointment_day_boundary_alarms_ring_the_night_before_and_texts_do_not_show_the_date():
    """새벽 1시 약속(하루 경계): 알람은 전날 밤에 울리고 문구는 그 시각 그대로 — 날짜는 글자에 안 나옴"""
    # 21:00 저장, 다음 날 새벽 1:00 약속, 이동 30분 → 출발 0:20 · 기상 전날 23:20 · 준비 23:40 (준비 전 활동 20분)
    env = setup(kst(22, 1260), 1800)
    async with env.client:
        plan, packages = await commit(env, request(iso(23, 60), 20))

    assert [[p["type"], iso_z(from_ms(p["atMillis"]))] for p in packages.values()] == [
        ["WAKE", iso_z(kst(22, 1400))],  # 22일 23:20 — 약속(23일 1:00)보다 하루 앞선 날짜
        ["PREPARE", iso_z(kst(22, 1420))],
        ["DEPARTURE", iso_z(kst(23, 20))],  # 23일 0:20
        ["ARRIVE", iso_z(kst(23, 50))],
    ]
    assert packages["PREPARE"]["notice"] == "오전 12:20 출발까지 40분"
    assert packages["DEPARTURE"]["notice"] == "지금 나가면 오전 12:50 도착 · 여유 10분"
    assert (
        packages["WAKE"]["voiceText"]
        == "좋은 아침이에요. 오늘 첫 일정은 오전 1시 전공 수업이에요. 오전 12시 20분에 나가면 돼요."
    )

    # 여유 계산은 날짜를 보지 않고 밀리초 차이라 자정을 넘어도 같은 표
    assert slack_of(plan, kst(22, 1400)) == (10, "COMFORT")
    assert slack_of(plan, kst(22, 1405)) == (5, "TIGHT")
    assert slack_of(plan, kst(22, 1425)) == (-15, "LATE_RISK")

"""일정 수정 · 삭제의 순수 계산 (본 레포 supabase/functions/_shared/edit.test.ts의 서버판) — 표로 확인"""

from datetime import UTC, datetime, timedelta, timezone

from app.shared.calc import SameDayWake
from app.shared.edit import (
    CONFIRM_MOVE_MINUTES,
    UNSET,
    DayWakeRow,
    alarm_changes,
    apply_patch,
    delete_mode,
    editor_verdict,
    later_rows_to_delete,
    needs_confirm,
    pick_base_row,
    scope_effect,
    wake_action,
    wakes_of_rows,
)
from app.shared.js import ms, to_iso

KST = timezone(timedelta(hours=9))


def kst(local: str) -> datetime:
    """new Date(`${local}:00+09:00`)"""
    return datetime.fromisoformat(f"{local}:00").replace(tzinfo=KST).astimezone(UTC)


def iso(local: str) -> str:
    return to_iso(kst(local))


def ms_of(value: str) -> int:
    return ms(datetime.fromisoformat(value.replace("Z", "+00:00")))


# ---- 범위 효과 ----


def test_scope_effect_반복_일정은_이번만_그_행만_이후_모두는_새_기준():
    """scopeEffect: 반복 일정은 이번만 = 그 행만 + 이번만 고침 표시 / 이후 모두 = 이 행이 새 기준(표시 지움) + 뒤 회차 지움"""
    rule = "FREQ=WEEKLY;BYDAY=TU,TH"
    base = {"recurring": True, "group": False, "current_rule": rule}
    assert scope_effect(**base, scope="this", patch_rule=UNSET) == {
        "span": "ROW",
        "mark": "EDITED",
        "deleteLater": False,
        "rule": {"kind": "KEEP"},
    }
    assert scope_effect(**base, scope="following", patch_rule=UNSET) == {
        "span": "FROM_HERE",
        "mark": "CLEAR",
        "deleteLater": True,
        "rule": {"kind": "KEEP"},
    }


def test_scope_effect_이번만은_반복_요일_무시_이후_모두는_규칙_변경_반영():
    """scopeEffect: 이번만 고칠 때 반복 요일은 무시, 이후 모두면 같은 규칙은 그대로 · 다른 규칙은 새로 · null은 묶음 모든 행의 규칙을 비움"""
    rule = "FREQ=WEEKLY;BYDAY=TU,TH"
    base = {"recurring": True, "group": False, "current_rule": rule}
    assert scope_effect(**base, scope="this", patch_rule="FREQ=DAILY")["rule"] == {"kind": "KEEP"}
    assert scope_effect(**base, scope="this", patch_rule=None)["rule"] == {"kind": "KEEP"}
    assert scope_effect(**base, scope="following", patch_rule=rule)["rule"] == {"kind": "KEEP"}
    assert scope_effect(**base, scope="following", patch_rule="FREQ=DAILY")["rule"] == {
        "kind": "SET",
        "value": "FREQ=DAILY",
    }
    assert scope_effect(**base, scope="following", patch_rule=None)["rule"] == {"kind": "CLEAR_ALL"}


def test_scope_effect_한_번짜리와_공동_약속은_그_행_하나():
    """scopeEffect: 한 번짜리는 범위와 상관없이 그 행 하나(반복을 켜면 이 행에 규칙), 공동 약속은 늘 그 행 하나 · 규칙 그대로"""
    keep = {"span": "ROW", "mark": "KEEP", "deleteLater": False, "rule": {"kind": "KEEP"}}
    for scope in ("this", "following"):
        assert (
            scope_effect(recurring=False, group=False, scope=scope, patch_rule=UNSET, current_rule=None)
            == keep
        )
        assert scope_effect(
            recurring=False, group=False, scope=scope, patch_rule="FREQ=DAILY", current_rule=None
        )["rule"] == {"kind": "SET", "value": "FREQ=DAILY"}
        assert scope_effect(recurring=False, group=False, scope=scope, patch_rule=None, current_rule=None)[
            "rule"
        ] == {"kind": "KEEP"}
        assert (
            scope_effect(
                recurring=False, group=True, scope=scope, patch_rule="FREQ=DAILY", current_rule="FREQ=DAILY"
            )
            == keep
        )


def test_delete_mode_범위별_지우기_방식():
    """deleteMode: 반복 + 이번만 = 알람만 지움(행은 둠), 반복 + 이후 모두 = 뒤 회차부터 지움, 한 번짜리 · 공동 약속 = 행 지움"""
    assert delete_mode(recurring=True, group=False, scope="this") == "CANCEL_THIS"
    assert delete_mode(recurring=True, group=False, scope="following") == "FROM_HERE"
    assert delete_mode(recurring=False, group=False, scope="this") == "ROW"
    assert delete_mode(recurring=False, group=False, scope="following") == "ROW"
    assert delete_mode(recurring=True, group=True, scope="following") == "ROW"


def test_editor_verdict_만든_사람만():
    """editorVerdict: 만든 사람만 · 못 찾음 · 공동 약속의 참여만 한 사람"""
    assert editor_verdict(row=None, user_id="a") == "NOT_VISIBLE"
    assert editor_verdict(row={"creator_id": "a", "group_id": None}, user_id="a") == "OK"
    assert editor_verdict(row={"creator_id": "a", "group_id": "g"}, user_id="a") == "OK"
    assert editor_verdict(row={"creator_id": "a", "group_id": "g"}, user_id="b") == "NOT_CREATOR"


def test_x_appt_01_editor_verdict_만든_사람이_빈_공동_약속은_참여한_그룹원이():
    """X-APPT-01 editorVerdict: 만든 사람이 비어 있는 공동 약속은 참여한 그룹원이 고치고 지움 — 참여 안 한 사람 · 개인 약속은 거절"""
    orphan = {"creator_id": None, "group_id": "g"}
    assert editor_verdict(row=orphan, user_id="b", participating=True) == "OK"
    assert editor_verdict(row=orphan, user_id="b", participating=False) == "NOT_CREATOR"
    assert editor_verdict(row=orphan, user_id="b") == "NOT_CREATOR"
    personal = {"creator_id": None, "group_id": None}
    assert editor_verdict(row=personal, user_id="b", participating=True) == "NOT_CREATOR"
    # 만든 사람이 있으면 참여자도 못 지움
    assert (
        editor_verdict(row={"creator_id": "a", "group_id": "g"}, user_id="b", participating=True)
        == "NOT_CREATOR"
    )


# ---- 고친 값 반영 ----

ROW = {
    "title": "세미나",
    "appointment_at": iso("2026-10-29T09:00"),
    "dest_name": "세종대학교",
    "dest_lat": "37.550000",
    "dest_lng": 127.07,
    "importance": "NORMAL",
}


def test_apply_patch_장소_이름만_같은_걸_보내면_옛_좌표_유지():
    """applyPatch: 장소 이름만 같은 걸 보내면 옛 좌표 유지, 이름이 바뀌면 좌표를 비움 (로컬 리뷰 반영)"""
    same = apply_patch(ROW, {"destination": {"name": "세종대학교"}, "title": "바꿈"})
    nxt = same["next"]
    assert [nxt["dest_name"], nxt["dest_lat"], nxt["dest_lng"], same["changed"]["place"]] == [
        "세종대학교",
        37.55,
        127.07,
        False,
    ]
    renamed = apply_patch(ROW, {"destination": {"name": "건국대학교"}})
    assert [renamed["next"]["dest_lat"], renamed["next"]["dest_lng"], renamed["changed"]["place"]] == [
        None,
        None,
        True,
    ]
    moved = apply_patch(ROW, {"destination": {"name": "세종대학교", "lat": 37.6, "lng": 127.1}})
    assert [moved["next"]["dest_lat"], moved["changed"]["place"]] == [37.6, True]


def test_apply_patch_안_넣은_칸은_그대로_같은_값은_바뀐_것이_아님():
    """applyPatch: 안 넣은 칸은 그대로, 같은 값은 바뀐 것이 아님 (numeric이 글자로 와도 같은 좌표로 봄)"""
    same = apply_patch(
        ROW,
        {
            "title": "  세미나 ",
            "at": kst("2026-10-29T09:00"),
            "destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07},
            "importance": "NORMAL",
        },
    )
    assert same["any"] is False
    assert same["changed"] == {"title": False, "at": False, "place": False, "importance": False}
    assert same["affects_alarms"] is False
    assert apply_patch(ROW, {})["next"] == {
        "title": "세미나",
        "appointment_at": ROW["appointment_at"],
        "dest_name": "세종대학교",
        "dest_lat": 37.55,
        "dest_lng": 127.07,
        "importance": "NORMAL",
    }


def test_apply_patch_제목만_바뀌면_알람은_안_건드림():
    """applyPatch: 제목만 바뀌면 알람은 안 건드림, 시각 · 장소 · 중요도는 알람을 다시 계산"""
    title = apply_patch(ROW, {"title": "발표"})
    assert [title["any"], title["affects_alarms"], title["next"]["title"]] == [True, False, "발표"]
    assert apply_patch(ROW, {"at": kst("2026-10-29T10:00")})["affects_alarms"] is True
    assert apply_patch(ROW, {"importance": "HIGH"})["affects_alarms"] is True
    place = apply_patch(ROW, {"destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276}})
    assert [
        place["changed"]["place"],
        place["affects_alarms"],
        place["next"]["dest_name"],
        place["next"]["dest_lat"],
    ] == [True, True, "강남역", 37.4979]


def test_apply_patch_장소_이름만_바뀌고_좌표가_없으면_옛_좌표는_비움():
    """applyPatch: 장소 이름만 바뀌고 좌표가 없으면 옛 좌표는 비움 (다른 곳의 좌표를 안 남김)"""
    r = apply_patch(ROW, {"destination": {"name": "도서관"}})
    assert [r["changed"]["place"], r["next"]["dest_lat"], r["next"]["dest_lng"]] == [True, None, None]
    # 같은 이름 · 같은 좌표를 다시 보내면 바뀐 것이 아님
    same = apply_patch(ROW, {"destination": {"name": "세종대학교", "lat": 37.55, "lng": 127.07}})
    assert same["changed"]["place"] is False
    # 이름은 같은데 좌표만 달라도 바뀐 것
    other = apply_patch(ROW, {"destination": {"name": "세종대학교", "lat": 37.56, "lng": 127.07}})
    assert other["changed"]["place"] is True


# ---- 바뀌는 알람 · 확인 ----

BEFORE = [
    {"alarm_type": "WAKE", "scheduled_at": iso("2026-10-29T07:25")},
    {"alarm_type": "PREPARE", "scheduled_at": iso("2026-10-29T07:55")},
    {"alarm_type": "DEPARTURE", "scheduled_at": iso("2026-10-29T08:15")},
    {"alarm_type": "ARRIVE", "scheduled_at": iso("2026-10-29T08:50")},
]


def moved(minutes: int, over: dict[str, bool] | None = None) -> list[dict]:
    over = over or {}
    return [
        {
            "alarm_type": b["alarm_type"],
            "scheduled_at": to_iso(
                datetime.fromtimestamp((ms_of(b["scheduled_at"]) + minutes * 60_000) / 1000, UTC)
            ),
            "is_enabled": over.get(b["alarm_type"], True),
        }
        for b in BEFORE
    ]


def test_alarm_changes_시각이_바뀌는_기상_준비_출발만():
    """alarmChanges: 시각이 바뀌는 기상 · 준비 · 출발만 (도착 목표는 안 셈), 같은 시각이면 빠짐"""
    lines = alarm_changes(BEFORE, moved(60))
    assert [line["kind"] for line in lines] == ["WAKE", "PREPARE", "DEPARTURE"]
    assert lines[0] == {"kind": "WAKE", "from": iso("2026-10-29T07:25"), "to": iso("2026-10-29T08:25")}
    assert alarm_changes(BEFORE, moved(0)) == []
    # 기상만 직접 고른 시각이라 안 움직이는 경우
    keep_wake = [
        {**a, "scheduled_at": BEFORE[0]["scheduled_at"]} if a["alarm_type"] == "WAKE" else a
        for a in moved(30)
    ]
    assert [line["kind"] for line in alarm_changes(BEFORE, keep_wake)] == ["PREPARE", "DEPARTURE"]


def test_alarm_changes_새로_생기거나_사라지는_알람_꺼지는_알람은_줄에_안_넣음():
    """alarmChanges: 새로 생기거나 사라지는 알람(그날 첫 일정이 바뀜) · 꺼지는 알람은 줄에 안 넣음"""
    without_wake = [a for a in moved(60) if a["alarm_type"] != "WAKE"]
    assert [line["kind"] for line in alarm_changes(BEFORE, without_wake)] == ["PREPARE", "DEPARTURE"]
    before_without_wake = [a for a in BEFORE if a["alarm_type"] != "WAKE"]
    assert [line["kind"] for line in alarm_changes(before_without_wake, moved(60))] == [
        "PREPARE",
        "DEPARTURE",
    ]
    off = moved(60, {"WAKE": False, "PREPARE": False})
    assert [line["kind"] for line in alarm_changes(BEFORE, off)] == ["DEPARTURE"]


def test_needs_confirm_5분_이상_늦춰질_때만():
    """needsConfirm: 5분 이상 늦춰질 때만 (앞당김 · 4분 늦춤은 바로 저장) — 앱 CONFIRM_MOVE_MINUTES와 같은 경계"""
    assert CONFIRM_MOVE_MINUTES == 5
    cases = [(-60, False), (-5, False), (-1, False), (0, False), (4, False), (5, True), (6, True), (60, True)]
    for minutes, expected in cases:
        assert needs_confirm(alarm_changes(BEFORE, moved(minutes))) is expected, f"{minutes}분"
    # 한 알람이라도 5분 이상 늦춰지면 확인 (기상은 앞당겨지고 출발은 늦춰짐)
    mixed = []
    for a in moved(0):
        if a["alarm_type"] == "DEPARTURE":
            mixed.append({**a, "scheduled_at": iso("2026-10-29T08:25")})
        elif a["alarm_type"] == "WAKE":
            mixed.append({**a, "scheduled_at": iso("2026-10-29T07:00")})
        else:
            mixed.append(a)
    assert needs_confirm(alarm_changes(BEFORE, mixed)) is True
    assert needs_confirm([]) is False


# ---- 이후 모두 — 지울 뒤 회차 ----


def series(rows: list[tuple]) -> list[dict]:
    return [
        {
            "appointment_id": r[0],
            "occurrence_on": r[1],
            "appointment_at": iso(f"{r[1]}T09:00"),
            "exception_kind": r[2] if len(r) > 2 else None,
        }
        for r in rows
    ]


NOW = kst("2026-10-26T10:00")


def test_later_rows_to_delete_뒤_회차_지금_이후_기록_없는_회차만():
    """laterRowsToDelete: 이 회차보다 뒤 · 지금 이후 · 기록 없는 회차만. 고치기는 예외(이번만 고친 · 지운) 회차를 남기고 지우기는 지움"""
    rows = series(
        [
            ("r1", "2026-10-27"),
            ("r2", "2026-10-29"),
            ("r3", "2026-11-03", "EDITED"),
            ("r4", "2026-11-05", "CANCELED"),
            ("r5", "2026-11-10"),
        ]
    )
    target = rows[1]
    assert later_rows_to_delete(rows=rows, target=target, now=NOW, record_ids=set(), mode="EDIT") == ["r5"]
    assert later_rows_to_delete(rows=rows, target=target, now=NOW, record_ids=set(), mode="DELETE") == [
        "r3",
        "r4",
        "r5",
    ]
    # 기록 있는 회차는 남김
    assert later_rows_to_delete(
        rows=rows, target=target, now=NOW, record_ids={"r3", "r5"}, mode="DELETE"
    ) == ["r4"]
    # 마지막 회차 뒤에는 없음
    assert later_rows_to_delete(rows=rows, target=rows[4], now=NOW, record_ids=set(), mode="DELETE") == []


def test_later_rows_to_delete_지난_회차는_지우지_않음():
    """laterRowsToDelete: 지난 회차(약속 시각이 지금 이전)는 지우지 않음"""
    rows = series([("r1", "2026-10-20"), ("r2", "2026-10-22"), ("r3", "2026-10-29")])
    assert later_rows_to_delete(rows=rows, target=rows[0], now=NOW, record_ids=set(), mode="DELETE") == ["r3"]


# ---- 7.1.3 재판정 ----


def W(id_: str, local: str, enabled: bool = True) -> SameDayWake:
    return SameDayWake(appointmentId=id_, appointmentAt=kst(local), isEnabled=enabled)


def test_wake_action_앞_일정이_지워져_첫_일정이_되면_기상을_더함():
    """wakeAction: 앞 일정이 지워져 이제 첫 일정이 된 일정(전에는 밀려 기상이 없었음)은 기상을 더함"""
    before_wakes = [W("a1", "2026-10-29T10:00")]  # 전: a1이 첫 일정, a2(13시)는 밀려 있었음
    verdict = wake_action(
        id="a2", at=kst("2026-10-29T13:00"), has_wake=False, before_wakes=before_wakes, sim=[]
    )
    assert verdict == {"action": "ADD", "earlierAfter": False}


def test_wake_action_앞에_새_일정이_생겨_첫_일정이_아니면_기상을_뗌():
    """wakeAction: 앞에 새 일정이 생기거나 옮겨져 더 이상 첫 일정이 아니면 기상을 뗌"""
    verdict = wake_action(
        id="a1",
        at=kst("2026-10-29T10:00"),
        has_wake=True,
        before_wakes=[W("a1", "2026-10-29T10:00")],
        sim=[W("a2", "2026-10-29T08:00")],
    )
    assert verdict == {"action": "REMOVE", "earlierAfter": True}


def test_wake_action_이미_울린_기상은_떼지_않음():
    """wakeAction: 이미 울린 기상(wakeRung)은 앞 기상이 생겨도 떼지 않음 (NONE) — 앞 기상 여부(earlierAfter)는 그대로 알려 줌"""
    verdict = wake_action(
        id="a1",
        at=kst("2026-10-29T10:00"),
        has_wake=True,
        wake_rung=True,
        before_wakes=[W("a1", "2026-10-29T10:00")],
        sim=[W("a2", "2026-10-29T08:00")],
    )
    assert verdict == {"action": "NONE", "earlierAfter": True}


def test_wake_action_그대로면_none():
    """wakeAction: 그대로면 NONE — 사용자가 일부러 기상을 안 만든 일정(전에도 밀리지 않았음)은 되살리지 않음, 앞 기상이 꺼져 있으면 앞서지 않은 것"""
    # 전에도 지금도 첫 일정인데 기상이 없음 (기상을 안 고른 일정)
    first = wake_action(id="a1", at=kst("2026-10-29T10:00"), has_wake=False, before_wakes=[], sim=[])
    assert first["action"] == "NONE"
    # 전에도 지금도 밀려 있음
    displaced = wake_action(
        id="a3",
        at=kst("2026-10-29T17:00"),
        has_wake=False,
        before_wakes=[W("a1", "2026-10-29T10:00")],
        sim=[W("a2", "2026-10-29T13:00")],
    )
    assert displaced["action"] == "NONE"
    # 앞 일정의 기상이 꺼져 있으면 앞선 기상으로 안 침 (has_earlier_wake 규칙)
    off = wake_action(
        id="a2",
        at=kst("2026-10-29T13:00"),
        has_wake=True,
        before_wakes=[W("a1", "2026-10-29T10:00", False)],
        sim=[W("a1", "2026-10-29T10:00", False)],
    )
    assert off["action"] == "NONE"
    # 같은 시각이면 앞선 것이 아님 → 둘 다 기상을 가짐
    same = wake_action(
        id="a2",
        at=kst("2026-10-29T10:00"),
        has_wake=True,
        before_wakes=[W("a1", "2026-10-29T10:00")],
        sim=[W("a1", "2026-10-29T10:00")],
    )
    assert same["action"] == "NONE"


def test_wakes_of_rows_기상_알람이_없는_일정은_기상을_안_가짐():
    """wakesOfRows: 기상 알람이 없는 일정은 기상을 안 가짐 (꺼진 기상은 isEnabled false로)"""
    rows = [
        DayWakeRow(id="a1", at=kst("2026-10-29T10:00"), wake_enabled=True),
        DayWakeRow(id="a2", at=kst("2026-10-29T13:00"), wake_enabled=None),
        DayWakeRow(id="a3", at=kst("2026-10-29T15:00"), wake_enabled=False),
    ]
    assert [[w["appointmentId"], w["isEnabled"]] for w in wakes_of_rows(rows)] == [
        ["a1", True],
        ["a3", False],
    ]


# ---- 기준 회차 ----


def test_pick_base_row_기준_회차_고르기():
    """pickBaseRow: 예외 아닌 회차 중 가장 늦은 행 → 없으면 이번만 고친 행 중 가장 늦은 것 → 지운 회차는 기준이 아님"""
    rows = series(
        [
            ("r1", "2026-11-03"),
            ("r2", "2026-11-10", "EDITED"),
            ("r3", "2026-11-17", "CANCELED"),
            ("r0", "2026-10-27"),
        ]
    )
    assert pick_base_row(rows)["appointment_id"] == "r1"
    only_exceptions = [r for r in rows if r["appointment_id"] not in ("r1", "r0")]
    assert pick_base_row(only_exceptions)["appointment_id"] == "r2"  # 예외뿐 → 이번만 고친 것
    assert pick_base_row(series([("x", "2026-11-03", "CANCELED")])) is None
    assert pick_base_row([]) is None
    both_edited = series([("e1", "2026-11-03", "EDITED"), ("e2", "2026-11-10", "EDITED")])
    assert pick_base_row(both_edited)["appointment_id"] == "e2"

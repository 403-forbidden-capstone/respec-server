"""calc/recur.test.ts — 반복 규칙 읽기 · 다음 회차 날짜"""

import pytest

from app.shared.calc.recur import (
    ROLL_WINDOW_DAYS,
    RRULE_PATTERN,
    add_days_to_key,
    next_occurrences,
    next_routine_at,
    occurs_on,
    parse_recurrence,
    parse_repeat_days,
    weekday_of_date,
)
from app.shared.js import RangeError, parse_iso, to_iso
from app.shared.tz import local_date_key


def kst(date, hhmm="00:00"):
    """한국 시각. 시험 날짜: 2026-10-27 화 · 10-28 수 · 10-29 목 · 11-02 월 · 11-03 화 · 11-04 수 · 11-09 월 · 11-10 화"""
    return parse_iso(f"{date}T{hhmm}:00+09:00")


def days(rule):
    r = parse_recurrence(rule)
    return list(r["days"]) if r and r["freq"] == "WEEKLY" else r


def dates(items):
    return [o["date"] for o in items]


def utc_day(d):
    """JS getUTCDay — 일 0 … 토 6"""
    return (d.weekday() + 1) % 7


# ---------------------------------------------------------------------------
# 규칙 읽기 — 앱 mobile/src/features/alarm/__tests__/repeat.test.ts의 규칙 표와 같은 해석 (요일 번호: 월 0 … 일 6)
# ---------------------------------------------------------------------------


def test_앱이_쓴_규칙을_그대로_읽음_매일_주간_요일_코드_repeat_test_ts_요일_RRULE_표():
    assert parse_recurrence("FREQ=DAILY") == {"freq": "DAILY"}
    assert days("FREQ=WEEKLY;BYDAY=TH") == [3]
    assert days("FREQ=WEEKLY;BYDAY=MO,WE") == [0, 2]
    assert days("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") == [0, 1, 2, 3, 4]
    assert days("FREQ=WEEKLY;BYDAY=SA,SU") == [5, 6]


def test_요일_조합_127가지_앱_repeatToRrule이_만드는_글자를_읽으면_같은_요일이고_쓸_때_받는_글자_모양_RRULE_PATTERN에도_통과():
    BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
    for mask in range(1, 128):
        picked = [i for i in range(7) if mask & (1 << i)]
        rule = (
            "FREQ=DAILY" if len(picked) == 7 else f"FREQ=WEEKLY;BYDAY={','.join(BYDAY[i] for i in picked)}"
        )  # repeat.ts repeatToRrule
        assert RRULE_PATTERN.match(rule), rule
        assert (parse_recurrence(rule) if len(picked) == 7 else days(rule)) == (
            {"freq": "DAILY"} if len(picked) == 7 else picked
        ), rule


def test_RRULE_앞_접두사_소문자_순서_겹친_요일이_달라도_읽음_DB에서_온_글자_repeat_test_ts():
    assert days("RRULE:FREQ=WEEKLY;BYDAY=WE,MO") == [0, 2]  # 요일은 월요일부터 정렬
    assert days("freq=weekly;byday=fr") == [4]
    assert days("BYDAY=MO;FREQ=WEEKLY") == [0]
    assert days("FREQ=WEEKLY;BYDAY=MO,MO,TU") == [0, 1]
    assert parse_recurrence("rrule:freq=daily") == {"freq": "DAILY"}


def test_읽지_못하는_글자_요일_없는_주간은_반복_없음_두_모양_밖은_거절_월간_간격_횟수_끝_날짜():
    for rule in (None, "", "  "):
        assert parse_recurrence(rule) is None, str(rule)
    for rule in [
        "FREQ=MONTHLY;BYMONTHDAY=3", "FREQ=MONTHLY", "FREQ=YEARLY", "FREQ=HOURLY", "FREQ=WEEKLY", "FREQ=WEEKLY;BYDAY=XX", "FREQ=WEEKLY;BYDAY=", "FREQ=WEEKLY;BYDAY=MO,XX",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH", "FREQ=WEEKLY;BYDAY=TH;COUNT=3", "FREQ=WEEKLY;BYDAY=TH;UNTIL=20261231", "FREQ=DAILY;INTERVAL=2", "FREQ=DAILY;COUNT=5", "FREQ=DAILY;BYDAY=MO",
        "FREQ=DAILY;FREQ=WEEKLY", "FREQ=DAILY;", "DAILY", "BYDAY=MO", "FREQ", "FREQ=DAILY=1",
    ]:  # fmt: skip
        assert parse_recurrence(rule) is None, rule


def test_쓸_때_받는_글자_RRULE_PATTERN은_읽는_쪽이_반드시_읽음_받았는데_회차를_못_만드는_규칙이_없게():
    for rule in [
        "FREQ=DAILY",
        "FREQ=WEEKLY;BYDAY=TH",
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        "FREQ=WEEKLY;BYDAY=SU",
        "FREQ=WEEKLY;BYDAY=SU,SU",
    ]:
        assert RRULE_PATTERN.match(rule), rule
        assert parse_recurrence(rule) is not None, rule
    for rule in [
        "FREQ=MONTHLY",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH",
        "freq=daily",
        "RRULE:FREQ=DAILY",
        "FREQ=WEEKLY",
    ]:
        assert not RRULE_PATTERN.match(rule), rule


def test_RRULE_PATTERN은_끝의_줄바꿈을_받지_않음_JS_달러_기호와_같게():
    assert not RRULE_PATTERN.match("FREQ=DAILY\n")  # 파이썬 $였다면 맞았을 글자


def test_규칙_앞뒤_공백은_JS_trim처럼_지우고_이름_뜻이_같은_키_중복은_거절():
    assert parse_recurrence("  FREQ=DAILY \n") == {"freq": "DAILY"}
    assert parse_recurrence("　FREQ=DAILY﻿") == {"freq": "DAILY"}
    assert parse_recurrence("FREQ=DAILY;freq=DAILY") is None


# ---------------------------------------------------------------------------
# 날짜 글자
# ---------------------------------------------------------------------------


def test_날짜의_요일_월_0_일_6_날짜_더하기_규칙에_드는_날():
    assert weekday_of_date("2026-11-02") == 0
    assert weekday_of_date("2026-11-03") == 1
    assert weekday_of_date("2026-11-08") == 6
    assert add_days_to_key("2026-10-31", 1) == "2026-11-01"
    assert add_days_to_key("2026-12-31", 1) == "2027-01-01"
    assert add_days_to_key("2026-03-01", -1) == "2026-02-28"
    with pytest.raises(RangeError):
        weekday_of_date("2026-1-1")
    tu_th = parse_recurrence("FREQ=WEEKLY;BYDAY=TU,TH")
    assert tu_th
    assert occurs_on(tu_th, "2026-11-03") is True
    assert occurs_on(tu_th, "2026-11-04") is False
    assert occurs_on({"freq": "DAILY"}, "2026-11-04") is True


def test_날짜_더하기는_달_해_윤년_넘김과_큰_값():
    assert add_days_to_key("2028-02-28", 1) == "2028-02-29"  # 윤년
    assert add_days_to_key("2028-02-28", 2) == "2028-03-01"
    assert add_days_to_key("2026-01-01", -1) == "2025-12-31"
    assert add_days_to_key("2026-11-15", 365) == "2027-11-15"
    assert add_days_to_key("2026-11-15", -3650) == "2016-11-17"
    assert add_days_to_key("2026-02-31", 0) == "2026-03-03"  # JS Date.UTC처럼 없는 날짜는 이어서 셈
    assert weekday_of_date("2026-02-31") == weekday_of_date("2026-03-03")


# ---------------------------------------------------------------------------
# 다음 회차
# ---------------------------------------------------------------------------


def test_매일_기준_회차_다음_날부터_14일_안의_날짜_전부_시각은_같은_시_분():
    items = next_occurrences(
        {
            "rule": "FREQ=DAILY",
            "baseAt": kst("2026-10-27", "10:00"),
            "now": kst("2026-10-27", "10:10"),
            "timeZone": "Asia/Seoul",
        }
    )
    assert len(items) == 14
    assert items[0]["date"] == "2026-10-28"
    assert items[13]["date"] == "2026-11-10"  # 11-10 10:00 < 지금 + 14일 = 11-10 10:10
    assert all(to_iso(o["at"]).endswith("T01:00:00.000Z") for o in items)  # 한국 10:00
    assert ROLL_WINDOW_DAYS == 14


def test_주간_요일_반복_요일에_드는_날만_화_목():
    items = next_occurrences(
        {
            "rule": "FREQ=WEEKLY;BYDAY=TU,TH",
            "baseAt": kst("2026-10-27", "10:00"),
            "now": kst("2026-10-27", "10:10"),
            "timeZone": "Asia/Seoul",
        }
    )
    assert dates(items) == ["2026-10-29", "2026-11-03", "2026-11-05", "2026-11-10"]
    assert [to_iso(o["at"]) for o in items] == [
        "2026-10-29T01:00:00.000Z",
        "2026-11-03T01:00:00.000Z",
        "2026-11-05T01:00:00.000Z",
        "2026-11-10T01:00:00.000Z",
    ]


def test_첫_회차는_고른_날짜_그대로_요일이_규칙에_안_맞아도_두고_다음_회차부터_규칙_화요일에_고른_월_수_반복():
    items = next_occurrences(
        {
            "rule": "FREQ=WEEKLY;BYDAY=MO,WE",
            "baseAt": kst("2026-10-27", "10:00"),
            "now": kst("2026-10-27", "10:10"),
            "timeZone": "Asia/Seoul",
        }
    )
    assert dates(items) == ["2026-10-28", "2026-11-02", "2026-11-04", "2026-11-09"]
    assert "2026-10-27" not in dates(items)


def test_요일은_내_시간대_날짜의_요일_새벽_1시_약속은_UTC로는_전날이어도_한국_요일_UTC_요일이_아님():
    base = kst("2026-10-27", "01:00")  # 화요일 한국 01:00 = UTC 월요일 16:00
    assert utc_day(base) == 1
    now = kst("2026-10-27", "00:00")
    tue = next_occurrences(
        {
            "rule": "FREQ=WEEKLY;BYDAY=TU",
            "baseAt": base,
            "now": now,
            "timeZone": "Asia/Seoul",
            "windowDays": 8,
        }
    )
    assert dates(tue) == ["2026-11-03"]
    assert to_iso(tue[0]["at"]) == "2026-11-02T16:00:00.000Z"  # UTC로는 월요일
    assert utc_day(tue[0]["at"]) == 1
    mon = next_occurrences(
        {
            "rule": "FREQ=WEEKLY;BYDAY=MO",
            "baseAt": base,
            "now": now,
            "timeZone": "Asia/Seoul",
            "windowDays": 8,
        }
    )
    assert dates(mon) == ["2026-11-02"]  # 한국 월요일 01:00 = UTC 일요일 16:00
    assert utc_day(mon[0]["at"]) == 0
    assert local_date_key(mon[0]["at"], "Asia/Seoul") == "2026-11-02"


def test_창_지금_회차_시각_지금_14일_둘_다_포함_안_함_창_끝_1분_전은_포함():
    base = kst("2026-10-27", "10:00")
    rule = "FREQ=DAILY"
    # 지금과 같은 시각의 회차는 제외 (10-28 10:00이 지금) · 14일 뒤 같은 시각(11-11 10:00)도 제외
    at_now = next_occurrences(
        {"rule": rule, "baseAt": base, "now": kst("2026-10-28", "10:00"), "timeZone": "Asia/Seoul"}
    )
    assert at_now[0]["date"] == "2026-10-29"
    assert at_now[-1]["date"] == "2026-11-10"
    assert len(at_now) == 13
    # 창 끝 정각: 11-10 10:00 == 지금(10-27 10:00) + 14일 → 제외
    exact_end = next_occurrences(
        {"rule": rule, "baseAt": base, "now": kst("2026-10-27", "10:00"), "timeZone": "Asia/Seoul"}
    )
    assert exact_end[-1]["date"] == "2026-11-09"
    # 창 끝 1분 전: 지금 10:01 → 끝 11-10 10:01 > 11-10 10:00 → 포함
    just_in = next_occurrences(
        {"rule": rule, "baseAt": base, "now": kst("2026-10-27", "10:01"), "timeZone": "Asia/Seoul"}
    )
    assert just_in[-1]["date"] == "2026-11-10"
    # 창 길이를 바꾸면 그만큼
    short = next_occurrences(
        {
            "rule": rule,
            "baseAt": base,
            "now": kst("2026-10-27", "10:10"),
            "timeZone": "Asia/Seoul",
            "windowDays": 3,
        }
    )
    assert dates(short) == ["2026-10-28", "2026-10-29", "2026-10-30"]


def test_서버가_며칠_못_돌았어도_지난_회차는_안_메움_이미_지난_오늘_회차도_안_만듦():
    stale = next_occurrences(
        {
            "rule": "FREQ=DAILY",
            "baseAt": kst("2026-10-20", "10:00"),
            "now": kst("2026-10-27", "10:05"),
            "timeZone": "Asia/Seoul",
        }
    )
    assert stale[0]["date"] == "2026-10-28"  # 10-21 ~ 10-27은 지난 회차, 오늘(10-27) 10:00도 이미 지남
    early_today = next_occurrences(
        {
            "rule": "FREQ=DAILY",
            "baseAt": kst("2026-10-20", "10:00"),
            "now": kst("2026-10-27", "09:00"),
            "timeZone": "Asia/Seoul",
        }
    )
    assert early_today[0]["date"] == "2026-10-27"  # 오늘 10:00은 아직 앞


def test_afterDate_이_날짜_뒤부터_셈_중간_날짜가_비었을_때_묶음_첫_회차_날짜부터_다시_구함():
    base = kst("2026-11-03", "10:00")  # 가장 늦은 회차
    now = kst("2026-10-27", "10:05")
    rule = "FREQ=WEEKLY;BYDAY=TU"
    assert dates(next_occurrences({"rule": rule, "baseAt": base, "now": now, "timeZone": "Asia/Seoul"})) == [
        "2026-11-10"
    ]
    assert dates(
        next_occurrences(
            {"rule": rule, "baseAt": base, "now": now, "timeZone": "Asia/Seoul", "afterDate": "2026-10-20"}
        )
    ) == ["2026-11-03", "2026-11-10"]
    assert dates(
        next_occurrences(
            {
                "rule": "FREQ=DAILY",
                "baseAt": base,
                "now": now,
                "timeZone": "Asia/Seoul",
                "afterDate": "2026-10-28",
            }
        )
    )[:2] == ["2026-10-29", "2026-10-30"]


def test_일광절약시간_시간대도_같은_벽시계_시_분_뉴욕_09시30은_11_1_전후로_UTC_시각이_한_시간_바뀜():
    base = parse_iso("2026-10-27T13:30:00Z")  # 뉴욕 09:30 EDT
    items = next_occurrences(
        {
            "rule": "FREQ=DAILY",
            "baseAt": base,
            "now": parse_iso("2026-10-27T14:00:00Z"),
            "timeZone": "America/New_York",
        }
    )
    at = {o["date"]: to_iso(o["at"]) for o in items}
    assert at["2026-10-28"] == "2026-10-28T13:30:00.000Z"
    assert at["2026-11-01"] == "2026-11-01T14:30:00.000Z"  # 시계가 되돌아간 날 — EST
    assert at["2026-11-02"] == "2026-11-02T14:30:00.000Z"


def test_요일_판정은_사용자_시간대_기준_LA_월요일_18시는_UTC_화요일_02시():
    base = parse_iso("2026-10-27T01:00:00Z")  # LA 10/26(월) 18:00 PDT
    items = next_occurrences(
        {
            "rule": "FREQ=WEEKLY;BYDAY=MO",
            "baseAt": base,
            "now": parse_iso("2026-10-27T02:00:00Z"),
            "timeZone": "America/Los_Angeles",
        }
    )
    assert dates(items) == ["2026-11-02"]
    assert to_iso(items[0]["at"]) == "2026-11-03T02:00:00.000Z"  # 11/2는 PST(-8)


def test_두_모양_밖_규칙_없음은_회차_없음_거절():
    common = {
        "baseAt": kst("2026-10-27", "10:00"),
        "now": kst("2026-10-27", "10:10"),
        "timeZone": "Asia/Seoul",
    }
    for rule in (
        None,
        "",
        "FREQ=MONTHLY;BYMONTHDAY=27",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU",
        "FREQ=DAILY;COUNT=3",
        "FREQ=WEEKLY",
    ):
        assert next_occurrences({"rule": rule, **common}) == [], str(rule)


def test_모르는_시간대_이름이면_서울로_방어():
    items = next_occurrences(
        {
            "rule": "FREQ=DAILY",
            "baseAt": kst("2026-10-27", "10:00"),
            "now": kst("2026-10-27", "10:10"),
            "timeZone": "Mars/Olympus",
            "windowDays": 2,
        }
    )
    assert dates(items) == ["2026-10-28", "2026-10-29"]


def test_인자는_dict_하나_또는_같은_이름의_키워드로_줄_수_있음():
    kwargs = {
        "rule": "FREQ=DAILY",
        "baseAt": kst("2026-10-27", "10:00"),
        "now": kst("2026-10-27", "10:10"),
        "timeZone": "Asia/Seoul",
        "windowDays": 2,
    }
    assert next_occurrences(**kwargs) == next_occurrences(kwargs)
    assert next_routine_at(
        repeatDays="THU", stepOffsetMinutes=420, now=kst("2026-10-29", "06:00"), timeZone="Asia/Seoul"
    ) == kst("2026-10-29", "07:00")


# ---------------------------------------------------------------------------
# 루틴 반복 요일 — 앱 mobile/src/features/routine/__tests__/map.test.ts의 규칙 표 (요일 번호: 월 0 … 일 6)
# ---------------------------------------------------------------------------


def test_루틴_repeat_days_범위_쉼표_일요일을_넘는_범위_공백_소문자_map_test_ts_저장한_글자를_읽음():
    assert parse_repeat_days("MON~FRI") == [0, 1, 2, 3, 4]
    assert parse_repeat_days("MON,WED,FRI") == [0, 2, 4]
    assert parse_repeat_days("sat~mon") == [0, 5, 6]  # 토 일 월
    assert parse_repeat_days("FRI, SAT") == [4, 5]
    assert parse_repeat_days("MON-WED") == [0, 1, 2]


def test_루틴_repeat_days_모르는_글자_빈_값은_건너뜀():
    assert parse_repeat_days(None) == []
    assert parse_repeat_days("") == []
    assert parse_repeat_days("주중,XYZ,WED") == [2]
    assert parse_repeat_days("MON~XYZ") == []


def test_루틴_repeat_days_어떤_요일_조합이든_앱이_저장한_글자_formatRepeatDays를_읽으면_같음():
    CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

    def fmt(picked):
        # 앱 features/routine/map.ts formatRepeatDays와 같음 — 3일 이상 이어지면 'MON~FRI', 나머지는 쉼표
        parts = []
        i = 0
        while i < 7:
            if not picked[i]:
                i += 1
                continue
            end = i
            while end + 1 < 7 and picked[end + 1]:
                end += 1
            if end - i >= 2:
                parts.append(f"{CODES[i]}~{CODES[end]}")
            else:
                parts.extend(CODES[k] for k in range(i, end + 1))
            i = end + 1
        return ",".join(parts)

    for mask in range(128):
        picked = [(mask & (1 << i)) != 0 for i in range(7)]
        assert parse_repeat_days(fmt(picked)) == [i for i, p in enumerate(picked) if p], fmt(picked)


# ---------------------------------------------------------------------------
# 루틴 단계 알람의 다음 시각
# ---------------------------------------------------------------------------


def test_루틴_다음_발생_지금_뒤_첫_요일의_같은_단계_시각_월_수_금_07시30():
    def nxt(now_at):
        r = next_routine_at(
            {"repeatDays": "MON,WED,FRI", "stepOffsetMinutes": 450, "now": now_at, "timeZone": "Asia/Seoul"}
        )
        return to_iso(r) if r else None

    assert nxt(kst("2026-11-04", "07:29")) == to_iso(kst("2026-11-04", "07:30"))  # 오늘 수요일 아직 안 울림
    assert nxt(kst("2026-11-04", "07:30")) == to_iso(kst("2026-11-06", "07:30"))  # 정각이면 지난 것 → 금
    assert nxt(kst("2026-11-04", "07:31")) == to_iso(kst("2026-11-06", "07:30"))
    assert nxt(kst("2026-11-06", "08:00")) == to_iso(kst("2026-11-09", "07:30"))  # 금 → 다음 월
    assert nxt(kst("2026-11-03", "23:59")) == to_iso(kst("2026-11-04", "07:30"))  # 화 밤 → 수


def test_루틴_다음_발생_평일_요일_하나_요일_없음():
    seoul = "Asia/Seoul"
    assert to_iso(
        next_routine_at(
            {
                "repeatDays": "MON~FRI",
                "stepOffsetMinutes": 1290,
                "now": kst("2026-11-06", "22:00"),
                "timeZone": seoul,
            }
        )
    ) == to_iso(kst("2026-11-09", "21:30"))  # 금 21:30은 이미 지남 → 월
    assert to_iso(
        next_routine_at(
            {
                "repeatDays": "THU",
                "stepOffsetMinutes": 420,
                "now": kst("2026-10-29", "07:00"),
                "timeZone": seoul,
            }
        )
    ) == to_iso(kst("2026-11-05", "07:00"))  # 정각 → 일주일 뒤
    assert (
        next_routine_at(
            {"repeatDays": "", "stepOffsetMinutes": 420, "now": kst("2026-10-29", "07:00"), "timeZone": seoul}
        )
        is None
    )
    assert (
        next_routine_at(
            {
                "repeatDays": None,
                "stepOffsetMinutes": 420,
                "now": kst("2026-10-29", "07:00"),
                "timeZone": seoul,
            }
        )
        is None
    )
    assert (
        next_routine_at(
            {
                "repeatDays": "XYZ",
                "stepOffsetMinutes": 420,
                "now": kst("2026-10-29", "07:00"),
                "timeZone": seoul,
            }
        )
        is None
    )


def test_루틴_단계가_자정을_넘으면_시작한_날의_요일이_기준_월요일_밤에_시작해_화요일_00시30에_울리는_단계():
    # 월요일 루틴, 단계 offset 1470 = 24:30
    def nxt(now_at):
        return to_iso(
            next_routine_at(
                {"repeatDays": "MON", "stepOffsetMinutes": 1470, "now": now_at, "timeZone": "Asia/Seoul"}
            )
        )

    assert nxt(kst("2026-11-02", "23:30")) == to_iso(
        kst("2026-11-03", "00:30")
    )  # 월 밤 → 오늘 시작한 루틴의 화 00:30
    assert nxt(kst("2026-11-03", "00:29")) == to_iso(
        kst("2026-11-03", "00:30")
    )  # 이미 화요일이어도 월요일 루틴의 단계가 아직 앞
    assert nxt(kst("2026-11-03", "00:31")) == to_iso(
        kst("2026-11-10", "00:30")
    )  # 지나면 다음 주 월요일 루틴의 화 00:30
    assert nxt(kst("2026-11-05", "12:00")) == to_iso(kst("2026-11-10", "00:30"))
    assert nxt(kst("2026-11-04", "07:00")) == to_iso(kst("2026-11-10", "00:30"))

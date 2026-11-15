"""본 레포 `_shared/push-copy.test.ts`의 서버판 시험 — 종류별 문구 · 채널 · 이동 경로 (친구 논리설계 11장 종류 표)"""

import re
from datetime import UTC, datetime

from app.shared.push_copy import (
    CHANNEL_IDS,
    CLIENT_KINDS,
    GROUP_KINDS,
    PATHS,
    SERVER_ONLY_KINDS,
    compose_copy,
)

ALL = [*CLIENT_KINDS, *SERVER_ONLY_KINDS]
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)  # 한국 11/8 21:00


def test_종류_표():
    """종류 표: 앱이 부를 수 있는 6종 + 서버 배치만 보내는 미확인 셋(본인 WAKE_MISSED · 친구 WAKE_NUDGE · 후속 WAKE_NUDGE_UP), 그룹 종류는 둘"""
    assert sorted(CLIENT_KINDS) == [
        "ALARM_CHANGED",
        "FRIEND_ACCEPTED",
        "FRIEND_REQUEST",
        "GROUP_APPT_CHANGED",
        "GROUP_MEMBER_JOINED",
        "WAKE_PING",
    ]
    assert list(SERVER_ONLY_KINDS) == ["WAKE_MISSED", "WAKE_NUDGE", "WAKE_NUDGE_UP"]
    assert sorted(GROUP_KINDS) == ["GROUP_APPT_CHANGED", "GROUP_MEMBER_JOINED"]


def test_채널():
    """채널: 5갈래(깨우기 / 알람 바뀜 / 요청 / 그룹 / 미확인), 모두 _v1, 요청 둘 · 그룹 둘 · 친구 미확인 둘은 같은 채널, 알람 코어 채널과 안 겹침"""
    assert len({CHANNEL_IDS[k] for k in ALL}) == 5
    assert all(re.fullmatch(r"[a-z_]+_v1", CHANNEL_IDS[k]) for k in ALL)
    assert CHANNEL_IDS["FRIEND_REQUEST"] == CHANNEL_IDS["FRIEND_ACCEPTED"]
    assert CHANNEL_IDS["GROUP_MEMBER_JOINED"] == CHANNEL_IDS["GROUP_APPT_CHANGED"]
    assert (
        CHANNEL_IDS["WAKE_NUDGE"] == CHANNEL_IDS["WAKE_PING"]
    )  # 친구가 보낸 성격이라 깨우기와 같은 채널(친구 채널)
    assert CHANNEL_IDS["WAKE_NUDGE_UP"] == CHANNEL_IDS["WAKE_PING"]
    assert all(CHANNEL_IDS[k] != "detect_v1" for k in ALL)


def test_누르면_갈_화면_경로가_종류마다_있음():
    """누르면 갈 화면 경로가 종류마다 있음 (받는 사람 id와 함께 알림 데이터에 실림)"""
    assert all(re.match(r"/[a-z-]+", PATHS[k]) for k in ALL)
    assert PATHS["WAKE_PING"] == "/wake"
    assert PATHS["WAKE_MISSED"] == "/wake"
    assert PATHS["WAKE_NUDGE"] == "/group"  # 친구 미확인 · 후속은 그 사람의 기상 확인(G03)
    assert PATHS["WAKE_NUDGE_UP"] == "/group"


def test_문구는_종류별_고정():
    """문구는 종류별 고정: 깨우기 · 알람 바뀜은 보낸 사람 이름, 요청 · 수락 · 미확인은 고정 문구"""
    assert compose_copy("WAKE_PING", {"senderName": "수연"}) == {"title": "깨우기", "body": "수연님이 깨워요"}
    assert compose_copy("FRIEND_REQUEST", {"senderName": "수연"}) == {
        "title": "친구 요청",
        "body": "새 친구 요청이 도착했어요",
    }
    assert compose_copy("FRIEND_ACCEPTED", {"senderName": "수연"}) == {
        "title": "친구 요청",
        "body": "친구 요청이 수락됐어요",
    }
    assert compose_copy("WAKE_MISSED", {}) == {"title": "기상 확인", "body": "기상 확인이 안 됐어요"}
    # 이름이 비어 있으면 '친구'
    assert compose_copy("WAKE_PING", {})["body"] == "친구님이 깨워요"


def test_친구_미확인_후속():
    """친구 미확인 · 후속: 안 일어난(일어난) 사람 이름만 문구에, 이름이 없으면 친구"""
    assert compose_copy("WAKE_NUDGE", {"subjectName": "지형", "senderName": "수연"}) == {
        "title": "깨워 볼까요?",
        "body": "지형님이 아직 안 일어났어요",
    }
    assert compose_copy("WAKE_NUDGE_UP", {"subjectName": "지형"}) == {
        "title": "일어났어요",
        "body": "지형님이 일어났어요",
    }
    assert compose_copy("WAKE_NUDGE", {})["body"] == "친구님이 아직 안 일어났어요"
    assert len(compose_copy("WAKE_NUDGE", {"subjectName": "가" * 300})["body"]) <= 200


def _alarm_body(**ctx):
    return compose_copy("ALARM_CHANGED", {"now": NOW, **ctx})["body"]


def test_10_37_알람_제안_문구():
    """10-37: 알람 제안 문구 — "○○님이 내일 기상 알람을 7:30 → 7:00으로 제안했어요" (이름 + 날짜 말 + 알람 종류 + 이전 → 새 시각, 받는 사람 시간대의 H:mm), 원래대로 보낸 건 원래 시각"""
    assert compose_copy("ALARM_CHANGED", {"senderName": "수연"})["title"] == "알람 제안"
    assert (
        _alarm_body(
            senderName="수연",
            alarmType="WAKE",
            previousAt="2026-11-08T22:30:00Z",
            newAt="2026-11-08T22:00:00Z",
            timeZone="Asia/Seoul",
        )
        == "수연님이 내일 기상 알람을 7:30 → 7:00으로 제안했어요"
    )
    assert (
        _alarm_body(
            senderName="수연",
            alarmType="WAKE",
            previousAt="2026-11-08T22:00:00Z",
            newAt="2026-11-08T22:30:00Z",
        )
        == "수연님이 내일 기상 알람을 7:00 → 7:30으로 제안했어요"
    )  # 시간대 기본 Asia/Seoul, 원래대로
    assert (
        _alarm_body(
            senderName="수연",
            alarmType="WAKE",
            previousAt="2026-11-08T23:00:00Z",
            newAt="2026-11-08T22:55:00Z",
        )
        == "수연님이 내일 기상 알람을 8:00 → 7:55로 제안했어요"
    )
    assert (
        _alarm_body(
            senderName="수연",
            alarmType="DEPARTURE",
            previousAt="2026-11-09T05:00:00Z",
            newAt="2026-11-09T05:05:00Z",
            timeZone="America/New_York",
        )
        == "수연님이 내일 출발 알람을 0:00 → 0:05로 제안했어요"
    )


def test_알람_제안_문구_날짜_말_알람_종류():
    """알람 제안 문구: 날짜 말(오늘 · 내일 · 모레 · 그 밖은 M월 D일, 받는 사람 시간대 기준), 알람 종류(기상 · 준비 · 출발 · 도착, 모르면 생략)"""

    def copy(alarm_type, at):
        ctx = {"senderName": "수연", "previousAt": at, "newAt": re.sub(r":00:00Z$", ":05:00Z", at)}
        if alarm_type:
            ctx["alarmType"] = alarm_type
        return _alarm_body(**ctx)

    assert (
        copy("WAKE", "2026-11-08T13:00:00Z") == "수연님이 오늘 기상 알람을 22:00 → 22:05로 제안했어요"
    )  # 한국 11/8 22:00
    assert copy("WAKE", "2026-11-08T22:00:00Z") == "수연님이 내일 기상 알람을 7:00 → 7:05로 제안했어요"
    assert copy("WAKE", "2026-11-09T22:00:00Z") == "수연님이 모레 기상 알람을 7:00 → 7:05로 제안했어요"
    assert copy("WAKE", "2026-11-12T22:00:00Z") == "수연님이 11월 13일 기상 알람을 7:00 → 7:05로 제안했어요"
    assert copy("PREPARE", "2026-11-08T22:00:00Z") == "수연님이 내일 준비 알람을 7:00 → 7:05로 제안했어요"
    assert copy("DEPARTURE", "2026-11-08T22:00:00Z") == "수연님이 내일 출발 알람을 7:00 → 7:05로 제안했어요"
    assert copy("ARRIVE", "2026-11-08T22:00:00Z") == "수연님이 내일 도착 알람을 7:00 → 7:05로 제안했어요"
    assert copy(None, "2026-11-08T22:00:00Z") == "수연님이 내일 알람을 7:00 → 7:05로 제안했어요"
    # 받는 사람 시간대 기준 날짜 (뉴욕은 UTC-5): 한국 11/9 07:00은 뉴욕 11/8 17:00 → 오늘
    assert (
        _alarm_body(
            senderName="수연",
            alarmType="WAKE",
            previousAt="2026-11-08T22:00:00Z",
            newAt="2026-11-08T22:05:00Z",
            timeZone="America/New_York",
        )
        == "수연님이 오늘 기상 알람을 17:00 → 17:05로 제안했어요"
    )


def test_알람_제안_문구_시각_뒤_조사():
    """알람 제안 문구: 시각 뒤 조사 — 마지막 숫자를 읽었을 때 받침이 있으면(영 · 삼 · 육) 으로, 없으면 로"""
    expected = {
        "00": "으로",
        "01": "로",
        "02": "로",
        "03": "으로",
        "04": "로",
        "05": "로",
        "06": "으로",
        "07": "로",
        "08": "로",
        "09": "로",
        "10": "으로",
        "15": "로",
        "20": "으로",
        "55": "로",
    }
    for minute, particle in expected.items():
        body = _alarm_body(
            senderName="수연",
            alarmType="WAKE",
            previousAt="2026-11-08T22:30:00Z",
            newAt=f"2026-11-08T22:{minute}:00Z",
        )
        assert body == f"수연님이 내일 기상 알람을 7:30 → 7:{minute}{particle} 제안했어요", minute


def test_알람_제안_문구_시각을_모르거나_이상하면():
    """알람 제안 문구: 시각을 모르거나 이상하면 시각 없이 이름과 알람만, 이름이 비면 친구, 이름이 길면 잘림"""
    assert _alarm_body(senderName="수연", alarmType="WAKE") == "수연님이 기상 알람을 제안했어요"
    assert (
        _alarm_body(senderName="수연", alarmType="WAKE", newAt="2026-11-08T22:10:00Z")
        == "수연님이 내일 기상 알람을 제안했어요"
    )  # 한쪽만 있으면 시각은 뺌
    assert (
        _alarm_body(senderName="수연", alarmType="WAKE", previousAt="nonsense", newAt="2026-11-08T22:10:00Z")
        == "수연님이 내일 기상 알람을 제안했어요"
    )
    assert _alarm_body(alarmType="WAKE") == "친구님이 기상 알람을 제안했어요"
    assert (
        len(
            _alarm_body(
                senderName="가" * 300,
                alarmType="WAKE",
                previousAt="2026-11-08T22:00:00Z",
                newAt="2026-11-08T22:10:00Z",
            )
        )
        <= 200
    )


def test_그룹_알림은_그룹_약속만():
    """그룹 알림은 그룹 · 약속만 — 사람 이름이 문구에 안 들어감"""
    assert compose_copy("GROUP_MEMBER_JOINED", {"senderName": "수연", "groupName": "캡스톤팀"}) == {
        "title": "캡스톤팀",
        "body": "새 멤버가 들어왔어요",
    }
    assert compose_copy(
        "GROUP_APPT_CHANGED", {"senderName": "수연", "groupName": "캡스톤팀", "appointmentTitle": "팀 회의"}
    ) == {"title": "캡스톤팀", "body": "‘팀 회의’ 약속이 바뀌었어요"}
    assert "수연" not in str(
        compose_copy("GROUP_MEMBER_JOINED", {"senderName": "수연", "groupName": "캡스톤팀"})
    )
    assert compose_copy("GROUP_APPT_CHANGED", {}) == {"title": "그룹", "body": "약속이 바뀌었어요"}


def test_이름이_길면_잘라서_제목_100자_본문_200자를_넘지_않음():
    """이름이 길면 잘라서 제목 100자 · 본문 200자를 넘지 않음 (Expo 한도 안)"""
    long = "가" * 300
    group = compose_copy("GROUP_APPT_CHANGED", {"groupName": long, "appointmentTitle": long})
    assert (
        len(group["title"]) <= 100
        and len(group["body"]) <= 200
        and group["title"].endswith("…")
        and group["body"].endswith("약속이 바뀌었어요")
    )
    wake = compose_copy("WAKE_PING", {"senderName": long})
    assert len(wake["body"]) <= 200


def test_이름_자르기는_40자():
    """이름 자르기는 40자 — 40자까지는 그대로, 41자부터 40자 뒤에 …"""
    at40 = "나" * 40
    at41 = "나" * 41
    assert compose_copy("GROUP_MEMBER_JOINED", {"groupName": at40})["title"] == at40
    assert compose_copy("GROUP_MEMBER_JOINED", {"groupName": at41})["title"] == f"{at40}…"
    assert (
        compose_copy("GROUP_APPT_CHANGED", {"groupName": "팀", "appointmentTitle": at41})["body"]
        == f"‘{at40}…’ 약속이 바뀌었어요"
    )


def test_공동_약속_취소_알림_문구():
    """공동 약속 취소 알림 문구: 약속 제목이 있으면 ‘제목’ 약속이 취소됐어요, 없으면 약속이 취소됐어요 — 바뀜 문구와 구분 (9-20)"""
    assert (
        compose_copy(
            "GROUP_APPT_CHANGED",
            {"groupName": "캡스톤팀", "appointmentTitle": "팀 주간회의", "change": "CANCELED"},
        )["body"]
        == "‘팀 주간회의’ 약속이 취소됐어요"
    )
    assert (
        compose_copy("GROUP_APPT_CHANGED", {"groupName": "캡스톤팀", "change": "CANCELED"})["body"]
        == "약속이 취소됐어요"
    )
    assert (
        compose_copy(
            "GROUP_APPT_CHANGED",
            {"groupName": "캡스톤팀", "appointmentTitle": "팀 주간회의", "change": "CHANGED"},
        )["body"]
        == "‘팀 주간회의’ 약속이 바뀌었어요"
    )

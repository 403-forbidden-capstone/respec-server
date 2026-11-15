"""appointment-edit 요청 읽기 (본 레포 supabase/functions/_shared/edit-request.test.ts의 서버판) — 모양 검증은 requests 시험과 같은 방식"""

import pytest

from app.core.errors import AppError
from app.core.read import Reader
from app.shared.edit_request import read_edit_request
from app.shared.js import to_iso

ID = "11111111-1111-4111-8111-111111111111"


def read(body):
    r = Reader.root(body)
    request = read_edit_request(r)
    r.finish()
    return request


def issues_of(body) -> list[str]:
    try:
        read(body)
    except AppError as e:
        return [i["path"] for i in e.details["issues"]]
    return []


def test_update_앱_appointment_patch에_해당하는_칸을_읽음():
    """UPDATE: 앱 AppointmentPatch에 해당하는 칸을 읽음 (제목 · 시각 · 장소+좌표 · 중요도 · 반복 규칙), scope 기본은 this"""
    r = read(
        {
            "action": "UPDATE",
            "appointment_id": ID,
            "patch": {
                "title": " 발표 ",
                "appointment_at": "2026-10-29T10:00:00+09:00",
                "destination": {"name": "강남역", "lat": 37.4979, "lng": 127.0276},
                "importance": "HIGH",
                "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
            },
        }
    )
    assert r["action"] == "UPDATE"
    assert r["scope"] == "this"
    assert r["confirmed"] is False
    assert r["patch"]["title"] == "발표"
    assert to_iso(r["patch"]["at"]) == "2026-10-29T01:00:00.000Z"
    assert r["patch"]["destination"] == {"name": "강남역", "lat": 37.4979, "lng": 127.0276}
    assert r["patch"]["importance"] == "HIGH"
    assert r["patch"]["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=TU"


def test_update_안_넣은_칸은_그대로_반복_규칙_null은_반복_끔():
    """UPDATE: 안 넣은 칸은 그대로(키 없음), 반복 규칙 null은 반복 끔, 좌표 없는 장소는 이름만, confirmed · following"""
    r = read(
        {
            "action": "UPDATE",
            "appointment_id": ID,
            "scope": "following",
            "confirmed": True,
            "patch": {"recurrence_rule": None, "destination": {"name": "도서관"}},
        }
    )
    assert r["action"] == "UPDATE"
    assert r["patch"] == {"recurrence_rule": None, "destination": {"name": "도서관"}}
    assert r["scope"] == "following"
    assert r["confirmed"] is True
    empty = read({"action": "UPDATE", "appointment_id": ID, "patch": {}})
    assert empty["patch"] == {}


def test_delete_restore_leave_id만_받음():
    """DELETE · RESTORE · LEAVE: id (+ scope)만 받음"""
    assert read({"action": "DELETE", "appointment_id": ID, "scope": "following"}) == {
        "action": "DELETE",
        "appointment_id": ID,
        "scope": "following",
    }
    assert read({"action": "DELETE", "appointment_id": ID}) == {
        "action": "DELETE",
        "appointment_id": ID,
        "scope": "this",
    }
    assert read({"action": "RESTORE", "appointment_id": ID}) == {"action": "RESTORE", "appointment_id": ID}
    assert read({"action": "LEAVE", "appointment_id": ID}) == {"action": "LEAVE", "appointment_id": ID}


def test_필수_값이_없거나_틀리면_어느_칸인지_알려_줌():
    """필수 값이 없거나 틀리면 어느 칸인지 알려 줌: action · appointment_id · patch · scope"""
    assert issues_of({}) == ["action", "appointment_id"]
    assert issues_of({"action": "MOVE", "appointment_id": ID}) == ["action"]
    assert issues_of({"action": "UPDATE", "appointment_id": "nope", "patch": {}}) == ["appointment_id"]
    assert issues_of({"action": "UPDATE", "appointment_id": ID}) == ["patch"]
    assert issues_of({"action": "DELETE", "appointment_id": ID, "scope": "all"}) == ["scope"]
    assert issues_of({"action": "DELETE", "appointment_id": ID, "scope": "THIS"}) == [
        "scope"
    ]  # 앱 EditScope와 같은 소문자만


def test_patch_칸_검사():
    """patch 칸 검사: 빈 제목 · 오프셋 없는 시각 · 모르는 중요도 · 두 모양 밖 반복 규칙 · 이름 없는 장소 · 좌표 한쪽만"""

    def issues(patch):
        return issues_of({"action": "UPDATE", "appointment_id": ID, "patch": patch})

    assert issues({"title": "   "}) == ["patch.title"]
    assert issues({"title": "x" * 101}) == ["patch.title"]
    assert issues({"appointment_at": "2026-10-29T10:00:00"}) == ["patch.appointment_at"]
    assert issues({"importance": "URGENT"}) == ["patch.importance"]
    assert issues({"recurrence_rule": "FREQ=MONTHLY"}) == ["patch.recurrence_rule"]
    assert issues({"recurrence_rule": "FREQ=WEEKLY"}) == ["patch.recurrence_rule"]
    assert issues({"destination": {}}) == ["patch.destination.name"]
    assert issues({"destination": {"name": "강남역", "lat": 37.4}}) == ["patch.destination.lng"]
    assert "patch.destination.lat" in issues(
        {"destination": {"name": "강남역", "lat": 91, "lng": 127}}
    )  # 범위 밖


def test_본문이_객체가_아니면_validation_failed():
    """본문이 객체가 아니면 VALIDATION_FAILED"""
    with pytest.raises(AppError) as info:
        Reader.root([])
    assert info.value.code == "VALIDATION_FAILED"


def test_patch_destination_이름이_비거나_공백이면_거절():
    """patch.destination 이름이 비거나 공백이면 조용히 무시하지 않고 거절 (로컬 리뷰 반영)"""
    body = {"action": "UPDATE", "appointment_id": ID, "patch": {"destination": {"name": "   "}}}
    assert issues_of(body) == ["patch.destination.name"]

"""calc/importance.test.ts — 새 일정 중요도 자동: 기본 LOW, 시험 · 면접처럼 명확한 것만 HIGH. 요청에 중요도가 있으면 그 값(수동 우선)"""

from app.shared.calc.importance import auto_importance


def test_기본_LOW_시험_면접_고사만_HIGH_공부_준비_일정은_LOW():
    assert auto_importance("스터디") == "LOW"
    assert auto_importance("") == "LOW"
    assert auto_importance(None) == "LOW"
    assert auto_importance("자료구조 중간고사") == "HIGH"
    assert auto_importance("카카오 면접") == "HIGH"
    assert auto_importance("토익 시험") == "HIGH"
    assert auto_importance("시험 공부") == "LOW"
    assert auto_importance("면접 준비 스터디") == "LOW"


def test_공백이_여러_개여도_JS_정규식_공백으로_접음():
    # 원본은 replace(/\s+/g, ' ') 뒤에 검사 — 줄바꿈 · 전각 공백 · NBSP도 한 칸 공백으로 보고 "시험 공부"가 됨
    assert auto_importance("시험\n\n공부") == "LOW"
    assert auto_importance("시험　준비") == "LOW"
    assert auto_importance("면접  대비") == "LOW"
    assert auto_importance("시험﻿공부") == "LOW"  # BOM은 JS \s에 들어 있음 (파이썬 \s에는 없음)
    assert auto_importance("시험​공부") == "HIGH"  # 폭 0 공백은 JS \s가 아님 → 대비하는 일정으로 안 봄


def test_요청_중요도를_안_보내면_제목으로_자동_보내면_그_값_사용자_수동_우선():
    # 원본은 readCommitRequest(Reader.root(body)).draft.importance를 확인 — requests.ts(다른 묶음)가 이전된 뒤 돌 것
    from app.core.read import Reader
    from app.shared.requests import read_commit_request

    def body(importance=None):
        appointment = {
            "title": "카카오 면접",
            "appointment_at": "2026-10-20T10:00:00+09:00",
            "destination": {"name": "판교", "lat": 37.4, "lng": 127.1},
        }
        if importance:
            appointment["importance"] = importance
        return {
            "appointment": appointment,
            "participant": {
                "origin": {"name": "집", "lat": 37.5, "lng": 127},
                "transport_type": "TRANSIT",
                "buffer_minutes": 10,
            },
            "kinds": ["WAKE"],
        }

    assert read_commit_request(Reader.root(body())).draft.importance == "HIGH"
    assert read_commit_request(Reader.root(body("LOW"))).draft.importance == "LOW"

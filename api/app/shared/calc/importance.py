"""본 레포 supabase/functions/_shared/calc/importance.ts의 서버판 (기준 b140f70) — 새 일정 중요도 자동 판단

사용자가 고르지 않았을 때만 씀 (직접 고르면 그 값이 이김). 기본은 LOW(약)
  제목에 시험 · 면접처럼 명확한 중요 일정 말이 있으면 HIGH. "시험 공부 · 면접 준비"처럼 대비하는 일정은 그 자체가 시험이 아니라 LOW
  서버(requests.ts) · 앱 새 일정 화면 · AI 일정 카드가 같은 함수를 씀. 알람 세기(D69)는 그대로 — 중요도가 HIGH면 한 단계 올림
"""

import re

from ..js import JS_WS
from .types import Importance

__all__ = ["DEFAULT_IMPORTANCE", "auto_importance"]

DEFAULT_IMPORTANCE: Importance = "LOW"

_CLEAR_HIGH = re.compile("시험|면접|고사|토익|토플")
_PREPARING = re.compile(f"(시험|면접|고사|토익|토플)[{JS_WS}]*(공부|준비|대비|스터디)")
_WHITESPACE_RUN = re.compile(f"[{JS_WS}]+")


def auto_importance(title: str | None) -> Importance:
    t = _WHITESPACE_RUN.sub(" ", title if title is not None else "")
    return "HIGH" if _CLEAR_HIGH.search(t) and not _PREPARING.search(t) else DEFAULT_IMPORTANCE

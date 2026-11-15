"""요청 키(Idempotency-Key) 경우 — alarm-commit의 서버 길만 확인 (Edge는 키를 무시하므로 Edge와 견주지 않음)

경우 하나 = 호출 몇 번(부르는 사람 · 본문 · 헤더) + 기대. 호출마다 응답과 DB 줄 수(약속 · 알람 · 요청 키 줄)를 거두고 check가 기대를 확인함.
실행: python dev/contract/run.py --group keys  (서버 + 격리 스택만 있으면 됨. 가짜 Expo · Edge는 안 씀)"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import fx
from cases_plan import MANUAL40, new_appt
from fx import A, at, from_now

KEY = "contract-key-0001"
H = {"Idempotency-Key": KEY}


@dataclass
class Step:
    user: str
    body: object
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Result:
    status: int
    body: Any
    appointments: int
    alarms: int
    keys: int  # private.request_keys 줄 수 (커밋된 키만)


@dataclass
class KeyCase:
    name: str
    steps: list[Step]
    check: Callable[[list[Result]], list[str]]
    setup: str = ""


def core(body: Any) -> Any:
    """응답에서 호출마다 달라지는 값(request_id)과 다시 돌려줬다는 표시(replayed)를 뺌"""
    if isinstance(body, dict):
        return {k: core(v) for k, v in body.items() if k not in ("request_id", "replayed")}
    if isinstance(body, list):
        return [core(v) for v in body]
    return body


def expect(cond: bool, message: str, out: list[str]) -> None:
    if not cond:
        out.append(message)


def same_and_stable(r: list[Result]) -> list[str]:
    """두 번 보냈는데 응답이 같고 약속 · 알람 줄이 안 늘어났고 키 줄이 하나. 다시 돌려줬다는 표시(replayed)는 DB 함수 결과에만 있고 서버 응답에는 안 나옴(응답 모양은 Edge와 같게)"""
    out: list[str] = []
    expect(r[0].status == 200 and r[1].status == 200, f"상태 {r[0].status}/{r[1].status} (200/200 기대)", out)
    expect(core(r[0].body) == core(r[1].body), "두 응답 내용이 다름", out)
    expect(
        (r[0].appointments, r[0].alarms) == (r[1].appointments, r[1].alarms),
        f"줄 수가 늘었음 {r[0].appointments}/{r[0].alarms} → {r[1].appointments}/{r[1].alarms}",
        out,
    )
    expect(r[1].keys == 1, f"키 줄 {r[1].keys}개 (1개 기대)", out)
    return out


def conflict(r: list[Result]) -> list[str]:
    out: list[str] = []
    expect(r[0].status == 200, f"첫 호출 상태 {r[0].status}", out)
    code = ((r[1].body or {}).get("error") or {}).get("code") if isinstance(r[1].body, dict) else None
    expect(
        r[1].status == 409 and code == "CONFLICT", f"두 번째 {r[1].status}/{code} (409/CONFLICT 기대)", out
    )
    expect((r[0].appointments, r[0].alarms) == (r[1].appointments, r[1].alarms), "409인데 줄 수가 바뀜", out)
    expect(r[1].keys == 1, f"키 줄 {r[1].keys}개 (1개 기대)", out)
    return out


def no_key_twice(r: list[Result], per_call: int = 1) -> list[str]:
    """키가 없거나 무시된 경우: 두 번 다 새로 처리 → 약속이 두 벌, 키 줄 없음"""
    out: list[str] = []
    expect(r[0].status == 200 and r[1].status == 200, f"상태 {r[0].status}/{r[1].status}", out)
    expect(
        r[1].appointments == r[0].appointments + per_call,
        f"약속 줄 {r[0].appointments} → {r[1].appointments} (두 벌 기대)",
        out,
    )
    expect(r[1].keys == 0, f"키 줄 {r[1].keys}개 (0개 기대)", out)
    return out


def key_cases() -> list[KeyCase]:
    t3 = at(3, 10)

    def new(title: str = "키 약속", **extra: object) -> dict:
        return {"appointment": new_appt(t3, title=title), "participant": MANUAL40, **extra}

    standalone = {"type": "STANDALONE", "alarm": {"scheduled_at": at(2, 7)}}
    existing = (
        fx.appointment(1, A, t3)
        + fx.participant(1, 1, A, source="MANUAL", est=40)
        + fx.full_set(110, 1, 1, A, t3, travel=40)
    )

    def reordered(body: dict) -> dict:
        return {k: body[k] for k in reversed(list(body))}

    def rebuilt_ok(r: list[Result]) -> list[str]:
        """실패(422)한 요청의 키는 안 남고, 같은 키로 고쳐 보내면 성공"""
        out: list[str] = []
        expect(r[0].status == 422, f"첫 호출 {r[0].status} (422 기대)", out)
        expect(
            r[0].keys == 0 and r[0].appointments == 0,
            f"실패했는데 키 {r[0].keys} · 약속 {r[0].appointments}줄이 남음",
            out,
        )
        expect(
            r[1].status == 200 and r[1].keys == 1 and r[1].appointments == 1,
            f"고쳐 보낸 호출 {r[1].status} · 키 {r[1].keys} · 약속 {r[1].appointments}",
            out,
        )
        return out

    def users_apart(r: list[Result]) -> list[str]:
        out: list[str] = []
        expect(r[0].status == 200 and r[1].status == 200, f"상태 {r[0].status}/{r[1].status}", out)
        expect(
            r[1].appointments == 2 and r[1].keys == 2,
            f"약속 {r[1].appointments} · 키 {r[1].keys}줄 (사용자마다 따로 2/2 기대)",
            out,
        )
        return out

    def boundary_ok(r: list[Result]) -> list[str]:
        return same_and_stable(r)

    def standalone_stable(r: list[Result]) -> list[str]:
        out = same_and_stable(r)
        expect(r[1].appointments == 0, f"일정 없는 알람인데 약속 줄이 생김 ({r[1].appointments})", out)
        return out

    return [
        KeyCase(
            "keys/같은 키 · 같은 요청 두 번 → 같은 응답 · 행 안 늘어남",
            [Step("A", new(), H), Step("A", new(), H)],
            same_and_stable,
        ),
        KeyCase(
            "keys/같은 키 · 같은 요청인데 JSON 키 순서만 다름 → 같은 요청",
            [Step("A", new(), H), Step("A", reordered(new()), H)],
            same_and_stable,
        ),
        KeyCase(
            "keys/같은 키 · 다른 본문(제목) → 409",
            [Step("A", new("첫 제목"), H), Step("A", new("다른 제목"), H)],
            conflict,
        ),
        KeyCase(
            "keys/같은 키 · 다른 본문(이동시간) → 409",
            [
                Step("A", new(), H),
                Step("A", {**new(), "participant": {"est_travel_minutes": 55, "travel_source": "MANUAL"}}, H),
            ],
            conflict,
        ),
        KeyCase(
            "keys/같은 키 · 일정 없는 알람 두 번 → 같은 응답 · 행 안 늘어남",
            [Step("A", standalone, H), Step("A", standalone, H)],
            standalone_stable,
        ),
        KeyCase(
            "keys/같은 키 · 일정 없는 알람 다른 시각 → 409",
            [
                Step("A", standalone, H),
                Step("A", {"type": "STANDALONE", "alarm": {"scheduled_at": at(2, 8)}}, H),
            ],
            conflict,
        ),
        KeyCase(
            "keys/같은 키 · 기존 약속에 알람 저장 두 번 → 같은 응답 · 행 안 늘어남",
            [
                Step("A", {"appointment_id": fx.uid(1), "participant": MANUAL40}, H),
                Step("A", {"appointment_id": fx.uid(1), "participant": MANUAL40}, H),
            ],
            same_and_stable,
            setup=existing,
        ),
        KeyCase(
            "keys/같은 키 · 기존 약속 다른 이동 → 409",
            [
                Step("A", {"appointment_id": fx.uid(1), "participant": MANUAL40}, H),
                Step(
                    "A",
                    {
                        "appointment_id": fx.uid(1),
                        "participant": {"est_travel_minutes": 70, "travel_source": "MANUAL"},
                    },
                    H,
                ),
            ],
            conflict,
            setup=existing,
        ),
        KeyCase(
            "keys/키 없이 두 번 → 약속 두 벌(기존 동작)", [Step("A", new()), Step("A", new())], no_key_twice
        ),
        KeyCase(
            "keys/키가 너무 짧음(7자) → 키 없는 것으로 · 오류 없음",
            [
                Step("A", new(), {"Idempotency-Key": "abcdefg"}),
                Step("A", new(), {"Idempotency-Key": "abcdefg"}),
            ],
            no_key_twice,
        ),
        KeyCase(
            "keys/키에 쓸 수 없는 글자(공백) → 키 없는 것으로",
            [
                Step("A", new(), {"Idempotency-Key": "bad key value"}),
                Step("A", new(), {"Idempotency-Key": "bad key value"}),
            ],
            no_key_twice,
        ),
        KeyCase(
            "keys/키가 너무 김(101자) → 키 없는 것으로",
            [
                Step("A", new(), {"Idempotency-Key": "k" * 101}),
                Step("A", new(), {"Idempotency-Key": "k" * 101}),
            ],
            no_key_twice,
        ),
        KeyCase(
            "keys/키 경계 8자 → 키로 쓰임",
            [
                Step("A", new(), {"Idempotency-Key": "abcd1234"}),
                Step("A", new(), {"Idempotency-Key": "abcd1234"}),
            ],
            boundary_ok,
        ),
        KeyCase(
            "keys/키 경계 100자 · 허용 글자 전부 → 키로 쓰임",
            [
                Step("A", new(), {"Idempotency-Key": ("aZ09_.:-" * 13)[:100]}),
                Step("A", new(), {"Idempotency-Key": ("aZ09_.:-" * 13)[:100]}),
            ],
            boundary_ok,
        ),
        KeyCase(
            "keys/다른 사용자가 같은 키 → 각자 따로 처리",
            [Step("A", new(), H), Step("B", new(), H)],
            users_apart,
        ),
        KeyCase(
            "keys/실패(출발 지남 422)한 요청의 키는 안 남음 → 같은 키로 고쳐 보내면 성공",
            [
                Step("A", {"appointment": new_appt(from_now(20), title="늦음"), "participant": MANUAL40}, H),
                Step("A", new(), H),
            ],
            rebuilt_ok,
        ),
    ]

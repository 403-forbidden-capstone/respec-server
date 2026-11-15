"""틀이 돌아가는지 한 경우로 확인 — alarm-suggest(새 약속, 직접 입력 이동)"""

import json
from datetime import datetime, timedelta, timezone

import harness as h

KST = timezone(timedelta(hours=9))


def at(days: int, hh: int, mm: int = 0) -> str:
    base = datetime.now(KST).replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=days)
    return base.isoformat()


env = h.setup_env()
case = h.Case(
    name="suggest-new-manual",
    fn="alarm-suggest",
    user="A",
    body={
        "appointment": {
            "appointment_at": at(3, 10),
            "destination": {"name": "세종대", "lat": 37.5509, "lng": 127.0736},
            "importance": "NORMAL",
        },
        "participant": {"est_travel_minutes": 40, "travel_source": "MANUAL"},
    },
)
fixed = h.fixed_ids(set(h.UUID_RE.findall(case.setup + json.dumps(case.body))))
ok, diffs, edge, server = h.compare(case, env, fixed, edge_first=True)
print("same" if ok else "DIFF", edge["status"], server["status"])
for d in diffs:
    print(" ", d)
print(json.dumps(edge["body"], ensure_ascii=False)[:600])

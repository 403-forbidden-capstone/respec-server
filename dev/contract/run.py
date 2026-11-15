"""계약 시험 실행 — python dev/contract/run.py [--only 글자] [--group suggest|commit|roll|edit] [--out 결과.json]
각 경우를 Edge · 서버 순서를 번갈아 가며 돌려 같음 / 다름을 찍고, 다른 경우는 다른 곳(최대 12줄)을 보여 줌.
Edge(`supabase functions serve`, 본 레포 코드) · 서버(uvicorn 28000)가 떠 있어야 함 — docs/local-dev.md"""

from __future__ import annotations

import argparse
import json
import sys
import time

import harness as h


def load(groups: set[str]) -> list[h.Case]:
    cases: list[h.Case] = []
    if groups & {"suggest", "commit"}:
        import cases_plan

        if "suggest" in groups:
            cases += cases_plan.suggest_cases()
        if "commit" in groups:
            cases += cases_plan.commit_cases()
    if groups & {"roll", "edit"}:
        import cases_roll_edit

        if "roll" in groups:
            cases += cases_roll_edit.roll_cases()
        if "edit" in groups:
            cases += cases_roll_edit.edit_cases()
    if groups & {"wake", "editpush", "suggestgen"}:
        import cases_push

        if "wake" in groups:
            cases += cases_push.wake_cases()
        if "editpush" in groups:
            cases += cases_push.edit_push_cases()
        if "suggestgen" in groups:
            cases += cases_push.suggest_gen_cases()
    return cases


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument(
        "--group",
        default="suggest,commit,roll,edit,wake,editpush,suggestgen",
        help="suggest,commit,roll,edit(앞 4개) · wake,editpush,suggestgen(푸시 · 예약 작업, 가짜 Expo 필요)",
    )
    ap.add_argument("--out", default="")
    ap.add_argument("--show", action="store_true", help="같은 경우도 응답 상태를 보여 줌")
    args = ap.parse_args()
    cases = [c for c in load(set(args.group.split(","))) if args.only in c.name]
    env = h.setup_env()
    report: list[dict] = []
    same = 0
    started = time.time()
    for i, case in enumerate(cases):
        fixed = h.fixed_ids(
            set(
                h.UUID_RE.findall(
                    case.setup
                    + json.dumps(case.body, ensure_ascii=False)
                    + json.dumps([t[2] for t in case.then], ensure_ascii=False)
                )
            )
        )
        try:
            ok, diffs, edge, server = h.compare(case, env, fixed, edge_first=(i % 2 == 0))
        except Exception as exc:  # 틀 자체의 오류는 경우를 건너뛰고 기록
            print(f"ERROR  {case.name}: {str(exc)[:300]}")
            report.append({"name": case.name, "result": "error", "detail": str(exc)[:500]})
            continue
        same += ok
        db = edge["db"] or {}
        rows = " ".join(
            f"{k[:4]}{len(db.get(k, []))}"
            for k in ("appointments", "appointment_participants", "alarms")
            if db.get(k)
        )
        pushes = [(edge.get("expo") or {}).get("messages") or []] + [
            (t.get("expo") or {}).get("messages") or [] for t in edge.get("then", [])
        ]
        sent = "+".join(str(len(p)) for p in pushes)
        status = (
            f"{edge['status']}/{server['status']}"
            + (f" 행:{rows}" if rows else "")
            + (f" 푸시:{sent}" if any(pushes) else "")
        )
        if case.fn in h.JOB_FUNCS and isinstance(edge["body"], dict):
            status += f" 응답:{json.dumps(edge['body'], ensure_ascii=False)[:90]}"
            for t in edge.get("then", []):
                if isinstance(t["body"], dict):
                    status += " 이어서:" + json.dumps(
                        {k: v for k, v in t["body"].items() if k != "receipts"}, ensure_ascii=False
                    )
        print(f"{'같음' if ok else '다름'}  {case.name}  [{status}]")
        if not ok:
            for d in diffs:
                print(f"       {d}")
        summary = {
            "body": edge["body"],
            "expo": (edge.get("expo") or {}).get("messages"),
            "then": [
                {"body": t["body"], "expo": (t.get("expo") or {}).get("messages")}
                for t in edge.get("then", [])
            ],
        }
        report.append(
            {
                "name": case.name,
                "result": "same" if ok else "diff",
                "status": status,
                "diffs": diffs,
                "summary": summary,
                "edge": edge if not ok else None,
                "server": server if not ok else None,
            }
        )
    print(
        f"\n합계 {same}/{len(cases)} 같음 · 다름 {sum(1 for r in report if r['result'] == 'diff')} · 틀 오류 {sum(1 for r in report if r['result'] == 'error')} ({time.time() - started:.0f}초)"
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

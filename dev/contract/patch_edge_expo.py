"""격리 스택 안 Edge 함수 **사본**(dev/.stack/supabase/functions)의 Expo 주소를 가짜 서버로 바꿈 (계약 시험 전용).

본 레포 코드는 안 건드림 — `local_stack.py prepare`가 복사한 사본만 고침(prepare를 다시 하면 되돌아가므로 그 뒤엔 다시 실행).
Edge 함수는 도커 안에서 돌아서 호스트의 가짜 서버는 host.docker.internal로 접근함. 여러 번 실행해도 같음.
  python dev/contract/patch_edge_expo.py"""

from __future__ import annotations

from pathlib import Path

STACK_PUSH = (
    Path(__file__).resolve().parent.parent / ".stack" / "supabase" / "functions" / "_shared" / "push.ts"
)
REAL = "https://exp.host"
FAKE = "http://host.docker.internal:28999"


def main() -> int:
    if not STACK_PUSH.is_file():
        print(f"사본이 없음: {STACK_PUSH} — local_stack.py prepare 먼저")
        return 1
    text = STACK_PUSH.read_text(encoding="utf-8")
    if REAL in text:
        STACK_PUSH.write_text(text.replace(REAL, FAKE), encoding="utf-8", newline="\n")
        print("사본 push.ts의 Expo 주소를 가짜 서버로 바꿈")
    elif FAKE in text:
        print("이미 바뀌어 있음")
    else:
        print("Expo 주소를 못 찾음 — 본 레포 push.ts가 바뀌었는지 확인")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

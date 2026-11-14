"""scheduler 시작점 — `python -m app.scheduler` (compose가 이 명령으로 scheduler 컨테이너를 띄움)

지금은 자리표시자(S1b): 작업 없이 떠 있기만 하고 SIGTERM · Ctrl+C에 정상 종료함.
본격 구현은 S3(완결점 1-10): `server_heartbeat.alive_at` 1분 갱신, wake-check · alarm-roll · 제안 작업, `server_routes` 모드 읽기.
그때까지 `/health/deep`의 스케줄러 검사는 꺼 둠(`HEALTH_REQUIRE_SCHEDULER=false`).
이 컨테이너가 바로 죽으면 배포 직후 점검("running · 재시작 0, 20초")이 실패하므로 자리표시자라도 떠 있어야 함.
"""

import logging
import signal
import threading
from types import FrameType

from .core.config import Settings

log = logging.getLogger("respec.scheduler")


def main() -> int:
    settings = Settings()  # 설정이 틀리면 여기서 종료 (오류 글에 입력값은 안 찍힘)
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("scheduler 종료 신호 받음 signal=%s", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info(
        "scheduler 자리표시자 시작 version=%s (실제 작업은 S3에서 구현 — 지금은 아무 작업도 안 돎)",
        settings.app_version,
    )
    # 시간 제한 없는 stop.wait()는 Windows에서 Ctrl+C로 안 깨어남 → 짧은 주기로 돌면서 신호 핸들러가 돌 틈을 줌
    while not stop.wait(1.0):
        pass
    log.info("scheduler 종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

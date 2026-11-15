"""scheduler — `python -m app.scheduler` (compose가 api와 같은 이미지에서 이 명령으로 띄움, 프로세스는 하나라 중복 없음)

하는 일 (서버 전환 설계 7절 · 완결점 1-10):
  ① 1분마다 server_heartbeat.alive_at 기록 — mode와 상관없이 스케줄러 루프가 돌고 있다는 표시 (/health/deep이 15분 넘게 멈추면 실패로 답함)
     성공은 5분에 한 번 `heartbeat ok at=…` 로그 한 줄로 남김 → 24시간 뒤 "간격 6분 이하"를 로그로 확인함 (표에는 마지막 값 하나만 남아서)
  ② 작업(jobs.py)마다 정시에, 그때의 server_routes(kind = 'job') mode를 읽어 그대로 따름
       edge    아무것도 안 함 (pg_cron이 Edge를 부름)
       shadow  세기만 함 (쓰기 · 푸시 없음), 결과를 로그 한 줄로 — Edge와 대조하려고. 정시 기준으로 세므로 정시 전에만 돎
       server  실제로 함. 지금 시각 기준으로 처리하고, 성공하면 server_heartbeat.last_success_at 기록 (pg_cron이 이것을 보고 Edge 호출을 건너뜀)
     서버 구현이 아직 없는 작업이 server로 정해지면 shadow로 낮추고 오류 로그를 남김 — 성공 기록은 안 함 → pg_cron이 계속 Edge를 부름
     (기상 확인이 조용히 멈추는 길을 막음). 표를 못 읽거나 모르는 값이면 edge로 봄 (= 서버는 아무것도 안 함)
되돌리기: server_routes의 mode를 edge로 한 줄 바꾸면 다음 회차부터 서버는 안 함. 서버가 멈춰도 pg_cron이 Edge로 이어받음.
SUPABASE_SECRET_KEY가 없으면 시작하지 않고 종료 코드 1 — 아무것도 못 하면서 떠 있는 것보다 배포 직후 점검에서 바로 드러나는 편이 안전함.
  (키가 틀려 401이 계속되는 경우는 안 죽고 1분마다 경고만 남김 — /health/deep의 db 검사와 감시가 잡음)
로그에는 작업 이름 · 정시 · 수 · 오류 종류(상태 번호)만 (사용자 · 알람 번호, 주소, 키, 응답 본문은 안 남김).
"""

import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import FrameType

from .core.config import Settings
from .db import Db, DbError
from .jobs import JOBS, Job

log = logging.getLogger("respec.scheduler")

HEARTBEAT_SECONDS = 60
HEARTBEAT_LOG_SECONDS = 300  # 하트비트 성공 로그는 5분에 한 줄 (줄 사이 간격 ≤ 6분이면 "간격 6분 이하")
# 앞당김이 없는 작업은 정시를 이만큼 넘겨 깨어나도 한 번은 돎(오래 막혔다 깨어난 경우 그 회차는 놓친 것으로 봄).
# server 작업은 "지금 밀린 것을 찾아 처리" 방식이라 놓쳐도 다음 회차가 주움
LATE_LIMIT_SECONDS = 60
MODES = ("edge", "shadow", "server")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _why(exc: Exception) -> str:
    """로그에 남길 이유 — DbError는 종류(상태 번호)만 담고 있어 그대로, 그 밖의 예외는 종류 이름만 (내용에 입력값이 섞일 수 있음)"""
    return str(exc) if isinstance(exc, DbError) else type(exc).__name__


def _latest_start(job: Job, boundary: datetime) -> datetime:
    """이 시각이 되면 그 회차는 놓친 것. 앞당김이 있는 작업(shadow가 Edge보다 먼저 세려는 것)은 정시가 마감 —
    pg_cron이 정시에 Edge를 부르면 Edge가 MISSED를 기록해 버려 그 뒤에 세면 대조가 안 됨"""
    return boundary if job.lead_seconds else boundary + timedelta(seconds=LATE_LIMIT_SECONDS)


class Scheduler:
    def __init__(
        self,
        db: Db,
        jobs: tuple[Job, ...] = JOBS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._db = db
        self._jobs = jobs
        self._clock = clock
        self._due: dict[str, datetime] = {}  # 작업별 다음 정시
        self._last_tick: datetime | None = None
        self._last_heartbeat: datetime | None = None
        self._last_heartbeat_log: datetime | None = None

    def tick(self, now: datetime) -> None:
        """1초마다 부름 — 하트비트가 때 됐으면 쓰고, 정시(앞당김 포함)가 된 작업을 돌림. 작업 하나의 실패가 루프를 멈추지 않음"""
        if self._last_tick is not None and now < self._last_tick:
            # 시계가 뒤로 감(시간 맞춤): 벽시계 뺄셈이 음수가 돼 하트비트가 그만큼 멈추고 정시도 그만큼 밀리므로 지금 기준으로 다시 잡음
            log.warning("시계가 뒤로 감 — 하트비트 · 정시를 지금 기준으로 다시 잡음")
            self._due.clear()
            self._last_heartbeat = None
        self._last_tick = now
        self._heartbeat(now)
        for job in self._jobs:
            boundary = self._due.setdefault(job.name, job.next_boundary(now))
            if now < boundary - timedelta(seconds=job.lead_seconds):
                continue
            nxt = job.next_boundary(
                boundary
            )  # 먼저 넘겨 둠 — 아래에서 예외가 나도 같은 회차를 되풀이하지 않게
            if now >= _latest_start(job, boundary):
                missed = 1
                # 한참 막혔다 깨어나도 한 번에 따라잡음 (1초에 한 회차씩 밟지 않게)
                while now >= _latest_start(job, nxt):
                    missed += 1
                    nxt = job.next_boundary(nxt)
                self._due[job.name] = nxt
                log.warning("회차 놓침 job=%s boundary=%s count=%d", job.name, _stamp(boundary), missed)
                continue
            self._due[job.name] = nxt
            self._run(job, boundary, now)

    def _heartbeat(self, now: datetime) -> None:
        if (
            self._last_heartbeat is not None
            and (now - self._last_heartbeat).total_seconds() < HEARTBEAT_SECONDS
        ):
            return
        # 실패해도 1분 뒤에 다시 (1초마다 DB를 두드리지 않게). alive_at이 멈추면 /health/deep이 알림
        self._last_heartbeat = now
        try:
            self._db.heartbeat([job.name for job in self._jobs], now)
        except Exception as exc:  # noqa: BLE001 — DbError가 아닌 예외도 같은 틱의 작업을 막으면 안 됨
            log.warning("heartbeat 쓰기 실패: %s", _why(exc))
            return
        if (
            self._last_heartbeat_log is None
            or (now - self._last_heartbeat_log).total_seconds() >= HEARTBEAT_LOG_SECONDS
        ):
            self._last_heartbeat_log = now
            log.info("heartbeat ok at=%s", _stamp(now))

    def _mode(self, job: Job) -> str:
        """표를 못 읽거나 모르는 값이면 edge — 서버가 아무것도 안 하는 쪽이 언제나 안전함"""
        try:
            modes = self._db.read_job_modes()
        except Exception as exc:  # noqa: BLE001 — 어떤 예외든 이 회차는 edge (예외가 새면 회차가 사라지기만 함)
            log.warning("스위치 표를 못 읽어 edge로 봄 job=%s: %s", job.name, _why(exc))
            return "edge"
        mode = modes.get(job.name, "edge")
        if mode not in MODES:
            log.warning("모르는 mode라 edge로 봄 job=%s", job.name)
            return "edge"
        return mode

    def _run(self, job: Job, boundary: datetime, now: datetime) -> None:
        mode = self._mode(job)
        if mode == "edge":
            return
        if mode == "server" and job.server is None:
            log.error(
                "서버 구현 없는 작업이 server로 설정됨 → shadow로 낮춤 (성공 기록 안 함, pg_cron이 Edge를 계속 부름) job=%s",
                job.name,
            )
            mode = "shadow"
        handler = job.shadow if mode == "shadow" else job.server
        if handler is None:
            log.info("shadow job=%s boundary=%s 세는 구현 없음 — 안 함", job.name, _stamp(boundary))
            return
        # shadow는 정시를 기준으로 셈(Edge가 정시에 볼 후보와 맞추려고). server는 지금 시각 기준 —
        # 앞당긴 채 정시를 넘기면 아직 안 온 후보까지 처리하게 됨
        at = boundary if mode == "shadow" else now
        try:
            summary = handler(self._db, at)
        except DbError as exc:
            log.warning("%s 실패 job=%s boundary=%s: %s", mode, job.name, _stamp(boundary), exc)
            return
        except Exception as exc:  # noqa: BLE001 — 작업의 버그가 스케줄러(하트비트 포함)를 죽이면 안 됨. 내용은 안 찍고 종류만
            log.error("%s 예외 job=%s boundary=%s: %s", mode, job.name, _stamp(boundary), type(exc).__name__)
            return
        log.info("%s job=%s boundary=%s %s", mode, job.name, _stamp(boundary), summary or "")
        if mode == "server":
            try:
                self._db.record_success(job.name, self._clock())
            except Exception as exc:  # noqa: BLE001 — 기록이 없으면 pg_cron이 Edge를 부름 (겹쳐도 안전)
                log.warning("last_success_at 기록 실패 job=%s: %s", job.name, _why(exc))

    def now(self) -> datetime:
        return self._clock()


def main() -> int:
    settings = Settings()  # 설정이 틀리면 여기서 종료 (오류 글에 입력값은 안 찍힘)
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if settings.supabase_secret_key is None:
        log.error(
            "SUPABASE_SECRET_KEY가 없어 scheduler를 시작하지 않음 (스위치 표 · heartbeat를 읽고 쓸 수 없음)"
        )
        return 1

    stop = threading.Event()

    # 신호 핸들러는 깃발만 올림 (로그 · DB 호출을 하다 신호가 겹치는 것을 피함) — 로그는 아래에서
    def _stop(signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    db = Db(settings)
    scheduler = Scheduler(db)
    log.info("scheduler 시작 version=%s jobs=%s", settings.app_version, ",".join(job.name for job in JOBS))
    try:
        # 시간 제한 없는 wait()는 Windows에서 Ctrl+C로 안 깨어남 → 1초 간격으로 돌면서 신호 핸들러가 돌 틈을 줌
        while not stop.is_set():
            try:
                scheduler.tick(scheduler.now())
            except Exception as exc:  # noqa: BLE001 — 루프는 계속 돎 (tick 안의 예상 못 한 버그). 종류만 남김
                log.error("scheduler tick 예외: %s", type(exc).__name__)
            stop.wait(1.0)
    finally:
        db.close()
    log.info("scheduler 종료 신호 받음")
    log.info("scheduler 종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

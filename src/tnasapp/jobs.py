"""持久化任务队列。

所有耗时操作都必须进队列（设计文档 §5「统一任务系统」），这样才能做到：
进度可见、可取消、可重试、日志可查、进程重启后不丢状态。

状态机（设计文档 §5 指定）::

    queued ──► running ──► completed
       │          │  │
       │          │  └──► failed
       │          ├──► paused ──► running
       │          └──► canceling ──► canceled
       └──► canceled

任务处理器通过 :class:`JobContext` 与队列交互：上报进度、写日志、在循环里调用
:meth:`JobContext.checkpoint` 以获得「可取消 / 可暂停」能力。
"""

import json
import threading
import time

from . import logx

STATES = (
    "queued",
    "running",
    "paused",
    "canceling",
    "canceled",
    "completed",
    "failed",
)

#: 任务中断/失败后最多自动重试几次
DEFAULT_MAX_ATTEMPTS = 3

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at       REAL    NOT NULL,
        started_at       REAL,
        finished_at      REAL,
        type             TEXT    NOT NULL,
        title            TEXT    NOT NULL DEFAULT '',
        params           TEXT    NOT NULL DEFAULT '{}',
        state            TEXT    NOT NULL DEFAULT 'queued',
        progress         REAL    NOT NULL DEFAULT 0,
        message          TEXT    NOT NULL DEFAULT '',
        result           TEXT,
        error            TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        pause_requested  INTEGER NOT NULL DEFAULT 0,
        attempts         INTEGER NOT NULL DEFAULT 0,
        max_attempts     INTEGER NOT NULL DEFAULT 3,
        parent_id        INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(type)",
    """
    CREATE TABLE IF NOT EXISTS job_logs (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id  INTEGER NOT NULL,
        ts      REAL    NOT NULL,
        level   TEXT    NOT NULL,
        message TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_logs_job ON job_logs(job_id, id)",
]


class JobCanceled(Exception):
    """任务被用户取消。处理器应让它冒泡出去，队列会把状态置为 ``canceled``。"""


class JobContext:
    """交给任务处理器的句柄。**处理器只应通过它触碰队列。**"""

    def __init__(self, manager, job):
        self._manager = manager
        self.job_id = int(job["id"])
        self.type = job["type"]
        self.params = _loads(job.get("params"), {})
        self.attempts = int(job.get("attempts") or 0)
        self._seq = 0

    # ---- 进度 ----

    def progress(self, done, total=None, message=None):
        """上报进度。``total`` 给定时 ``done/total`` 换算成百分比。"""
        if total and total > 0:
            pct = max(0.0, min(100.0, float(done) * 100.0 / float(total)))
            if message is None:
                message = "%d / %d" % (done, total)
        else:
            pct = max(0.0, min(100.0, float(done)))
        self._manager._update(self.job_id, progress=pct, message=message)
        self._seq += 1
        # 每 5 次进度上报刷一次心跳，避免过于频繁地写库
        if self._seq % 5 == 0:
            self.checkpoint()

    def message(self, text):
        self._manager._update(self.job_id, message=str(text))

    # ---- 日志 ----

    def log(self, message, level="INFO"):
        self._manager._log(self.job_id, level, message)

    # ---- 协作式控制 ----

    def checkpoint(self):
        """在长循环里周期调用。

        * 收到取消请求 → 抛 :class:`JobCanceled`
        * 收到暂停请求 → **阻塞**直到恢复或取消（这是「暂停」的实现方式）
        """
        while True:
            row = self._manager._row(self.job_id)
            if row is None:
                raise JobCanceled("任务已被删除")
            if row["cancel_requested"]:
                raise JobCanceled("任务已取消")
            if row["pause_requested"]:
                if row["state"] != "paused":
                    self._manager._update(self.job_id, state="paused")
                self._manager._sleep(0.4)
                continue
            if row["state"] == "paused":
                self._manager._update(self.job_id, state="running")
            return

    def canceled(self):
        row = self._manager._row(self.job_id)
        return bool(row and row["cancel_requested"])

    # ---- 结果 ----

    def set_result(self, payload):
        self._manager._update(self.job_id, result=json.dumps(payload, ensure_ascii=False))


class JobManager:
    """任务队列。

    :param store: :class:`tnasapp.store.Store` 实例
    :param workers: 工作线程数
    """

    def __init__(self, store, workers=2, logger=None):
        self.store = store
        self.workers = max(1, int(workers))
        self.log = logger or logx.get("jobs")
        self._handlers = {}
        self._threads = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._running = set()
        self._lock = threading.RLock()
        self._accepting = True

    # ---- 生命周期 ----

    def start(self):
        self.store.migrate(logger=self.log)
        self._requeue_interrupted()
        self._stop.clear()
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._worker_loop, name="job-worker-%d" % index, daemon=True
            )
            thread.start()
            self._threads.append(thread)
        self.log.info("任务队列已启动，工作线程 %d 个", self.workers)
        return self

    def stop(self, timeout=8.0):
        """停止队列。正在跑的任务会被标回 ``queued``，下次启动自动续跑。"""
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []
        self._requeue_for_shutdown()
        self.log.info("任务队列已停止")

    def _requeue_interrupted(self):
        """上次进程被杀导致卡在 running/canceling/paused 的任务，重新排队。"""
        rows = self.store.query(
            "SELECT id, attempts, max_attempts, cancel_requested FROM jobs"
            " WHERE state IN ('running','canceling','paused')"
        )
        for row in rows:
            if row["cancel_requested"]:
                self._update(
                    row["id"], state="canceled", message="已取消", finished_at=time.time()
                )
                continue
            if int(row["attempts"] or 0) >= int(row["max_attempts"] or DEFAULT_MAX_ATTEMPTS):
                self._update(
                    row["id"],
                    state="failed",
                    error="上次运行被中断且已达最大重试次数",
                    finished_at=time.time(),
                )
            else:
                self._update(
                    row["id"],
                    state="queued",
                    progress=0,
                    message="上次运行被中断，已重新排队",
                    pause_requested=0,
                )
                self._log(row["id"], "WARN", "上次运行被中断，已重新排队")

    def _requeue_for_shutdown(self):
        with self._lock:
            running = list(self._running)
        for job_id in running:
            row = self._row(job_id)
            if row is None:
                continue
            if row["cancel_requested"]:
                self._update(job_id, state="canceled", finished_at=time.time())
                continue
            if int(row["attempts"] or 0) >= int(row["max_attempts"] or DEFAULT_MAX_ATTEMPTS):
                self._update(
                    job_id,
                    state="failed",
                    error="服务停止，任务被中断",
                    finished_at=time.time(),
                )
            else:
                self._update(
                    job_id,
                    state="queued",
                    message="服务停止，任务已重新排队",
                    pause_requested=0,
                )

    # ---- 处理器注册 ----

    def register(self, job_type, handler=None):
        """注册处理器。

        可当装饰器用::

            @jobs.register("scan")
            def run_scan(ctx): ...

        也可以传统调用：``jobs.register("scan", run_scan)``。
        """
        if handler is None:

            def decorator(func):
                self.register(job_type, func)
                return func

            return decorator
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        self._handlers[job_type] = handler
        return handler

    def handler(self, job_type):
        """``register`` 的别名，语义更贴近装饰器用法。"""
        return self.register(job_type)

    # ---- 提交与查询 ----

    def submit(self, job_type, params=None, title="", max_attempts=DEFAULT_MAX_ATTEMPTS,
               parent_id=None):
        if job_type not in self._handlers:
            raise ValueError("未注册的任务类型：%s" % job_type)
        if not self._accepting:
            raise RuntimeError("服务正在关闭，暂不接受新任务")
        cursor = self.store.insert(
            "INSERT INTO jobs (created_at, type, title, params, state, max_attempts, parent_id)"
            " VALUES (?,?,?,?,'queued',?,?)",
            (
                time.time(),
                job_type,
                str(title or ""),
                json.dumps(params or {}, ensure_ascii=False, default=str),
                int(max_attempts),
                parent_id,
            ),
        )
        self._wake.set()
        return self.get(cursor)

    def get(self, job_id):
        row = self._row(job_id)
        return _serialize(row) if row else None

    def list(self, state=None, job_type=None, limit=50, offset=0, active_only=False):
        clauses, params = [], []
        if state:
            states = [state] if isinstance(state, str) else list(state)
            clauses.append("state IN (%s)" % ",".join("?" * len(states)))
            params.extend(states)
        if job_type:
            clauses.append("type = ?")
            params.append(job_type)
        if active_only:
            clauses.append("state IN ('queued','running','paused','canceling')")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([int(limit), int(offset)])
        rows = self.store.query(
            "SELECT * FROM jobs%s ORDER BY id DESC LIMIT ? OFFSET ?" % where, tuple(params)
        )
        return [_serialize(row) for row in rows]

    def counts(self):
        result = {state: 0 for state in STATES}
        for row in self.store.query("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"):
            result[row["state"]] = int(row["n"])
        result["total"] = sum(result.values())
        return result

    def logs(self, job_id, offset=0, limit=500):
        rows = self.store.query(
            "SELECT id, ts, level, message FROM job_logs WHERE job_id=? AND id>?"
            " ORDER BY id ASC LIMIT ?",
            (int(job_id), int(offset), int(limit)),
        )
        for row in rows:
            row["time"] = _fmt_time(row["ts"])
        return rows

    # ---- 控制 ----

    def cancel(self, job_id):
        row = self._row(job_id)
        if row is None:
            return False
        if row["state"] in ("completed", "failed", "canceled"):
            return False
        if row["state"] == "queued":
            self._update(job_id, state="canceled", finished_at=time.time(), message="已取消")
        else:
            self._update(job_id, cancel_requested=1, state="canceling", message="正在取消…")
        self._wake.set()
        return True

    def pause(self, job_id):
        row = self._row(job_id)
        if row is None or row["state"] not in ("running", "queued"):
            return False
        self._update(job_id, pause_requested=1, message="已请求暂停")
        return True

    def resume(self, job_id):
        row = self._row(job_id)
        if row is None or row["state"] != "paused":
            return False
        self._update(job_id, pause_requested=0, state="running", message="已恢复")
        self._wake.set()
        return True

    def retry(self, job_id):
        """基于原任务重投一个新任务（原任务保持不变，便于追溯）。"""
        row = self._row(job_id)
        if row is None:
            return None
        return self.submit(
            row["type"],
            _loads(row["params"], {}),
            title=row["title"],
            parent_id=row["id"],
        )

    def delete(self, job_id):
        row = self._row(job_id)
        if row is None:
            return False
        if row["state"] in ("running", "canceling", "paused"):
            raise RuntimeError("任务正在运行，请先取消再删除")
        self.store.execute("DELETE FROM job_logs WHERE job_id=?", (int(job_id),))
        self.store.execute("DELETE FROM jobs WHERE id=?", (int(job_id),))
        return True

    def purge_finished(self, keep=200):
        """只保留最近 ``keep`` 条已完成/失败/取消的任务，其余删掉（有界增长）。"""
        rows = self.store.query(
            "SELECT id FROM jobs WHERE state IN ('completed','failed','canceled')"
            " ORDER BY id DESC LIMIT -1 OFFSET ?",
            (int(keep),),
        )
        for row in rows:
            self.store.execute("DELETE FROM job_logs WHERE job_id=?", (row["id"],))
            self.store.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
        return len(rows)

    # ---- 内部 ----

    def _row(self, job_id):
        try:
            return self.store.query_one("SELECT * FROM jobs WHERE id=?", (int(job_id),))
        except (TypeError, ValueError):
            return None

    def _update(self, job_id, **fields):
        if not fields:
            return
        columns, values = [], []
        for key, value in fields.items():
            if value is None and key not in ("started_at", "finished_at", "error", "result"):
                continue
            columns.append("%s=?" % key)
            values.append(value)
        if not columns:
            return
        values.append(int(job_id))
        self.store.execute("UPDATE jobs SET %s WHERE id=?" % ",".join(columns), tuple(values))

    def _log(self, job_id, level, message):
        self.store.execute(
            "INSERT INTO job_logs (job_id, ts, level, message) VALUES (?,?,?,?)",
            (int(job_id), time.time(), str(level).upper(), logx.redact(str(message))),
        )

    def _sleep(self, seconds):
        self._stop.wait(seconds)

    def _wake_wait(self, timeout=0.5):
        self._wake.wait(timeout)
        self._wake.clear()

    def _claim(self):
        """原子领取一个排队中的任务。用 BEGIN IMMEDIATE 避免并发重复领取。"""
        conn = self.store.connect()
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE jobs SET state='running', started_at=?, attempts=attempts+1,"
                " progress=0, error=NULL WHERE id=?",
                (time.time(), row["id"]),
            )
            conn.execute("COMMIT")
            return dict(row)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                claimed = self._claim()
            except Exception as exc:  # 数据库瞬时故障不能杀死工作线程
                self.log.error("领取任务失败：%s", exc)
                self._wake_wait(1.0)
                continue
            if claimed is None:
                self._wake_wait(0.5)
                continue
            self._run(claimed)

    def _run(self, row):
        job_id = int(row["id"])
        with self._lock:
            self._running.add(job_id)
        job = self._row(job_id) or row
        ctx = JobContext(self, job)
        self._log(job_id, "INFO", "任务开始：%s" % (row["title"] or row["type"]))
        try:
            handler = self._handlers[row["type"]]
            handler(ctx)
            self._update(
                job_id,
                state="completed",
                progress=100,
                message="已完成",
                finished_at=time.time(),
            )
            self._log(job_id, "INFO", "任务完成")
        except JobCanceled:
            self._update(
                job_id, state="canceled", message="已取消", finished_at=time.time()
            )
            self._log(job_id, "WARN", "任务被取消")
        except Exception as exc:
            import traceback

            detail = "%s: %s" % (type(exc).__name__, exc)
            self._log(job_id, "ERROR", "任务失败：%s" % detail)
            self._log(job_id, "DEBUG", traceback.format_exc())
            self.log.error("任务 %s 失败：%s", job_id, detail)
            self._update(
                job_id,
                state="failed",
                error=detail,
                message="失败",
                finished_at=time.time(),
            )
        finally:
            with self._lock:
                self._running.discard(job_id)


def _loads(text, default):
    try:
        value = json.loads(text) if text else default
        return value if value is not None else default
    except (ValueError, TypeError):
        return default


def _fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return ""


def _serialize(row):
    data = dict(row)
    data["params"] = _loads(data.get("params"), {})
    raw_result = data.get("result")
    data["result"] = _loads(raw_result, None) if raw_result else None
    for key in ("created_at", "started_at", "finished_at"):
        if data.get(key) is not None:
            data[key + "_text"] = _fmt_time(data[key])
    data["cancel_requested"] = bool(data.get("cancel_requested"))
    data["pause_requested"] = bool(data.get("pause_requested"))
    data["progress"] = round(float(data.get("progress") or 0), 2)
    data["is_active"] = data.get("state") in ("queued", "running", "paused", "canceling")
    return data

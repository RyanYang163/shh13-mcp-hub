"""持久化：SQLite 封装 + 运行时配置（JSON）。

**迁移策略**（指引 50：禁止升级时删库）：用 ``PRAGMA user_version`` 记版本，
每个迁移是一段向前推进的 SQL，按序补跑。旧库永远不会被删掉重建。

**并发**：每次操作开一条新连接（``sqlite3`` 的连接不能跨线程共享），
开 WAL 让读不阻塞写，``busy_timeout`` 兜住瞬时锁竞争。
"""

import json
import os
import sqlite3
import threading
import time


class Store:
    """带迁移的 SQLite 封装。

    :param db_path: 数据库文件路径
    :param migrations: 迁移列表，索引 ``i`` 对应「升到版本 ``i+1``」的 SQL 或可调用对象
    """

    def __init__(self, db_path, migrations=None):
        self.db_path = db_path
        self.migrations = list(migrations or [])
        self._lock = threading.RLock()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    # ---- 连接 ----

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def execute(self, sql, params=(), commit=True):
        with self._lock:
            conn = self.connect()
            try:
                cur = conn.execute(sql, params)
                if commit:
                    conn.commit()
                return cur.fetchall() if cur.description else []
            finally:
                conn.close()

    def insert(self, sql, params=()):
        """执行 INSERT 并返回新行的 rowid（必须是同一条连接）。"""
        with self._lock:
            conn = self.connect()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def executemany(self, sql, seq, commit=True):
        with self._lock:
            conn = self.connect()
            try:
                conn.executemany(sql, seq)
                if commit:
                    conn.commit()
            finally:
                conn.close()

    def query(self, sql, params=()):
        return [dict(row) for row in self.execute(sql, params, commit=False)]

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql, params=(), default=None):
        with self._lock:
            conn = self.connect()
            try:
                cur = conn.execute(sql, params)
                row = cur.fetchone()
                return row[0] if row is not None else default
            finally:
                conn.close()

    # ---- 迁移 ----

    def user_version(self):
        return int(self.scalar("PRAGMA user_version", default=0) or 0)

    def migrate(self, logger=None):
        """把库升到最新版本。返回 (旧版本, 新版本)。"""
        with self._lock:
            current = self.user_version()
            target = len(self.migrations)
            if current > target:
                # 库比程序新（用户降级了应用）——不破坏数据，只提示
                if logger:
                    logger.warning(
                        "数据库版本(%d)高于本程序支持的版本(%d)，跳过迁移以免损坏数据",
                        current,
                        target,
                    )
                return current, current
            for index in range(current, target):
                migration = self.migrations[index]
                version = index + 1
                if logger:
                    logger.info("应用数据库迁移 %d -> %d", index, version)
                conn = self.connect()
                try:
                    if callable(migration):
                        migration(conn)
                    else:
                        conn.executescript(migration)
                    conn.execute("PRAGMA user_version=%d" % version)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            return current, target


class Settings:
    """运行时配置，落在 ``<data>/config/runtime.json``。

    **不含任何凭据明文**——使用远程引擎时用户填的 API Key 单独存
    ``secrets.json``（权限 600），并从日志与接口响应里剔除。
    """

    def __init__(self, path, defaults=None):
        self.path = path
        self.defaults = dict(defaults or {})
        self._lock = threading.RLock()
        self._data = dict(self.defaults)
        self.load()

    def load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self._data = dict(self.defaults)
                    self._data.update(loaded)
            except (OSError, ValueError):
                self._data = dict(self.defaults)
        return self._data

    def save(self):
        with self._lock:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)  # 原子替换，避免读到半个文件

    def all(self):
        with self._lock:
            return dict(self._data)

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, self.defaults.get(key, default))

    def set(self, key, value):
        with self._lock:
            if self._data.get(key) == value:
                return False
            self._data[key] = value
        self.save()
        return True

    def update(self, mapping):
        changed = False
        with self._lock:
            for key, value in dict(mapping).items():
                if self._data.get(key) != value:
                    self._data[key] = value
                    changed = True
        if changed:
            self.save()
        return changed


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, payload, mode=None):
    """原子写 JSON；``mode`` 给定时同时设权限（凭据文件用 0o600）。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)
    return path


def now_ts():
    """当前时间戳（秒，浮点）。"""
    return time.time()


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

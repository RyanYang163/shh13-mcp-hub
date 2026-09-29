"""目录推导与路径安全校验。

两个关键点：

1. **不硬编码 ``/usr/local/<appid>``**。Deb 包内的逻辑路径是 ``/usr/local/<appid>/``，
   但 App Center 实际把第三方应用装在 ``/Volume*/@apps/<appid>/``
   （指引 4.7 / 12.9.2：平台映射是实现细节，代码必须跟随平台提供的安装上下文）。
   所以安装目录从 ``sys.argv[0]`` / ``__file__`` 反推，环境变量可覆盖。

2. **白名单路径校验**。所有涉及用户文件的 API 都必须走 :meth:`AllowedRoots.check`：
   先 ``realpath`` 再比对白名单根。因为 ``realpath`` 会把 symlink 展开，指向白名单之外的
   软链会解析到外面从而被拒——这同时实现了指引 38.2「默认不跟随指向允许目录外的 symlink」。
"""

import os
import sys

#: 找不到任何可写目录时的最后兜底（正常情况下不会走到）
_FALLBACK_STATE_DIR = "/var/lib"


def _env(*names):
    """返回第一个非空的环境变量值。"""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _guess_install_dir(app_id):
    """反推应用安装目录。

    优先级：环境变量 > ``sys.argv[0]`` 所在目录的父目录（若父目录名为 ``bin``）
    > ``__file__`` > ``/usr/local/<app_id>``。

    运行期 ``bin/<appid>`` 是个 zipapp，``sys.argv[0]`` 即该文件路径，
    其父目录名为 ``bin``，再上一层就是安装根。
    """
    override = _env("APP_INSTALL_DIR")
    if override:
        return os.path.realpath(override)

    candidates = []
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        candidates.append(argv0)
    if __file__:
        candidates.append(__file__)

    for cand in candidates:
        try:
            path = os.path.realpath(cand)
        except OSError:
            continue
        base = os.path.dirname(path)
        if os.path.basename(base) == "bin":
            return os.path.dirname(base)
        if os.path.isdir(base):
            return base

    return "/usr/local/" + app_id


def _first_writable(candidates):
    """返回第一个「能创建且能写入」的候选目录，都不行则返回 None。"""
    for cand in candidates:
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".write-probe")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
            return os.path.realpath(cand)
        except OSError:
            continue
    return None


class AppPaths:
    """一个应用的全部落点。所有运行期写入都在 :attr:`data_dir` 之内。"""

    def __init__(self, app_id, install_dir=None, data_dir=None, webui_dir=None):
        self.app_id = app_id
        self.install_dir = os.path.realpath(install_dir or _guess_install_dir(app_id))

        # 运行期数据目录。优先 install_dir/data（对应 /Volume*/@apps/<appid>/data），
        # 不可写时退到 /var/lib/<appid>（systemd StateDirectory= 提供），再退到用户家目录。
        candidates = [
            data_dir or _env("APP_DATA_DIR"),
            os.path.join(self.install_dir, "data"),
            os.path.join(_FALLBACK_STATE_DIR, app_id),
            os.path.join(os.path.expanduser("~"), ".local", "share", app_id),
        ]
        resolved = _first_writable(candidates)
        if resolved is None:
            raise RuntimeError(
                "无法找到可写的数据目录。请检查 %s/data 的属主与权限，"
                "或通过 APP_DATA_DIR 指定。" % self.install_dir
            )
        self.data_dir = resolved

        self.webui_dir = os.path.realpath(
            webui_dir or _env("APP_WEBUI_DIR") or os.path.join(self.install_dir, "webui")
        )

        # data_dir 下的子目录，一次建齐（清单见 README 的「运行时写入路径清单」）
        self.config_dir = os.path.join(self.data_dir, "config")
        self.db_dir = os.path.join(self.data_dir, "db")
        self.cache_dir = os.path.join(self.data_dir, "cache")
        self.tmp_dir = os.path.join(self.data_dir, "tmp")
        self.log_dir = os.path.join(self.data_dir, "logs")
        self.output_dir = os.path.join(self.data_dir, "output")
        for path in (
            self.config_dir,
            self.db_dir,
            self.cache_dir,
            self.tmp_dir,
            self.log_dir,
            self.output_dir,
        ):
            os.makedirs(path, exist_ok=True)

        self.runtime_config_path = os.path.join(self.config_dir, "runtime.json")
        self.db_path = os.path.join(self.db_dir, "app.db")
        self.log_path = os.path.join(self.log_dir, "app.log")

    # ---- tmp ----

    def clean_tmp(self):
        """清理上次崩溃残留的临时文件（指引 12.9.6：启动时必须清理）。"""
        removed = 0
        try:
            entries = os.listdir(self.tmp_dir)
        except OSError:
            return 0
        for name in entries:
            full = os.path.join(self.tmp_dir, name)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    _rmtree(full)
                else:
                    os.remove(full)
                removed += 1
            except OSError:
                continue
        return removed

    def tmp_file(self, name):
        """在 ``data/tmp`` 下开一个临时文件（**不写共享 ``/tmp``**，见指引 12.9.6）。"""
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
        return os.path.join(self.tmp_dir, safe)

    def describe(self):
        return {
            "app_id": self.app_id,
            "install_dir": self.install_dir,
            "data_dir": self.data_dir,
            "webui_dir": self.webui_dir,
            "log_path": self.log_path,
            "db_path": self.db_path,
        }


def _rmtree(path):
    """不依赖 shutil 的递归删除（只用于我们自己的 tmp 目录）。"""
    import shutil

    shutil.rmtree(path, ignore_errors=True)


class PathDenied(Exception):
    """路径不在白名单内（疑似目录穿越或 symlink 逃逸）。

    ``i18n_key`` / ``i18n_args`` 让消息在**抛出的那一刻**就保留可翻译的形状 ——
    一旦被 ``%`` 格式化过，就再也对不上服务端词表，英文界面下永远翻不出来。
    ``str(exc)`` 仍返回渲染好的中文，老代码不受影响。
    """

    def __init__(self, key, args=None):
        super().__init__(key % args if args else key)
        self.i18n_key = key
        self.i18n_args = args


class AllowedRoots:
    """允许访问的目录白名单。

    默认**空**——即什么都不允许。用户必须在应用里显式添加目录，
    应用再把它持久化。这与 MCP 官方 filesystem server 的 allowlist 机制一致
    （设计文档 §11.3：不能把整个 ``/`` 暴露给 AI）。
    """

    def __init__(self, roots=None):
        self._roots = []
        for root in roots or []:
            self.add(root)

    def add(self, path):
        """把一个目录加入白名单，返回规范化的真实路径。"""
        if not path:
            raise ValueError("路径不能为空")
        if "\x00" in path:
            raise PathDenied("路径含非法字符")
        real = os.path.realpath(os.path.expanduser(str(path)))
        if real not in self._roots:
            self._roots.append(real)
        return real

    def remove(self, path):
        real = os.path.realpath(os.path.expanduser(str(path)))
        if real in self._roots:
            self._roots.remove(real)
            return True
        return False

    def roots(self):
        return list(self._roots)

    def is_allowed(self, path):
        try:
            self.check(path)
            return True
        except PathDenied:
            return False

    def check(self, path, must_exist=True):
        """校验并返回规范化后的真实路径。

        步骤（指引 38.1）：``resolve`` → ``normalize`` → ``check_allowed_root``。
        任何一步不过就抛 :class:`PathDenied`。
        """
        if path is None:
            raise PathDenied("路径不能为空")
        path = str(path)
        if "\x00" in path:
            raise PathDenied("路径含非法字符")
        if not path.strip():
            raise PathDenied("路径不能为空")
        # 明确的穿越尝试先拦一道（realpath 之后其实也能拦住，但报错更清楚）
        if ".." in path.split(os.sep):
            # 允许 .. 出现在「相对于白名单根内部」的情形，用 realpath 判定
            pass
        if must_exist and not os.path.exists(path):
            raise PathDenied("路径不存在：%s", (path,))

        real = os.path.realpath(os.path.expanduser(path))
        if not self._roots:
            raise PathDenied("尚未配置可访问目录（白名单为空）")
        for root in self._roots:
            if real == root or real.startswith(root + os.sep):
                return real
        raise PathDenied("路径不在允许访问的目录内：%s", (path,))

    def check_write(self, path, must_exist=False):
        """写操作用；语义与 :meth:`check` 相同，便于日后加更严的规则。"""
        return self.check(path, must_exist=must_exist)


def is_subpath(child, parent):
    """``child`` 是否在 ``parent`` 之下（均在 realpath 之后比较）。"""
    child = os.path.realpath(child)
    parent = os.path.realpath(parent)
    return child == parent or child.startswith(parent + os.sep)


def human_size(num):
    """把字节数格式化成人类可读字符串。"""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(num) < 1024.0 or unit == "PB":
            if unit == "B":
                return "%d B" % int(num)
            return "%.1f %s" % (num, unit)
        num /= 1024.0
    return "%.1f PB" % num

"""命令行入口。

Deb 包内 systemd 用 ``--socket`` 启动（监听 ``/var/api/<appid>.sock``）；
本机开发用 ``--tcp 127.0.0.1:18811``（Windows 上根本没有 Unix socket，
所以开发模式默认走 TCP）。两者跑的是同一份业务代码，便于在没有 TOS 设备时
把后端真正跑起来做功能验证。
"""

import argparse
import os
import signal
import sys
import threading

from . import paths as paths_mod


def build_parser(app_id, description=""):
    parser = argparse.ArgumentParser(
        prog=app_id,
        description=description or ("%s — TOS 7 应用后端" % app_id),
    )
    parser.add_argument(
        "--socket",
        metavar="PATH",
        help="监听 Unix socket（默认 /var/api/%s.sock）" % app_id,
    )
    parser.add_argument(
        "--tcp",
        metavar="[HOST:]PORT",
        help="监听 TCP（本机开发用；Windows 不支持 Unix socket 时必须用这个）",
    )
    parser.add_argument("--data-dir", metavar="DIR", help="覆盖运行期数据目录")
    parser.add_argument(
        "--install-dir", metavar="DIR", help="覆盖安装目录（默认从 argv[0] 反推）"
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        help="日志级别（默认 INFO；DEBUG 仅用于排障）",
    )
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    parser.add_argument(
        "--print-paths", action="store_true", help="打印各目录落点后退出（排障用）"
    )
    return parser


def parse_tcp(value):
    """解析 ``[HOST:]PORT``。"""
    if not value:
        return None
    if ":" in value:
        host, _, port = value.rpartition(":")
        return host or "127.0.0.1", int(port)
    return "127.0.0.1", int(value)


def main(app_id, version, factory, description="", argv=None, default_workers=None):
    """通用入口。

    :param factory: ``factory(paths=..., log_level=...) -> App``
    """
    parser = build_parser(app_id, description)
    args = parser.parse_args(argv)

    if args.version:
        print("%s %s" % (app_id, version))
        return 0

    if args.data_dir:
        os.environ["APP_DATA_DIR"] = args.data_dir
    if args.install_dir:
        os.environ["APP_INSTALL_DIR"] = args.install_dir

    app_paths = paths_mod.AppPaths(app_id)
    if args.print_paths:
        import json

        print(json.dumps(app_paths.describe(), ensure_ascii=False, indent=2))
        return 0

    log_level = args.log_level or os.environ.get("LOG_LEVEL") or "INFO"
    app = factory(paths=app_paths, log_level=log_level)
    if default_workers:
        app.jobs.workers = default_workers

    socket_path = args.socket
    tcp = parse_tcp(args.tcp)
    if not socket_path and not tcp:
        # Windows 上没有 Unix socket，开发时自动退到 TCP
        if os.name == "nt":
            tcp = ("127.0.0.1", 18080)
        else:
            socket_path = "/var/api/%s.sock" % app_id

    stopping = threading.Event()

    def _on_signal(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        app.log.info("收到信号 %s，正在优雅退出…", signum)
        threading.Thread(target=app.shutdown, daemon=True).start()

    for sig in ("SIGTERM", "SIGINT"):
        handler = getattr(signal, sig, None)
        if handler is not None:
            try:
                signal.signal(handler, _on_signal)
            except (ValueError, OSError):
                pass

    try:
        if socket_path:
            app.run(socket_path=socket_path)
        else:
            app.run(host=tcp[0], port=tcp[1])
    except Exception as exc:
        app.log.error("服务异常退出：%s", exc)
        return 1
    finally:
        try:
            app.jobs.stop()
        except Exception:
            pass
    return 0


def run(app_id, version, factory, **kwargs):
    """``main`` 的便捷包装：直接 ``sys.exit`` 退出码。"""
    sys.exit(main(app_id, version, factory, **kwargs))

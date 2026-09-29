"""HTTP 服务：Unix socket / TCP 双模、路由、静态资源、CORS、平台鉴权头解析。

为什么后端要自己 serve 前端：iframe 应用的平台侧只做**转发**——平台 Nginx 把
``/<appid>/`` 与 ``/v2/proxy/<appid>/`` 转到 ``/var/api/<appid>.sock``，静态页面与
API 都从这条路径进后端（指引 8.7.1 要求后端监听 socket；FAQ 19.3 的
``curl --unix-socket /var/api/<appid>.sock http://localhost/`` 就是在验证首页）。

**路径前缀兼容**：同一个后端要能同时应答 ``/``、``/<appid>/…``、``/v2/proxy/<appid>/…``
三种前缀（指引 8.9 明确要求后端兼容多种代理形态，避免换代理模式就 404）。
前端因此只需使用**相对路径**，不必探测前缀。

**鉴权**（指引 8.10）：前端会带 ``X-Csrf-Token`` 与自定义 ``Cookie`` 头；后端解析并校验。
默认 ``auth_mode="lenient"``：不一致只记 WARN 不拦——socket 本身是 0660、
只有平台代理（root）能连，若因平台未透传自定义 ``Cookie`` 头就 401，会把应用彻底打死
（宁可安全边界落在 socket 权限上，也不能让功能不可用）。设为 ``strict`` 则硬拦并返回 401。
"""

import json
import mimetypes
import os
import posixpath
import re
import socket
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import i18n, logx, paths as paths_mod

#: 这些扩展名不参与「SPA 回退到 index.html」，避免把 API 404 变成 HTML
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}

MAX_BODY = 64 * 1024 * 1024  # 单请求体上限，防止内存被打爆


def _localize(response, headers):
    """在出口处把错误消息换成请求语言。

    为什么要放在出口而不是 ``Response.error`` 里：那里还不知道这次请求要什么语言。
    语言由前端解析好（用户显式选择 → navigator.language → zh-cn）后放进
    ``Accept-Language`` 头，这里读它即可 —— 后端不必再单独读一遍设置。

    ``zh-cn`` 时**原样返回**（payload 里已经是中文原文，翻译等于白做）。
    """
    info = getattr(response, "i18n", None)
    if not info:
        return response
    lang = i18n.negotiate(headers.get("Accept-Language", "") if headers else "")
    if lang == i18n.DEFAULT:
        return response
    message, hint, args, hint_args, code, extra = info
    payload = {"ok": False, "error": i18n.tr(message, lang, args)}
    if hint:
        payload["hint"] = i18n.tr(hint, lang, hint_args)
    if code:
        payload["code"] = code
    if extra:
        payload.update(extra)
    return Response.json(payload, status=response.status)


class ErrorMessage:
    """给 ``Response.error`` 用的结构化错误：保留 key + 参数 + 状态码。

    为什么要个类：消息一旦被 ``%`` 格式化，就再也对不上词表 key 了
    （英文界面下永远翻不出来）。凡是需要插值的提示，都先包成它再往上抛。
    """
    __slots__ = ("i18n_key", "i18n_args", "status")

    def __init__(self, key, args=None, status=400):
        self.i18n_key = key
        self.i18n_args = args
        self.status = status

    def __str__(self):
        return i18n.render(self.i18n_key, self.i18n_args)


class Request:
    """一次请求的只读视图。"""

    def __init__(self, method, path, query, headers, body, remote="", app_id=""):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.remote = remote
        self.app_id = app_id
        self.auth = _parse_auth(headers)
        self._json = _UNSET

    # ---- 查询参数 ----

    def arg(self, name, default=None):
        values = self.query.get(name)
        return values[0] if values else default

    def arg_list(self, name):
        return list(self.query.get(name, []))

    def int_arg(self, name, default=0):
        try:
            return int(self.arg(name, default))
        except (TypeError, ValueError):
            return default

    def float_arg(self, name, default=0.0):
        try:
            return float(self.arg(name, default))
        except (TypeError, ValueError):
            return default

    def bool_arg(self, name, default=False):
        value = self.arg(name)
        if value is None:
            return default
        return str(value).lower() in ("1", "true", "yes", "on")

    def json_body(self):
        if self._json is _UNSET:
            try:
                self._json = json.loads(self.body.decode("utf-8")) if self.body else None
            except (ValueError, UnicodeDecodeError):
                self._json = None
        return self._json

    def param(self, name, default=None):
        """先看 body（JSON 对象），再看 query。"""
        body = self.json_body()
        if isinstance(body, dict) and name in body:
            return body[name]
        return self.arg(name, default)


class Response:
    """一次响应。"""

    def __init__(self, status=200, body=b"", content_type="application/octet-stream",
                 headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.headers = {"Content-Type": content_type}
        if headers:
            self.headers.update(headers)
        # 错误响应在出口处按请求语言翻译用；普通响应保持 None（见 _localize）
        self.i18n = None

    @classmethod
    def json(cls, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        return cls(status, body, "application/json; charset=utf-8")

    @classmethod
    def text(cls, text, status=200):
        return cls(status, str(text).encode("utf-8"), "text/plain; charset=utf-8")

    @classmethod
    def html(cls, html, status=200):
        return cls(status, str(html).encode("utf-8"), "text/html; charset=utf-8")

    @classmethod
    def error(cls, message, status=400, hint=None, code=None, args=None, hint_args=None,
              extra=None):
        """统一错误体。

        设计文档 §52：**不要**给用户看 ``Error 500``，要给可读原因与下一步动作。

        **多语言**：``message`` / ``hint`` 传**中文原文**（即 tnasapp.i18n 词表里的 key），
        插值参数走 ``args`` / ``hint_args``，**不要自己先做 % 格式化** ——
        先格式化就把 key 丢了，英文界面下永远翻不出来。真正翻译在 ``_send`` 出口做，
        那里才知道这次请求要什么语言。

        ``code`` 是给前端做分支判断用的**结构化错误码**：前端不要拿 message 里的
        中文措辞做正则匹配，那样界面一换语言就静默失效。
        """
        if getattr(message, "i18n_key", None) is not None:
            # 自带 key + 参数的对象（ErrorMessage / paths.PathDenied）透传出去
            key = message.i18n_key
            if args is None:
                args = getattr(message, "i18n_args", None)
            message = key
        elif isinstance(message, BaseException):
            message = str(message)
        payload = {"ok": False, "error": i18n.render(message, args)}
        if hint:
            payload["hint"] = i18n.render(hint, hint_args)
        if code:
            payload["code"] = code
        if extra:
            payload.update(extra)          # 例如目录选择器要一并带回 roots
        response = cls.json(payload, status=status)
        response.i18n = (message, hint, args, hint_args, code, extra)
        return response

    @classmethod
    def file(cls, path, download_name=None):
        with open(path, "rb") as fh:
            body = fh.read()
        ext = os.path.splitext(path)[1].lower()
        ctype = STATIC_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        headers = {}
        if download_name:
            quoted = urllib.parse.quote(download_name)
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''%s" % quoted
        return cls(200, body, ctype, headers)


_UNSET = object()


def _parse_auth(headers):
    """按指引 8.10 解析平台鉴权头。"""
    cookie_str = headers.get("Cookie") or ""
    csrf = headers.get("X-Csrf-Token") or ""
    parts = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            parts[key.strip()] = value.strip()
    cookie_csrf = parts.get("X-Csrf-Token", "")
    return {
        "cookie_present": bool(cookie_str),
        "csrf_header": csrf,
        "session": parts.get("TMSESSNAME", ""),
        "consistent": bool(csrf) and csrf == cookie_csrf,
    }


class _Route:
    def __init__(self, method, pattern, handler):
        self.method = method.upper()
        self.pattern = pattern
        self.handler = handler
        self.names = []
        # /api/jobs/<job_id>/logs  ->  ^/api/jobs/(?P<job_id>[^/]+)/logs$
        regex = re.sub(r"<([a-zA-Z_][a-zA-Z0-9_]*)>", r"(?P<\1>[^/]+)", pattern)
        self.regex = re.compile("^" + regex + "$")
        self.names = re.findall(r"<([a-zA-Z_][a-zA-Z0-9_]*)>", pattern)

    def match(self, path):
        found = self.regex.match(path)
        if not found:
            return None
        params = {}
        for name in self.names:
            params[name] = urllib.parse.unquote(found.group(name))
        return params


class Router:
    def __init__(self):
        self.routes = []

    def add(self, method, pattern, handler):
        self.routes.append(_Route(method, pattern, handler))
        return handler

    def match(self, method, path):
        allowed = False
        for route in self.routes:
            params = route.match(path)
            if params is None:
                continue
            if route.method == method or (route.method == "GET" and method == "HEAD"):
                return route.handler, params
            allowed = True
        if allowed:
            return None, {"__method_not_allowed__": True}
        return None, None


class App:
    """一个 TOS 应用的运行时容器：路由 + 存储 + 任务队列 + 白名单 + 配置。

    业务代码的典型用法::

        app = App("shh11-media-audio", "媒体音频提取器")

        @app.get("/api/status")
        def status(req):
            return {"ok": True, "version": app.version}

        app.jobs.register("extract", handle_extract)
        app.run()
    """

    def __init__(self, app_id, title, version="1.0.0", workers=2, log_level="INFO",
                 extra_migrations=None, paths=None, description="", engines=None):
        from .jobs import SCHEMA, JobManager
        from .store import Settings, Store

        self.app_id = app_id
        self.title = title
        self.version = version
        self.description = description
        self.paths = paths or paths_mod.AppPaths(app_id)
        self.engines = engines if engines is not None else {}
        self.started_at = time.time()

        self.paths.clean_tmp()
        self.log = logx.setup("app", log_level, self.paths.log_path)
        self.store = Store(self.paths.db_path, list(SCHEMA) + list(extra_migrations or []))
        self.settings = Settings(self.paths.runtime_config_path, self.default_settings())
        self.allowed = paths_mod.AllowedRoots()
        self._last_accept_language = ""
        self.jobs = JobManager(self.store, workers=workers, logger=logx.get("jobs"),
                               lang_provider=self._ui_lang)

        self.routes = Router()
        self.auth_mode = str(self.settings.get("auth_mode", "lenient"))
        self._server = None
        self._bind = None

        self._register_core_routes()

    def _ui_lang(self):
        """任务线程用的界面语言。

        任务线程没有请求上下文，取不到 Accept-Language，所以这里：
          1) 优先界面里显式选的语言（前端 UI.langSelect 会写进 settings.ui_language）
          2) 否则用**最后一次请求**带的 Accept-Language（Handler._send 里记的）
          3) 都没有 → zh-cn
        """
        chosen = str(self.settings.get("ui_language") or "")
        if chosen:
            return chosen
        return self._last_accept_language or ""

    # ---- 应用可覆写 ----

    def default_settings(self):
        """应用自定义的默认配置项。"""
        return {
            "auth_mode": "lenient",
            # 界面语言：空 = 跟随浏览器（前端会把解析结果放进 Accept-Language）
            "ui_language": "",
            "debug": False,
        }

    # ---- 路由装饰器 ----

    def route(self, method, pattern):
        def decorator(func):
            self.routes.add(method, pattern, func)
            return func

        return decorator

    def get(self, pattern):
        return self.route("GET", pattern)

    def post(self, pattern):
        return self.route("POST", pattern)

    def put(self, pattern):
        return self.route("PUT", pattern)

    def delete(self, pattern):
        return self.route("DELETE", pattern)

    # ---- 核心路由 ----

    def _register_core_routes(self):
        @self.get("/health")
        def _health(req):
            return Response.json(
                {
                    "status": "ok",
                    "app": self.app_id,
                    "version": self.version,
                    "uptime": round(time.time() - self.started_at, 1),
                }
            )

        @self.get("/api/app")
        def _app_info(req):
            return Response.json(self.info())

        @self.get("/api/jobs")
        def _jobs_list(req):
            return Response.json(
                {
                    "ok": True,
                    "jobs": self.jobs.list(
                        state=req.arg("state"),
                        job_type=req.arg("type"),
                        limit=max(1, min(500, req.int_arg("limit", 50))),
                        offset=max(0, req.int_arg("offset", 0)),
                        active_only=req.bool_arg("active"),
                    ),
                    "counts": self.jobs.counts(),
                }
            )

        @self.post("/api/jobs")
        def _jobs_create(req):
            body = req.json_body()
            if not isinstance(body, dict):
                return Response.error("请求体必须是 JSON 对象", 400, "示例：{\"type\":\"scan\",\"params\":{}}")
            job_type = body.get("type")
            if not job_type:
                return Response.error("缺少 type 字段", 400, "可用类型见 GET /api/app 的 job_types")
            try:
                job = self.jobs.submit(
                    job_type,
                    body.get("params") or {},
                    title=body.get("title") or "",
                    max_attempts=int(body.get("max_attempts") or 3),
                )
            except ValueError as exc:
                return Response.error(str(exc), 400)
            except RuntimeError as exc:
                return Response.error(str(exc), 503)
            return Response.json({"ok": True, "job": job}, status=201)

        @self.get("/api/jobs/<job_id>")
        def _jobs_get(req, job_id):
            job = self.jobs.get(job_id)
            if not job:
                return Response.error("任务不存在", 404)
            return Response.json({"ok": True, "job": job})

        @self.post("/api/jobs/<job_id>/cancel")
        def _jobs_cancel(req, job_id):
            if not self.jobs.cancel(job_id):
                return Response.error("任务不存在或已结束", 409)
            return Response.json({"ok": True, "job": self.jobs.get(job_id)})

        @self.post("/api/jobs/<job_id>/pause")
        def _jobs_pause(req, job_id):
            if not self.jobs.pause(job_id):
                return Response.error("任务不在可暂停状态", 409)
            return Response.json({"ok": True, "job": self.jobs.get(job_id)})

        @self.post("/api/jobs/<job_id>/resume")
        def _jobs_resume(req, job_id):
            if not self.jobs.resume(job_id):
                return Response.error("任务不在暂停状态", 409)
            return Response.json({"ok": True, "job": self.jobs.get(job_id)})

        @self.post("/api/jobs/<job_id>/retry")
        def _jobs_retry(req, job_id):
            job = self.jobs.retry(job_id)
            if not job:
                return Response.error("任务不存在", 404)
            return Response.json({"ok": True, "job": job}, status=201)

        @self.delete("/api/jobs/<job_id>")
        def _jobs_delete(req, job_id):
            try:
                if not self.jobs.delete(job_id):
                    return Response.error("任务不存在", 404)
            except RuntimeError as exc:
                return Response.error(str(exc), 409)
            return Response.json({"ok": True})

        @self.get("/api/jobs/<job_id>/logs")
        def _jobs_logs(req, job_id):
            if not self.jobs.get(job_id):
                return Response.error("任务不存在", 404)
            return Response.json(
                {
                    "ok": True,
                    "logs": self.jobs.logs(
                        job_id,
                        offset=max(0, req.int_arg("offset", 0)),
                        limit=max(1, min(2000, req.int_arg("limit", 500))),
                    ),
                }
            )

        @self.get("/api/settings")
        def _settings_get(req):
            payload = self.settings.all()
            payload["allowed_roots"] = self.allowed.roots()
            payload["auth"] = {"mode": self.auth_mode, "received": req.auth}
            return Response.json({"ok": True, "settings": payload})

        @self.post("/api/settings")
        def _settings_set(req):
            body = req.json_body()
            if not isinstance(body, dict):
                return Response.error("请求体必须是 JSON 对象", 400)
            roots = body.pop("allowed_roots", None)
            changed = self.settings.update(body)
            if isinstance(roots, list):
                self.set_allowed_roots(roots)
                changed = True
            return Response.json(
                {"ok": True, "changed": changed, "settings": self.settings.all()}
            )

    # ---- 白名单 ----

    def set_allowed_roots(self, roots):
        """整体替换白名单（会逐个 realpath 后持久化）。"""
        self.allowed = paths_mod.AllowedRoots()
        normalized = []
        for root in roots or []:
            try:
                normalized.append(self.allowed.add(root))
            except (ValueError, OSError):
                continue
        self.settings.set("allowed_roots", normalized)
        self.log.info("可访问目录白名单已更新：%d 项", len(normalized))
        return normalized

    def load_allowed_roots(self):
        stored = self.settings.get("allowed_roots") or []
        if stored:
            self.set_allowed_roots(stored)
        return self.allowed.roots()

    # ---- 元信息 ----

    def info(self):
        return {
            "ok": True,
            "app": self.app_id,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "platform": "TOS 7",
            "uptime": round(time.time() - self.started_at, 1),
            "job_types": sorted(self.jobs._handlers.keys()),
            "engines": {
                name: (engine.get("available") if isinstance(engine, dict) else bool(engine))
                for name, engine in self.engines.items()
            },
            "paths": self.paths.describe(),
            "job_counts": self.jobs.counts(),
        }

    # ---- 静态资源 ----

    def resolve_static(self, relpath):
        """把 URL 路径映射到 ``webui/`` 下的真实文件；越界返回 None。"""
        relpath = urllib.parse.unquote(relpath or "/")
        relpath = posixpath.normpath(relpath).lstrip("/")
        if relpath in ("", "."):
            relpath = "index.html"
        if relpath.startswith("..") or relpath.startswith("/"):
            return None
        full = os.path.realpath(os.path.join(self.paths.webui_dir, relpath))
        root = os.path.realpath(self.paths.webui_dir)
        if not (full == root or full.startswith(root + os.sep)):
            return None
        if not os.path.isfile(full):
            return None
        return full

    # ---- 运行 ----

    def run(self, socket_path=None, host=None, port=None, background=False):
        """启动服务。``socket_path`` 与 ``host``/``port`` 二选一。"""
        self.load_allowed_roots()
        self.jobs.start()

        handler = _make_handler(self)
        if socket_path:
            server = _UnixHTTPServer(socket_path, handler)
            self._bind = ("unix", socket_path)
        else:
            server = _ThreadingHTTPServer((host or "127.0.0.1", int(port or 0)), handler)
            self._bind = ("tcp", "%s:%d" % server.server_address[:2])

        self._server = server
        self.log.info(
            "%s v%s 已启动，监听 %s（数据目录 %s）",
            self.app_id,
            self.version,
            self._bind[1],
            self.paths.data_dir,
        )
        if background:
            thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
            thread.start()
            return server
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            self.log.info("收到中断信号")
        finally:
            self.shutdown()
        return server

    def shutdown(self):
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        try:
            self.jobs.stop()
        except Exception:
            pass
        if self._bind and self._bind[0] == "unix":
            try:
                os.unlink(self._bind[1])
            except OSError:
                pass
        self.log.info("服务已停止")


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


#: Windows 上的 CPython 可能没编译 AF_UNIX 支持；有则用真实实现，没有则退化为会报错的桩
_AF_UNIX = getattr(socket, "AF_UNIX", None)


if _AF_UNIX is not None:

    class _UnixHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        """监听 Unix domain socket 的 HTTP 服务（Linux 上 iframe 应用的形态）。"""

        address_family = socket.AF_UNIX
        daemon_threads = True
        request_queue_size = 128

        def __init__(self, socket_path, handler):
            # 指引 12.9.5-4：绑定前必须清理残留 socket，否则启动失败
            parent = os.path.dirname(socket_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.exists(socket_path):
                os.unlink(socket_path)
            self.socket_path = socket_path
            super().__init__(socket_path, handler)
            # 指引 8.7.1：mode 0660，属主为服务用户，平台代理（root）可读
            try:
                os.chmod(socket_path, 0o660)
            except OSError:
                pass

        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name = "localhost"
            self.server_port = 0

        def server_close(self):
            super().server_close()
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

else:

    class _UnixHTTPServer:  # type: ignore[no-redef]
        """当前解释器不支持 Unix socket（Windows 常见）。"""

        def __init__(self, socket_path, handler):
            raise RuntimeError(
                "本机 Python 不支持 Unix domain socket，无法以 --socket 模式启动；"
                "开发时请改用 --tcp 127.0.0.1:<port>"
            )


def _make_handler(app):
    """为每个 :class:`App` 生成一个请求处理器类。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "%s/%s" % (app.app_id, app.version)
        sys_version = ""

        # ---- 基础 ----

        def log_message(self, fmt, *args):
            # 交给结构化日志，去掉 BaseHTTPRequestHandler 的裸 stderr 输出
            app.log.debug("%s - %s", self.address_string(), fmt % args)

        def address_string(self):
            addr = self.client_address
            if isinstance(addr, tuple) and addr:
                return str(addr[0])
            return str(addr or "-")

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                return b"", ErrorMessage("Content-Length 非法")
            if length < 0:
                return b"", ErrorMessage("Content-Length 非法")
            if length > MAX_BODY:
                return b"", ErrorMessage("请求体过大（上限 %d 字节）", (MAX_BODY,), status=413)
            if not length:
                return b"", None
            return self.rfile.read(length), None

        def _send(self, response):
            # 记下这次的语言，给任务线程用（它没有请求上下文）
            app._last_accept_language = self.headers.get("Accept-Language", "") or ""
            response = _localize(response, self.headers)
            body = response.body or b""
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            # 指引 8.3.1：平台代理可能做跨源预检，这里给足 CORS 头
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD" and body:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        # ---- 分发 ----

        def _dispatch(self):
            try:
                self._handle()
            except Exception as exc:  # 任何未捕获异常都要变成可读 JSON，而不是 500 空响应
                import traceback

                app.log.error("处理 %s %s 失败：%s", self.command, self.path, exc)
                app.log.debug("%s", traceback.format_exc())
                try:
                    self._send(
                        Response.error(
                            "服务内部错误",
                            500,
                            "请查看应用日志（journalctl -u %s）", hint_args=(app.app_id,),
                        )
                    )
                except Exception:
                    pass

        def _handle(self):
            parsed = urllib.parse.urlsplit(self.path)
            raw_path = urllib.parse.unquote(parsed.path or "/")
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

            if self.command == "OPTIONS":
                # 指引 8.3.1 的 CORS 预检：Method + Headers + Credentials 都要给
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
                self.send_header(
                    "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
                )
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Content-Type, X-Csrf-Token, Cookie, X-Requested-With",
                )
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            body, body_error = self._read_body()
            if body_error:
                self._send(Response.error(body_error, getattr(body_error, "status", 400)))
                return

            rel_path = self._strip_prefix(raw_path)
            request = Request(
                self.command,
                rel_path,
                query,
                self.headers,
                body,
                remote=self.address_string(),
                app_id=app.app_id,
            )

            if app.auth_mode == "strict" and rel_path.startswith("/api/") and not request.auth["consistent"]:
                app.log.warning("鉴权头缺失或不一致，已按 strict 模式拒绝：%s", rel_path)
                self._send(
                    Response.error(
                        "未通过平台鉴权",
                        401,
                        "请在 TOS 桌面内打开本应用（前端需携带 X-Csrf-Token 与 Cookie 头）",
                    )
                )
                return
            if rel_path.startswith("/api/") and not request.auth["consistent"]:
                app.log.debug("鉴权头缺失或不一致（lenient 模式放行）：%s", rel_path)

            handler, params = app.routes.match(self.command, rel_path)
            if handler is None:
                if params and params.get("__method_not_allowed__"):
                    self._send(Response.error("不支持的请求方法：%s", 405, args=(self.command,)))
                    return
                # 非 API 路径回退到静态资源（前端用相对路径，刷新子路径也能打开）。
                # 回退只对「干净的」路径生效——含 ``..`` 的路径一律 404，绝不回退，
                # 否则任何穿越尝试都会拿到首页 200，掩盖攻击面。
                if not rel_path.startswith("/api/") and not rel_path.startswith("/health"):
                    if ".." not in rel_path.split("/"):
                        static = app.resolve_static(rel_path)
                        if static is None and "." not in posixpath.basename(rel_path):
                            static = app.resolve_static("index.html")
                        if static:
                            self._send(Response.file(static))
                            return
                self._send(
                    Response.error(
                        "接口不存在：%s",
                        404,
                        "可用接口见 GET /api/app",
                        args=(rel_path,),
                    )
                )
                return

            result = handler(request, **params)
            self._send(result if isinstance(result, Response) else Response.json(result))

        def _strip_prefix(self, raw_path):
            """剥掉平台可能加上的前缀，让业务路由只关心 ``/api/...``。"""
            path = raw_path or "/"
            for prefix in ("/v2/proxy/" + app.app_id, "/" + app.app_id):
                if path == prefix:
                    return "/"
                if path.startswith(prefix + "/"):
                    return path[len(prefix):]
            return path

        # OPTIONS 必须一起分发，否则 CORS 预检会 501（指引 8.3.1 明确要求支持）
        do_GET = (
            do_POST
        ) = (
            do_PUT
        ) = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _dispatch

    return Handler

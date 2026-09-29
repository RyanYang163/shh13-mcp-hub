"""MCP Hub —— 应用装配、WebUI 管理接口与 MCP 传输端点。

对外提供两样东西：

1. **MCP 服务端**（给 AI 客户端用）
   * HTTP 传输：``POST /mcp``（JSON-RPC 2.0）。经平台代理暴露为
     ``/v2/proxy/shh13-mcp-hub/mcp``，因此**访问受 TOS 会话约束** ——
     调用方必须先通过平台的登录态，这是第一层门。
   * stdio 传输：``bin/shh13-mcp-hub --mcp-stdio``（本机 / SSH 场景）。

2. **WebUI 管理界面**（给管理员用）：查看工具清单、调整权限级别、维护目录白名单、
   翻阅审计日志、复制客户端配置片段、还原回收站里的文件。

安全边界（设计文档 §11.3 / §39）：

* 目录白名单 —— 每一次涉及路径的调用都 realpath 后比对白名单；
* 三级权限 —— 默认只读；危险工具（本应用里是「移入回收站」）默认不可用；
* 无 shell 执行入口 —— 工具表是固定命名的具体能力；
* 全量审计 —— 每次调用（含被拒的）都落库。
"""

import json
import os
import time

from tnasapp import fsapi, server as srv

from . import mcpserver as mcp
from . import tools as toolset

APP_ID = "shh13-mcp-hub"
APP_VERSION = "1.0.025"
TITLE = "MCP Hub"

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS mcp_audit (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL    NOT NULL,
        tool        TEXT    NOT NULL,
        level       TEXT    NOT NULL DEFAULT 'READ_ONLY',
        params      TEXT    NOT NULL DEFAULT '',
        allowed     INTEGER NOT NULL DEFAULT 0,
        error       TEXT,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        caller      TEXT    NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mcp_audit_ts ON mcp_audit(ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS mcp_recycle (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       REAL    NOT NULL,
        original TEXT    NOT NULL,
        stored   TEXT    NOT NULL,
        size     INTEGER NOT NULL DEFAULT 0,
        reason   TEXT    NOT NULL DEFAULT '',
        restored INTEGER NOT NULL DEFAULT 0
    )
    """,
]


def create_app(paths=None, log_level="INFO"):
    app = srv.App(
        APP_ID, TITLE, version=APP_VERSION, workers=2, log_level=log_level,
        extra_migrations=MIGRATIONS, paths=paths,
        description="让 AI 通过 MCP 使用 TNAS —— 默认只读，目录白名单受限。",
        engines={},
    )
    registry = mcp.Registry(app)
    app.registry = registry
    toolset.register(app, registry)
    fsapi.register(app)
    _register_routes(app)
    _register_jobs(app)
    app.log.info("MCP 工具已注册：%s", ", ".join(registry.names()))
    return app


# ---------------------------------------------------------------- 路由


def _register_routes(app):
    registry = app.registry

    # ---- MCP 端点 ----

    @app.post("/mcp")
    def _mcp_post(req):
        caller = "http:%s" % (req.remote or "-")
        response, is_notification = mcp.handle_http_body(
            registry, req.body.decode("utf-8", "replace"), caller=caller)
        if is_notification:
            return srv.Response.json({"ok": True}, status=202)
        # 客户端要求 SSE 时按 text/event-stream 回一条 message 事件
        accept = (req.headers.get("Accept") or "")
        if "text/event-stream" in accept:
            payload = json.dumps(response, ensure_ascii=False)
            return srv.Response(200, ("event: message\ndata: %s\n\n" % payload).encode("utf-8"),
                                "text/event-stream; charset=utf-8")
        return srv.Response.json(response)

    @app.get("/mcp")
    def _mcp_get(req):
        return srv.Response.error(
            "本服务不提供服务端发起的 SSE 流", 405,
            "请用 POST /mcp 发送 JSON-RPC 请求；工具清单用 tools/list 方法获取。")

    @app.delete("/mcp")
    def _mcp_delete(req):
        return srv.Response.json({"ok": True})

    # ---- WebUI 管理接口 ----

    @app.get("/api/mcp/status")
    def _status(req):
        level = registry.current_level()
        return srv.Response.json({
            "ok": True,
            "level": level,
            "level_label": mcp.LEVEL_LABELS[level],
            "levels": [{"id": key, "label": mcp.LEVEL_LABELS[key]}
                       for key in ("READ_ONLY", "READ_WRITE", "DANGEROUS")],
            "tool_count": len(registry.names()),
            "allowed_roots": app.allowed.roots(),
            "audit_count": app.store.scalar("SELECT COUNT(*) FROM mcp_audit", default=0),
            "denied_count": app.store.scalar(
                "SELECT COUNT(*) FROM mcp_audit WHERE allowed=0", default=0),
            "recycle_count": app.store.scalar(
                "SELECT COUNT(*) FROM mcp_recycle WHERE restored=0", default=0),
        })

    @app.post("/api/mcp/level")
    def _set_level(req):
        body = req.json_body() or {}
        level = body.get("level")
        if not level:
            return srv.Response.error("缺少 level 字段", 400,
                                      "可用：READ_ONLY / READ_WRITE / DANGEROUS")
        try:
            applied = registry.set_level(level)
        except ValueError as exc:
            return srv.Response.error(str(exc), 400)
        app.log.warning("MCP 权限级别已改为 %s（由 %s 发起）", applied, req.remote or "-")
        return srv.Response.json({"ok": True, "level": applied,
                                  "level_label": mcp.LEVEL_LABELS[applied]})

    @app.get("/api/mcp/tools")
    def _tools(req):
        level = registry.current_level()
        groups = registry.describe_groups()
        for items in groups.values():
            for item in items:
                item["available"] = mcp.LEVELS[level] >= mcp.LEVELS[item["level"]]
                item["level_label"] = mcp.LEVEL_LABELS[item["level"]]
        return srv.Response.json({"ok": True, "level": level, "groups": groups,
                                  "count": len(registry.names())})

    @app.get("/api/mcp/audit")
    def _audit(req):
        limit = max(1, min(1000, req.int_arg("limit", 200)))
        only_denied = req.bool_arg("denied")
        where = " WHERE allowed=0" if only_denied else ""
        rows = app.store.query(
            "SELECT * FROM mcp_audit%s ORDER BY id DESC LIMIT ?" % where, (limit,))
        for row in rows:
            row["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"]))
            row["allowed"] = bool(row["allowed"])
        return srv.Response.json({"ok": True, "entries": rows})

    @app.post("/api/mcp/audit/clear")
    def _audit_clear(req):
        count = app.store.scalar("SELECT COUNT(*) FROM mcp_audit", default=0)
        app.store.execute("DELETE FROM mcp_audit")
        registry.audit("__audit_clear__", registry.current_level(),
                       {"removed": count}, True, None, 0)
        return srv.Response.json({"ok": True, "removed": count})

    @app.post("/api/mcp/test")
    def _test(req):
        """在界面上直接试调一个工具（走同一套权限门与审计）。"""
        body = req.json_body() or {}
        name = body.get("tool")
        if not name:
            return srv.Response.error("缺少 tool 字段", 400)
        arguments = body.get("arguments") or {}
        ok, payload = registry.call(name, arguments, caller="webui")
        return srv.Response.json({"ok": ok, "result": payload if ok else None,
                                  "error": None if ok else payload})

    @app.get("/api/mcp/config")
    def _config(req):
        """给 AI 客户端用的配置片段。"""
        host = (req.headers.get("X-Forwarded-Host")
                or req.headers.get("Host") or "your-nas")
        host = host.split(",")[0].strip()
        base = "http://%s" % host if not host.startswith("http") else host
        http_url = "%s/v2/proxy/%s/mcp" % (base.rstrip("/"), APP_ID)
        return srv.Response.json({
            "ok": True,
            "app_id": APP_ID,
            "level": registry.current_level(),
            "http": {
                "url": http_url,
                "note": "该地址经 TOS 平台代理暴露，**需要有效的 TOS 登录会话**"
                        "（浏览器里打开 TOS 后再用）。平台未登录时会返回 please login。",
                "sample": {
                    "mcpServers": {
                        APP_ID: {"type": "http", "url": http_url}
                    }
                },
            },
            "stdio": {
                "command": "/usr/local/%s/bin/%s" % (APP_ID, APP_ID),
                "args": ["--mcp-stdio"],
                "note": "stdio 传输需在 NAS 上（或经 SSH / 容器）直接运行该程序，"
                        "适合本机型 MCP 客户端。",
                "sample": {
                    "mcpServers": {
                        APP_ID: {"command": "/usr/local/%s/bin/%s" % (APP_ID, APP_ID),
                                 "args": ["--mcp-stdio"]}
                    }
                },
            },
            "tools": registry.describe_all(),
        })

    @app.get("/api/mcp/recycle")
    def _recycle(req):
        rows = app.store.query(
            "SELECT * FROM mcp_recycle ORDER BY id DESC LIMIT ?",
            (max(1, min(500, req.int_arg("limit", 200))),))
        for row in rows:
            row["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"]))
            row["exists"] = os.path.isfile(row["stored"])
        return srv.Response.json({"ok": True, "items": rows})

    @app.post("/api/mcp/recycle/restore")
    def _restore(req):
        body = req.json_body() or {}
        item_ids = body.get("item_ids") or []
        if not item_ids:
            return srv.Response.error("没有指定要还原的条目", 400)
        placeholders = ",".join("?" * len(item_ids))
        rows = app.store.query(
            "SELECT * FROM mcp_recycle WHERE id IN (%s) AND restored=0" % placeholders,
            tuple(int(value) for value in item_ids))
        import shutil

        restored, skipped = 0, 0
        for row in rows:
            stored, original = row["stored"], row["original"]
            if not os.path.isfile(stored):
                skipped += 1
                continue
            target = original
            if os.path.exists(target):
                stem, ext = os.path.splitext(target)
                target = "%s-restored-%d%s" % (stem, int(time.time()), ext)
            try:
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.move(stored, target)
            except (OSError, shutil.Error) as exc:
                app.log.warning("还原失败：%s —— %s", original, exc)
                skipped += 1
                continue
            app.store.execute("UPDATE mcp_recycle SET restored=1 WHERE id=?", (row["id"],))
            restored += 1
        registry.audit("__recycle_restore__", registry.current_level(),
                       {"restored": restored, "skipped": skipped}, True, None, 0)
        return srv.Response.json({"ok": True, "restored": restored, "skipped": skipped})


# ---------------------------------------------------------------- 任务


def _register_jobs(app):
    @app.jobs.register("folder_size")
    def _job_folder_size(ctx):
        """大目录统计走队列，避免阻塞 WebUI。"""
        root = ctx.params.get("path")
        if not root:
            raise ValueError("没有指定目录")
        real = app.allowed.check(root)
        total = 0
        count = 0
        dirs = 0
        stack = [(real, 0)]
        while stack:
            directory, depth = stack.pop()
            if depth > toolset.MAX_DEPTH:
                continue
            ctx.checkpoint()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                dirs += 1
                                stack.append((entry.path, depth + 1))
                            elif entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                                count += 1
                        except OSError:
                            continue
            except (PermissionError, OSError):
                continue
            if count % 500 == 0:
                ctx.progress(count % 100000, 100000, "已统计 %d 个文件" % count)
        ctx.set_result({"path": real, "bytes": total, "file_count": count,
                        "dir_count": dirs})


def main(argv=None):
    """入口。``--mcp-stdio`` 时以 stdio 传输跑 MCP 服务端（不作为 TOS 服务）。"""
    import sys

    argv = list(argv if argv is not None else sys.argv[1:])
    if "--mcp-stdio" in argv:
        argv.remove("--mcp-stdio")
        app = create_app(log_level=os.environ.get("LOG_LEVEL") or "WARN")
        app.store.migrate(logger=app.log)
        app.load_allowed_roots()
        app.log.info("以 stdio 传输启动 MCP 服务端，已注册 %d 个工具",
                     len(app.registry.names()))
        return mcp.stdio_loop(app.registry)

    from tnasapp import cli

    return cli.main(APP_ID, APP_VERSION, create_app, argv=argv,
                    description="MCP Hub —— 让 AI 通过 MCP 使用 TNAS")

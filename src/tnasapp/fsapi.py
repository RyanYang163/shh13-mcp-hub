"""文件浏览 API（所有涉及用户文件的应用共用）。

对应前端 ``UI.pickDir()`` 调用的 ``/api/fs/*``。**所有路径都必须过白名单**，
所以这个模块只接受 :class:`tnasapp.paths.AllowedRoots` 里已有的目录。

安全要点（指引 38）：

* 每个请求的 ``path`` 都走 :meth:`AllowedRoots.check`：``realpath`` → 规范化 →
  比对白名单根。目录穿越与 symlink 逃逸都会在这里被拒。
* 只**列出**目录内容，不做任何写操作。
* 起始位置（``path`` 为空时）只暴露卷根与共享文件夹，**不暴露 ``/``**。
"""

import os

from . import paths as paths_mod
from .server import Response

#: 卷根扫描范围。TOS 把共享文件夹放在 /Volume<N>/ 下。
VOLUME_GLOB_PREFIX = "/Volume"


def discover_roots(max_volumes=16):
    """列出可以作为「起始位置」的目录。

    TOS 上返回 ``['/Volume1', '/Volume2', ...]`` 以及每个卷下的共享文件夹；
    开发机（Windows/macOS）上退化为可用的盘符 / 家目录，便于本机跑通。
    """
    found = []
    for index in range(1, max_volumes + 1):
        volume = "%s%d" % (VOLUME_GLOB_PREFIX, index)
        if os.path.isdir(volume):
            found.append(volume)

    if not found:
        # 非 TOS 环境（开发机）：给出可用的盘符或家目录
        if os.name == "nt":
            for letter in "CDEFGH":
                drive = "%s:\\" % letter
                if os.path.isdir(drive):
                    found.append(drive)
        else:
            for candidate in (os.path.expanduser("~"), "/mnt", "/media", "/srv", "/data"):
                if os.path.isdir(candidate):
                    found.append(os.path.realpath(candidate))
    return found


def shared_folders(volume_root, limit=200):
    """列出一个卷下的共享文件夹（跳过 ``@`` 开头的系统目录）。"""
    result = []
    try:
        with os.scandir(volume_root) as it:
            for entry in it:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name.startswith("@") or entry.name.startswith("."):
                    continue
                result.append(entry.path)
                if len(result) >= limit:
                    break
    except OSError:
        pass
    return sorted(result)


_ICON_BY_EXT = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".tif": "image", ".tiff": "image",
    ".heic": "image", ".svg": "image",
    ".mp3": "music", ".flac": "music", ".wav": "music", ".m4a": "music",
    ".aac": "music", ".ogg": "music", ".opus": "music", ".wma": "music",
    ".mp4": "video", ".mkv": "video", ".mov": "video", ".avi": "video",
    ".webm": "video", ".wmv": "video", ".flv": "video", ".m4v": "video", ".ts": "video",
    ".pdf": "fileText", ".txt": "fileText", ".md": "fileText", ".doc": "fileText",
    ".docx": "fileText", ".csv": "fileText", ".json": "fileText", ".log": "fileText",
    ".zip": "package", ".gz": "package", ".bz2": "package", ".tar": "package",
    ".7z": "package", ".rar": "package",
    ".db": "database", ".sqlite": "database",
}


def icon_for(name, is_dir):
    if is_dir:
        return "folder"
    ext = os.path.splitext(name)[1].lower()
    return _ICON_BY_EXT.get(ext, "file")


def _entry(path, name, is_dir, size=None, mtime=None):
    return {
        "name": name,
        "path": path,
        "is_dir": bool(is_dir),
        "size": size,
        "mtime": mtime,
        "icon": icon_for(name, is_dir),
    }


def list_dir(app, raw_path, limit=2000, include_files=True):
    """列目录。``raw_path`` 为空或不在白名单时，回退到「起始位置」列表。"""
    if not raw_path:
        return _roots_payload(app)

    try:
        target = app.allowed.check(raw_path)
    except paths_mod.PathDenied as exc:
        # 白名单外：不报 500，给出可读原因 + 起始位置，让用户自己改选
        payload = _roots_payload(app)
        payload.update({
            "ok": False,
            "error": str(exc),
            "hint": "该目录不在允许访问范围内，请先在「设置」里把它加入可访问目录。",
        })
        return payload

    if not os.path.isdir(target):
        return {
            "ok": False,
            "error": "不是目录：%s" % raw_path,
            "hint": "请选择目录（不是文件）。",
        }

    entries = []
    child_dirs = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    child_dirs.append(_entry(entry.path, entry.name, True))
                elif include_files:
                    try:
                        stat = entry.stat(follow_symlinks=False)
                        size, mtime = stat.st_size, stat.st_mtime
                    except OSError:
                        size, mtime = None, None
                    entries.append(_entry(entry.path, entry.name, False, size, mtime))
                if len(entries) + len(child_dirs) >= limit:
                    break
    except PermissionError:
        return {
            "ok": False,
            "error": "无法读取此目录",
            "hint": "当前用户没有权限、文件正被其他程序占用，或路径已不存在。"
                    "可尝试把该目录加入可访问目录，或改选其它目录。",
        }
    except OSError as exc:
        return {"ok": False, "error": "无法读取此目录：%s" % exc, "hint": "请改选其它目录。"}

    child_dirs.sort(key=lambda item: item["name"].lower())
    files = sorted(entries, key=lambda item: item["name"].lower())
    parent = os.path.dirname(target.rstrip(os.sep)) or None
    # 父目录若在白名单外就不给「上一级」，避免引导用户点进被拒的路径
    if parent and not app.allowed.is_allowed(parent):
        parent = None

    return {
        "ok": True,
        "path": target,
        "parent": parent,
        "entries": child_dirs + files,
        "dir_count": len(child_dirs),
        "file_count": len(files),
    }


def _roots_payload(app):
    """起始位置：白名单里的目录 + 卷根 + 共享文件夹。"""
    entries = []
    seen = set()

    for root in app.allowed.roots():
        if root not in seen:
            seen.add(root)
            entries.append(_entry(root, os.path.basename(root.rstrip(os.sep)) or root, True))

    for volume in discover_roots():
        if volume not in seen:
            seen.add(volume)
            entries.append(_entry(volume, os.path.basename(volume.rstrip(os.sep)) or volume, True))
        for share in shared_folders(volume):
            if share in seen:
                continue
            seen.add(share)
            if app.allowed.is_allowed(share):
                entries.append(_entry(share, os.path.relpath(share, volume), True))

    configured = bool(app.allowed.roots())
    return {
        "ok": True,
        "path": "",
        "parent": None,
        "entries": entries,
        "dir_count": len(entries),
        "file_count": 0,
        "hint": None if configured else (
            "还没有配置可访问目录。请到「设置」里添加你希望本应用读取的目录"
            "（例如 /Volume1/Photos），应用默认只读、不会修改你的文件。"
        ),
    }


def stat_path(app, raw_path):
    """单个路径的元信息（文件大小、修改时间、是否目录）。"""
    try:
        target = app.allowed.check(raw_path)
    except paths_mod.PathDenied as exc:
        return {"ok": False, "error": str(exc)}
    try:
        stat = os.stat(target)
    except OSError as exc:
        return {"ok": False, "error": "无法读取：%s" % exc}
    return {
        "ok": True,
        "path": target,
        "name": os.path.basename(target) or target,
        "is_dir": os.path.isdir(target),
        "size": stat.st_size,
        "size_text": paths_mod.human_size(stat.st_size),
        "mtime": stat.st_mtime,
    }


def register(app, list_path="/api/fs/list", stat_path_route="/api/fs/stat"):
    """把文件浏览接口挂到 :class:`tnasapp.server.App` 上。"""

    @app.get(list_path)
    def _fs_list(req):
        payload = list_dir(app, req.arg("path", ""), limit=req.int_arg("limit", 2000),
                           include_files=req.bool_arg("files", True))
        payload["roots"] = app.allowed.roots()
        return Response.json(payload)

    @app.get(stat_path_route)
    def _fs_stat(req):
        return Response.json(stat_path(app, req.arg("path", "")))

    @app.get("/api/fs/roots")
    def _fs_roots(req):
        return Response.json(
            {"ok": True, "roots": app.allowed.roots(), "volumes": discover_roots()}
        )

    app.log.debug("文件浏览接口已注册：%s / %s", list_path, stat_path_route)
    return app

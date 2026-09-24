#!/usr/bin/env python3
"""TOS 7 Deb 单包构建器 —— 纯 Python，跨平台。

为什么不用官方模板的 ``dpkg-deb --build``：那个只能跑在 Linux 上，而本机的开发与
自检环境是 Windows（没有 dpkg / 没有 docker）。用标准库直接写 ``ar`` + 两个 tar.gz，
就能在本机产出**真 deb** 并逐文件核对，不必把校验全部押在 CI 上。

产物命名遵循指引 15.1 / 4.2.4：**文件名不含版本号**，版本由 Release tag 承载::

    build/output/<appid>_<platform>.deb
    build/output/<appid>_<platform>.deb.sha256

关键实现点：

* ``bin/<appid>`` 用标准库 ``zipapp`` 生成——单文件、内含纯 .py 源码，
  **不是 ELF**，因此不会撞指引 16.4 的一票否决项「包内不得含二进制可执行文件」。
* 所有文本文件在入包前强制转 LF（指引 4.6：CRLF 是 Top 1 驳回原因）。
* 归档时间戳固定，构建可复现（指引 11.4）。
* ``md5sums`` 自动生成（指引 11：由 dpkg-deb 生成，我们不手写但必须提供）。

用法::

    python3 tools/build_deb.py                 # 默认 x86_64
    python3 tools/build_deb.py x86_64
    python3 tools/build_deb.py aarch64
    python3 tools/build_deb.py --inspect build/output/x_y.deb   # 只读回校验
"""

import argparse
import bz2
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import zipapp

# 归档内时间戳固定，保证同源码多次构建哈希一致
FIXED_MTIME = 1700000000

TEXT_EXTS = {
    ".sh", ".py", ".ini", ".lang", ".service", ".conf", ".env", ".md", ".txt",
    ".json", ".yml", ".yaml", ".css", ".js", ".html", ".svg", ".csv",
}

ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64"}

#: 必须随包分发（审核项 C2 许可证声明 / C3 隐私政策在包内可查）
REQUIRED_COMPLIANCE = ["LICENSE", "NOTICE", "PRIVACY.md"]

#: 可选随包分发
OPTIONAL_FILES = ["THIRD_PARTY_NOTICES.md", "sbom.spdx.json"]


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- 基础工具


def read_config(path="config.ini"):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def to_lf(data):
    """把 CRLF 归一成 LF（只对文本内容调用）。"""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def is_probably_text(path):
    return os.path.splitext(path)[1].lower() in TEXT_EXTS


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- tar 构造


def make_tar_gz(entries, root_prefix=""):
    """构造 tar.gz。

    :param entries: ``[(archive_name, source_path_or_bytes, mode)]``
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        added_dirs = set()
        for name, source, mode in entries:
            archive_name = (root_prefix + name).lstrip("/")
            if root_prefix:
                archive_name = root_prefix + name
            # 先补父目录条目：dpkg 对缺失目录条目会报 warning
            segments = archive_name.split("/")[:-1]
            for depth in range(1, len(segments) + 1):
                parent = "/".join(segments[:depth]) + "/"
                if parent in added_dirs:
                    continue
                added_dirs.add(parent)
                info = tarfile.TarInfo(parent)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = FIXED_MTIME
                tar.addfile(info)

            if isinstance(source, bytes):
                payload = source
            else:
                with open(source, "rb") as fh:
                    payload = fh.read()
                if is_probably_text(source):
                    payload = to_lf(payload)

            info = tarfile.TarInfo(archive_name)
            info.size = len(payload)
            # 权限完全由调用方给定的 mode 决定，**不看宿主文件系统** ——
            # Windows 上 os.chmod 设不了执行位，os.stat 拿到的永远是 0644/0666，
            # 会让包内的 bin/<appid> 丢掉可执行位（实测踩过）。
            info.mode = mode
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = FIXED_MTIME
            tar.addfile(info, io.BytesIO(payload))

    # mtime=0 让 gzip 头固定
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def make_ar(members):
    """构造 ar 归档（deb 的外层格式）。``members`` = [(name, bytes)]"""
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, payload in members:
        header = "%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (
            name,
            FIXED_MTIME,
            0,
            0,
            "100644",
            len(payload),
        )
        out.write(header.encode("ascii"))
        out.write(payload)
        if len(payload) % 2:
            out.write(b"\n")
    return out.getvalue()


# ---------------------------------------------------------------- 构建


def build_zipapp(src_dir, target, interpreter="/usr/bin/python3", main=None):
    """把 ``src/`` 打成单文件 zipapp（内含 .py 源码，非二进制）。"""
    if not os.path.isdir(src_dir):
        raise BuildError("缺少源码目录：%s" % src_dir)
    if not os.path.isfile(os.path.join(src_dir, "__main__.py")):
        raise BuildError("源码目录缺少 __main__.py：%s" % src_dir)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    if os.path.exists(target):
        os.remove(target)
    # 过滤 __pycache__ / .pyc，避免把本机缓存打进包
    tmp = target + ".srccopy"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    try:
        shutil.copytree(
            src_dir, tmp,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
        )
        zipapp.create_archive(tmp, target, interpreter=interpreter, main=main,
                             compressed=True)
    finally:
        if os.path.exists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    os.chmod(target, 0o755)
    # 再次确认 zip 内没有二进制
    with open(target, "rb") as fh:
        head = fh.read(4096)
    if head.startswith(b"\x7fELF"):
        raise BuildError("bin/ 产物是 ELF 二进制，会撞指引 16.4 一票否决项")
    return target


def build_webui_bz2(webui_dir, target):
    """把 ``webui/`` 打成 ``webui.bz2``（tar+bzip2，固定文件名）。"""
    if not os.path.isdir(webui_dir):
        raise BuildError("缺少前端目录：%s" % webui_dir)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    if os.path.exists(target):
        os.remove(target)

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for root, dirs, files in os.walk(webui_dir):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, webui_dir).replace(os.sep, "/")
                with open(full, "rb") as fh:
                    payload = fh.read()
                if is_probably_text(full):
                    payload = to_lf(payload)
                info = tarfile.TarInfo("./" + rel)
                info.size = len(payload)
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = FIXED_MTIME
                tar.addfile(info, io.BytesIO(payload))

    with open(target, "wb") as fh:
        fh.write(bz2.compress(raw.getvalue(), compresslevel=9))
    return target


def compute_md5sums(staging_dir):
    """生成 DEBIAN/md5sums（相对路径、两个空格分隔）。"""
    lines = []
    root = os.path.join(staging_dir, "usr")
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "DEBIAN")
        for name in sorted(files):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, staging_dir).replace(os.sep, "/")
            digest = hashlib.md5()
            with open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            lines.append("%s  %s" % (digest.hexdigest(), rel))
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def build(app_dir=None, platform="x86_64", out_dir=None, quiet=False):
    """构建 .deb，返回产物路径。"""
    app_dir = os.path.abspath(app_dir or os.getcwd())
    os.chdir(app_dir)

    config = read_config("config.ini")
    app_id = config["id"]
    version = config["version"]
    if platform not in ARCH_MAP:
        raise BuildError("不支持的平台：%s（只允许 x86_64 / aarch64）" % platform)
    dpkg_arch = ARCH_MAP[platform]

    build_root = os.path.join(app_dir, "build", platform)
    staging = os.path.join(build_root, "staging")
    out_dir = out_dir or os.path.join(app_dir, "build", "output")
    os.makedirs(out_dir, exist_ok=True)

    log = (lambda *a: None) if quiet else (lambda *a: print(*a))
    log("=== 构建 %s v%s (%s / %s) ===" % (app_id, version, platform, dpkg_arch))

    # ---- 前置校验 ----
    if config.get("application_type") != "deb":
        raise BuildError("application_type 必须是 'deb'（当前 %r）" % config.get("application_type"))
    if "type" in config and "open_path" in config:
        raise BuildError("type 与 open_path 互斥，不能同时存在（指引 8.3）")
    if config.get("id") != app_id:
        raise BuildError("config.ini 的 id 与目录不一致")
    for key in ("system_id", "package", "user", "version", "platform", "icon"):
        if not config.get(key):
            raise BuildError("config.ini 缺少必填字段：%s" % key)
    if config.get("platform") != platform:
        # platform 字段声明的是提交架构；本机构建其它架构时必须显式改配置
        log("  ⚠️  config.ini 的 platform=%s 与本次构建的 %s 不一致"
            % (config["platform"], platform))

    control_path = os.path.join(app_dir, "DEBIAN", "control")
    if not os.path.isfile(control_path):
        raise BuildError("缺少 DEBIAN/control")
    with open(control_path, "r", encoding="utf-8") as fh:
        control_text = fh.read()
    control_version = None
    for line in control_text.splitlines():
        if line.startswith("Version:"):
            control_version = line.split(":", 1)[1].strip()
    if control_version != version:
        raise BuildError(
            "DEBIAN/control 的 Version(%s) 与 config.ini 的 version(%s) 不一致"
            "（指引 4.2.2，会自动驳回）" % (control_version, version)
        )

    missing = [name for name in REQUIRED_COMPLIANCE if not os.path.isfile(os.path.join(app_dir, name))]
    if missing:
        raise BuildError("缺少合规文件：%s（审核项 C2/C3 会失败）" % ", ".join(missing))

    with open(os.path.join(app_dir, app_id + ".lang"), "rb") as fh:
        lang_raw = fh.read()
    if lang_raw[:3] == b"\xef\xbb\xbf":
        raise BuildError("%s.lang 含 UTF-8 BOM（指引 8.5.5 禁止）" % app_id)

    icon_rel = str(config["icon"]).lstrip("/")
    if not os.path.isfile(os.path.join(app_dir, icon_rel)):
        raise BuildError("图标文件不存在：%s（icon 字段声明的是 %s）" % (icon_rel, config["icon"]))

    # ---- staging ----
    if os.path.isdir(build_root):
        shutil.rmtree(build_root)
    app_root = os.path.join(staging, "usr", "local", app_id)
    os.makedirs(app_root, exist_ok=True)
    os.makedirs(os.path.join(staging, "DEBIAN"), exist_ok=True)

    def copy_in(relative, mode=0o644):
        source = os.path.join(app_dir, relative)
        if not os.path.isfile(source):
            return False
        target = os.path.join(app_root, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(source, "rb") as fh:
            payload = fh.read()
        if is_probably_text(source):
            payload = to_lf(payload)
        with open(target, "wb") as fh:
            fh.write(payload)
        try:
            os.chmod(target, mode)
        except OSError:
            pass
        # 记下权限：Windows 上 os.chmod 不生效，打包时不能回头去 stat 文件系统。
        # key 以 staging 为根（usr/local/<appid>/...），与后面 os.walk(staging) 查表一致。
        modes["usr/local/%s/%s" % (app_id, relative.replace(os.sep, "/"))] = mode
        return True

    #: 相对 app_root 的显式权限表（打包时以此为准）
    modes = {}

    copied = []
    for name in ["config.ini", app_id + ".lang", app_id + ".env"] + REQUIRED_COMPLIANCE + OPTIONAL_FILES:
        if copy_in(name):
            copied.append(name)
    log("  配置文件与合规材料：%s" % ", ".join(copied))

    # 图标与 systemd 单元（保持目录形态）
    for relative_dir in ("images", "init.d", "nginx", "depends"):
        source_dir = os.path.join(app_dir, relative_dir)
        if not os.path.isdir(source_dir):
            continue
        for base, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                full = os.path.join(base, name)
                rel = os.path.relpath(full, app_dir)
                copy_in(rel, 0o755 if name.endswith(".sh") else 0o644)
        log("  已放入目录：%s/" % relative_dir)

    # 后端：bin/<appid> = src/ 打成的 zipapp
    zipapp_target = os.path.join(app_root, "bin", app_id)
    build_zipapp(os.path.join(app_dir, "src"), zipapp_target)
    modes["usr/local/%s/bin/%s" % (app_id, app_id)] = 0o755
    log("  后端已生成：bin/%s（zipapp，%.0f KB）"
        % (app_id, os.path.getsize(zipapp_target) / 1024))

    # 前端
    if config.get("type") == "iframe" or config.get("open_path") is True:
        webui_bz2 = os.path.join(app_root, "webui.bz2")
        build_webui_bz2(os.path.join(app_dir, "webui"), webui_bz2)
        modes["usr/local/%s/webui.bz2" % app_id] = 0o644
        log("  前端已生成：webui.bz2（%.0f KB）" % (os.path.getsize(webui_bz2) / 1024))
        if not os.path.isdir(os.path.join(app_root, "webui")):
            pass  # webui/ 由 postinst 解压得到

    # nginx 只在外部打开时必需
    if config.get("open_path") is True and not os.path.isdir(os.path.join(app_root, "nginx")):
        raise BuildError("外部打开模式必须提供 nginx/<appid>.conf（指引 8.3.2）")

    # ---- DEBIAN ----
    control_out = control_text.replace("Architecture: amd64", "Architecture: " + dpkg_arch)
    control_out = control_out.replace("Architecture: arm64", "Architecture: " + dpkg_arch)
    if not control_out.endswith("\n"):
        control_out += "\n"

    control_members = [("control", control_out.encode("utf-8"))]
    for script in ("preinst", "postinst", "prerm", "postrm"):
        path = os.path.join(app_dir, "DEBIAN", script)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                payload = to_lf(fh.read())
            if not payload.startswith(b"#!"):
                raise BuildError("DEBIAN/%s 缺少 shebang" % script)
            control_members.append((script, payload))
    md5sums = compute_md5sums(staging)
    if md5sums:
        control_members.append(("md5sums", md5sums))

    control_tar = make_tar_gz(
        [(name, payload, 0o644 if name == "control" else 0o755)
         for name, payload in control_members],
        root_prefix="./",
    )

    data_entries = []
    for base, dirs, files in os.walk(staging):
        rel_base = os.path.relpath(base, staging)
        if rel_base.startswith("DEBIAN"):
            continue
        for name in files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, staging).replace(os.sep, "/")
            mode = modes.get(rel, 0o644)
            data_entries.append((rel, full, mode))
    data_entries.sort(key=lambda item: item[0])
    data_tar = make_tar_gz(data_entries, root_prefix="./")

    deb_bytes = make_ar([
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", control_tar),
        ("data.tar.gz", data_tar),
    ])

    deb_path = os.path.join(out_dir, "%s_%s.deb" % (app_id, platform))
    with open(deb_path, "wb") as fh:
        fh.write(deb_bytes)
    digest = hashlib.sha256(deb_bytes).hexdigest()
    with open(deb_path + ".sha256", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("%s  %s\n" % (digest, os.path.basename(deb_path)))

    log("")
    log("=== 完成 ===")
    log("  产物：%s（%.1f KB）" % (deb_path, len(deb_bytes) / 1024))
    log("  SHA-256：%s" % digest)
    return deb_path


# ---------------------------------------------------------------- 读回校验


def read_ar(data):
    if not data.startswith(b"!<arch>\n"):
        raise BuildError("不是 ar 归档（deb 外层格式）")
    offset = 8
    members = []
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        name = header[0:16].decode("ascii").strip()
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError:
            break
        offset += 60
        members.append((name, data[offset:offset + size]))
        offset += size + (size % 2)
    return members


def read_tar_gz(payload):
    raw = gzip.decompress(payload)
    out = []
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for info in tar.getmembers():
            item = {
                "name": info.name,
                "size": info.size,
                "mode": info.mode,
                "is_dir": info.isdir(),
            }
            if info.isfile() and info.size < (1 << 20):
                handle = tar.extractfile(info)
                item["data"] = handle.read() if handle else b""
            out.append(item)
    return out


def inspect(deb_path, verbose=True):
    """读回一个 .deb 并做结构自检，返回 ``(ok, problems, summary)``。"""
    problems = []
    with open(deb_path, "rb") as fh:
        data = fh.read()
    members = dict(read_ar(data))
    for required in ("debian-binary", "control.tar.gz", "data.tar.gz"):
        if required not in members:
            problems.append("缺少 ar 成员：%s" % required)
    if problems:
        return False, problems, {}

    if members["debian-binary"] != b"2.0\n":
        problems.append("debian-binary 内容应为 '2.0\\n'")

    control_files = {item["name"].lstrip("./"): item for item in read_tar_gz(members["control.tar.gz"])}
    for required in ("control", "md5sums"):
        if required not in control_files:
            problems.append("control.tar.gz 缺少 %s" % required)

    control_text = control_files.get("control", {}).get("data", b"").decode("utf-8", "replace")
    fields = {}
    for line in control_text.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()
    for key in ("package", "version", "architecture", "maintainer", "description", "depends"):
        if key not in fields:
            problems.append("control 缺少字段：%s" % key)

    data_files = read_tar_gz(members["data.tar.gz"])
    files = [item for item in data_files if not item["is_dir"]]
    names = [item["name"].lstrip("./") for item in files]
    summary = {
        "package": fields.get("package"),
        "version": fields.get("version"),
        "architecture": fields.get("architecture"),
        "file_count": len(files),
        "total_size": sum(item["size"] for item in files),
        "files": names,
    }

    # 一票否决项：包内不得有 ELF 二进制（生命周期脚本除外）
    for item in files:
        head = (item.get("data") or b"")[:4]
        if head == b"\x7fELF":
            problems.append("包内含 ELF 二进制：%s（指引 16.4 一票否决）" % item["name"])

    # 必备文件
    app_id = fields.get("package") or ""
    must_have = [
        "usr/local/%s/config.ini" % app_id,
        "usr/local/%s/%s.lang" % (app_id, app_id),
        "usr/local/%s/bin/%s" % (app_id, app_id),
        "usr/local/%s/images/icons/%s.svg" % (app_id, app_id),
        "usr/local/%s/init.d/%s.service" % (app_id, app_id),
        "usr/local/%s/webui.bz2" % app_id,
        "usr/local/%s/LICENSE" % app_id,
        "usr/local/%s/NOTICE" % app_id,
        "usr/local/%s/PRIVACY.md" % app_id,
    ]
    for needed in must_have:
        if needed not in names:
            problems.append("包内缺少：%s" % needed)

    # CRLF 与 BOM 只对**文本条目**有意义。
    # 不能拿压缩产物的字节去扫 —— zipapp（deflate）与 webui.bz2（bzip2）的压缩流里
    # 出现 `0D 0A` 是必然或概率事件（imagedec.py 的 deflate 流就必然含它），
    # 第一版按字节盲扫导致**每个**打包了 imagedec 的应用都误报 CRLF。
    BINARY_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"BZh", b"\x7fELF", b"\xfd7zXZ")
    for item in files:
        payload = item.get("data")
        if payload is None:
            continue
        if payload.startswith(BINARY_MAGICS):
            continue
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            continue  # 不是文本，跳过行尾检查
        if payload[:3] == b"\xef\xbb\xbf":
            problems.append("含 BOM：%s" % item["name"])
        if b"\r\n" in payload:
            problems.append("含 CRLF：%s" % item["name"])

    # zipapp 后端要能当脚本执行（shebang + zip 头），且体积合理
    bin_item = next((i for i in files if i["name"].lstrip("./") == "usr/local/%s/bin/%s" % (app_id, app_id)), None)
    if bin_item is not None:
        payload = bin_item.get("data") or b""
        if not payload.startswith(b"#!"):
            problems.append("bin/%s 缺少 shebang" % app_id)
        if b"__main__.py" not in payload:
            problems.append("bin/%s 不像 zipapp（未找到 __main__.py）" % app_id)
        if not (bin_item["mode"] & 0o111):
            problems.append("bin/%s 没有执行权限" % app_id)

    # md5sums 必须覆盖所有数据文件
    md5_text = control_files.get("md5sums", {}).get("data", b"").decode("utf-8", "replace")
    md5_paths = {line.split("  ", 1)[1].strip() for line in md5_text.splitlines() if "  " in line}
    missing_md5 = [name for name in names if name not in md5_paths]
    if missing_md5:
        problems.append("md5sums 未覆盖 %d 个文件（例如 %s）" % (len(missing_md5), missing_md5[0]))

    if verbose:
        print("=== %s ===" % os.path.basename(deb_path))
        print("  Package     : %s" % summary["package"])
        print("  Version     : %s" % summary["version"])
        print("  Architecture: %s" % summary["architecture"])
        print("  文件数      : %d（解包共 %.1f KB）"
              % (summary["file_count"], summary["total_size"] / 1024))
        print("  生命周期脚本: %s" % ", ".join(
            sorted(k for k in control_files if k in ("preinst", "postinst", "prerm", "postrm"))))
        print("  包内文件：")
        for name in names:
            print("    %s" % name)
        print()
    return (not problems), problems, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="TOS 7 Deb 单包构建器")
    parser.add_argument("platform", nargs="?", default="x86_64",
                        choices=["x86_64", "aarch64"], help="目标架构")
    parser.add_argument("--inspect", metavar="DEB", help="只读回校验已存在的 deb")
    parser.add_argument("--out-dir", metavar="DIR", help="产物目录")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.inspect:
            ok, problems, _ = inspect(args.inspect)
            if problems:
                print("[ERROR] %d 项问题：" % len(problems))
                for item in problems:
                    print("  x %s" % item)
                return 1
            print("[通过] 包结构自检无问题")
            return 0
        path = build(platform=args.platform, out_dir=args.out_dir, quiet=args.quiet)
        print(path)
        return 0
    except BuildError as exc:
        print("[ERROR] %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

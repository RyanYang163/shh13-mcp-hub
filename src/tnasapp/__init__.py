"""tnasapp — TOS 7 应用公共框架（纯 Python 标准库）。

本框架被内联（vendoring）复制进每个应用仓库的 ``src/tnasapp/``，不做共享运行时。

设计约束（全部来自官方《TOS 7 应用开发指引》与《审核标准》第 16 章）：

* **只用 Python 3.10 标准库**。Deb 应用不得依赖未预装的运行时（指引 16.5 rank 13
  「Dependency not preinstalled; command not found」是高频驳回项），因此不 import 任何
  pip 包。可选的外部引擎（ffmpeg / tesseract / tesseract 等）只通过「检测到才用」的方式
  调用，绝不作为启动前提。
* **不产生任何预编译二进制**。``bin/<appid>`` 是 ``zipapp`` 打出的单文件，内部全是 .py，
  规避指引 16.4 的一票否决项「包内不得含二进制可执行文件」。
* **iframe 应用监听 Unix socket** ``/var/api/<appid>.sock``（mode 0660），并在启动前清理
  残留 socket（指引 8.7.1 / 12.9.5）。后端同时负责 serve 前端静态资源——平台只做转发
  （指引 FAQ 19.3 的 ``curl --unix-socket ... http://localhost/`` 即验证这一点）。
* **前端资源引用一律相对路径**，规避 Vite 式绝对路径在 ``/<appid>/`` 前缀下的白屏问题。

公共模块：

=================  ==========================================================
``paths``          目录推导与白名单路径校验（防目录穿越 / symlink 逃逸）
``logx``           标准日志（指引 8.7.1 的格式）+ 敏感信息脱敏
``store``          SQLite 封装 + ``PRAGMA user_version`` 迁移
``jobs``           持久化任务队列：进度 / 日志 / 取消 / 暂停 / 重试
``server``         Unix socket / TCP 双模 HTTP 服务、路由、静态资源、CORS
``fsapi``          文件浏览 API（白名单内列目录，供前端目录选择器使用）
``cli``            命令行入口（``--socket`` / ``--tcp`` / ``--data-dir`` / ``--debug``）
=================  ==========================================================
"""

__all__ = ["paths", "logx", "store", "jobs", "server", "fsapi", "cli"]
__version__ = "1.0.0"

# 显式导入各子模块，让 `import tnasapp` 之后 `tnasapp.server` 之类的写法直接可用。
# 模块之间没有循环依赖，导入成本可以忽略。
from . import cli, fsapi, imagedec, jobs, logx, paths, server, store  # noqa: E402,F401

"""日志：标准格式 + 敏感信息脱敏。

指引 8.7.1 规定的输出格式：

    [YYYY-MM-DD HH:MM:SS] [LEVEL] [component] message

另外指引明确禁止把密码 / API Key / Token / Cookie / 文件内容 / 敏感 OCR 内容写进日志
（指引 12.3 / 16.2 Security 的「Log Security」项），所以本模块对日志正文做**兜底脱敏**——
即使业务代码不小心把密钥拼进了消息，落盘前也会被打码。

后端默认把日志写到 stdout（systemd journal 自动收集，指引 8.7.1 推荐），
同时按 RotatingFileHandler 落一份到 ``<data>/logs/app.log``（有界、可轮转，符合
指引 12.9.6 对日志文件「必须有轮转策略」的要求）。
"""

import logging
import logging.handlers
import os
import re
import sys

LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

#: 形如 ``...password=xxx`` / ``"api_key": "xxx"`` 的赋值，打码保留键名
_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"authorization|auth|cookie|credential|private[_-]?key)"
    r"(\s*[:=]\s*)"
    r"([^\s,;&\"'\]\}]{3,})"
)

#: 形如 ``Bearer xxxxx`` / ``Basic xxxxx``
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")

#: 形如 ``TMSESSNAME=uuid`` 的会话 cookie
_COOKIE_RE = re.compile(r"(?i)\b(TMSESSNAME|X-Csrf-Token)=([^;\s]{6,})")

MASK = "***REDACTED***"


def redact(text):
    """对一段文本脱敏。非字符串原样返回。

    顺序要紧：**先**打码 ``Bearer <token>`` 这类整体凭据，**再**打码
    ``key=value`` 形式。反过来会让 ``Authorization:`` 这条先吃掉 ``Bearer``，
    把真正的令牌留成原文（实测踩过）。
    """
    if not isinstance(text, str):
        return text
    out = _BEARER_RE.sub(lambda m: m.group(1) + " " + MASK, text)
    out = _ASSIGN_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, out)
    out = _COOKIE_RE.sub(lambda m: m.group(1) + "=" + MASK, out)
    return out


class _RedactingFilter(logging.Filter):
    """在格式化之前把 message 与 args 里的敏感串打码。"""

    def filter(self, record):
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: (redact(v) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a for a in record.args
                    )
        except Exception:  # 脱敏自身绝不能影响业务
            pass
        return True


class _Formatter(logging.Formatter):
    """指引 8.7.1 的标准输出格式。"""

    default_time_format = "%Y-%m-%d %H:%M:%S"

    def format(self, record):
        ts = self.formatTime(record, self.default_time_format)
        level = record.levelname
        if level == "WARNING":
            level = "WARN"
        component = getattr(record, "component", None) or record.name
        message = record.getMessage()
        line = "[%s] [%s] [%s] %s" % (ts, level, component, message)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


_configured = set()


def setup(component, level="INFO", log_path=None, to_stdout=True, max_bytes=2 * 1024 * 1024,
          backup_count=5):
    """配置并返回一个 logger。

    :param component: 组件名，出现在日志的 ``[component]`` 位置
    :param level: ``DEBUG`` / ``INFO`` / ``WARN`` / ``ERROR``
    :param log_path: 若给定，额外落一份轮转文件日志
    :param to_stdout: 是否输出到 stdout（systemd journal 会收集）
    """
    logger = logging.getLogger(component)
    logger.setLevel(LEVELS.get(str(level).upper(), logging.INFO))
    logger.propagate = False
    if component in _configured:
        return logger

    formatter = _Formatter()
    redactor = _RedactingFilter()

    if to_stdout:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        logger.addHandler(stream)

    if log_path:
        try:
            parent = os.path.dirname(log_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            rotating.setFormatter(formatter)
            rotating.addFilter(redactor)
            logger.addHandler(rotating)
        except OSError as exc:  # 落盘失败不能拖垮服务
            logger.warning("文件日志不可用（%s），仅输出到 stdout", exc)

    _configured.add(component)
    return logger


def get(component):
    """取一个已配置的 logger；未配置过则用默认 INFO + stdout。"""
    if component in _configured:
        return logging.getLogger(component)
    return setup(component)

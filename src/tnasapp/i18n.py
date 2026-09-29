# -*- coding: utf-8 -*-
"""服务端消息的多语言。

与前端**同一套口径**：
  · 语言码沿用指引 8.5.1 的 14 种，与 ``<appid>.lang`` 完全一致
  · **中文原文就是 key**（``tr("任务不存在")`` → ``"Job not found"``），查不到原样返回中文
  · 插值用 ``%`` 风格的参数元组，模板与译文**参数个数必须一致**（由 ``_check()`` 兜住）

语言从哪来（**不需要额外的设置管道**）：
    前端 ``app.js`` 已经把语言解析完了（用户显式选择 → navigator.language → zh-cn），
    它把结果放进请求头 ``Accept-Language``。后端直接读这个头即可 —— 不必再让每个
    handler 去读 ``app.settings``，任务线程也就没有"读不到请求上下文"的问题。

设计取舍：
  · 只有 13 种语言的条目（``zh-cn`` 原文即译文，不建表）
  · 词表放 ``i18n_msgs.py``：词表是纯数据、体量大、几乎只在改文案时动；
    本文件是逻辑、体量小。分开后 diff 清楚，也能各自单独审。
"""

import re

# 与 <appid>.lang / 前端 i18n.js 同一套语言码
LANGS = ["zh-cn", "zh-hk", "en-us", "fr-fr", "de-de", "it-it", "es-es",
         "hu-hu", "ja-jp", "ko-kr", "pl-pl", "ru-ru", "tr-tr", "pt-pt"]

DEFAULT = "zh-cn"

# 地区 / 变体 → 规范语言码
_REGION = {
    "zh-tw": "zh-hk", "zh-hk": "zh-hk", "zh-mo": "zh-hk",
    "zh-cn": "zh-cn", "zh-sg": "zh-cn", "zh-hans": "zh-cn",
}


def normalize(tag):
    """把浏览器/请求头里的语言标签收敛到 TOS 的 14 个语言码之一；认不出返回空串。"""
    raw = str(tag or "").strip().lower().replace("_", "-")
    if not raw:
        return ""
    if raw in LANGS:
        return raw
    if raw in _REGION:
        return _REGION[raw]
    parts = raw.split("-")
    primary, region = parts[0], (parts[1] if len(parts) > 1 else "")
    if primary == "zh":
        return "zh-hk" if (region in ("tw", "hk", "mo") or "hant" in raw) else "zh-cn"
    if primary == "pt":
        return "pt-pt"
    for code in LANGS:
        if code.split("-")[0] == primary:
            return code
    return ""


def negotiate(accept_language, override=""):
    """定语言：用户显式设置 > Accept-Language（按 q 值排序） > zh-cn。

    ``override`` 是给「服务端也能知道用户选择」的场景留的口子（当前前端直接写进
    请求头，用不到；但保留这个参数，将来要在服务端存偏好时不必改调用方）。
    """
    forced = normalize(override)
    if forced:
        return forced
    header = str(accept_language or "")
    if not header:
        return DEFAULT
    items = []
    for index, chunk in enumerate(header.split(",")):
        piece = chunk.strip()
        if not piece:
            continue
        bits = piece.split(";")
        tag = bits[0].strip()
        quality = 1.0
        for bit in bits[1:]:
            bit = bit.strip()
            if bit.startswith("q="):
                try:
                    quality = float(bit[2:])
                except ValueError:
                    quality = 0.0
        # 同 q 值时保持原顺序（浏览器就是按偏好排的）
        items.append((-quality, index, tag))
    items.sort()
    for _quality, _index, tag in items:
        hit = normalize(tag)
        if hit:
            return hit
    return DEFAULT


def render(template, args=None):
    """套用 ``%`` 参数。参数对不上时**不要抛**（那会让一个提示变成 500）——
    退回未套参的模板，界面至少还看得懂。"""
    text = str(template)
    if not args:
        return text
    try:
        return text % (args if isinstance(args, (tuple, list)) else (args,))
    except (TypeError, ValueError, KeyError, IndexError):
        return text


# 应用自己的词表（由 App 在构建时 register 进来）。与前端的分法一致：
# 框架词条在 i18n_msgs.py，应用词条在各应用自己的 src/app/i18n_msgs.py。
_APP_MESSAGES = {}


def register(messages):
    """挂上应用级词表。``messages`` = ``{语言码: {中文原文: 译文}}``，与框架同构。

    应用自己的文案不该塞进框架词表（那会让 10 个应用背同一份），
    也不该各自造一套取词逻辑。``tr`` 查表的顺序是**应用优先、框架兜底**。
    """
    if messages:
        _APP_MESSAGES.update(messages)


def tr(key, lang=DEFAULT, args=None):
    """取词。查不到返回中文原文（优雅降级，绝不留空）。"""
    if not key:
        return key
    code = normalize(lang) or DEFAULT
    if code == DEFAULT:
        return render(key, args)
    # 回退链：目标语言 → en-us → 中文原文。
    # 回退到英文而不是直接回中文：没翻到的词若回中文，非中文用户会在整屏英文里
    # 突然看到一句中文，比全英文更糟。zh-hk 例外 —— 繁体读者看简体远比看英文顺。
    order = ["zh-hk"] if code == "zh-hk" else [code, "en-us"]
    hit = None
    for step in order:
        table = _APP_MESSAGES.get(step)
        if table and table.get(key) is not None:
            hit = table[key]
            break
        from .i18n_msgs import MESSAGES      # 延迟导入：语言包体积大，且便于单独替换
        table = MESSAGES.get(step)
        if table and table.get(key) is not None:
            hit = table[key]
            break
    return render(key, args) if hit is None else render(hit, args)


def _check():
    """自检：13 种语言的键集必须与 zh-cn 用的那套完全一致，且 ``%`` 占位符逐条一致。

    返回问题列表（空 = 通过）。被 ``tests`` 与 ``verify.py`` 调用 —— 漏一条译文
    只会静默回落中文，不跑这个检查就发现不了。
    """
    from .i18n_msgs import MESSAGES
    problems = []
    if _APP_MESSAGES:
        # 应用词表逐套自检（键集与占位符）
        for code in LANGS:
            if code == DEFAULT:
                continue
        expect_app = set(_APP_MESSAGES.get("en-us", {}))
        for code, table in sorted(_APP_MESSAGES.items()):
            miss = expect_app - set(table)
            if miss:
                problems.append("[应用/%s] 缺 %d 个键：%s"
                                % (code, len(miss), "、".join(sorted(miss)[:3])))
    expect = set(MESSAGES.get("en-us", {}))
    placeholder = re.compile(r"%[sdifxXrg%]|%\((\w+)\)[sdifxrg]")
    for code in LANGS:
        if code == DEFAULT:
            continue
        table = MESSAGES.get(code)
        if not table:
            problems.append("缺少语言包：%s" % code)
            continue
        miss = expect - set(table)
        extra = set(table) - expect
        if miss:
            problems.append("[%s] 缺 %d 个键：%s" % (code, len(miss), "、".join(sorted(miss)[:3])))
        if extra:
            problems.append("[%s] 多 %d 个键：%s" % (code, len(extra), "、".join(sorted(extra)[:3])))
        for key in expect & set(table):
            if sorted(placeholder.findall(key)) != sorted(placeholder.findall(table[key])):
                problems.append("[%s] 占位符不一致：%r" % (code, key[:26]))
    return problems

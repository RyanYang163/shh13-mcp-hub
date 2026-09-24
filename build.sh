#!/bin/bash
# ============================================================
# 构建入口（指引 15.1 / 4.2.4）
#   产物：build/output/<appid>_<platform>.deb
#   文件名**不含版本号**，版本由 GitHub Release 的 tag 承载。
#
# 真正的构建逻辑在 tools/build_deb.py —— 纯 Python 实现，不依赖 dpkg-deb，
# 因此 Windows 开发机上也能产出真 deb 并做结构自检。
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

PLATFORM="${1:-x86_64}"

PY="$(command -v python3 || command -v python || true)"
if [ -z "${PY}" ]; then
    echo "ERROR: 需要 python3 才能构建" >&2
    exit 1
fi

exec "${PY}" tools/build_deb.py "${PLATFORM}"

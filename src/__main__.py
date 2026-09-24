#!/usr/bin/env python3
"""zipapp 入口。

除常规服务模式外，还支持把本程序**直接当 MCP 服务端**跑：

    bin/shh13-mcp-hub --mcp-stdio      # stdio 传输（给本机/SSH 场景的 MCP 客户端用）

作为 TOS 服务运行时走 HTTP 传输，端点经平台代理暴露为
``/v2/proxy/shh13-mcp-hub/mcp``。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

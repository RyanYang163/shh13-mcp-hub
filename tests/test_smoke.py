"""shh13-mcp-hub 冒烟测试（占位）。

业务代码写完后，把本文件替换为该应用的正式用例。
运行：``python3 -m unittest discover -s tests -v``
"""

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)


class PlaceholderTest(unittest.TestCase):
    def test_framework_importable(self):
        import tnasapp  # noqa: F401

        self.assertTrue(hasattr(tnasapp, "server"))

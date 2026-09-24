"""端到端：应用真的跑起来之后的 HTTP 路由、MCP over HTTP、以及白名单边界。"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from tnasapp.paths import AppPaths  # noqa: E402

from app import main as app_main  # noqa: E402

APP_ID = app_main.APP_ID


class ApiClient:
    def __init__(self, base):
        self.base = base

    def request(self, method, path, payload=None, raw=False, extra_headers=None):
        data = None
        headers = dict(extra_headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read()
                return response.status, (body if raw else json.loads(body.decode("utf-8")))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"raw": body}

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, payload=None, **kwargs):
        return self.request("POST", path, payload, **kwargs)

    def rpc(self, method, params=None, request_id=1):
        return self.post("/mcp", {"jsonrpc": "2.0", "id": request_id,
                                  "method": method, "params": params or {}})


def write_file(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class AppEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shh13-api-")
        cls.files_dir = os.path.join(cls.tmp, "files")
        cls.outside = os.path.join(cls.tmp, "outside")
        os.makedirs(os.path.join(cls.files_dir, "sub"))
        os.makedirs(cls.outside)
        write_file(os.path.join(cls.files_dir, "hello.txt"), "hello-mcp")
        write_file(os.path.join(cls.files_dir, "big.txt"), "x" * 4096)
        write_file(os.path.join(cls.files_dir, "sub", "inner.txt"), "inner")
        write_file(os.path.join(cls.outside, "secret.txt"), "TOP-SECRET-PAYLOAD-9f3a")

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = AppPaths(APP_ID, install_dir=repo, data_dir=os.path.join(cls.tmp, "data"))
        cls.app = app_main.create_app(paths=paths, log_level="ERROR")
        cls.app.set_allowed_roots([cls.files_dir])
        cls.server = cls.app.run(host="127.0.0.1", port=0, background=True)
        cls.client = ApiClient("http://127.0.0.1:%d" % cls.server.server_address[1])
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.shutdown()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 每个用例前把权限级别与白名单复位，避免相互影响
        self.client.post("/api/mcp/level", {"level": "READ_ONLY"})
        self.client.post("/api/settings", {"allowed_roots": [self.files_dir]})

    # ---------------------------------------------------------- 基础

    def test_health(self):
        status, body = self.client.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["app"], APP_ID)

    def test_index_html_uses_relative_paths(self):
        status, body = self.client.get("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"mcp.js", body)
        self.assertNotIn(b'src="/', body)

    def test_prefix_compatibility(self):
        for prefix in ("", "/" + APP_ID, "/v2/proxy/" + APP_ID):
            status, _body = self.client.get(prefix + "/api/mcp/status")
            self.assertEqual(status, 200, prefix)

    # ---------------------------------------------------------- MCP over HTTP

    def test_mcp_initialize(self):
        status, body = self.client.rpc("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", body["result"]["capabilities"])
        self.assertTrue(body["result"]["serverInfo"]["name"])

    def test_mcp_tools_list_includes_both_ends(self):
        _status, body = self.client.rpc("tools/list")
        names = {tool["name"] for tool in body["result"]["tools"]}
        self.assertIn("read_text_file", names)
        self.assertIn("delete_file", names)

    def test_mcp_tools_call_read_works(self):
        _status, body = self.client.rpc("tools/call", {
            "name": "read_text_file",
            "arguments": {"path": os.path.join(self.files_dir, "hello.txt")},
        })
        self.assertFalse(body["result"]["isError"])
        self.assertIn("hello-mcp", body["result"]["content"][0]["text"])

    def test_mcp_call_outside_whitelist_masked(self):
        """白名单外的读取必须被拒，且**文件内容一个字都不能回到调用方**。

        注意断言的是「内容」而不是「路径」—— 错误消息里出现路径是正常且必要的
        （否则用户不知道该把哪个目录加进白名单），但内容绝不能泄漏。
        """
        _status, body = self.client.rpc("tools/call", {
            "name": "read_text_file",
            "arguments": {"path": os.path.join(self.outside, "secret.txt")},
        })
        self.assertTrue(body["result"]["isError"])
        text = body["result"]["content"][0]["text"]
        self.assertNotIn("TOP-SECRET-PAYLOAD-9f3a", text)
        self.assertIn("不可访问", text)

    def test_mcp_dangerous_tool_denied_by_default(self):
        _status, body = self.client.rpc("tools/call", {
            "name": "delete_file",
            "arguments": {"path": os.path.join(self.files_dir, "hello.txt")},
        })
        self.assertTrue(body["result"]["isError"])
        self.assertTrue(os.path.exists(os.path.join(self.files_dir, "hello.txt")))

    def test_mcp_notification_accepted_without_body(self):
        status, _body = self.client.post("/mcp", {"jsonrpc": "2.0",
                                                  "method": "notifications/initialized"})
        self.assertEqual(status, 202)

    def test_mcp_get_rejected(self):
        status, _body = self.client.get("/mcp")
        self.assertEqual(status, 405)

    def test_mcp_sse_when_requested(self):
        status, body = self.client.post(
            "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            raw=True, extra_headers={"Accept": "text/event-stream"})
        self.assertEqual(status, 200)
        self.assertIn(b"event: message", body)
        self.assertIn(b"data: ", body)

    # ---------------------------------------------------------- 管理接口

    def test_status_endpoint(self):
        status, body = self.client.get("/api/mcp/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["level"], "READ_ONLY")
        self.assertGreater(body["tool_count"], 10)
        self.assertIn(self.files_dir, body["allowed_roots"])

    def test_tools_endpoint_marks_availability(self):
        status, body = self.client.get("/api/mcp/tools")
        self.assertEqual(status, 200)
        flat = [tool for items in body["groups"].values() for tool in items]
        by_name = {tool["name"]: tool for tool in flat}
        self.assertTrue(by_name["read_text_file"]["available"])
        self.assertFalse(by_name["delete_file"]["available"])

    def test_level_change_takes_effect(self):
        status, body = self.client.post("/api/mcp/level", {"level": "READ_WRITE"})
        self.assertEqual(status, 200)
        self.assertEqual(body["level"], "READ_WRITE")
        _status, body = self.client.get("/api/mcp/tools")
        flat = [tool for items in body["groups"].values() for tool in items]
        by_name = {tool["name"]: tool for tool in flat}
        self.assertTrue(by_name["move_file"]["available"])
        self.assertFalse(by_name["delete_file"]["available"], "危险工具在可写级别下仍应关闭")

    def test_invalid_level_rejected(self):
        status, body = self.client.post("/api/mcp/level", {"level": "ROOT"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_audit_records_successful_calls(self):
        self.client.rpc("tools/call", {
            "name": "get_file_info",
            "arguments": {"path": os.path.join(self.files_dir, "hello.txt")}})
        status, body = self.client.get("/api/mcp/audit?limit=50")
        self.assertEqual(status, 200)
        self.assertTrue(any(entry["tool"] == "get_file_info" for entry in body["entries"]))
        self.assertTrue(all("time" in entry for entry in body["entries"]))

    def test_audit_records_denials(self):
        self.client.rpc("tools/call", {
            "name": "delete_file",
            "arguments": {"path": os.path.join(self.files_dir, "hello.txt")}})
        _status, body = self.client.get("/api/mcp/audit?denied=true&limit=50")
        self.assertTrue(any(entry["tool"] == "delete_file" for entry in body["entries"]))
        self.assertTrue(all(not entry["allowed"] for entry in body["entries"]))

    def test_webui_test_call_is_gated_too(self):
        status, body = self.client.post("/api/mcp/test", {
            "tool": "delete_file",
            "arguments": {"path": os.path.join(self.files_dir, "hello.txt")}})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"], "界面上的试调也必须走权限门")

    def test_config_endpoint(self):
        status, body = self.client.get("/api/mcp/config")
        self.assertEqual(status, 200)
        self.assertIn("/v2/proxy/", body["http"]["url"])
        self.assertIn("mcpServers", body["http"]["sample"])
        self.assertIn("--mcp-stdio", body["stdio"]["args"])
        self.assertTrue(body["tools"])

    def test_whitelist_change(self):
        status, _body = self.client.post("/api/settings",
                                         {"allowed_roots": [self.files_dir, self.outside]})
        self.assertEqual(status, 200)
        _status, body = self.client.get("/api/mcp/status")
        self.assertIn(self.outside, body["allowed_roots"])

    def test_dangerous_level_enables_recycle_flow(self):
        """提到危险级别后，delete_file 应当**移入回收站**而不是永久删除。"""
        target = os.path.join(self.files_dir, "to-recycle.txt")
        write_file(target, "payload")
        self.client.post("/api/mcp/level", {"level": "DANGEROUS"})
        _status, body = self.client.rpc("tools/call", {
            "name": "delete_file", "arguments": {"path": target, "reason": "端到端测试"}})
        self.assertFalse(body["result"]["isError"], body["result"])
        self.assertFalse(os.path.exists(target), "原位置应已不存在")

        status, listing = self.client.get("/api/mcp/recycle")
        self.assertEqual(status, 200)
        self.assertTrue(listing["items"], "回收站里应当有记录")
        item = listing["items"][0]
        self.assertTrue(item["exists"], "回收站里的文件应当真的在")
        with open(item["stored"], encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "payload")

        # 还原
        status, restored = self.client.post("/api/mcp/recycle/restore",
                                            {"item_ids": [item["id"]]})
        self.assertEqual(status, 200)
        self.assertEqual(restored["restored"], 1)
        self.assertTrue(os.path.exists(target), "还原后文件应回到原位置")

    def test_job_end_to_end(self):
        status, body = self.client.post("/api/jobs", {
            "type": "folder_size", "params": {"path": self.files_dir}})
        self.assertEqual(status, 201)
        job_id = body["job"]["id"]
        deadline = time.time() + 30
        job = None
        while time.time() < deadline:
            _status, payload = self.client.get("/api/jobs/%d" % job_id)
            job = payload["job"]
            if job["state"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        self.assertEqual(job["state"], "completed", json.dumps(job, ensure_ascii=False))
        self.assertGreaterEqual(job["result"]["file_count"], 3)

    def test_folder_size_refuses_outside_whitelist(self):
        status, body = self.client.post("/api/jobs", {
            "type": "folder_size", "params": {"path": self.outside}})
        self.assertEqual(status, 201)
        job_id = body["job"]["id"]
        deadline = time.time() + 30
        job = None
        while time.time() < deadline:
            _status, payload = self.client.get("/api/jobs/%d" % job_id)
            job = payload["job"]
            if job["state"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        self.assertEqual(job["state"], "failed")
        self.assertIn("允许访问", job["error"])

    def test_unknown_job_type(self):
        status, _body = self.client.post("/api/jobs", {"type": "nope"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()

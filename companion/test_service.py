import copy
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import json
import os
from pathlib import Path
import select
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

import service


def sample(external="chat-a", channel="manual"):
    return {"channel": channel, "source": "test-import",
            "conversation": {"external_id": external, "title": "测试会话", "recipient": external},
            "messages": [{"external_id": "message-1", "sender": "测试对方", "direction": "incoming",
                          "text": "请整理演示材料。<script>alert(1)</script>", "timestamp": "2026-01-01T00:00:00Z"}]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve() / "private"
        self.store = service.Store(self.directory)
        self.addCleanup(self.store.close)

    def test_import_deduplicates_and_preserves_text(self):
        first = self.store.import_messages(sample())
        second = self.store.import_messages(sample())
        self.assertEqual((first["inserted"], second["inserted"], second["duplicates"]), (1, 0, 1))
        messages = self.store.read_messages(first["conversation_id"])
        self.assertEqual(messages["messages"][0]["text"], sample()["messages"][0]["text"])
        self.assertTrue(messages["untrusted_text"])
        self.assertEqual(messages["messages"][0]["source"], "test-import")

    def test_content_hash_deduplicates_without_message_id(self):
        payload = sample()
        del payload["messages"][0]["external_id"]
        self.store.import_messages(payload)
        self.assertEqual(self.store.import_messages(payload)["duplicates"], 1)

    def test_import_conflict_rolls_back_whole_batch(self):
        first = self.store.import_messages(sample())
        payload = sample()
        second = copy.deepcopy(payload["messages"][0])
        second["external_id"] = "second"
        payload["messages"].insert(0, second)
        payload["messages"][1]["text"] = "attempt to replace known message"
        with self.assertRaisesRegex(service.ServiceError, "相同消息 ID"):
            self.store.import_messages(payload)
        self.assertEqual(len(self.store.read_messages(first["conversation_id"])["messages"]), 1)

    def test_channels_are_separate_and_recipient_cannot_change(self):
        one = self.store.import_messages(sample())
        two = self.store.import_messages(sample(channel="testprobe"))
        self.assertNotEqual(one["conversation_id"], two["conversation_id"])
        payload = sample()
        payload["conversation"]["recipient"] = "another-person"
        with self.assertRaises(service.ServiceError):
            self.store.import_messages(payload)
        self.assertEqual(len(self.store.list_conversations()), 2)

    def test_analysis_and_draft_reject_cross_conversation_references(self):
        one = self.store.import_messages(sample())
        two = self.store.import_messages(sample("chat-b"))
        other_message = self.store.read_messages(two["conversation_id"])["messages"][0]["id"]
        with self.assertRaises(service.ServiceError):
            self.store.save_analysis({"conversation_id": one["conversation_id"], "summary": "跨会话内容",
                                      "message_ids": [other_message]})
        with self.assertRaises(service.ServiceError):
            self.store.create_reply_draft({"conversation_id": one["conversation_id"], "text": "不应保存",
                                           "message_ids": [other_message]})
        with self.assertRaises(service.ServiceError):
            self.store.read_messages(one["conversation_id"], before_id=other_message)
        self.assertEqual(self.store.list_actions(), [])

    def test_unknown_recipient_and_untrusted_status_are_rejected(self):
        first = self.store.import_messages(sample())
        for extra in ({"recipient": "unknown"}, {"status": "sent"}):
            with self.assertRaises(service.ServiceError):
                self.store.create_reply_draft({"conversation_id": first["conversation_id"], "text": "草稿", **extra})
        with self.assertRaises(service.ServiceError):
            self.store.create_reply_draft({"conversation_id": "unknown", "text": "草稿"})
        self.assertEqual(self.store.list_actions(), [])

    def test_full_workflow_persists_and_approval_never_sends(self):
        first = self.store.import_messages(sample())
        conversation = first["conversation_id"]
        analysis = self.store.save_analysis({"conversation_id": conversation, "summary": "准备演示材料",
                                             "todos": ["整理功能清单", {"title": "确认时间", "done": False}]})
        self.assertEqual(len(analysis["message_ids"]), 1)
        draft = self.store.create_reply_draft({"conversation_id": conversation, "text": "收到，我明天给出初稿。"})
        self.assertEqual(draft["status"], "draft")
        approved = self.store.approve_draft(draft["id"])
        self.assertEqual(approved["status"], "ready")
        self.assertFalse(approved["delivery_verified"])
        self.assertEqual(self.store.approve_draft(draft["id"])["approved_at"], approved["approved_at"])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db:
                self.store.db.execute("UPDATE actions SET status='sent' WHERE id=?", (draft["id"],))
        reopened = service.Store(self.directory)
        try:
            state = reopened.state(conversation)
            self.assertEqual(state["analyses"][0]["summary"], analysis["summary"])
            self.assertEqual(state["actions"][0]["status"], "ready")
            self.assertFalse(state["transport"]["can_send"])
        finally:
            reopened.close()
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)

    def test_demo_is_labelled_and_idempotent(self):
        demo = self.store.load_demo()
        again = self.store.load_demo()
        self.assertTrue(demo["synthetic"])
        self.assertEqual(len(again["state"]["messages"]), 2)
        self.assertEqual(again["state"]["conversations"][0]["channel"], "demo")
        self.assertIn("合成", again["state"]["conversations"][0]["title"])

    def test_concurrent_connections_do_not_rebind_recipient_or_duplicate_messages(self):
        other = service.Store(self.directory)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                for index in range(8):
                    barrier = threading.Barrier(2)
                    left, right = sample("race-" + str(index)), sample("race-" + str(index))
                    if index % 2:
                        right["conversation"]["recipient"] = "different-recipient"
                    def run(store, payload):
                        barrier.wait(timeout=2)
                        try:
                            return store.import_messages(payload)
                        except service.ServiceError as error:
                            return {"error": error.code}
                    futures = [executor.submit(run, self.store, left), executor.submit(run, other, right)]
                    values = [future.result(timeout=8) for future in futures]
                    if index % 2:
                        self.assertEqual(sum(item.get("error") == "recipient_conflict" for item in values), 1)
                        self.assertEqual(sum(item.get("inserted", 0) for item in values), 1)
                    else:
                        self.assertEqual(sorted(item["inserted"] for item in values), [0, 1])
                        self.assertEqual(sorted(item["duplicates"] for item in values), [0, 1])
        finally:
            other.close()

    def test_new_draft_does_not_inherit_or_change_previous_approval(self):
        cid = self.store.import_messages(sample())["conversation_id"]
        first = self.store.create_reply_draft({"conversation_id": cid, "text": "最初正文"})
        self.store.approve_draft(first["id"])
        edited = self.store.create_reply_draft({"conversation_id": cid, "text": "编辑后的新正文"})
        self.assertNotEqual(first["id"], edited["id"])
        self.assertEqual(edited["status"], "draft")
        self.assertIsNone(edited["approved_at"])
        previous = next(item for item in self.store.list_actions(cid) if item["id"] == first["id"])
        self.assertEqual((previous["text"], previous["status"]), ("最初正文", "ready"))


class MCPTests(unittest.TestCase):
    setUp = StoreTests.setUp

    def initialize(self, mcp):
        result = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        self.assertEqual(result["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_lifecycle_and_context_to_draft_tool_chain(self):
        cid = self.store.import_messages(sample())["conversation_id"]
        mcp = service.MCP(self.store)
        premature = mcp.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
        self.assertEqual(premature["error"]["code"], -32002)
        self.initialize(mcp)
        tools = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"list_conversations", "read_messages", "save_analysis", "create_reply_draft", "list_actions"})
        response = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "read_messages", "arguments": {"conversation_id": cid}}})
        context = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(context["untrusted_text"])
        for name, args in (("save_analysis", {"summary": "准备演示", "todos": ["提供初稿"]}),
                           ("create_reply_draft", {"text": "收到，周五前给出初稿。"})):
            response = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": name, "arguments": {"conversation_id": cid, **args}}})
            self.assertFalse(response["result"]["isError"])
        self.assertEqual(self.store.list_actions()[0]["status"], "draft")

    def test_notifications_cannot_mutate_and_invalid_request_has_error(self):
        cid = self.store.import_messages(sample())["conversation_id"]
        mcp = service.MCP(self.store)
        self.initialize(mcp)
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {
            "name": "create_reply_draft", "arguments": {"conversation_id": cid, "text": "应被忽略"}}}))
        self.assertEqual(self.store.list_actions(), [])
        self.assertEqual(mcp.handle([])["error"]["code"], -32600)
        self.assertEqual(mcp.handle({"jsonrpc": "2.0", "id": True, "method": "ping"})["error"]["code"], -32600)

    def test_stdio_contains_json_only_and_parse_errors_are_bounded(self):
        output = io.StringIO()
        service.run_mcp(self.store, io.BytesIO(b'not json\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), output)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["result"], {})
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":"\\ud800"}'):
            with self.assertRaises((ValueError, UnicodeError)):
                service.strict_json(raw)

    def test_stdio_subprocess_can_reopen_shared_database(self):
        cid = self.store.import_messages(sample())["conversation_id"]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": service.PROTOCOL, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read_messages", "arguments": {"conversation_id": cid}}}]
        result = subprocess.run([sys.executable, str(Path(service.__file__).resolve()), "--mcp", "--data-dir", str(self.directory)],
                                input="".join(json.dumps(item) + "\n" for item in requests), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        messages = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(messages), 2)
        self.assertEqual(json.loads(messages[-1]["result"]["content"][0]["text"])["conversation"]["id"], cid)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.web = root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<html>synthetic UI fixture</html>")
        self.store = service.Store(root / "data")
        self.server = service.make_server(self.store, 0, self.web, "synthetic-test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()

    def request(self, method, path, payload=None, headers=None, auth=True):
        headers = dict(headers or {})
        if auth:
            headers.setdefault("Authorization", "Bearer synthetic-test-token")
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            value = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw.decode()
            return response.status, value, dict(response.getheaders())
        finally:
            conn.close()

    def test_http_import_analysis_draft_approve_and_config(self):
        status, result, _ = self.request("POST", "/api/import", sample())
        self.assertEqual(status, 200)
        cid = result["conversation_id"]
        status, result, _ = self.request("POST", "/api/analysis", {"conversation_id": cid, "summary": "摘要", "todos": ["任务"]})
        self.assertEqual(status, 200)
        status, result, _ = self.request("POST", "/api/drafts", {"conversation_id": cid, "text": "仅为草稿"})
        self.assertEqual(status, 200)
        identifier = result["action"]["id"]
        status, _, _ = self.request("POST", "/api/drafts/" + identifier + "/approve", {"text": "审批时不能偷偷换正文"})
        self.assertEqual(status, 400)
        status, result, _ = self.request("POST", "/api/drafts/" + identifier + "/approve", {})
        self.assertEqual((status, result["action"]["status"]), (200, "ready"))
        self.assertFalse(result["action"]["delivery_verified"])
        status, result, _ = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertFalse(result["model_connected"])
        self.assertIn("--mcp", result["mcp_config"]["mcpServers"]["wechat_workbench"]["args"])

    def test_host_origin_and_token_boundaries(self):
        self.assertEqual(self.request("GET", "/api/state", auth=False)[0], 401)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/demo", {}, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/demo", {}, headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.store.list_conversations(), [])

    def test_size_type_traversal_and_send_endpoint_are_rejected(self):
        self.assertEqual(self.request("POST", "/api/demo", {}, headers={"Content-Length": str(service.MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.request("POST", "/api/demo", {}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("GET", "/../data/workbench.sqlite3")[0], 404)
        self.assertEqual(self.request("GET", "/%2e%2e/data/workbench.sqlite3")[0], 404)
        self.assertEqual(self.request("POST", "/api/send", {"recipient": "unknown", "text": "不应发送"})[0], 404)

    def test_static_page_has_no_token_and_security_headers(self):
        status, body, headers = self.request("GET", "/", auth=False)
        self.assertEqual(status, 200)
        self.assertNotIn("synthetic-test-token", body)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_launcher_receives_one_startup_json_line(self):
        directory = Path(self.temp.name).resolve() / "launcher-data"
        child = subprocess.Popen([sys.executable, str(Path(service.__file__).resolve()),
                                  "--serve", "--port", "0", "--data-dir", str(directory),
                                  "--web-root", str(self.web)], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            readable, _, _ = select.select([child.stdout], [], [], 5)
            self.assertTrue(readable, "startup JSON timed out")
            startup = json.loads(child.stdout.readline())
            self.assertEqual(startup["pid"], child.pid)
            self.assertEqual(startup["url"], startup["api_origin"] + "/#token=" + startup["token"])
            conn = http.client.HTTPConnection("127.0.0.1", startup["port"], timeout=3)
            try:
                conn.request("GET", "/api/state", headers={"Authorization": "Bearer " + startup["token"]})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertFalse(json.loads(response.read())["transport"]["can_send"])
            finally:
                conn.close()
        finally:
            if child.poll() is None:
                child.terminate()  # Only the temporary service process created by this test.
            remaining, diagnostic = child.communicate(timeout=5)
        self.assertEqual(remaining, "")
        self.assertEqual(diagnostic, "")


if __name__ == "__main__":
    unittest.main()

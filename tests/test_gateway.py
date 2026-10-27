"""Protocol and executor checks use an isolated fake ACP child, never WorkBuddy."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from buddygate.acp import AcpClient
from buddygate.cli import approve_task
from buddygate.store import Store
from buddygate.worker import execute, safe_text


MOCK_ACP = r'''
import json, os, sys
from pathlib import Path

mode = os.environ.get("BUDDYGATE_MOCK_MODE", "complete")
log = Path(os.environ["BUDDYGATE_MOCK_LOG"])
def record(value):
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\n")
def send(value):
    print(json.dumps(value), flush=True)
def answer(request, result):
    send({"jsonrpc":"2.0", "id":request["id"], "result":result})
def update(kind, text):
    send({"jsonrpc":"2.0", "method":"session/update", "params":{
        "sessionId":"mock-session-stable", "update":{"sessionUpdate":kind,
        "content":{"type":"text", "text":text}}}})

for line in sys.stdin:
    request = json.loads(line)
    record(request)
    method = request.get("method")
    if method == "initialize":
        answer(request, {"protocolVersion":1, "agentCapabilities":{"loadSession":True}})
    elif method == "session/new":
        answer(request, {"sessionId":"mock-session-stable"})
    elif method == "session/load":
        answer(request, {})
    elif method == "session/prompt":
        update("agent_thought_chunk", "PRIVATE_REASONING_MUST_NEVER_ESCAPE")
        if mode == "crash":
            update("agent_message_chunk", "Partial visible work")
            sys.exit(7)
        if mode == "permission":
            send({"jsonrpc":"2.0", "id":"permission-request", "method":"session/request_permission", "params":{
                "sessionId":"mock-session-stable", "toolCall":{"title":"Write requested fixture"},
                "options":[{"optionId":"one", "kind":"allow_once", "name":"Allow once"},
                           {"optionId":"no", "kind":"reject_once", "name":"Reject once"},
                           {"optionId":"forever", "kind":"allow_always", "name":"Always allow"}]}})
            reply = json.loads(sys.stdin.readline())
            record(reply)
        if mode == "foreign-session":
            send({"jsonrpc":"2.0", "method":"session/update", "params":{
                "sessionId":"foreign-session", "update":{"sessionUpdate":"agent_message_chunk",
                "content":{"type":"text", "text":"FOREIGN_SESSION_RESULT_MUST_NOT_ESCAPE"}}}})
            send({"jsonrpc":"2.0", "id":"foreign-permission", "method":"session/request_permission", "params":{
                "sessionId":"foreign-session", "toolCall":{"title":"Foreign session operation"},
                "options":[{"optionId":"one", "kind":"allow_once", "name":"Allow once"}]}})
            record(json.loads(sys.stdin.readline()))
        if mode == "no-once-options":
            send({"jsonrpc":"2.0", "id":"permanent-permission", "method":"session/request_permission", "params":{
                "sessionId":"mock-session-stable", "toolCall":{"title":"Permanent permission only"},
                "options":[{"optionId":"forever", "kind":"allow_always", "name":"Always allow"},
                           {"optionId":"never", "kind":"reject_always", "name":"Always reject"}]}})
            record(json.loads(sys.stdin.readline()))
        if mode == "cancel":
            update("agent_message_chunk", "Waiting for cancellation")
            for pending in sys.stdin:
                pending = json.loads(pending)
                record(pending)
                if pending.get("method") == "session/cancel":
                    break
        if mode == "unknown-capability":
            send({"jsonrpc":"2.0", "id":"forbidden-client-request", "method":"fs/write_text_file", "params":{"path":"outside"}})
            record(json.loads(sys.stdin.readline()))
        update("agent_message_chunk", "Visible actual result; api_key=NOT_A_REAL_SECRET")
        answer(request, {"stopReason":"end_turn"})
    elif method == "echo":
        answer(request, request.get("params", {}))
'''


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.environ.get("BUDDYGATE_TEST_TEMP", tempfile.gettempdir())).resolve()
        self.root = self.base / f"buddygate-gateway-test-{uuid.uuid4()}"
        self.root.mkdir()
        self.addCleanup(self.cleanup_root)
        self.script = self.root / "fake_acp.py"
        self.script.write_text(MOCK_ACP, encoding="utf-8")
        self.log = self.root / "acp-events.jsonl"
        self.store = Store(self.root / "tasks.sqlite3")
        self.environment = {"supported": True, "node": sys.executable, "cli": str(self.script)}

    def cleanup_root(self):
        resolved = self.root.resolve()
        if resolved.parent != self.base or not resolved.name.startswith("buddygate-gateway-test-"):
            raise RuntimeError("Refusing cleanup outside the allocated test directory")
        shutil.rmtree(resolved)

    def task(self):
        task = self.store.submit_task(str(self.root), "Create fixture report", ["No publication"], timeout_seconds=15)
        claimed = self.store.claim_next("unit-test-worker")
        self.assertEqual(task["id"], claimed["id"])
        return claimed

    def env(self, mode):
        return mock.patch.dict(os.environ, {"BUDDYGATE_MOCK_MODE": mode, "BUDDYGATE_MOCK_LOG": str(self.log)})

    def records(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def wait_status(self, task_id, status):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            task = self.store.get_task(task_id)
            if task["status"] == status:
                return task
            time.sleep(0.03)
        self.fail(f"Task never reached {status}; current={self.store.get_task(task_id)['status']}")

    def threaded_execute(self, task):
        errors = []
        def target():
            try:
                execute(self.store, task, self.environment)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=target)
        thread.start()
        self.addCleanup(lambda: thread.join(timeout=20))
        return thread, errors

    def test_stdio_mcp_handshake_tools_and_invalid_arguments(self):
        messages = [
            {"jsonrpc":"2.0", "id":1, "method":"initialize", "params":{"protocolVersion":"2025-06-18"}},
            {"jsonrpc":"2.0", "method":"notifications/initialized"},
            {"jsonrpc":"2.0", "id":2, "method":"tools/list"},
            {"jsonrpc":"2.0", "id":3, "method":"tools/call", "params":{"name":"buddy_list", "arguments":{}}},
            {"jsonrpc":"2.0", "id":4, "method":"tools/call", "params":{"name":"buddy_submit", "arguments":{"workspace":str(self.root), "objective":"x", "timeout_seconds":True}}},
            {"jsonrpc":"2.0", "id":5, "method":"tools/call", "params":{"name":"buddy_get", "arguments":{}}},
            {"jsonrpc":"2.0", "id":6, "method":"ping"},
        ]
        environment = os.environ.copy()
        environment["BUDDYGATE_STATE_DIR"] = str(self.root / "mcp-state")
        process = subprocess.run([sys.executable, "-m", "buddygate", "mcp"],
            input="".join(json.dumps(message) + "\n" for message in messages),
            capture_output=True, encoding="utf-8", timeout=15, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = {reply["id"]: reply for reply in map(json.loads, process.stdout.splitlines())}
        self.assertEqual(set(replies), {1,2,3,4,5,6})
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("buddy_approve", [item["name"] for item in replies[2]["result"]["tools"]])
        self.assertEqual(json.loads(replies[3]["result"]["content"][0]["text"]), [])
        self.assertTrue(replies[4]["result"]["isError"])
        self.assertTrue(replies[5]["result"]["isError"])
        self.assertEqual(replies[6]["result"], {})

    def test_malformed_mcp_params_do_not_kill_server(self):
        environment = os.environ.copy()
        environment["BUDDYGATE_STATE_DIR"] = str(self.root / "mcp-state")
        messages = [
            {"jsonrpc":"2.0", "id":1, "method":"initialize", "params":[1]},
            {"jsonrpc":"2.0", "id":2, "method":"tools/call", "params":"bad"},
            {"jsonrpc":"2.0", "id":3, "method":"ping"},
        ]
        process = subprocess.run([sys.executable, "-m", "buddygate", "mcp"],
            input="".join(json.dumps(message) + "\n" for message in messages),
            capture_output=True, encoding="utf-8", timeout=15, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = list(map(json.loads, process.stdout.splitlines()))
        self.assertIn("error", replies[0])
        self.assertIn("error", replies[1])
        self.assertEqual(replies[-1]["result"], {})

    def test_real_acp_child_transports_messages_but_excludes_private_thought(self):
        task = self.task()
        with self.env("complete"):
            execute(self.store, task, self.environment)
        completed = self.store.get_task(task["id"])
        self.assertEqual(completed["status"], "needs_review")
        self.assertTrue(completed["stop_confirmed"])
        self.assertIn("Visible actual result", completed["result"]["text"])
        serialized = json.dumps({"task":completed, "events":self.store.get_events(task["id"])})
        self.assertNotIn("PRIVATE_REASONING_MUST_NEVER_ESCAPE", serialized)
        self.assertNotIn("NOT_A_REAL_SECRET", serialized)
        self.assertEqual([item["method"] for item in self.records() if "method" in item], ["initialize", "session/new", "session/prompt"])

    def test_permission_only_exposes_once_and_returns_actual_choice(self):
        task = self.task()
        with self.env("permission"):
            thread, errors = self.threaded_execute(task)
            waiting = self.wait_status(task["id"], "waiting_permission")
            permission = waiting["permission"]
            self.assertEqual({item["kind"] for item in permission["options"]}, {"allow_once", "reject_once"})
            with self.assertRaisesRegex(ValueError, "one-time"):
                approve_task(self.store, task["id"], permission["id"], "forever", "Not authorized")
            approve_task(self.store, task["id"], permission["id"], "one", "User authorized the fixture write")
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        replies = [item for item in self.records() if item.get("id") == "permission-request" and "result" in item]
        self.assertEqual(replies[0]["result"]["outcome"], {"outcome":"selected", "optionId":"one"})
        self.assertEqual(self.store.get_task(task["id"])["status"], "needs_review")

    def test_cancel_notifies_real_child_and_waits_for_terminal_response(self):
        task = self.task()
        with self.env("cancel"):
            thread, errors = self.threaded_execute(task)
            self.wait_status(task["id"], "running")
            # Wait for prompt receipt, not merely the initial claimed state.
            until = time.monotonic() + 8
            while time.monotonic() < until:
                if self.log.exists() and any(item.get("method") == "session/prompt" for item in self.records()):
                    break
                time.sleep(0.03)
            else:
                self.fail("Fake ACP child never received a prompt")
            self.store.request_cancel(task["id"])
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        result = self.store.get_task(task["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["stop_confirmed"])
        self.assertTrue(any(item.get("method") == "session/cancel" for item in self.records()))

    def test_child_crash_becomes_uncertain_and_never_replays(self):
        task = self.task()
        with self.env("crash"):
            execute(self.store, task, self.environment)
        failed = self.store.get_task(task["id"])
        self.assertEqual(failed["status"], "uncertain")
        self.assertFalse(failed["stop_confirmed"])
        self.store.submit_task(str(self.root), "Subsequent task")
        self.assertIsNone(Store(self.store.db_path).claim_next("replacement-worker"))
        self.assertEqual(sum(item.get("method") == "session/prompt" for item in self.records()), 1)

    def test_foreign_session_update_and_permission_are_rejected(self):
        task = self.task()
        with self.env("foreign-session"):
            execute(self.store, task, self.environment)
        completed = self.store.get_task(task["id"])
        events = self.store.get_events(task["id"])
        self.assertEqual(completed["status"], "needs_review")
        self.assertNotIn("FOREIGN_SESSION_RESULT_MUST_NOT_ESCAPE", json.dumps({"task":completed, "events":events}))
        self.assertFalse(any(event["kind"] == "permission_requested" for event in events))
        reply = next(item for item in self.records() if item.get("id") == "foreign-permission" and "result" in item)
        self.assertEqual(reply["result"], {"outcome":{"outcome":"cancelled"}})

    def test_permission_without_once_option_is_denied_without_waiting(self):
        task = self.task()
        with self.env("no-once-options"):
            execute(self.store, task, self.environment)
        completed = self.store.get_task(task["id"])
        self.assertEqual(completed["status"], "needs_review")
        self.assertFalse(any(event["kind"] == "permission_requested" for event in self.store.get_events(task["id"])))
        reply = next(item for item in self.records() if item.get("id") == "permanent-permission" and "result" in item)
        self.assertEqual(reply["result"], {"outcome":{"outcome":"cancelled"}})

    def test_rework_loads_same_session_and_retains_constraints(self):
        task = self.task()
        with self.env("complete"):
            execute(self.store, task, self.environment)
            reworked = self.store.rework_task(task["id"], "Correct the fixture date")
            execute(self.store, self.store.claim_next("worker-round-two"), self.environment)
        records = self.records()
        self.assertEqual(sum(item.get("method") == "session/new" for item in records), 1)
        loads = [item for item in records if item.get("method") == "session/load"]
        self.assertEqual(loads[0]["params"]["sessionId"], "mock-session-stable")
        prompts = [item["params"]["prompt"][0]["text"] for item in records if item.get("method") == "session/prompt"]
        self.assertIn("No publication", prompts[1])
        self.assertIn("Correct the fixture date", prompts[1])
        self.assertEqual(reworked["attempt"], 2)
        self.assertEqual(self.store.get_task(task["id"])["result"]["attempt"], 2)

    def test_unknown_acp_client_capability_is_denied(self):
        with self.env("unknown-capability"):
            client = AcpClient([sys.executable, str(self.script)], str(self.root), lambda item: None, lambda item: self.fail("Unexpected permission"), os.environ.copy())
            try:
                client.request("initialize", {}, timeout=5)
                client.request("session/prompt", {"sessionId":"mock-session-stable"}, timeout=5)
            finally:
                client.close()
        reply = next(item for item in self.records() if item.get("id") == "forbidden-client-request" and "error" in item)
        self.assertEqual(reply["error"]["code"], -32601)

    def test_redaction_covers_headers_json_fields_and_github_tokens(self):
        secret = "FAKE_CREDENTIAL_FOR_TEST_ONLY"
        token = "ghp_" + "A" * 30
        samples = [f"Authorization: Bearer {secret}", json.dumps({"authorization":"Bearer " + secret}),
                   json.dumps({"api_key":secret}), f"token={token}"]
        for sample in samples:
            with self.subTest(sample=sample):
                redacted = safe_text(sample)
                self.assertNotIn(secret, redacted)
                self.assertNotIn(token, redacted)


if __name__ == "__main__":
    unittest.main()

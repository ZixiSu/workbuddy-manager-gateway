"""Waiting tests: local observation, MCP concurrency, and no business side effects.

Every test uses an isolated state directory. The subprocess MCP tests start the
real server, wait on a task that no executor is running, and never dispatch a
prompt or call a model.
"""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from buddygate import waiting
from buddygate.mcp import MAX_CONCURRENT_WAITS
from buddygate.store import Store


class WaitCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="buddygate-wait-")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state = self.base / "state"
        self.state.mkdir()
        self.store = Store(self.state / "tasks.sqlite3")
        patcher = mock.patch.dict(os.environ, {"BUDDYGATE_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def task(self, status=None, stop_confirmed=False, **changes):
        task = self.store.submit_task(str(self.base), "Wait fixture task")
        fields = dict(changes)
        if status is not None:
            fields["status"] = status
        if stop_confirmed:
            fields["stop_confirmed"] = True
        if fields:
            task = self.store.update_task(task["id"], **fields)
        return task

    def wait(self, task_id, **arguments):
        from buddygate.cli import invoke

        arguments.setdefault("wait_seconds", 5)
        arguments.setdefault("poll_interval", 0.05)
        arguments["task_id"] = task_id
        return invoke("buddy_wait", arguments)

    def events(self, task_id):
        return self.store.get_events(task_id)


class WaitOutcomeTests(WaitCase):
    def test_reviewable_attempt_returns_once_without_accepting(self):
        task = self.task(status="needs_review", stop_confirmed=True, result={"text": "wrote report.txt"})
        before = self.store.get_task(task["id"])
        events_before = len(self.events(task["id"]))

        payload = self.wait(task["id"])

        self.assertEqual(payload["outcome"], "ready_for_review")
        self.assertTrue(payload["settled"])
        self.assertTrue(payload["review_ready"])
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["model_calls_during_wait"], 0)
        self.assertFalse(payload["prompt_resent"])
        self.assertFalse(payload["tool_stream_included"])
        self.assertIn("wrote report.txt", payload["result_excerpt"])
        self.assertEqual(self.store.get_task(task["id"]), before, "waiting must not modify the record")
        self.assertEqual(len(self.events(task["id"])), events_before, "waiting must not add events")

    def test_permission_is_reported_without_a_decision(self):
        task = self.task(status="waiting_permission", permission={
            "id": "perm-1", "source": "desktop", "decision": None,
            "options": [{"optionId": "one", "kind": "allow_once", "name": "Allow once"},
                        {"optionId": "two", "kind": "reject_once", "name": "Reject"}]})
        events_before = len(self.events(task["id"]))

        payload = self.wait(task["id"])

        self.assertEqual(payload["outcome"], "needs_attention")
        self.assertEqual(payload["detail"], "permission_pending")
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["permission_pending"]["id"], "perm-1")
        self.assertFalse(payload["permission_pending"]["decided"])
        current = self.store.get_task(task["id"])
        self.assertEqual(current["status"], "waiting_permission")
        self.assertIsNone(current["permission"]["decision"], "waiting must not decide a permission")
        self.assertEqual(len(self.events(task["id"])), events_before)

    def test_finished_states_are_reported_with_their_real_stop_certainty(self):
        cases = [
            ({"status": "uncertain"}, "uncertain", "unconfirmed"),
            ({"status": "failed"}, "failed", "unconfirmed"),
            ({"status": "cancelled", "stop_confirmed": True}, "cancelled", "confirmed"),
            ({"status": "blocked", "stop_confirmed": True}, "needs_attention", "confirmed"),
        ]
        for fields, expected, certainty in cases:
            with self.subTest(status=fields["status"]):
                task = self.task(**fields)
                payload = self.wait(task["id"])
                self.assertEqual(payload["outcome"], expected)
                self.assertTrue(payload["settled"])
                self.assertEqual(payload["stop_certainty"], certainty)
                self.assertFalse(payload["accepted"])

    def test_timeout_leaves_the_task_and_never_resends(self):
        task = self.task(status="running")
        events_before = self.events(task["id"])

        payload = self.wait(task["id"], wait_seconds=0.4)
        self.assertEqual(payload["outcome"], "wait_expired")
        self.assertFalse(payload["settled"])
        self.assertEqual(payload["status"], "running")
        self.assertFalse(payload["review_ready"])
        self.assertIn("buddy_wait", payload["next_step"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "running")

        again = self.wait(task["id"], wait_seconds=0.2)
        self.assertEqual(again["outcome"], "wait_expired")
        self.assertEqual(self.store.get_task(task["id"])["status"], "running")
        self.assertEqual([event["kind"] for event in self.events(task["id"])],
                         [event["kind"] for event in events_before],
                         "a wait must not add or replay dispatch events")
        self.assertEqual(sum(event["kind"] == "submitted" for event in self.events(task["id"])), 1)

    def test_attempt_change_is_not_absorbed(self):
        task = self.task(status="needs_review", stop_confirmed=True, result={"text": "first round"})
        self.assertEqual(self.wait(task["id"])["outcome"], "ready_for_review")

        reworked = self.store.rework_task(task["id"], "Fix the date")
        self.assertEqual(reworked["attempt"], 2)

        payload = self.wait(task["id"], attempt=1)
        self.assertEqual(payload["outcome"], "attempt_changed")
        self.assertEqual(payload["attempt"], 1, "the answer names the round this wait was bound to")
        self.assertEqual(payload["current_attempt"], 2, "and the round the record is actually on")
        self.assertIsNone(payload["result_excerpt"], "the new round's result must not be collected")
        self.assertIn("attempt", payload["next_step"])

    def test_accepted_task_is_read_only(self):
        task = self.task(status="needs_review", stop_confirmed=True, result={"text": "done"})
        accepted = self.store.accept_task(task["id"], "Checked bytes", ["sha256 matches"])
        snapshot = self.store.get_task(accepted["id"])
        events_before = len(self.events(task["id"]))

        payload = self.wait(task["id"])

        self.assertEqual(payload["outcome"], "already_accepted")
        self.assertTrue(payload["accepted"])
        self.assertEqual(self.store.get_task(task["id"]), snapshot)
        self.assertEqual(len(self.events(task["id"])), events_before)

    def test_review_without_confirmed_stop_is_not_reported_ready(self):
        task = self.task(status="needs_review")  # no confirmed stop
        payload = self.wait(task["id"], wait_seconds=0.3)
        self.assertEqual(payload["outcome"], "wait_expired")
        self.assertEqual(payload["detail"], "review_recorded_but_stop_unconfirmed")
        self.assertFalse(payload["review_ready"])

    def test_waiting_is_cancellable_without_cancelling_the_task(self):
        task = self.task(status="running")
        events_before = len(self.events(task["id"]))
        cancel = threading.Event()
        results = []

        def run():
            report = waiting.wait_for_task(self.store, task["id"], attempt=1, wait_seconds=10,
                                           poll_interval=0.05, cancel_event=cancel)
            results.append(report)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.2)
        cancel.set()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["outcome"], "wait_cancelled")
        self.assertFalse(results[0]["settled"])
        current = self.store.get_task(task["id"])
        self.assertEqual(current["status"], "running", "stopping observation must not cancel the task")
        self.assertEqual(len(self.events(task["id"])), events_before)

    def test_unknown_task_is_not_found(self):
        payload = self.wait("task-00000000-0000-0000-0000-000000000000", wait_seconds=1)
        self.assertEqual(payload["outcome"], "not_found")
        self.assertEqual(payload["task_id"], "task-00000000-0000-0000-0000-000000000000")

    def test_invalid_wait_options_are_refused(self):
        task = self.task(status="running")
        for bad in ({"wait_seconds": 0}, {"wait_seconds": 10_000},
                    {"poll_interval": 0}, {"poll_interval": 60}):
            with self.subTest(**bad):
                with self.assertRaises(ValueError):
                    self.wait(task["id"], **bad)


class SideEffectOrderTests(WaitCase):
    """A rejected wait option must be refused before anything is dispatched."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("buddygate.cli.start_worker")
        self.start_worker = patcher.start()
        self.addCleanup(patcher.stop)

    def test_invalid_run_options_create_nothing_and_start_nothing(self):
        from buddygate.cli import invoke

        for bad in ({"wait_seconds": 0}, {"wait_seconds": -1}, {"wait_seconds": 10_000},
                    {"wait_seconds": float("nan")}, {"wait_seconds": float("inf")},
                    {"poll_interval": 0}, {"poll_interval": 60}):
            with self.subTest(**bad):
                request = {"workspace": str(self.base), "objective": "Must not dispatch", "constraints": [],
                           "idempotency_key": f"refused-{sorted(bad.items())[0][1]}"}
                request.update(bad)
                with self.assertRaises(ValueError):
                    invoke("buddy_run", request)
                self.assertEqual(self.store.list_tasks(), [], "no task may be created")
                self.assertFalse(self.start_worker.called, "no worker may be started")

    def test_invalid_rework_options_leave_the_attempt_untouched(self):
        from buddygate.cli import invoke

        task = self.task(status="needs_review", stop_confirmed=True, result={"text": "first"})
        events_before = self.events(task["id"])
        with self.assertRaises(ValueError):
            invoke("buddy_rework_and_wait", {"task_id": task["id"], "feedback": "Fix it", "wait_seconds": 0})
        current = self.store.get_task(task["id"])
        self.assertEqual(current["attempt"], 1, "a rejected wait must not queue another round")
        self.assertEqual(current["status"], "needs_review")
        self.assertEqual([event["kind"] for event in self.events(task["id"])],
                         [event["kind"] for event in events_before])
        self.assertFalse(self.start_worker.called)


class AttemptBindingTests(WaitCase):
    """The two races around an explicit attempt binding."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("buddygate.cli.start_worker")
        self.start_worker = patcher.start()
        self.addCleanup(patcher.stop)

    def advance_round(self, task_id, text):
        """Land another round on the same task the way a real rework would."""
        self.store.update_task(task_id, status="needs_review", stop_confirmed=True,
                               result={"text": "first round"})
        self.store.rework_task(task_id, "Another collaborator's round")
        self.store.update_task(task_id, status="needs_review", stop_confirmed=True,
                               result={"text": text})
        return self.store.get_task(task_id)

    def test_run_binds_the_attempt_it_created(self):
        import buddygate.cli as cli

        real = cli.wait_for_outcome
        seen = {}

        def reworked_between_the_steps(database, task_id, mode, args, cancel_event=None, attempt=None):
            seen["attempt"] = attempt
            self.advance_round(task_id, "second round")
            return real(database, task_id, mode, args, cancel_event=cancel_event, attempt=attempt)

        with mock.patch.object(cli, "wait_for_outcome", reworked_between_the_steps):
            payload = cli.invoke("buddy_run", {"workspace": str(self.base), "objective": "Race fixture",
                                               "constraints": [], "idempotency_key": "race-key",
                                               "wait_seconds": 0.4, "poll_interval": 0.05})

        self.assertEqual(seen["attempt"], 1, "the wait is bound to the round the submit created")
        self.assertEqual(payload["outcome"], "attempt_changed")
        self.assertEqual(payload["attempt"], 1)
        self.assertEqual(payload["current_attempt"], 2)
        self.assertIsNone(payload["result_excerpt"], "the next round's text must not be collected")
        self.assertEqual(self.store.get_task(payload["task_id"])["attempt"], 2)

    def test_a_round_change_while_the_answer_is_assembled_is_not_absorbed(self):
        import buddygate.cli as cli

        task = self.task(status="needs_review", stop_confirmed=True, result={"text": "first round"})
        task_id = task["id"]
        real = cli.build_payload
        holder = {}

        def reworked_before_assembly(database, report, mode, record=None):
            holder["report_outcome"] = report["outcome"]
            self.advance_round(task_id, "second round")
            return real(database, report, mode, record)

        with mock.patch.object(cli, "build_payload", reworked_before_assembly):
            payload = cli.invoke("buddy_wait", {"task_id": task_id, "wait_seconds": 1,
                                                "poll_interval": 0.05})

        self.assertEqual(holder["report_outcome"], "ready_for_review", "the wait did observe round 1 ready")
        self.assertEqual(payload["outcome"], "attempt_changed",
                         "the answer must not claim round 1 is ready once the record moved")
        self.assertEqual(payload["attempt"], 1)
        self.assertEqual(payload["current_attempt"], 2)
        self.assertIsNone(payload["result_excerpt"])
        self.assertFalse(payload["review_ready"])


class WaitSlotTests(unittest.TestCase):
    """Deterministic checks of the in-connection wait registry."""

    def setUp(self):
        from buddygate.mcp import _WaitSlots

        self.slots = _WaitSlots(1)

    def test_ids_of_different_json_types_are_different_waits(self):
        from buddygate.mcp import _WaitSlots

        slots = _WaitSlots(2)
        first, second = threading.Event(), threading.Event()
        self.assertEqual(slots.reserve(1, first), "ok")
        self.assertEqual(slots.reserve("1", second), "ok")
        self.assertEqual(slots.pending(), 2)
        self.assertTrue(slots.cancel(1))
        self.assertTrue(first.is_set())
        self.assertFalse(second.is_set(), "the string id must not be cancelled with the number id")
        self.assertTrue(slots.cancel("1"))
        self.assertTrue(second.is_set())

    def test_duplicate_in_flight_id_is_refused_and_keeps_the_holder(self):
        holder, intruder = threading.Event(), threading.Event()
        self.assertEqual(self.slots.reserve(1, holder), "ok")
        self.assertEqual(self.slots.reserve(1, intruder), "duplicate")
        self.assertEqual(self.slots.pending(), 1, "a duplicate must not overwrite the holder")
        self.assertTrue(self.slots.cancel(1))
        self.assertTrue(holder.is_set())
        self.assertFalse(intruder.is_set())
        self.assertEqual(self.slots.reserve(2, threading.Event()), "busy",
                         "the duplicate must not have consumed a second slot")
        self.assertFalse(self.slots.release(1, intruder), "only the holder may release the slot")
        self.assertEqual(self.slots.pending(), 1)
        self.assertTrue(self.slots.release(1, holder))
        self.assertEqual(self.slots.pending(), 0)
        self.assertEqual(self.slots.reserve(3, threading.Event()), "ok", "the slot is free again")


class McpArgumentTests(unittest.TestCase):
    def test_wait_numbers_must_be_finite(self):
        from buddygate.mcp import WAIT_SECONDS, validate_arguments

        schema = {"properties": {"wait_seconds": WAIT_SECONDS}, "required": []}
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    validate_arguments({"wait_seconds": bad}, schema)


class RunIdempotencyTests(WaitCase):
    def setUp(self):
        super().setUp()
        # Never spawn a real worker process from a test.
        patcher = mock.patch("buddygate.cli.start_worker")
        self.start_worker = patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_key_creates_one_task_and_one_submission(self):
        from buddygate.cli import invoke

        request = {"workspace": str(self.base), "objective": "Fixture objective", "constraints": [],
                   "idempotency_key": "wait-fixture-key", "wait_seconds": 0.3, "poll_interval": 0.05}
        first = invoke("buddy_run", dict(request))
        second = invoke("buddy_run", dict(request))

        self.assertEqual(first["outcome"], "wait_expired")
        self.assertEqual(second["outcome"], "wait_expired")
        self.assertEqual(first["task_id"], second["task_id"], "the same key must not create a second task")
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.assertEqual([event["kind"] for event in self.events(first["task_id"])], ["submitted"],
                         "one submission and no dispatch")
        self.assertTrue(self.start_worker.called, "a queued submission still starts the idle worker")

    def test_rework_and_wait_binds_to_the_new_attempt(self):
        from buddygate.cli import invoke

        task = self.task(status="needs_review", stop_confirmed=True, result={"text": "first"})
        payload = invoke("buddy_rework_and_wait", {"task_id": task["id"], "feedback": "Fix it",
                                                   "wait_seconds": 0.3, "poll_interval": 0.05})
        self.assertEqual(payload["mode"], "buddy_rework_and_wait")
        self.assertEqual(payload["attempt"], 2)
        self.assertEqual(payload["outcome"], "wait_expired")
        self.assertEqual(self.store.get_task(task["id"])["attempt"], 2)


class McpProcess:
    """Minimal newline-JSON-RPC client for the real stdio server."""

    def __init__(self, state_dir: Path):
        environment = os.environ.copy()
        environment["BUDDYGATE_STATE_DIR"] = str(state_dir)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "buddygate", "mcp"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.lines.put(json.loads(line.decode("utf-8")))
        except (ValueError, OSError):
            pass
        self.lines.put(None)

    def send(self, message):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def call(self, request_id, name, arguments):
        self.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}})

    def receive(self, timeout=15):
        message = self.lines.get(timeout=timeout)
        if message is None:
            raise AssertionError("server closed stdout before replying")
        return message

    def close(self, timeout=15):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        code = self.process.wait(timeout=timeout)
        stderr = self.process.stderr.read().decode("utf-8", "replace")
        self._close_pipes()
        return code, stderr

    def _close_pipes(self):
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def terminate(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
        self._close_pipes()


def payload_of(reply):
    return json.loads(reply["result"]["content"][0]["text"])


class McpWaitTests(WaitCase):
    def client(self):
        client = McpProcess(self.state)
        self.addCleanup(client.terminate)
        return client

    def test_ping_and_read_only_calls_answer_during_a_pending_wait(self):
        task = self.task(status="running")
        client = self.client()
        client.call(10, "buddy_wait", {"task_id": task["id"], "wait_seconds": 30, "poll_interval": 0.05})

        # The wait is pending, so it has not answered yet.
        with self.assertRaises(queue.Empty):
            client.receive(0.5)

        client.send({"jsonrpc": "2.0", "id": 11, "method": "ping"})
        first = client.receive(10)
        self.assertEqual(first["id"], 11, "ping must be answered while a wait is pending")
        self.assertEqual(first["result"], {})

        client.call(12, "buddy_list", {})
        second = client.receive(10)
        self.assertEqual(second["id"], 12, "read-only queries must not be blocked by a wait")
        self.assertIsInstance(payload_of(second), list)

        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 10}})
        cancelled = client.receive(10)
        self.assertEqual(cancelled["id"], 10)
        self.assertFalse(cancelled["result"]["isError"])
        self.assertEqual(payload_of(cancelled)["outcome"], "wait_cancelled")

        code, stderr = client.close()
        self.assertEqual(code, 0, stderr)
        current = self.store.get_task(task["id"])
        self.assertEqual(current["status"], "running", "cancelling the wait must not cancel the task")
        self.assertFalse(any(event["kind"] == "cancel_requested" for event in self.events(task["id"])))

    def test_expired_wait_does_not_duplicate_or_resend(self):
        task = self.task(status="running")
        client = self.client()

        client.call(1, "buddy_wait", {"task_id": task["id"], "wait_seconds": 1, "poll_interval": 0.05})
        first = payload_of(client.receive(10))
        self.assertEqual(first["outcome"], "wait_expired")
        self.assertEqual(first["status"], "running")

        client.call(2, "buddy_wait", {"task_id": task["id"], "wait_seconds": 1, "poll_interval": 0.05})
        second = payload_of(client.receive(10))
        self.assertEqual(second["outcome"], "wait_expired")

        code, stderr = client.close()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(self.store.list_tasks()), 1, "waiting must never create a task")
        self.assertEqual(sum(event["kind"] == "submitted" for event in self.events(task["id"])), 1)
        self.assertEqual(self.store.get_task(task["id"])["status"], "running")

    def test_only_limited_waits_run_and_eof_shuts_down_promptly(self):
        tasks = [self.task(status="running") for _ in range(MAX_CONCURRENT_WAITS + 1)]
        client = self.client()
        for index, task in enumerate(tasks, start=1):
            client.call(index, "buddy_wait", {"task_id": task["id"], "wait_seconds": 30, "poll_interval": 0.05})

        first = client.receive(10)
        self.assertEqual(first["id"], MAX_CONCURRENT_WAITS + 1)
        self.assertIn("error", first)
        self.assertIn("Too many concurrent waits", first["error"]["message"])

        started = time.monotonic()
        code, stderr = client.close()
        self.assertEqual(code, 0, stderr)
        self.assertLess(time.monotonic() - started, 10, "closing stdin must stop wait threads")
        self.assertTrue(all(self.store.get_task(task["id"])["status"] == "running" for task in tasks))

    def test_number_and_string_request_ids_cancel_separately(self):
        task = self.task(status="running")
        client = self.client()
        client.call(1, "buddy_wait", {"task_id": task["id"], "wait_seconds": 30, "poll_interval": 0.05})
        client.call("1", "buddy_wait", {"task_id": task["id"], "wait_seconds": 30, "poll_interval": 0.05})

        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
        cancelled = client.receive(10)
        self.assertEqual(cancelled["id"], 1)
        self.assertEqual(payload_of(cancelled)["outcome"], "wait_cancelled")

        # The string id is a different request: still waiting, still answering.
        client.send({"jsonrpc": "2.0", "id": 99, "method": "ping"})
        self.assertEqual(client.receive(10)["id"], 99)

        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "1"}})
        second = client.receive(10)
        self.assertEqual(second["id"], "1")
        self.assertEqual(payload_of(second)["outcome"], "wait_cancelled")

        code, stderr = client.close()
        self.assertEqual(code, 0, stderr)

    def test_duplicate_in_flight_request_id_is_refused_without_dispatching(self):
        task = self.task(status="running")
        client = self.client()
        client.call(7, "buddy_wait", {"task_id": task["id"], "wait_seconds": 30, "poll_interval": 0.05})
        client.call(7, "buddy_wait", {"task_id": task["id"], "wait_seconds": 30, "poll_interval": 0.05})

        refused = client.receive(10)
        self.assertEqual(refused["id"], 7)
        self.assertIn("already has a wait in flight", refused["error"]["message"])

        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
        original = client.receive(10)
        self.assertEqual(original["id"], 7)
        self.assertEqual(payload_of(original)["outcome"], "wait_cancelled",
                         "the duplicate must not have replaced the original wait")

        code, stderr = client.close()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.assertEqual(sum(event["kind"] == "submitted" for event in self.events(task["id"])), 1)

    def test_initialize_advertises_the_one_call_wait(self):
        client = self.client()
        client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18"}})
        reply = client.receive(10)
        self.assertIn("buddy_run", reply["result"]["instructions"])
        self.assertIn("buddy_get", reply["result"]["instructions"])
        client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {tool["name"]: tool for tool in client.receive(10)["result"]["tools"]}
        for name in ("buddy_run", "buddy_wait", "buddy_rework_and_wait"):
            self.assertIn(name, tools)
            self.assertIn("wait_seconds", tools[name]["inputSchema"]["properties"])
        self.assertLess(tools["buddy_run"]["inputSchema"]["properties"]["wait_seconds"]["maximum"], 86400)
        code, stderr = client.close()
        self.assertEqual(code, 0, stderr)


if __name__ == "__main__":
    unittest.main()

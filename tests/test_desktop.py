"""Desktop backend and usage accounting, simulated without any real account.

Nothing here contacts WorkBuddy: ownership rows, renderer listings and desktop
operations are injected, so a passing test proves the gateway's own behaviour
(ordering, refusals, accounting) and not that this machine is connected.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from buddygate.desktop import (DesktopClient, DesktopConnection, DesktopError, DesktopUnavailable,
                               check_ownership, renderer_candidates, uuid7, valid_page)
from buddygate.desktop_exec import dispatch_title, execute_desktop, prompt_for
from buddygate.meter import codex_delta, codex_snapshot, native_usage
from buddygate.store import BACKEND_CLI, BACKEND_DESKTOP, Store
from buddygate.summary import summarize_task
from buddygate.worker import run_task


class FakeDesktop:
    """A stand-in for DesktopClient with recorded side effects."""

    def __init__(self, states=("running", "completed"), assistant="完成：写入了 report.txt",
                 usage=None, pending_value=None, fail_open=None, fail_send=None,
                 fail_create=None, on_poll=None, activity=None, report_activity=True,
                 native_request_id="req-canonical-1"):
        self.states = list(states)
        self.assistant = assistant
        self.usage = usage
        self.pending_value = pending_value
        self.fail_open = fail_open
        self.fail_send = fail_send
        self.fail_create = fail_create
        self.on_poll = on_poll
        # The server-side identity; deliberately different from the client UUID.
        self.native_request_id = native_request_id
        # Both flags default to false so a completed request counts as stopped.
        self.activity = dict(activity or {"hasActiveChildAgents": False,
                                          "hasRunningBackgroundTasks": False})
        self.report_activity = report_activity
        self.created = []
        self.sent = []
        self.resolved = []
        self.cancels = 0
        self.observations = 0
        self.observed_with = []
        self.closed = 0

    def open(self):
        if self.fail_open:
            raise self.fail_open
        return {"app_version": "5.9.3", "platform": "win32", "port": 61616, "peer_verified": True,
                "renderers_checked": 1}

    def create_conversation(self, conversation_id, title, cwd, model):
        if self.fail_create:
            raise self.fail_create
        self.created.append({"conversation_id": conversation_id, "title": title, "cwd": cwd, "model": model})
        return {"id": conversation_id, "state": "idle", "model": model}

    def send_prompt(self, conversation_id, text, request_id):
        if self.fail_send:
            raise self.fail_send
        self.sent.append({"conversation_id": conversation_id, "request_id": request_id, "text": text})
        return {"dispatched": True, "state": "sending"}

    def observe(self, conversation_id, request_id, native_request_id=None):
        self.observations += 1
        self.observed_with.append(native_request_id)
        if self.on_poll:
            self.on_poll(self)
        index = min(self.observations - 1, len(self.states) - 1)
        state = self.states[index]
        payload = {"conversationState": "active", "conversationExists": True, "requestCount": 1,
                   "conversationId": conversation_id, "conversationTitle": "【派工】测试",
                   "requestSeen": True, "state": state,
                   "matchBy": "client_request_id", "matchAmbiguous": False,
                   "requestsWithoutClientRequestId": 0,
                   "nativeRequestId": self.native_request_id,
                   "nativeRequestIdSource": "conversationRequestId",
                   "requestedModel": {"id": "requested-model"} if state == "completed" else None,
                   "responseModel": {"id": "served-model"} if state == "completed" else None,
                   "usage": self.usage if state == "completed" else None,
                   "assistant": self.assistant if state == "completed" else ""}
        if self.report_activity:
            payload.update(self.activity)
        return payload

    def pending(self, conversation_id):
        # A real desktop stops reporting a request once it has been resolved.
        return None if self.resolved else self.pending_value

    def resolve_pending(self, conversation_id, pending_id, response):
        self.resolved.append({"pending_id": pending_id, "response": response})
        return {"resolved": True}

    def cancel_conversation(self, conversation_id):
        self.cancels += 1
        return {"cancelRequested": True}

    def close(self):
        self.closed += 1


class DesktopTestCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.environ.get("BUDDYGATE_TEST_TEMP", tempfile.gettempdir())).resolve()
        self.root = self.base / f"buddygate-desktop-test-{uuid.uuid4()}"
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = Store(self.root / "tasks.sqlite3")

    def cleanup(self):
        resolved = self.root.resolve()
        if resolved.parent != self.base or not resolved.name.startswith("buddygate-desktop-test-"):
            raise RuntimeError("Refusing to clean a directory outside the allocated test root")
        shutil.rmtree(resolved)

    def claimed(self, **kwargs):
        kwargs.setdefault("backend", BACKEND_DESKTOP)
        task = self.store.submit_task(str(self.workspace), "写一个只读报告", ["只改 report.txt"], **kwargs)
        return self.store.claim_next("desktop-worker")

    def run_task(self, task, desktop, **kwargs):
        kwargs.setdefault("poll_interval", 0.01)
        kwargs.setdefault("cancel_grace", 2.0)
        return execute_desktop(self.store, task, client=desktop, **kwargs)


class OwnershipTests(unittest.TestCase):
    def test_requires_exclusive_loopback_binding(self):
        with self.assertRaisesRegex(DesktopUnavailable, "Nothing is listening"):
            check_ownership([], "C:/WorkBuddy/WorkBuddy.exe")
        with self.assertRaisesRegex(DesktopUnavailable, "not bound exclusively"):
            check_ownership([{"address": "0.0.0.0", "pid": 1, "path": "C:/WorkBuddy/WorkBuddy.exe"}],
                            "C:/WorkBuddy/WorkBuddy.exe")
        with self.assertRaisesRegex(DesktopUnavailable, "not bound exclusively"):
            check_ownership([{"address": "127.0.0.1", "pid": 1, "path": "C:/WorkBuddy/WorkBuddy.exe"},
                             {"address": "192.168.1.9", "pid": 1, "path": "C:/WorkBuddy/WorkBuddy.exe"}],
                            "C:/WorkBuddy/WorkBuddy.exe")

    def test_rejects_a_listener_owned_by_another_program(self):
        rows = [{"address": "127.0.0.1", "pid": 4242, "path": "C:/Other/browser.exe"}]
        with self.assertRaisesRegex(DesktopUnavailable, "different program"):
            check_ownership(rows, "C:/WorkBuddy/WorkBuddy.exe")
        with self.assertRaisesRegex(DesktopUnavailable, "could not be identified"):
            check_ownership([{"address": "127.0.0.1", "pid": 4242, "path": None}], "C:/WorkBuddy/WorkBuddy.exe")

    def test_accepts_only_the_configured_executable(self):
        rows = [{"address": "127.0.0.1", "pid": 4242, "path": "C:/WorkBuddy/WorkBuddy.exe"}]
        verified = check_ownership(rows, "C:/WorkBuddy/WorkBuddy.exe")
        self.assertTrue(verified["peer_verified"])
        self.assertEqual(verified["pids"], [4242])
        self.assertEqual(verified["addresses"], ["127.0.0.1"])


class RendererTests(unittest.TestCase):
    def connection(self):
        return DesktopConnection(executable=__file__, port=61616)

    def test_page_candidates_must_be_local_pages(self):
        good = "ws://127.0.0.1:61616/devtools/page/abc-1"
        self.assertTrue(valid_page(good, 61616))
        for bad in ("ws://10.0.0.5:61616/devtools/page/abc-1",
                    "ws://127.0.0.1:61616/devtools/browser/abc-1",
                    "ws://127.0.0.1:61616/devtools/page/abc-1?token=leak",
                    "http://127.0.0.1:61616/devtools/page/abc-1",
                    "ws://127.0.0.1:9999/devtools/page/abc-1"):
            with self.subTest(url=bad):
                self.assertFalse(valid_page(bad, 61616))
        targets = [{"type": "page", "webSocketDebuggerUrl": good},
                   {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:61616/devtools/page/abc-2"},
                   {"type": "service_worker", "webSocketDebuggerUrl": "ws://127.0.0.1:61616/devtools/page/abc-3"}]
        self.assertEqual(len(renderer_candidates(targets, 61616)), 2)
        with self.assertRaises(DesktopUnavailable):
            renderer_candidates({"unexpected": True}, 61616)

    def test_client_refuses_ambiguous_or_foreign_renderers(self):
        class Bridge:
            def __init__(self, identities):
                self.identities = identities
                self.calls = []

            def call(self, op, ws, **arguments):
                self.calls.append(op)
                if op != "identity":
                    raise AssertionError("No operation may run before identity verification")
                return self.identities.get(ws, {"native": False})

            def close(self):
                pass

        single = "ws://127.0.0.1:61616/devtools/page/only"
        client = DesktopClient(self.connection(),
                               bridge_factory=lambda node, helper: Bridge({single: {"native": True, "version": "5.9.3", "platform": "win32"}}),
                               checker=lambda port: [{"address": "127.0.0.1", "pid": 1, "path": str(Path(__file__).resolve())}],
                               lister=lambda port: [{"type": "page", "webSocketDebuggerUrl": single}])
        with mock.patch("buddygate.desktop.node_executable", return_value="node"):
            metadata = client.open()
        self.assertEqual(metadata["app_version"], "5.9.3")
        self.assertTrue(metadata["peer_verified"])

        two = [{"type": "page", "webSocketDebuggerUrl": single},
               {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:61616/devtools/page/second"}]
        ambiguous = DesktopClient(self.connection(),
                                  bridge_factory=lambda node, helper: Bridge({single: {"native": True, "version": "5.9.3", "platform": "win32"},
                                                                              "ws://127.0.0.1:61616/devtools/page/second": {"native": True, "version": "5.9.3", "platform": "win32"}}),
                                  checker=lambda port: [{"address": "127.0.0.1", "pid": 1, "path": str(Path(__file__).resolve())}],
                                  lister=lambda port: two)
        with mock.patch("buddygate.desktop.node_executable", return_value="node"):
            with self.assertRaisesRegex(DesktopUnavailable, "exactly one verified"):
                ambiguous.open()

        foreign = DesktopClient(self.connection(),
                                bridge_factory=lambda node, helper: Bridge({single: {"native": False}}),
                                checker=lambda port: [{"address": "127.0.0.1", "pid": 1, "path": str(Path(__file__).resolve())}],
                                lister=lambda port: [{"type": "page", "webSocketDebuggerUrl": single}])
        with mock.patch("buddygate.desktop.node_executable", return_value="node"):
            with self.assertRaisesRegex(DesktopUnavailable, "exactly one verified"):
                foreign.open()

    def test_configuration_rejects_non_loopback_and_missing_files(self):
        for port in (0, 80, 70000, True, "61616"):
            with self.subTest(port=port):
                with self.assertRaises(DesktopUnavailable):
                    DesktopConnection(executable=__file__, port=port)
        with self.assertRaises(DesktopUnavailable):
            DesktopConnection(executable=str(Path(__file__) / "missing" / "WorkBuddy.exe"), port=61616)
        with self.assertRaises(DesktopUnavailable):
            DesktopConnection(executable=__file__, port=61616, host="0.0.0.0")
        with self.assertRaises(DesktopUnavailable):
            DesktopConnection(executable=__file__, port=61616, arguments=["--remote-debugging-address=0.0.0.0"])


class CreateOnceTests(unittest.TestCase):
    """A create call with an unusable reply must never be replayed."""

    class Bridge:
        def __init__(self, create_value, conversation=None, read_error=None):
            self.create_value = create_value
            self.conversation = conversation
            self.read_error = read_error
            self.calls = []

        def call(self, op, ws, **arguments):
            self.calls.append(op)
            if op == "create":
                return self.create_value
            if op == "conversation":
                if self.read_error is not None:
                    raise self.read_error
                return self.conversation
            raise AssertionError(f"Unexpected operation {op}")

        def close(self):
            pass

    def client(self, bridge):
        client = DesktopClient(DesktopConnection(executable=__file__, port=61616),
                               bridge_factory=lambda node, helper: bridge)
        client.bridge = bridge
        client.ws = "ws://127.0.0.1:61616/devtools/page/only"
        return client

    def test_a_confirmed_create_is_not_read_back(self):
        bridge = self.Bridge({"id": "conversation-1", "state": "idle"})
        created = self.client(bridge).create_conversation("conversation-1", "标题", "C:/work", None)
        self.assertEqual(created["id"], "conversation-1")
        self.assertEqual(bridge.calls, ["create"])

    def test_an_unusable_reply_is_verified_by_a_read_only(self):
        bridge = self.Bridge({}, {"exists": True, "id": "conversation-1", "state": "idle"})
        created = self.client(bridge).create_conversation("conversation-1", "标题", "C:/work", None)
        self.assertEqual(created["confirmed_by"], "get")
        self.assertEqual(bridge.calls, ["create", "conversation"], "create must run exactly once")

    def test_a_failed_read_back_is_unknown_and_not_retried(self):
        bridge = self.Bridge({}, read_error=DesktopError("renderer disconnected"))
        with self.assertRaisesRegex(DesktopError, "not created again"):
            self.client(bridge).create_conversation("conversation-1", "标题", "C:/work", None)
        self.assertEqual(bridge.calls, ["create", "conversation"])

    def test_a_missing_conversation_is_not_recreated(self):
        bridge = self.Bridge({}, {"exists": False})
        with self.assertRaisesRegex(DesktopError, "not created again"):
            self.client(bridge).create_conversation("conversation-1", "标题", "C:/work", None)
        self.assertEqual(bridge.calls, ["create", "conversation"])


class DispatchTests(DesktopTestCase):
    def test_ids_are_persisted_before_the_prompt_is_sent(self):
        task = self.claimed()
        seen = {}

        class Inspecting(FakeDesktop):
            def send_prompt(inner, conversation_id, text, request_id):
                persisted = self.store.get_task(task["id"])
                seen.update(persisted["native"], prompt=text)
                seen["status_at_send"] = persisted["status"]
                return super().send_prompt(conversation_id, text, request_id)

        desktop = Inspecting()
        completed = self.run_task(task, desktop)
        self.assertEqual(seen["dispatch"], "reserved")
        self.assertEqual(seen["status_at_send"], "running")
        self.assertIn("【派工】", seen["prompt"])
        self.assertTrue(seen["prompt"].startswith(dispatch_title(task)))
        self.assertEqual(len(desktop.sent), 1)
        self.assertEqual(completed["status"], "needs_review")
        self.assertTrue(completed["stop_confirmed"])
        self.assertEqual(completed["session_id"], completed["native"]["conversation_id"])
        self.assertEqual(desktop.sent[0]["request_id"], completed["native"]["request_id"])
        self.assertEqual(len(desktop.created), 1)
        self.assertEqual(desktop.closed, 0, "an injected client is owned by the caller")

    def test_second_execution_of_a_sent_attempt_is_refused(self):
        task = self.claimed()
        desktop = FakeDesktop()
        self.run_task(task, desktop)
        self.assertEqual(len(desktop.sent), 1)
        replacement = FakeDesktop()
        again = self.run_task(self.store.get_task(task["id"]), replacement)
        self.assertEqual(again["status"], "uncertain")
        self.assertEqual(replacement.sent, [])
        self.assertTrue(any(event["kind"] == "dispatch_refused" for event in self.store.get_events(task["id"])))

    def test_unknown_send_outcome_is_not_retried(self):
        task = self.claimed()
        desktop = FakeDesktop(fail_send=DesktopError("renderer disconnected"))
        result = self.run_task(task, desktop)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["stop_confirmed"])
        self.assertEqual(result["native"]["dispatch"], "unknown")
        self.assertTrue(any(event["kind"] == "dispatch_unknown" for event in self.store.get_events(task["id"])))
        self.assertEqual(self.store.claim_next("another-worker"), None)
        second = self.run_task(self.store.get_task(task["id"]), FakeDesktop())
        self.assertEqual(second["status"], "uncertain")

    def test_unavailable_connection_blocks_before_any_conversation(self):
        task = self.claimed()
        desktop = FakeDesktop(fail_open=DesktopUnavailable("No desktop connection is configured"))
        result = self.run_task(task, desktop)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["stop_confirmed"])
        self.assertEqual(desktop.created, [])
        self.assertEqual(desktop.sent, [])
        self.assertIsNone(result["native"])

    def test_failed_creation_sends_nothing(self):
        task = self.claimed()
        desktop = FakeDesktop(fail_create=DesktopError("desktop did not confirm"))
        result = self.run_task(task, desktop)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(desktop.sent, [])
        self.assertEqual(result["native"]["dispatch"], "reserved")

    def test_restart_recovery_never_resends(self):
        task = self.claimed()
        self.store.update_task(task["id"], status="running", native={
            "conversation_id": "conversation-1", "request_id": "request-1", "dispatch": "sent"})
        restarted = Store(self.store.db_path)
        self.assertEqual(restarted.recover_interrupted(), 1)
        recovered = restarted.get_task(task["id"])
        self.assertEqual(recovered["status"], "uncertain")
        self.assertFalse(recovered["stop_confirmed"])
        self.assertEqual(recovered["native"]["dispatch"], "unknown")
        self.assertFalse(recovered["native"]["observation_resumed"],
                         "recovery must not claim that observation was resumed")
        interrupted = [event for event in restarted.get_events(task["id"]) if event["kind"] == "interrupted"]
        self.assertEqual(len(interrupted), 1)
        self.assertFalse(interrupted[0]["data"]["observation_resumed"])
        self.assertFalse(interrupted[0]["data"]["prompt_resent"])
        self.assertIn("observation was not resumed", recovered["error"])
        self.assertIsNone(restarted.claim_next("replacement"))
        desktop = FakeDesktop()
        execute_desktop(restarted, recovered, client=desktop, poll_interval=0.01)
        self.assertEqual(desktop.sent, [])
        self.assertEqual(desktop.observations, 0, "recovery does not silently resume observing")

    def test_rework_reuses_the_conversation_but_reserves_a_new_request(self):
        task = self.claimed()
        first = FakeDesktop()
        self.run_task(task, first)
        reworked = self.store.rework_task(task["id"], "补充来源说明")
        self.assertEqual(reworked["attempt"], 2)
        self.assertEqual(reworked["native"]["conversation_id"], first.sent[0]["conversation_id"])
        self.assertIsNone(reworked["native"]["request_id"])
        self.assertIsNone(reworked["native"]["dispatch"])
        second_task = self.store.claim_next("round-two")
        second = FakeDesktop()
        final = self.run_task(second_task, second)
        self.assertEqual(len(second.created), 0, "rework must reuse the visible conversation")
        self.assertEqual(second.sent[0]["conversation_id"], first.sent[0]["conversation_id"])
        self.assertNotEqual(second.sent[0]["request_id"], first.sent[0]["request_id"])
        self.assertIn("补充来源说明", second.sent[0]["text"])
        self.assertEqual(final["attempt"], 2)


    def test_reused_conversation_is_recorded_from_the_original_state(self):
        task = self.claimed()
        first = FakeDesktop()
        self.run_task(task, first)
        reserved = [event for event in self.store.get_events(task["id"])
                    if event["kind"] == "dispatch_reserved"]
        self.assertEqual(len(reserved), 1)
        self.assertFalse(reserved[0]["data"]["reused_conversation"],
                         "a brand-new conversation must not be reported as reused")
        self.store.rework_task(task["id"], "补充来源说明")
        second = self.store.claim_next("round-two")
        self.run_task(second, FakeDesktop())
        reserved = [event for event in self.store.get_events(task["id"])
                    if event["kind"] == "dispatch_reserved"]
        self.assertTrue(reserved[1]["data"]["reused_conversation"],
                        "the rework round reuses the visible conversation")


class ObservationTests(DesktopTestCase):
    def test_terminal_completion_records_usage_and_gaps(self):
        task = self.claimed()
        desktop = FakeDesktop(states=("running", "running", "completed"),
                              usage={"inputTokens": 1200, "totalTokens": 1500, "contextWindow": 200000})
        result = self.run_task(task, desktop)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("report.txt", result["result"]["text"])
        usage = result["usage"]["native"]
        self.assertEqual(usage["input_tokens"], 1200)
        self.assertEqual(usage["total_tokens"], 1500)
        self.assertEqual(usage["context_window"], 200000)
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertIsNone(usage["billed_tokens"])
        self.assertIn("Not a bill", " ".join(usage["notes"]))
        self.assertIsNone(result["usage"]["combined"])
        self.assertEqual(result["result"]["model"]["requested"], {"id": "requested-model"})

    def test_missing_usage_is_null_not_zero(self):
        task = self.claimed()
        result = self.run_task(task, FakeDesktop(states=("completed",), usage=None))
        self.assertEqual(result["status"], "needs_review")
        usage = result["usage"]["native"]
        self.assertIsNone(usage["total_tokens"])
        self.assertIsNone(usage["input_tokens"])
        self.assertEqual(usage["observed_fields"], [])

    def test_details_usage_is_exposed_with_scope_and_without_cost(self):
        task = self.claimed()
        usage = {
            "top": {"inputTokens": 37200, "totalTokens": 37200, "contextWindow": 300000},
            "details": {"promptTokens": 34265, "completionTokens": 289,
                        "promptCacheHitTokens": 34048, "promptCacheMissTokens": 217,
                        "promptCacheWriteTokens": 0},
            "observed_fields": ["inputTokens", "totalTokens", "contextWindow"],
            "observed_detail_fields": ["promptTokens", "completionTokens", "promptCacheHitTokens",
                                      "promptCacheMissTokens", "promptCacheWriteTokens"],
            "invalid_fields": [],
            "ignored_fields": ["cost", "estimatedCost"],
        }
        result = self.run_task(task, FakeDesktop(states=("completed",), usage=usage))
        self.assertEqual(result["status"], "needs_review")
        native = result["usage"]["native"]
        self.assertEqual(native["prompt_tokens"], 34265)
        self.assertEqual(native["output_tokens"], 289)
        self.assertEqual(native["prompt_cache_hit_tokens"], 34048)
        self.assertEqual(native["prompt_cache_miss_tokens"], 217)
        self.assertEqual(native["prompt_cache_write_tokens"], 0)
        self.assertEqual(native["total_tokens"], 37200)
        self.assertIn("final usage.details snapshot", native["scope"])
        self.assertIn("not verified", native["scope"])
        self.assertTrue(native["detail_arithmetic_check"]["prompt_split_consistent"])
        self.assertIsNone(native["cached_input_tokens"],
                          "a details counter is not silently relabelled as cached input")
        self.assertEqual(native["ignored_fields"], ["cost", "estimatedCost"])
        self.assertIsNone(native["billed_tokens"])
        self.assertIsNone(result["usage"]["combined"])

    def test_context_breakdown_is_reported_with_an_arithmetic_check(self):
        task = self.claimed()
        usage = {
            "top": {"inputTokens": 37206, "totalTokens": 37206, "contextWindow": 300000},
            "byCategory": {"systemPrompt": 6460, "conversation": 0, "tools": 27524, "mcp": 503,
                           "skills": 2719, "version": 1},
            "ignored_fields": ["cost"],
        }
        result = self.run_task(task, FakeDesktop(states=("completed",), usage=usage))
        native = result["usage"]["native"]
        self.assertEqual(native["context_breakdown"]["tools"], 27524)
        self.assertEqual(native["observed_context_fields"],
                         ["conversation", "mcp", "skills", "systemPrompt", "tools"])
        self.assertEqual(native["context_breakdown_check"]["sum"], 37206)
        self.assertTrue(native["context_breakdown_check"]["matches_input_tokens"])
        self.assertIsNone(native["prompt_tokens"], "an absent details snapshot stays null")
        self.assertEqual(native["observed_detail_fields"], [])

    def test_terminal_request_with_active_child_agents_is_not_confirmed_stopped(self):
        task = self.claimed()
        desktop = FakeDesktop(states=("completed",),
                              activity={"hasActiveChildAgents": True, "hasRunningBackgroundTasks": False})
        result = self.run_task(task, desktop, settle_grace=0.2)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["stop_confirmed"])
        self.assertEqual(result["native"]["activity"]["state"], "active")
        kinds = [event["kind"] for event in self.store.get_events(task["id"])]
        self.assertIn("terminal_activity_unconfirmed", kinds)
        self.assertIn("stop_unconfirmed", kinds)
        with self.assertRaisesRegex(ValueError, "explicit stopped confirmation"):
            self.store.rework_task(task["id"], "继续")

    def test_unknown_activity_flags_are_not_treated_as_stopped(self):
        task = self.claimed()
        result = self.run_task(task, FakeDesktop(states=("completed",), report_activity=False),
                               settle_grace=0.2)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["stop_confirmed"])
        self.assertEqual(result["native"]["activity"]["state"], "unknown")
        self.assertIsNone(result["native"]["activity"]["hasActiveChildAgents"])

    def test_activity_that_clears_allows_stop_confirmation(self):
        task = self.claimed()
        desktop = FakeDesktop(states=("completed",),
                              activity={"hasActiveChildAgents": True, "hasRunningBackgroundTasks": True})

        def clear(inner):
            if inner.observations >= 2:
                inner.activity = {"hasActiveChildAgents": False, "hasRunningBackgroundTasks": False}

        desktop.on_poll = clear
        result = self.run_task(task, desktop, settle_grace=5.0)
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(result["stop_confirmed"])
        self.assertEqual(result["result"]["activity"]["state"], "clear")
        self.assertIn("both desktop activity flags", result["result"]["stop_confirmed_basis"])

    def test_terminal_failure_is_blocked_with_stop_confirmed(self):
        task = self.claimed()
        result = self.run_task(task, FakeDesktop(states=("running", "failed")))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["stop_confirmed"])

    def test_empty_visible_answer_is_not_treated_as_success(self):
        task = self.claimed()
        result = self.run_task(task, FakeDesktop(states=("completed",), assistant="   "))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("without visible assistant text", result["error"])

    def test_permission_waits_for_an_explicit_manager_decision(self):
        task = self.claimed()
        pending = {"pending": {"id": "pending-1", "title": "写入 report.txt", "options": [
            {"optionId": "once", "kind": "allow_once", "name": "允许一次"},
            {"optionId": "deny", "kind": "reject_once", "name": "拒绝一次"},
            {"optionId": "forever", "kind": "allow_always", "name": "总是允许"},
        ]}}
        granted = threading.Event()
        polls = threading.Event()

        class GatedDesktop(FakeDesktop):
            """A real desktop cannot finish the turn while a permission is pending.

            The turn stays ``running`` no matter how many observations happen, and
            only a resolved one-time approval releases it to ``completed``. The
            manager's decision timing therefore cannot decide the test outcome.
            """

            def __init__(inner, **kwargs):
                super().__init__(states=("running",), on_poll=lambda _: polls.set(), **kwargs)
                inner.completed_before_decision = 0

            def observe(inner, conversation_id, request_id, native_request_id=None):
                payload = super().observe(conversation_id, request_id, native_request_id)
                if granted.is_set():
                    payload.update(state="completed", assistant=inner.assistant, usage=inner.usage,
                                   requestedModel={"id": "requested-model"},
                                   responseModel={"id": "served-model"})
                return payload

            def resolve_pending(inner, conversation_id, pending_id, response):
                result = super().resolve_pending(conversation_id, pending_id, response)
                if (response or {}).get("optionId") == "once":
                    granted.set()
                return result

        desktop = GatedDesktop(pending_value=pending)
        errors = []

        def executor():
            try:
                execute_desktop(self.store, task, client=desktop, poll_interval=0.01)
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)

        thread = threading.Thread(target=executor)
        thread.start()
        try:
            waiting = None
            for _ in range(200):
                waiting = self.store.get_task(task["id"])
                if waiting["status"] == "waiting_permission":
                    break
                polls.wait(0.05)
            self.assertIsNotNone(waiting)
            self.assertEqual(waiting["status"], "waiting_permission")
            permission = waiting["permission"]
            self.assertEqual({item["kind"] for item in permission["options"]},
                             {"allow_once", "reject_once", "allow_always"})
            self.assertEqual(desktop.resolved, [], "nothing may be approved automatically")

            # A manager who takes several more observation rounds to decide must
            # still find a pending request: the gate, not the clock, holds it.
            for _ in range(3):
                polls.clear()
                self.assertTrue(polls.wait(5), "the executor must keep observing while waiting")
                self.assertEqual(self.store.get_task(task["id"])["status"], "waiting_permission")
                self.assertEqual(desktop.resolved, [])
                self.assertTrue(thread.is_alive(), "the executor must not finish before a decision")

            with self.assertRaisesRegex(ValueError, "one-time"):
                self.store.decide_permission(task["id"], permission["id"], "forever", "不得永久授权")
            self.store.decide_permission(task["id"], permission["id"], "once", "用户授权本次写入")
        finally:
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(desktop.resolved), 1, "exactly one resolution, after the decision")
        self.assertEqual(desktop.resolved[0]["pending_id"], "pending-1")
        self.assertEqual(desktop.resolved[0]["response"], {"optionId": "once"})
        self.assertEqual(self.store.get_task(task["id"])["status"], "needs_review")
        # A late approval must not be delivered once the request is already terminal.
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.decide_permission(task["id"], permission["id"], "once", "迟到的批准")

    def test_unrecognised_permission_is_not_approved(self):
        task = self.claimed()
        pending = {"pending": {"id": "pending-2", "options": [{"optionId": "yes", "kind": "allow", "name": "允许"}]}}
        desktop = FakeDesktop(states=("running",), pending_value=pending)
        result = self.run_task(task, desktop, max_polls=3)
        self.assertEqual(desktop.resolved, [])
        self.assertEqual(result["status"], "uncertain")
        events = self.store.get_events(task["id"])
        self.assertTrue(any(event["kind"] == "permission_requires_desktop_decision" for event in events))

    def test_cancel_is_requested_once_and_waits_for_a_terminal_state(self):
        task = self.claimed()
        state = {"value": "running"}
        desktop = FakeDesktop(states=("running", "running"))
        desktop.observe = lambda conversation_id, request_id, native_request_id=None: {
            "conversationState": "active", "requestSeen": True, "state": state["value"],
            "hasActiveChildAgents": False, "hasRunningBackgroundTasks": False,
            "usage": None, "assistant": "已停止" if state["value"] == "cancelled" else ""}
        original = desktop.cancel_conversation
        desktop.cancel_conversation = lambda conversation_id: (state.__setitem__("value", "cancelled"),
                                                               original(conversation_id))[1]

        errors = []

        def executor():
            try:
                execute_desktop(self.store, task, client=desktop, poll_interval=0.02, cancel_grace=5.0)
            except BaseException as error:  # pragma: no cover
                errors.append(error)

        thread = threading.Thread(target=executor)
        thread.start()
        until = time.monotonic() + 8
        while time.monotonic() < until:
            if any(event["kind"] == "prompt_dispatched" for event in self.store.get_events(task["id"])):
                break
            time.sleep(0.02)
        self.store.request_cancel(task["id"])
        thread.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(desktop.cancels, 1)
        final = self.store.get_task(task["id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertTrue(final["stop_confirmed"])

    def test_timeout_keeps_the_run_unconfirmed(self):
        task = self.claimed(timeout_seconds=1)
        result = self.run_task(task, FakeDesktop(states=("running",)), poll_interval=0.05)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["stop_confirmed"])
        self.assertTrue(any(event["kind"] == "observation_timeout" for event in self.store.get_events(task["id"])))


class RequestIdentityTests(DesktopTestCase):
    """The canonical request id is the only identity that survives a restart."""

    def test_the_canonical_id_is_saved_at_the_first_observation(self):
        task = self.claimed()
        seen = {}

        class Observing(FakeDesktop):
            def observe(inner, conversation_id, request_id, native_request_id=None):
                if inner.observations == 1:
                    seen["before_second_poll"] = self.store.get_task(task["id"])["native"]
                return super().observe(conversation_id, request_id, native_request_id)

        desktop = Observing(native_request_id="canonical-request-a")
        result = self.run_task(task, desktop)
        self.assertEqual(result["status"], "needs_review")
        stored = seen["before_second_poll"]
        self.assertEqual(stored["native_request_id"], "canonical-request-a",
                         "the canonical id is persisted while the run is still going")
        self.assertEqual(stored["native_request_id_source"], "conversationRequestId")
        self.assertEqual(stored["native_request_id_matched_by"], "client_request_id")
        self.assertNotEqual(stored["native_request_id"], stored["request_id"],
                            "the client request UUID is not the canonical id")
        self.assertEqual(result["native"]["native_request_id"], "canonical-request-a")
        kinds = [event["kind"] for event in self.store.get_events(task["id"])]
        self.assertIn("native_request_identity_saved", kinds)
        self.assertLess(kinds.index("native_request_identity_saved"), kinds.index("execution_finished"))
        saved = next(event for event in self.store.get_events(task["id"])
                     if event["kind"] == "native_request_identity_saved")
        self.assertFalse(saved["data"]["client_request_id_is_canonical"])
        self.assertEqual(desktop.observed_with[0], None, "the first poll matches by the client id only")
        self.assertEqual(desktop.observed_with[1], "canonical-request-a",
                         "later polls offer the saved canonical id as evidence")

    def test_an_observation_that_lost_client_request_id_matches_the_saved_identity(self):
        task = self.claimed()
        saved = {}

        class Losing(FakeDesktop):
            """A renderer reload drops clientRequestId; the canonical id remains."""

            def observe(inner, conversation_id, request_id, native_request_id=None):
                payload = super().observe(conversation_id, request_id, native_request_id)
                if inner.observations == 1:
                    return payload
                saved.setdefault("first", self.store.get_task(task["id"])["native"].get("native_request_id"))
                matched = native_request_id == "canonical-request-b"
                return {"conversationState": "active", "conversationExists": True, "requestCount": 1,
                        "requestSeen": matched, "state": "completed" if matched else None,
                        "matchBy": "native_request_id" if matched else None,
                        "matchAmbiguous": False, "requestsWithoutClientRequestId": 1,
                        "nativeRequestId": "canonical-request-b" if matched else None,
                        "nativeRequestIdSource": "conversationRequestId" if matched else None,
                        "assistant": inner.assistant if matched else "",
                        "hasActiveChildAgents": False, "hasRunningBackgroundTasks": False}

        desktop = Losing(states=("running", "completed"), native_request_id="canonical-request-b")
        result = self.run_task(task, desktop)
        self.assertEqual(saved["first"], "canonical-request-b")
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(result["stop_confirmed"])
        self.assertTrue(result["native"]["client_request_id_missing"])
        self.assertEqual(result["native"]["native_request_id"], "canonical-request-b")
        kinds = [event["kind"] for event in self.store.get_events(task["id"])]
        self.assertIn("client_request_id_missing", kinds)
        self.assertEqual(len(desktop.sent), 1, "matching by the saved id never resends")

    def test_an_unidentifiable_restart_is_refused_rather_than_guessed(self):
        task = self.claimed(timeout_seconds=1)

        class Anonymous(FakeDesktop):
            def observe(inner, conversation_id, request_id, native_request_id=None):
                inner.observations += 1
                # Nothing about the user's own client id is accepted as canonical.
                return {"conversationState": "active", "conversationExists": True, "requestCount": 2,
                        "requestSeen": False, "state": None,
                        "matchBy": None, "matchAmbiguous": False,
                        "requestsWithoutClientRequestId": 2,
                        "nativeRequestId": None, "nativeRequestIdSource": None,
                        "hasActiveChildAgents": None, "hasRunningBackgroundTasks": None}

        desktop = Anonymous(states=("running",))
        result = self.run_task(task, desktop, poll_interval=0.02)
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["stop_confirmed"])
        self.assertEqual(len(desktop.sent), 1, "an unidentifiable run is never resent")
        self.assertIsNone(result["native"].get("native_request_id"),
                          "no canonical id is invented from the client UUID or the clock")
        events = self.store.get_events(task["id"])
        lost = [event for event in events if event["kind"] == "request_identity_lost"]
        self.assertEqual(len(lost), 1)
        self.assertFalse(lost[0]["data"]["native_request_id_saved"])
        self.assertFalse(any(event["kind"] == "native_request_identity_saved" for event in events))

    def test_two_canonical_ids_for_one_dispatch_are_not_averaged(self):
        task = self.claimed()

        class Shifting(FakeDesktop):
            def observe(inner, conversation_id, request_id, native_request_id=None):
                payload = super().observe(conversation_id, request_id, native_request_id)
                if inner.observations > 1:
                    payload["nativeRequestId"] = "canonical-request-z"
                return payload

        result = self.run_task(task, Shifting(native_request_id="canonical-request-a"))
        self.assertEqual(result["native"]["native_request_id"], "canonical-request-a",
                         "the first observed canonical id is kept")
        conflict = result["native"]["native_request_identity_conflict"]
        self.assertEqual(conflict["stored"], "canonical-request-a")
        self.assertEqual(conflict["observed"], "canonical-request-z")
        self.assertFalse(result["usage"]["transcript"]["available"])
        self.assertIn("Two different canonical request ids", result["usage"]["transcript"]["reason"])
        self.assertIsNone(result["usage"]["transcript"]["rounds"])
        self.assertTrue(any(event["kind"] == "native_request_identity_conflict"
                            for event in self.store.get_events(task["id"])))

    def test_rework_does_not_carry_the_previous_canonical_id(self):
        task = self.claimed()
        first = FakeDesktop(native_request_id="canonical-request-first")
        self.run_task(task, first)
        reworked = self.store.rework_task(task["id"], "补充来源说明")
        self.assertNotIn("native_request_id", reworked["native"])
        self.assertNotIn("native_request_id_source", reworked["native"])
        second = FakeDesktop(native_request_id="canonical-request-second")
        final = self.run_task(self.store.claim_next("round-two"), second)
        self.assertEqual(final["native"]["native_request_id"], "canonical-request-second")
        self.assertNotEqual(final["native"]["native_request_id"], "canonical-request-first")


class BackendRoutingTests(DesktopTestCase):
    def test_desktop_is_the_default_and_has_no_automatic_fallback(self):
        task = self.claimed()
        self.assertEqual(task["backend"], BACKEND_DESKTOP)
        desktop = FakeDesktop()
        run_task(self.store, task, discovery={"supported": False, "errors": ["no CLI"]}, desktop=desktop)
        self.assertEqual(len(desktop.sent), 1)

    def test_compatibility_mode_never_falls_back_to_desktop(self):
        task = self.claimed(backend=BACKEND_CLI)
        self.assertEqual(task["backend"], BACKEND_CLI)
        desktop = FakeDesktop()
        run_task(self.store, task, discovery={"supported": False, "errors": ["no CLI"]}, desktop=desktop)
        self.assertEqual(desktop.sent, [])
        self.assertEqual(desktop.created, [])
        result = self.store.get_task(task["id"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("executor unavailable", result["error"])

    def test_idempotency_fingerprint_includes_the_backend(self):
        first = self.store.submit_task(str(self.workspace), "同一目标", ["约束"], "shared-key")
        again = self.store.submit_task(str(self.workspace), "同一目标", ["约束"], "shared-key")
        self.assertEqual(first["id"], again["id"])
        with self.assertRaisesRegex(ValueError, "different request"):
            self.store.submit_task(str(self.workspace), "同一目标", ["约束"], "shared-key", backend=BACKEND_CLI)
        with self.assertRaises(ValueError):
            self.store.submit_task(str(self.workspace), "x", backend="unknown_backend")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.environ.get("BUDDYGATE_TEST_TEMP", tempfile.gettempdir())).resolve()
        self.root = self.base / f"buddygate-migration-test-{uuid.uuid4()}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.db = self.root / "tasks.sqlite3"

    def test_legacy_rows_keep_their_cli_meaning_and_gain_a_backend(self):
        legacy = {"id": "task-legacy", "workspace": str(self.root), "objective": "旧任务", "constraints": [],
                  "model": None, "timeout_seconds": 900, "attempt": 1, "status": "needs_review",
                  "session_id": "old-cli-session", "feedback": "", "result": {"text": "旧结果"},
                  "error": None, "permission": None, "worker_id": "1", "stop_confirmed": True,
                  "created_at": "2026-10-27T00:00:00+00:00", "updated_at": "2026-10-27T00:00:00+00:00"}
        connection = sqlite3.connect(str(self.db))
        connection.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
                                fingerprint TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                                 kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        connection.execute("INSERT INTO tasks VALUES(?,?,?,?)",
                           ("task-legacy", "legacy-key", "fingerprint", json.dumps(legacy)))
        connection.commit()
        connection.close()

        store = Store(self.db)
        migrated = store.get_task("task-legacy")
        self.assertEqual(migrated["backend"], BACKEND_CLI)
        self.assertEqual(migrated["status"], "needs_review")
        self.assertEqual(migrated["session_id"], "old-cli-session")
        self.assertEqual(migrated["result"]["text"], "旧结果")
        self.assertIsNone(migrated["native"])
        self.assertIsNone(migrated["usage"])
        # Re-opening must be idempotent and must not rewrite the fingerprint.
        Store(self.db)
        self.assertEqual(Store(self.db).get_task("task-legacy")["backend"], BACKEND_CLI)


class MeterTests(unittest.TestCase):
    def test_native_usage_never_invents_output_or_billing(self):
        normalized = native_usage({"usage": {"inputTokens": 10, "totalTokens": 25, "contextWindow": 1000,
                                             "outputTokens": 0, "cachedTokens": 5,
                                             "cost": 0.5, "estimatedCost": {"amount": 1}}})
        self.assertEqual(normalized["input_tokens"], 10)
        self.assertEqual(normalized["total_tokens"], 25)
        self.assertIsNone(normalized["output_tokens"], "a top-level output field was never observed")
        self.assertIsNone(normalized["cached_input_tokens"])
        self.assertIsNone(normalized["billed_tokens"])
        self.assertIsNone(normalized["billed_amount"])
        self.assertEqual(normalized["observed_fields"], ["contextWindow", "inputTokens", "totalTokens"])
        self.assertEqual(normalized["ignored_fields"], ["cachedTokens", "cost", "estimatedCost", "outputTokens"])
        self.assertIn("Not a bill", " ".join(normalized["notes"]))
        self.assertIn("not verified", normalized["scope"])
        self.assertIsNone(native_usage(None)["total_tokens"])
        self.assertIsNone(native_usage({"usage": {"inputTokens": True}})["input_tokens"])

    def test_native_details_are_mapped_by_name_and_stay_separate_from_top_level(self):
        normalized = native_usage({"usage": {
            "top": {"inputTokens": 37200, "totalTokens": 37200, "contextWindow": 300000},
            "details": {"promptTokens": 34265, "completionTokens": 289, "promptCacheHitTokens": 34048,
                        "promptCacheMissTokens": 217, "promptCacheWriteTokens": 0},
            "ignored_fields": ["cost"],
        }})
        self.assertEqual(normalized["prompt_tokens"], 34265)
        self.assertEqual(normalized["output_tokens"], 289)
        self.assertEqual(normalized["prompt_cache_hit_tokens"], 34048)
        self.assertEqual(normalized["prompt_cache_miss_tokens"], 217)
        self.assertEqual(normalized["prompt_cache_write_tokens"], 0)
        self.assertEqual(normalized["input_tokens"], 37200, "top-level and details counters are not merged")
        self.assertTrue(normalized["detail_arithmetic_check"]["prompt_split_consistent"])
        self.assertIsNone(normalized["cached_input_tokens"])

    def test_invalid_token_counters_are_refused(self):
        normalized = native_usage({"usage": {
            "top": {"inputTokens": True, "totalTokens": float("nan"), "contextWindow": float("inf")},
            "details": {"promptTokens": -1, "completionTokens": "289", "promptCacheWriteTokens": 1.5},
            "invalid_fields": ["inputTokens", "totalTokens", "contextWindow", "details.promptTokens",
                               "details.completionTokens", "details.promptCacheWriteTokens"],
        }})
        for field in ("input_tokens", "total_tokens", "context_window", "prompt_tokens",
                      "output_tokens", "prompt_cache_write_tokens"):
            self.assertIsNone(normalized[field], field)
        self.assertEqual(len(normalized["observed_invalid_fields"]), 6)
        self.assertIsNone(normalized["detail_arithmetic_check"])

    def codex_file(self, events, label="codex-meter"):
        path = Path(tempfile.gettempdir()).resolve() / f"{label}-{uuid.uuid4()}.jsonl"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        lines = []
        for index, total in enumerate(events):
            lines.append(json.dumps({"timestamp": f"2026-10-28T00:0{index}:00Z", "type": "event_msg",
                                     "payload": {"type": "token_count", "info": {"total_token_usage": total}}}))
        lines.append(json.dumps({"timestamp": "2026-10-28T00:09:00Z", "type": "event_msg",
                                 "payload": {"type": "agent_message", "message": "not a meter"}}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_codex_snapshot_and_delta_keep_channels_separate(self):
        path = self.codex_file([
            {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10,
             "reasoning_output_tokens": 3, "total_tokens": 113},
            {"input_tokens": 1500, "cached_input_tokens": 900, "output_tokens": 120,
             "reasoning_output_tokens": 30, "total_tokens": 1660},
        ])
        snapshot = codex_snapshot(str(path))
        self.assertEqual(snapshot["events"], 2)
        self.assertEqual(snapshot["window"]["first"], "2026-10-28T00:00:00Z")
        self.assertEqual(snapshot["window"]["last"], "2026-10-28T00:01:00Z")
        self.assertIn("other chats are never read", snapshot["scope"])
        unchanged = codex_delta(snapshot, snapshot)
        self.assertEqual(unchanged["delta"]["total_tokens"], 0)
        self.assertEqual(unchanged["delta"]["output_tokens"], 0)
        self.assertNotIn("error", unchanged)
        self.assertIsNone(unchanged.get("reset_fields"))

        before = codex_snapshot(str(path))
        earlier = dict(before)
        earlier["tokens"] = {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10,
                            "reasoning_output_tokens": 3, "total_tokens": 113}
        earlier["window"] = {"first": "2026-10-28T00:00:00Z", "last": "2026-10-28T00:00:00Z"}
        earlier["thread_file"] = str(path)
        delta = codex_delta(earlier, before)
        self.assertEqual(delta["delta"]["total_tokens"], 1547)
        self.assertEqual(delta["delta"]["output_tokens"], 110)
        self.assertEqual(delta["delta"]["reasoning_output_tokens"], 27)
        # Generation is the output channel only; uncached input is listed separately.
        self.assertEqual(delta["output_generation_tokens"], 110)
        self.assertEqual(delta["uncached_input_tokens"], 1400 - 860)
        self.assertNotIn("new_generation_tokens", delta)
        self.assertIn("never added to output_tokens", " ".join(delta["notes"]))
        self.assertEqual(delta["window"], {"first": "2026-10-28T00:00:00Z", "last": "2026-10-28T00:01:00Z"})

    def test_codex_delta_refuses_to_mix_two_thread_files(self):
        first = self.codex_file([{"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1,
                                  "reasoning_output_tokens": 0, "total_tokens": 11}], label="codex-a")
        second = self.codex_file([{"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 5,
                                   "reasoning_output_tokens": 0, "total_tokens": 55}], label="codex-b")
        before, after = codex_snapshot(str(first)), codex_snapshot(str(second))
        delta = codex_delta(before, after)
        self.assertIsNone(delta["delta"])
        self.assertIsNone(delta["output_generation_tokens"])
        self.assertIn("different thread files", delta["error"])

    def test_codex_reset_or_negative_increment_is_unavailable(self):
        higher = {"thread_file": "C:/threads/one.jsonl", "events": 4,
                  "tokens": {"input_tokens": 900, "cached_input_tokens": 500, "output_tokens": 90,
                             "reasoning_output_tokens": 9, "total_tokens": 999},
                  "window": {"first": "2026-10-28T00:00:00Z", "last": "2026-10-28T00:05:00Z"}}
        lower = {"thread_file": "C:/threads/one.jsonl", "events": 1,
                 "tokens": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1,
                            "reasoning_output_tokens": 0, "total_tokens": 11},
                 "window": {"first": "2026-10-28T00:00:00Z", "last": "2026-10-28T00:06:00Z"}}
        delta = codex_delta(higher, lower)
        self.assertIsNone(delta["delta"])
        self.assertIsNone(delta["output_generation_tokens"])
        self.assertIsNone(delta["uncached_input_tokens"])
        self.assertIn("reset", delta["error"])
        self.assertIn("events", delta["reset_fields"])

    def test_codex_missing_counters_are_reported_not_estimated(self):
        missing = codex_delta({"thread_file": "C:/threads/one.jsonl", "tokens": None},
                              {"thread_file": "C:/threads/one.jsonl",
                               "tokens": {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1,
                                          "reasoning_output_tokens": 0, "total_tokens": 6}})
        self.assertIsNone(missing["delta"])
        self.assertIn("no token counters", missing["error"])
        self.assertIsNone(missing["window"]["first"])

    def test_missing_thread_file_is_reported_not_estimated(self):
        result = codex_snapshot(str(Path(tempfile.gettempdir()) / "does-not-exist.jsonl"))
        self.assertEqual(result["events"], 0)
        self.assertIn("missing", result["error"])
        self.assertIsNone(result["tokens"])
        relative = codex_snapshot("relative.jsonl")
        self.assertIn("absolute", relative["error"])

    def test_uuid7_is_time_ordered_and_versioned(self):
        first = uuid7()
        second = uuid7()
        self.assertEqual(uuid.UUID(first).version, 7)
        self.assertEqual(uuid.UUID(first).variant, uuid.RFC_4122)
        self.assertEqual(uuid.UUID(second).version, 7)
        self.assertLess(first[:13], "8")


class SummaryTests(DesktopTestCase):
    def test_acceptance_text_reflects_what_was_recorded(self):
        task = self.claimed()
        self.run_task(task, FakeDesktop())
        pending = summarize_task(self.store.get_task(task["id"]))
        self.assertEqual(pending["acceptance"]["status"], "not_accepted_yet")
        self.assertIn("needs_review is not acceptance", pending["acceptance"]["detail"])
        self.assertTrue(pending["stop_confirmed"])
        self.assertEqual(pending["activity"]["state"], "clear")

        accepted = self.store.accept_task(task["id"], "已独立核对产物", ["逐字节比对 smoke-result.json"])
        view = summarize_task(accepted)
        self.assertEqual(view["acceptance"]["status"], "accepted_by_manager")
        self.assertIn("did not verify business correctness", view["acceptance"]["detail"])

        unstarted = summarize_task(self.store.submit_task(str(self.workspace), "尚未执行"))
        self.assertEqual(unstarted["acceptance"]["status"], "not_accepted")
        self.assertEqual(unstarted["usage"]["native"], None)
        self.assertIsNone(unstarted["usage"]["combined"])


class PromptTests(unittest.TestCase):
    def test_prompt_starts_with_an_explicit_dispatch_heading(self):
        task = {"id": "task-abcdef123456", "attempt": 2, "workspace": "C:/work",
                "objective": "写报告\n第二行不应进入标题", "constraints": ["只读"], "feedback": "补来源"}
        title = dispatch_title(task)
        prompt = prompt_for(task)
        self.assertTrue(title.startswith("【派工】"))
        self.assertNotIn("\n", title)
        self.assertTrue(prompt.startswith(title + "\n"))
        self.assertIn("第 2 轮", prompt)
        self.assertIn("补来源", prompt)
        self.assertIn("验收由管理者独立完成", prompt)


class InterfaceTests(unittest.TestCase):
    def test_mcp_exposes_the_desktop_surface_and_refuses_an_unknown_backend(self):
        from buddygate.mcp import handle

        tools = {tool["name"]: tool for tool in handle({"method": "tools/list"})["tools"]}
        for name in ("buddy_desktop_status", "buddy_desktop_connect", "buddy_usage", "buddy_codex_usage"):
            self.assertIn(name, tools)
        self.assertEqual(tools["buddy_submit"]["inputSchema"]["properties"]["backend"]["enum"],
                         ["desktop_visible", "cli_acp"])
        refused = handle({"method": "tools/call", "params": {"name": "buddy_submit", "arguments": {
            "workspace": str(Path(__file__).resolve().parent), "objective": "x", "backend": "auto"}}})
        self.assertTrue(refused["isError"])
        self.assertIn("must be one of", refused["content"][0]["text"])

    def test_no_personal_absolute_paths_are_baked_into_the_source(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for pattern in ("buddygate/*.py", "buddygate/*.mjs", "scripts/*.py", "scripts/*.mjs"):
            for path in root.glob(pattern):
                text = path.read_text(encoding="utf-8")
                for needle in ("D:/workbuddy", "D:\\workbuddy", "C:/Users/", "C:\\Users\\"):
                    if needle in text:
                        offenders.append(f"{path.name}: {needle}")
        self.assertEqual(offenders, [], "A personal install path must be supplied by configuration, not source")

    def test_publish_manifest_covers_every_source_file(self):
        root = Path(__file__).resolve().parent.parent
        listed = {line.strip() for line in (root / "docs" / "PUBLISH_MANIFEST.txt")
                  .read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.startswith("#")}
        actual = set()
        for pattern in ("buddygate/*.py", "buddygate/*.mjs", "scripts/*.py", "scripts/*.mjs", "tests/*.py"):
            for path in root.glob(pattern):
                actual.add(path.relative_to(root).as_posix())
        self.assertEqual(sorted(actual - listed), [], "Every source file must be listed before publishing")
        missing = sorted(name for name in listed if not (root / name).is_file())
        self.assertEqual(missing, [], "Manifest lists files that no longer exist")


if __name__ == "__main__":
    unittest.main()

"""Mock-only tests for the persisted transcript usage ledger.

No real WorkBuddy directory is read here: every case builds a temporary
``workbuddy.db`` plus ``projects`` tree, so the guards (path escape, several
matches, oversized input) are exercised without touching user data.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from buddygate.store import Store
from buddygate.summary import usage_view
from buddygate.usage_ledger import (Limits, compress_workspace_path_name, ledger_for_task,
                                    transcript_usage)

CONVERSATION = "01a181ba-daa6-7371-ac37-67ece0b0f9ab"
OTHER_CONVERSATION = "01a181bb-1111-7000-8000-000000000002"
REQUEST = "01a181badc527826b86c00d33a6cf078"
OTHER_REQUEST = "01a181badc527826b86c00d33a6cf079"
CWD = "C:/work/sample"


def round_record(message_id, request_id, input_tokens, output_tokens, *, cached=None,
                 reasoning=None, total=None, requests=1, timestamp=1, model="deepseek-v4.1-flash",
                 raw_miss=None, raw_hit=None, total_override=False):
    usage = {"requests": requests, "inputTokens": input_tokens, "outputTokens": output_tokens,
             "totalTokens": input_tokens + output_tokens if total is None else total}
    if cached is not None:
        usage["inputTokensDetails"] = [{"cached_tokens": cached}]
    if reasoning is not None:
        usage["outputTokensDetails"] = [{"reasoning_tokens": reasoning}]
    provider = {"messageId": message_id, "conversationRequestId": request_id,
                "requestModelId": model, "usage": usage}
    if raw_miss is not None or raw_hit is not None:
        provider["rawUsage"] = {}
        if raw_hit is not None:
            provider["rawUsage"]["prompt_cache_hit_tokens"] = raw_hit
        if raw_miss is not None:
            provider["rawUsage"]["prompt_cache_miss_tokens"] = raw_miss
        provider["rawUsage"]["credit"] = 1.21
    if total_override:
        usage["totalTokens"] = None
    return {"type": "function_call", "timestamp": timestamp, "providerData": provider}


def user_record(request_id, timestamp=0):
    return {"type": "message", "timestamp": timestamp,
            "providerData": {"conversationRequestId": request_id, "startsNewUserRequest": True}}


class LedgerCase(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("BUDDYGATE_TEST_TEMP", tempfile.gettempdir())).resolve()
        self.root = base / f"buddygate-ledger-test-{uuid.uuid4()}"
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.config = self.root / "config"
        (self.config / "projects").mkdir(parents=True)
        self.database = self.config / "workbuddy.db"
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)")
        connection.commit()
        connection.close()

    def cleanup(self):
        resolved = self.root.resolve()
        if resolved.parent != Path(os.environ.get("BUDDYGATE_TEST_TEMP", tempfile.gettempdir())).resolve() \
                or not resolved.name.startswith("buddygate-ledger-test-"):
            raise RuntimeError("Refusing to clean a directory outside the allocated test root")
        shutil.rmtree(resolved)

    def register(self, conversation_id=CONVERSATION, cwd=CWD):
        connection = sqlite3.connect(self.database)
        connection.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (conversation_id, cwd))
        connection.commit()
        connection.close()
        return cwd

    def write(self, records, conversation_id=CONVERSATION, cwd=CWD, raw_lines=()):
        self.register(conversation_id, cwd)
        directory = self.config / "projects" / compress_workspace_path_name(cwd)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{conversation_id}.jsonl"
        lines = [json.dumps(record) for record in records] + list(raw_lines)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def usage(self, **kwargs):
        kwargs.setdefault("native_request_id", REQUEST)
        kwargs.setdefault("config_dir", str(self.config))
        return transcript_usage(CONVERSATION, **kwargs)


class SumTests(LedgerCase):
    def test_rounds_are_summed_with_channels_kept_apart(self):
        self.write([
            user_record(REQUEST),
            round_record("01a181bb000000000000000000000001", REQUEST, 32577, 236, cached=256, reasoning=139),
            round_record("01a181bb000000000000000000000002", REQUEST, 32965, 112, cached=32768, reasoning=8),
            round_record("01a181bb000000000000000000000003", REQUEST, 33142, 178, cached=32896, reasoning=0),
        ])
        result = self.usage()
        self.assertTrue(result["available"], result.get("reason"))
        self.assertEqual(result["rounds"], 3)
        self.assertEqual(result["attributed_by"], "native_request_id")
        tokens = result["tokens"]
        self.assertEqual(tokens["input_tokens"], 32577 + 32965 + 33142)
        self.assertEqual(tokens["output_tokens"], 236 + 112 + 178)
        self.assertEqual(tokens["total_tokens"], tokens["input_tokens"] + tokens["output_tokens"])
        self.assertEqual(tokens["cache_hit_tokens"], 256 + 32768 + 32896)
        self.assertEqual(tokens["cache_miss_tokens"], tokens["input_tokens"] - tokens["cache_hit_tokens"])
        self.assertTrue(result["diagnostics"]["cache_miss_derived"])
        # Reasoning is listed separately and never folded into output.
        self.assertEqual(tokens["reasoning_tokens_separate"], 139 + 8)
        self.assertNotIn("reasoning", {key for key in tokens if key != "reasoning_tokens_separate"})
        self.assertEqual(result["completeness"]["status"], "all_recorded_rounds")
        self.assertEqual(result["completeness"]["gaps"], [])
        self.assertTrue(result["completeness"]["only_persisted_calls"])

    def test_identical_duplicate_round_is_counted_once(self):
        record = round_record("01a181bb000000000000000000000001", REQUEST, 100, 10, cached=40)
        self.write([user_record(REQUEST), record, record])
        result = self.usage()
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(result["tokens"]["input_tokens"], 100)
        self.assertEqual(result["diagnostics"]["duplicate_identical_rounds"], 1)

    def test_conflicting_duplicate_round_is_not_averaged(self):
        self.write([
            user_record(REQUEST),
            round_record("01a181bb000000000000000000000001", REQUEST, 100, 10),
            round_record("01a181bb000000000000000000000001", REQUEST, 900, 10),
            round_record("01a181bb000000000000000000000002", REQUEST, 200, 20),
        ])
        result = self.usage()
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(result["tokens"]["input_tokens"], 200)
        self.assertEqual(result["diagnostics"]["conflicting_message_ids"], 1)
        self.assertEqual(result["completeness"]["status"], "partial")
        self.assertTrue(any("conflicting" in gap for gap in result["completeness"]["gaps"]))

    def test_invalid_counters_are_refused_not_read_as_zero(self):
        record = round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)
        record["providerData"]["usage"]["inputTokens"] = float("nan")
        record["providerData"]["usage"]["outputTokens"] = True
        self.write([user_record(REQUEST), record])
        result = self.usage()
        self.assertTrue(result["available"])
        self.assertIsNone(result["tokens"]["input_tokens"])
        self.assertIsNone(result["tokens"]["output_tokens"])
        self.assertEqual(result["completeness"]["status"], "partial")
        self.assertTrue(any("invalid counter" in gap for gap in result["completeness"]["gaps"]))

    def test_a_broken_line_is_reported_and_other_rounds_still_count(self):
        self.write([user_record(REQUEST), round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)],
                   raw_lines=['{"providerData": not json'])
        result = self.usage()
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(result["tokens"]["input_tokens"], 100)
        self.assertEqual(result["diagnostics"]["parse_errors"], 1)
        self.assertTrue(any("unparsed" in gap for gap in result["completeness"]["gaps"]))

    def test_an_oversized_line_is_skipped_and_flagged(self):
        oversized = '{"providerData": ' + " " * 1200 + '"x"}'
        self.write([user_record(REQUEST), round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)],
                   raw_lines=[oversized])
        result = self.usage(limits=Limits(max_line_bytes=600))
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(result["tokens"]["input_tokens"], 100)
        self.assertEqual(result["diagnostics"]["oversized_lines"], 1)
        self.assertEqual(result["completeness"]["status"], "partial")

    def test_a_missing_channel_stays_null_and_marks_the_sum_partial(self):
        record = round_record("01a181bb000000000000000000000001", REQUEST, 100, 10, total_override=True)
        self.write([user_record(REQUEST), record])
        result = self.usage()
        self.assertIsNone(result["tokens"]["total_tokens"])
        self.assertEqual(result["tokens"]["input_tokens"], 100)
        self.assertIn("missing channel(s): total_tokens", " ".join(result["completeness"]["gaps"]))

    def test_a_reported_cache_miss_is_not_labelled_derived(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10, cached=40, raw_miss=60)])
        result = self.usage()
        self.assertEqual(result["tokens"]["cache_miss_tokens"], 60)
        self.assertFalse(result["diagnostics"]["cache_miss_derived"])

    def test_cache_hit_above_input_is_not_turned_into_a_negative_miss(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10, cached=400)])
        result = self.usage()
        self.assertIsNone(result["tokens"]["cache_miss_tokens"])
        self.assertTrue(result["per_round"][0]["cache_hit_exceeds_input"])

    def test_non_token_and_credit_fields_are_never_converted(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10, cached=40, raw_miss=60)])
        result = self.usage()
        self.assertNotIn("credit", result["per_round"][0])
        self.assertNotIn("1.21", json.dumps(result["per_round"]))
        self.assertNotIn("1.21", json.dumps(result["tokens"]))


class AttributionTests(LedgerCase):
    def test_another_request_in_the_same_conversation_is_excluded(self):
        self.write([
            user_record(OTHER_REQUEST, 0),
            round_record("01a181bb00000000000000000000000a", OTHER_REQUEST, 5000, 500),
            user_record(REQUEST, 10),
            round_record("01a181bb000000000000000000000001", REQUEST, 100, 10),
            round_record("01a181bb000000000000000000000002", REQUEST, 200, 20),
        ])
        result = self.usage()
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["tokens"]["input_tokens"], 300)
        self.assertEqual(result["diagnostics"]["excluded_rounds"], 1)
        self.assertEqual(result["diagnostics"]["other_request_ids"], [OTHER_REQUEST])
        self.assertTrue(any("other requests" in warning for warning in result["completeness"]["warnings"]))

    def test_several_requests_without_a_native_id_refuse_to_guess(self):
        self.write([
            user_record(OTHER_REQUEST),
            round_record("01a181bb00000000000000000000000a", OTHER_REQUEST, 5000, 500),
            user_record(REQUEST, 10),
            round_record("01a181bb000000000000000000000001", REQUEST, 100, 10),
        ])
        result = self.usage(native_request_id=None)
        self.assertFalse(result["available"])
        self.assertIn("refusing to guess", result["reason"])
        self.assertIsNone(result["tokens"])

    def test_a_single_request_conversation_is_attributed_without_an_id(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)])
        result = self.usage(native_request_id=None)
        self.assertTrue(result["available"])
        self.assertEqual(result["attributed_by"], "single_request_in_conversation")
        self.assertEqual(result["tokens"]["input_tokens"], 100)

    def test_a_round_of_another_request_is_never_counted_for_this_one(self):
        self.write([
            user_record(OTHER_REQUEST),
            round_record("01a181bb00000000000000000000000a", OTHER_REQUEST, 5000, 500),
        ])
        result = self.usage()
        self.assertFalse(result["available"])
        self.assertIn("another request", result["reason"])

    def test_rounds_without_a_request_id_are_excluded_and_reported(self):
        record = round_record("01a181bb000000000000000000000001", None, 100, 10)
        self.write([user_record(REQUEST), record,
                    round_record("01a181bb000000000000000000000002", REQUEST, 200, 20)])
        result = self.usage()
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(result["tokens"]["input_tokens"], 200)
        self.assertTrue(any("no conversation request id" in warning
                            for warning in result["completeness"]["warnings"]))


class PathGuardTests(LedgerCase):
    def test_a_non_uuid_conversation_id_is_refused_before_any_path(self):
        result = transcript_usage("../../workbuddy.db", config_dir=str(self.config))
        self.assertFalse(result["available"])
        self.assertIn("not a UUID", result["reason"])

    def test_a_missing_transcript_is_reported_not_estimated(self):
        self.register()
        result = self.usage()
        self.assertFalse(result["available"])
        self.assertIn("no transcript file", result["reason"])
        self.assertIsNone(result["tokens"])

    def test_several_matching_transcripts_are_refused(self):
        self.write([user_record(REQUEST), round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)])
        second = self.config / "projects" / "another-project"
        second.mkdir()
        shutil.copy(self.config / "projects" / compress_workspace_path_name(CWD) / f"{CONVERSATION}.jsonl",
                    second / f"{CONVERSATION}.jsonl")
        result = self.usage()
        self.assertFalse(result["available"])
        self.assertIn("several transcript files", result["reason"])

    def test_a_symlinked_transcript_is_refused(self):
        path = self.write([user_record(REQUEST),
                           round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)])
        target = self.root / "elsewhere.jsonl"
        path.replace(target)
        try:
            os.symlink(target, path)
        except (OSError, NotImplementedError):
            self.skipTest("This host does not allow creating symlinks")
        result = self.usage()
        self.assertFalse(result["available"])
        self.assertIn("link", result["reason"])

    def test_an_oversized_transcript_is_refused(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)])
        result = self.usage(limits=Limits(max_bytes=16))
        self.assertFalse(result["available"])
        self.assertIn("larger than the readable size bound", result["reason"])

    def test_an_unverified_config_directory_is_reported(self):
        empty = self.root / "empty"
        empty.mkdir()
        result = transcript_usage(CONVERSATION, native_request_id=REQUEST, config_dir=str(empty))
        self.assertFalse(result["available"])
        self.assertIn("config directory", result["reason"])


class SnapshotAndViewTests(LedgerCase):
    def test_a_snapshot_mismatch_is_reported_without_attribution(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 30000, 100),
                    round_record("01a181bb000000000000000000000002", REQUEST, 31432, 150)])
        result = self.usage(api_snapshot={"input_tokens": 37202, "output_tokens": 150,
                                          "prompt_cache_hit_tokens": None})
        alignment = result["snapshot_alignment"]
        self.assertFalse(alignment["aligned"])
        self.assertEqual(alignment["checks"]["input_tokens"]["last_round"], 31432)
        self.assertTrue(any("does not equal" in warning for warning in result["completeness"]["warnings"]))
        # The sum is still the sum of rounds, not the snapshot.
        self.assertEqual(result["tokens"]["input_tokens"], 61432)

    def test_matching_snapshot_is_reported_as_aligned(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)])
        result = self.usage(api_snapshot={"input_tokens": 100, "output_tokens": 10})
        self.assertTrue(result["snapshot_alignment"]["aligned"])

    def test_ledger_for_task_ignores_a_cli_compatibility_task(self):
        task = {"backend": "cli_acp", "session_id": "s-1", "native": None, "usage": None}
        self.assertIsNone(ledger_for_task(task, config_dir=str(self.config)))

    def test_usage_view_keeps_the_transcript_meter_separate(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)])
        view = usage_view({"native": {"input_tokens": 90}, "codex": None, "transcript": self.usage(),
                           "combined": None, "notes": []})
        self.assertEqual(view["native"]["input_tokens"], 90)
        self.assertEqual(view["transcript"]["rounds"], 1)
        self.assertEqual(view["transcript"]["tokens"]["input_tokens"], 100)
        self.assertIsNone(view["combined"])
        self.assertIn("Three meters", view["note"])

    def test_usage_without_a_transcript_still_reports_native(self):
        view = usage_view({"native": {"input_tokens": 90}, "codex": None})
        self.assertIsNone(view["transcript"])
        self.assertEqual(view["native"]["input_tokens"], 90)

    def test_the_identity_source_is_echoed_and_the_client_uuid_is_not_canonical(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10, cached=60)])
        result = self.usage(native_request_id_source="conversationRequestId")
        self.assertEqual(result["identity"]["native_request_id"], REQUEST)
        self.assertEqual(result["identity"]["source"], "conversationRequestId")
        self.assertFalse(result["identity"]["client_request_id_is_canonical"])
        self.assertEqual(result["attributed_by"], "native_request_id")

    def test_an_unknown_identity_source_is_not_echoed(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)])
        result = self.usage(native_request_id_source="guessed")
        self.assertIsNone(result["identity"]["source"])

    def test_a_conflicting_canonical_id_blocks_attribution(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10)])
        store = Store(self.root / "tasks.sqlite3")
        task = store.submit_task(str(self.root), "目标", [], None, None, backend="desktop_visible")
        store.update_task(task["id"], session_id=CONVERSATION, native={
            "backend": "desktop_visible", "conversation_id": CONVERSATION, "request_id": "client-1",
            "dispatch": "sent", "native_request_id": REQUEST,
            "native_request_identity_conflict": {"stored": REQUEST, "observed": OTHER_REQUEST}})
        result = ledger_for_task(store.get_task(task["id"]), config_dir=str(self.config))
        self.assertFalse(result["available"])
        self.assertIn("Two different canonical request ids", result["reason"])
        self.assertIsNone(result["rounds"])
        self.assertIsNone(result["tokens"])
        self.assertIsNone(result["transcript_file"], "no transcript is read when the identity conflicts")


class ExecutionIntegrationTests(LedgerCase):
    """One visible dispatch records only the rounds its own request persisted."""

    def test_a_run_records_the_rounds_of_its_own_request_only(self):
        from buddygate.desktop_exec import execute_desktop
        from tests.test_desktop import FakeDesktop

        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 100, 10, cached=40, timestamp=5),
                    round_record("01a181bb000000000000000000000002", REQUEST, 300, 20, cached=200, timestamp=9),
                    user_record(OTHER_REQUEST, timestamp=10),
                    round_record("01a181bb000000000000000000000003", OTHER_REQUEST, 999, 99, timestamp=11)])
        store = Store(self.root / "state" / "tasks.sqlite3")
        task = store.submit_task(str(self.root), "目标", [], None, None, backend="desktop_visible")
        store.update_task(task["id"], session_id=CONVERSATION, native={"conversation_id": CONVERSATION})
        claimed = store.claim_next("desktop-worker")
        result = execute_desktop(store, claimed, client=FakeDesktop(native_request_id=REQUEST),
                                 poll_interval=0.01, config_dir=str(self.config))
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["native"]["native_request_id"], REQUEST)
        transcript = result["usage"]["transcript"]
        self.assertTrue(transcript["available"], transcript.get("reason"))
        self.assertEqual(transcript["rounds"], 2)
        self.assertEqual(transcript["tokens"]["input_tokens"], 400)
        self.assertEqual(transcript["tokens"]["output_tokens"], 30)
        self.assertEqual(transcript["attributed_by"], "native_request_id")
        self.assertEqual(transcript["identity"]["source"], "conversationRequestId")
        self.assertTrue(any("other requests" in warning
                            for warning in transcript["completeness"]["warnings"]))


class CliRefreshTests(LedgerCase):
    """A read-only refresh must never modify an accepted record."""

    def accepted(self, native):
        state = self.root / "state"
        state.mkdir(exist_ok=True)
        store = Store(state / "tasks.sqlite3")
        task = store.submit_task(str(self.root), "只读核对用量", [], None, None, backend="desktop_visible")
        store.update_task(task["id"], status="needs_review", stop_confirmed=True,
                          session_id=CONVERSATION, native=native)
        store.accept_task(task["id"], "经理独立核对产物后验收", ["readme.md"])
        return store, task["id"]

    def refresh(self, task_id):
        from unittest import mock

        from buddygate import cli
        with mock.patch.dict(os.environ, {"BUDDYGATE_STATE_DIR": str(self.root / "state")}):
            return cli.invoke("buddy_usage", {"task_id": task_id, "refresh": True,
                                             "config_dir": str(self.config)})

    def test_an_accepted_task_is_read_without_being_modified(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 120, 8),
                    user_record(OTHER_REQUEST, timestamp=2),
                    round_record("01a181bb000000000000000000000002", OTHER_REQUEST, 200, 9, timestamp=3)])
        store, task_id = self.accepted({
            "backend": "desktop_visible", "conversation_id": CONVERSATION, "request_id": "client-1",
            "dispatch": "sent", "native_request_id": OTHER_REQUEST,
            "native_request_id_source": "conversationRequestId"})
        before = store.get_task(task_id)["usage"]
        result = self.refresh(task_id)
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["refresh"]["stored"])
        self.assertTrue(result["refresh"]["read_only"])
        self.assertFalse(result["refresh"]["prompt_resent"])
        self.assertTrue(result["usage"]["transcript"]["available"])
        self.assertEqual(result["usage"]["transcript"]["rounds"], 1)
        self.assertEqual(result["usage"]["transcript"]["tokens"]["input_tokens"], 200,
                         "only the attributed request is counted")
        self.assertEqual(store.get_task(task_id)["usage"], before,
                         "the accepted record keeps its stored usage unchanged")
        kinds = [event["kind"] for event in store.get_events(task_id)]
        self.assertNotIn("usage_refreshed", kinds)

    def test_an_unattributable_accepted_task_stays_unavailable(self):
        self.write([user_record(REQUEST),
                    round_record("01a181bb000000000000000000000001", REQUEST, 120, 8),
                    user_record(OTHER_REQUEST, timestamp=2),
                    round_record("01a181bb000000000000000000000002", OTHER_REQUEST, 200, 9, timestamp=3)])
        store, task_id = self.accepted({
            "backend": "desktop_visible", "conversation_id": CONVERSATION, "request_id": "client-1",
            "dispatch": "sent"})
        result = self.refresh(task_id)
        transcript = result["usage"]["transcript"]
        self.assertFalse(transcript["available"])
        self.assertIn("2 requests", transcript["reason"])
        self.assertIsNone(transcript["tokens"])
        self.assertFalse(result["refresh"]["stored"])
        self.assertNotIn("usage", [event["kind"] for event in store.get_events(task_id) if event["kind"] == "usage"],
                         "no usage event is written for a read-only refresh")


class ConfigPathTests(unittest.TestCase):
    def test_workspace_path_name_matches_the_observed_rule(self):
        self.assertEqual(compress_workspace_path_name(CWD), "C-work-sample")
        self.assertEqual(compress_workspace_path_name("c:/users/x/WorkBuddy-2026"), "c-users-x-WorkBuddy-2026")
        self.assertEqual(compress_workspace_path_name("C:\\\\a//b::c"), "C-a-b-c")

    def test_an_empty_workspace_path_is_refused(self):
        with self.assertRaises(Exception):
            compress_workspace_path_name("  ")


if __name__ == "__main__":
    unittest.main()

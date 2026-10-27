import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from buddygate.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        # Inherit Windows ACLs rather than tempfile's restrictive mkdir mode;
        # some desktop execution identities cannot reopen its 0700 directory.
        self.base = Path(os.environ.get("BUDDYGATE_TEST_TEMP", tempfile.gettempdir())).resolve()
        self.root = self.base / f"buddygate-store-test-{uuid.uuid4()}"
        self.root.mkdir()
        self.addCleanup(self.cleanup_root)
        self.db = self.root / "tasks.sqlite"
        self.store = Store(self.db)

    def cleanup_root(self):
        resolved = self.root.resolve()
        if resolved.parent != self.base or not resolved.name.startswith("buddygate-store-test-"):
            raise RuntimeError("Refusing to clean a directory outside the allocated test root")
        shutil.rmtree(resolved)

    def submit(self, **kwargs):
        return self.store.submit_task(str(self.root), "Create the requested report", **kwargs)

    def test_idempotency_conflicts_and_reopening(self):
        first = self.submit(idempotency_key="request-1", constraints=["Keep source dates"])
        second = Store(self.db).submit_task(str(self.root), "Create the requested report", ["Keep source dates"], "request-1")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(ValueError, "different request"):
            self.store.submit_task(str(self.root), "Changed objective", idempotency_key="request-1")
        with self.assertRaisesRegex(ValueError, "absolute directory"):
            self.store.submit_task("relative-dir", "x")
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_atomic_claim_across_processes(self):
        self.submit()
        self.submit()
        # Separate interpreters exercise SQLite's process lock without requiring
        # Windows named-pipe IPC or any desktop window.
        script = """
import json, sys, time
from pathlib import Path
from buddygate.store import Store
db, worker, output, barrier = sys.argv[1:]
store = Store(db)
until = time.monotonic() + 10
while not Path(barrier).exists():
    if time.monotonic() > until: raise RuntimeError('Test barrier timed out')
    time.sleep(0.01)
task = store.claim_next(worker)
Path(output).write_text(json.dumps(task['id'] if task else None), encoding='utf-8')
"""
        barrier = self.root / "start"
        processes = []
        outputs = []
        for index in range(2):
            output = self.root / f"claim-{index}.json"
            outputs.append(output)
            processes.append(subprocess.Popen(
                [sys.executable, "-c", script, str(self.db), f"worker-{index}", str(output), str(barrier)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ))
        try:
            barrier.write_text("start", encoding="utf-8")
            for process in processes:
                self.assertEqual(process.wait(timeout=15), 0)
            results = [json.loads(output.read_text(encoding="utf-8")) for output in outputs]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(sum(t["status"] == "running" for t in self.store.list_tasks()), 1)

    def test_restart_never_replays_uncertain_execution(self):
        first = self.submit()
        self.submit()
        self.store.claim_next("worker")
        restarted = Store(self.db)
        self.assertEqual(restarted.recover_interrupted(), 1)
        self.assertEqual(restarted.recover_interrupted(), 0)
        self.assertEqual(restarted.get_task(first["id"])["status"], "uncertain")
        self.assertIsNone(restarted.claim_next("replacement"))
        with self.assertRaisesRegex(ValueError, "stopped confirmation"):
            restarted.update_task(first["id"], status="failed")
        restarted.update_task(first["id"], status="failed", stop_confirmed=True)
        self.assertIsNotNone(restarted.claim_next("replacement"))

    def test_rework_preserves_session_constraints_and_rejects_old_attempt(self):
        task = self.submit(constraints=["No publication", "Keep original files"])
        self.store.claim_next("worker")
        self.store.update_task(task["id"], expected_attempt=1, session_id="native-session",
                               status="needs_review", result={"path": "report.txt"}, stop_confirmed=True)
        reworked = self.store.rework_task(task["id"], "Correct the dates")
        self.assertEqual(reworked["attempt"], 2)
        self.assertEqual(reworked["session_id"], "native-session")
        self.assertEqual(reworked["constraints"], task["constraints"])
        self.assertEqual(reworked["objective"], task["objective"])
        self.assertIsNone(reworked["result"])
        with self.assertRaisesRegex(ValueError, "Stale attempt"):
            self.store.update_task(task["id"], expected_attempt=1, result={"stale": True})
        events = self.store.get_events(task["id"])
        self.assertTrue(any(e["kind"] == "rework" and e["data"]["previous"]["result"] for e in events))

    def test_acceptance_has_independent_review_gate(self):
        task = self.submit()
        with self.assertRaisesRegex(ValueError, "finished result"):
            self.store.accept_task(task["id"], "checked", ["Compared output"])
        self.store.claim_next("worker")
        with self.assertRaisesRegex(ValueError, "Use accept_task"):
            self.store.update_task(task["id"], status="accepted")
        self.store.update_task(task["id"], status="needs_review", stop_confirmed=True)
        with self.assertRaisesRegex(ValueError, "independent verification"):
            self.store.accept_task(task["id"], "checked", [])
        accepted = self.store.accept_task(task["id"], "Checked actual report", ["Compared source dates with report"])
        self.assertEqual(accepted["status"], "accepted")
        with self.assertRaisesRegex(ValueError, "cannot be modified"):
            self.store.update_task(task["id"], result={"changed": True})

    def test_cancel_request_does_not_claim_executor_stopped(self):
        queued = self.submit()
        cancelled = self.store.request_cancel(queued["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["stop_confirmed"])
        active = self.submit()
        self.store.claim_next("worker")
        self.submit()
        requested = self.store.request_cancel(active["id"])
        self.assertEqual(requested["status"], "cancel_requested")
        self.assertFalse(requested["stop_confirmed"])
        self.assertIsNone(self.store.claim_next("other"))
        with self.assertRaisesRegex(ValueError, "Cancellation remains pending"):
            self.store.update_task(active["id"], status="needs_review", stop_confirmed=True)
        self.store.update_task(active["id"], status="cancelled", stop_confirmed=True)
        self.assertIsNotNone(self.store.claim_next("other"))

    def test_unconfirmed_failure_still_blocks_execution_and_rework(self):
        task = self.submit()
        self.store.claim_next("worker")
        self.store.update_task(task["id"], status="failed", error="Connection lost")
        self.submit()
        self.assertIsNone(self.store.claim_next("other"))
        with self.assertRaisesRegex(ValueError, "stopped confirmation"):
            self.store.rework_task(task["id"], "Try again")
        self.store.update_task(task["id"], stop_confirmed=True)
        self.assertIsNotNone(self.store.claim_next("other"))

    def test_immutable_fields_and_event_cursor(self):
        task = self.submit()
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.update_task(task["id"], objective="silently changed")
        marker = self.store.add_event(task["id"], "inspection", {"checked": "actual file"})
        self.assertEqual(self.store.get_events(task["id"], marker["id"]), [])
        later = self.store.add_event(task["id"], "progress", {"text": "Continuing"})
        self.assertEqual(self.store.get_events(task["id"], marker["id"]), [later])

    def waiting_permission(self, permission_id="permission-current"):
        task = self.submit()
        self.store.claim_next("permission-worker")
        permission = {"id": permission_id, "decision": None, "options": [
            {"optionId": "allow", "kind": "allow_once"},
            {"optionId": "reject", "kind": "reject_once"},
            {"optionId": "forever", "kind": "allow_always"},
        ]}
        return self.store.update_task(task["id"], status="waiting_permission", permission=permission)

    def test_concurrent_permission_decisions_have_exactly_one_winner(self):
        task = self.waiting_permission()
        # Two managers use independently opened SQLite connections. The barrier
        # starts their conflicting choices together, without mocking storage.
        managers = [Store(self.db), Store(self.db)]
        barrier = threading.Barrier(2)
        def decide(index):
            barrier.wait(timeout=5)
            option = ("allow", "reject")[index]
            try:
                managers[index].decide_permission(task["id"], "permission-current", option, f"Manager {index} checked this exact operation")
                return {"winner": option}
            except ValueError as error:
                return {"error": str(error)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, range(2)))
        winners = [item["winner"] for item in results if "winner" in item]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(sum("already has a decision" in item.get("error", "") for item in results), 1)
        current = self.store.get_task(task["id"])
        self.assertEqual(current["permission"]["decision"]["option_id"], winners[0])
        events = [item for item in self.store.get_events(task["id"]) if item["kind"] == "permission_decided"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["option_id"], winners[0])

    def test_expired_permission_and_permanent_options_cannot_be_decided(self):
        task = self.waiting_permission("permission-old")
        replacement = dict(task["permission"], id="permission-new")
        self.store.update_task(task["id"], permission=replacement)
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.decide_permission(task["id"], "permission-old", "allow", "Checked old request")
        with self.assertRaisesRegex(ValueError, "one-time"):
            self.store.decide_permission(task["id"], "permission-new", "forever", "This must not grant permanent permission")
        with self.assertRaisesRegex(ValueError, "concrete manager note"):
            self.store.decide_permission(task["id"], "permission-new", "allow", " ")
        self.assertIsNone(self.store.get_task(task["id"])["permission"]["decision"])
        self.assertFalse(any(item["kind"] == "permission_decided" for item in self.store.get_events(task["id"])))
        decided = self.store.decide_permission(task["id"], "permission-new", "reject", "Current operation is outside scope")
        self.assertEqual(decided["permission"]["decision"]["option_id"], "reject")
        self.store.request_cancel(task["id"])
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.decide_permission(task["id"], "permission-new", "allow", "Cancelled request")


if __name__ == "__main__":
    unittest.main()

"""Compare legacy status polling with the local wait on one real task.

This is a measurement aid, not a model benchmark. It never submits a task, never
touches WorkBuddy and never calls a model: it only reads the same local SQLite
record twice, so the numbers are read counts and wall-clock time, not tokens.

Typical use while a real task is running:

    python scripts/benchmark_wait.py --task-id task-... --duration 120

The manager can pair the output with a token_count snapshot of the current
thread; a flat snapshot across the window is evidence about the wait, while the
read counts themselves are not a saving.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buddygate.runtime import state_directory  # noqa: E402
from buddygate.store import Store  # noqa: E402
from buddygate.waiting import wait_for_task  # noqa: E402


def legacy_polling(database: Store, task_id: str, duration: float, interval: float,
                   stop: threading.Event) -> dict:
    """Simulate the old sleep/get loop: repeated status reads, no waiting call."""
    started = time.monotonic()
    deadline = started + duration
    reads = 0
    statuses = []
    while not stop.is_set():
        reads += 1
        try:
            status = database.get_task(task_id)["status"]
        except (KeyError, ValueError):
            status = "not_found"
        if not statuses or statuses[-1] != status:
            statuses.append(status)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if stop.wait(min(interval, remaining)):
            break
    return {"reads": reads, "status_changes": statuses, "seconds": round(time.monotonic() - started, 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--duration", type=float, default=60.0,
                        help="observation window in seconds; the wait uses the same window")
    parser.add_argument("--sample-interval", type=float, default=5.0,
                        help="seconds between legacy status reads (the old sleep/get cadence)")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--state-dir", help="override BUDDYGATE_STATE_DIR for this run")
    arguments = parser.parse_args()

    if arguments.duration <= 0 or arguments.sample_interval <= 0:
        parser.error("duration and sample-interval must be positive")
    database = Store(Path(arguments.state_dir).expanduser().resolve() / "tasks.sqlite3"
                     if arguments.state_dir else state_directory() / "tasks.sqlite3")
    try:
        before = database.get_task(arguments.task_id)
    except (KeyError, ValueError):
        print(json.dumps({"error": "unknown task", "task_id": arguments.task_id}), file=sys.stderr)
        return 2

    stop = threading.Event()
    legacy = {}
    legacy_thread = threading.Thread(
        target=lambda: legacy.update(legacy_polling(database, arguments.task_id, arguments.duration,
                                                    arguments.sample_interval, stop)),
        daemon=True)
    legacy_thread.start()

    started = time.monotonic()
    report = wait_for_task(database, arguments.task_id, attempt=before["attempt"],
                           wait_seconds=arguments.duration, poll_interval=arguments.poll_interval)
    stop.set()
    legacy_thread.join(timeout=arguments.sample_interval + 5)

    after = database.get_task(arguments.task_id)
    result = {
        "task_id": arguments.task_id,
        "window_seconds": round(time.monotonic() - started, 3),
        "legacy_polling": legacy or {"reads": None, "note": "legacy observer did not finish"},
        "local_wait": {
            "outcome": report["outcome"],
            "settled": report["settled"],
            "polls": report["polls"],
            "status": report["status"],
            "model_calls_during_wait": 0,
            "one_completion": report["settled"],
        },
        "record_unchanged_by_measurement": {
            "status": before["status"] == after["status"],
            "attempt": before["attempt"] == after["attempt"],
            "updated_at": before.get("updated_at") == after.get("updated_at"),
        },
        "what_this_is": {
            "local_reads_only": True,
            "model_calls": 0,
            "prompt_resent": False,
            "dispatched_anything": False,
            "caveat": "Read counts and wall-clock time are not token savings. Only a token_count snapshot of the "
                      "manager thread across this window can show that the wait spent no tokens.",
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

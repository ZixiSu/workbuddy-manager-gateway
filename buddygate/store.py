"""Durable, single-executor task records. No task is replayed after uncertainty."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid


STATUSES = frozenset({
    "queued", "running", "waiting_permission", "needs_review", "accepted",
    "cancel_requested", "cancelled", "blocked", "uncertain", "failed",
})
ACTIVE_STATUSES = frozenset({"running", "waiting_permission", "cancel_requested", "uncertain"})
_MUTABLE = frozenset({
    "status", "session_id", "feedback", "result", "error", "permission",
    "worker_id", "stop_confirmed", "model", "timeout_seconds",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    """Every mutation obtains a SQLite write reservation across processes.

    ``stop_confirmed`` records an explicit observation that execution ended.
    A timeout, a lost connection, or a cancel request never sets it implicitly.
    """

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    fingerprint TEXT NOT NULL,
                    body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    kind TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_task ON events(task_id,id);
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _get(db: sqlite3.Connection, task_id: str) -> dict:
        row = db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return json.loads(row["body"])

    @staticmethod
    def _all(db: sqlite3.Connection) -> list[dict]:
        return [json.loads(row["body"]) for row in db.execute("SELECT body FROM tasks ORDER BY rowid")]

    @staticmethod
    def _event(db: sqlite3.Connection, task_id: str, kind: str, data: dict) -> dict:
        created_at = _now()
        cursor = db.execute(
            "INSERT INTO events(task_id,kind,data,created_at) VALUES(?,?,?,?)",
            (task_id, kind, _json(data), created_at),
        )
        return {"id": cursor.lastrowid, "task_id": task_id, "kind": kind, "data": data, "created_at": created_at}

    def _save(self, db: sqlite3.Connection, task: dict, kind: str, data: dict | None = None) -> dict:
        task["updated_at"] = _now()
        db.execute("UPDATE tasks SET body=? WHERE id=?", (_json(task), task["id"]))
        self._event(db, task["id"], kind, data if data is not None else {
            "status": task["status"], "attempt": task["attempt"],
            "stop_confirmed": task["stop_confirmed"],
        })
        return task

    def submit_task(self, workspace: str, objective: str, constraints: list[str] | None = None,
                    idempotency_key: str | None = None, model: str | None = None,
                    timeout_seconds: int = 900) -> dict:
        directory = Path(workspace).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("workspace must be an existing absolute directory")
        workspace = os.path.normcase(str(directory.resolve(strict=True)))
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("objective must be nonempty text")
        if constraints is None:
            constraints = []
        if not isinstance(constraints, list) or any(not isinstance(item, str) for item in constraints):
            raise ValueError("constraints must be a list of strings")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be a nonempty string or None")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds < 1:
            raise ValueError("timeout_seconds must be a positive integer")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
            raise ValueError("idempotency_key must be nonempty text or None")
        request = {"workspace": workspace, "objective": objective, "constraints": constraints,
                   "model": model, "timeout_seconds": timeout_seconds}
        fingerprint = hashlib.sha256(_json(request).encode("utf-8")).hexdigest()
        with self._transaction() as db:
            if idempotency_key is not None:
                row = db.execute("SELECT fingerprint,body FROM tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if row is not None:
                    if row["fingerprint"] != fingerprint:
                        raise ValueError("idempotency_key belongs to a different request")
                    return json.loads(row["body"])
            now = _now()
            task = {"id": f"task-{uuid.uuid4()}", **request, "constraints": list(constraints),
                    "attempt": 1, "status": "queued", "session_id": None, "feedback": "",
                    "result": None, "error": None, "permission": None, "worker_id": None,
                    "stop_confirmed": False, "created_at": now, "updated_at": now}
            db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (task["id"], idempotency_key, fingerprint, _json(task)))
            self._event(db, task["id"], "submitted", {"status": "queued", "attempt": 1})
            return task

    def get_task(self, id: str) -> dict:
        with self._connection() as db:
            return self._get(db, id)

    def list_tasks(self) -> list[dict]:
        with self._connection() as db:
            return self._all(db)

    def claim_next(self, worker_id: str) -> dict | None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be nonempty text")
        with self._transaction() as db:
            tasks = self._all(db)
            # A failure label alone does not prove a claimed executor stopped.
            if any(t["status"] in ACTIVE_STATUSES or (t["worker_id"] and not t["stop_confirmed"]) for t in tasks):
                return None
            for task in tasks:
                if task["status"] == "queued":
                    task.update(status="running", worker_id=worker_id, stop_confirmed=False)
                    return self._save(db, task, "claimed", {"worker_id": worker_id, "attempt": task["attempt"]})
            return None

    def update_task(self, id: str, expected_attempt: int | None = None, **changes: Any) -> dict:
        unknown = set(changes) - _MUTABLE
        if unknown:
            raise ValueError(f"Task fields are immutable or unknown: {', '.join(sorted(unknown))}")
        if "status" in changes and changes["status"] not in STATUSES:
            raise ValueError("Unknown task status")
        if "stop_confirmed" in changes and not isinstance(changes["stop_confirmed"], bool):
            raise ValueError("stop_confirmed must be a boolean")
        if "timeout_seconds" in changes and (isinstance(changes["timeout_seconds"], bool) or not isinstance(changes["timeout_seconds"], int) or changes["timeout_seconds"] < 1):
            raise ValueError("timeout_seconds must be a positive integer")
        if "model" in changes and changes["model"] is not None and (not isinstance(changes["model"], str) or not changes["model"].strip()):
            raise ValueError("model must be a nonempty string or None")
        _json(changes)
        with self._transaction() as db:
            task = self._get(db, id)
            if expected_attempt is not None and task["attempt"] != expected_attempt:
                raise ValueError("Stale attempt: task has a newer execution round")
            if task["status"] == "accepted" and changes:
                raise ValueError("An accepted task cannot be modified")
            next_status = changes.get("status", task["status"])
            if next_status == "accepted":
                raise ValueError("Use accept_task with independent verification evidence")
            if next_status == "queued" and task["status"] != "queued":
                raise ValueError("Use rework_task to queue a new execution round")
            if task["status"] == "cancelled" and next_status != "cancelled":
                raise ValueError("Cancelled tasks require explicit rework, not a late report")
            if task["status"] == "cancel_requested" and next_status in {"running", "waiting_permission", "needs_review"}:
                raise ValueError("Cancellation remains pending until execution is confirmed stopped")
            if task["status"] == "uncertain" and next_status != "uncertain":
                if next_status not in {"blocked", "failed", "cancelled"} or changes.get("stop_confirmed") is not True:
                    raise ValueError("Uncertain execution needs explicit stopped confirmation before release")
            task.update(changes)
            return self._save(db, task, "updated", {"attempt": task["attempt"], "changes": changes})

    def add_event(self, id: str, kind: str, data: dict) -> dict:
        if not isinstance(kind, str) or not kind.strip() or not isinstance(data, dict):
            raise ValueError("An event requires a nonempty kind and dictionary data")
        with self._transaction() as db:
            self._get(db, id)
            return self._event(db, id, kind, data)

    def get_events(self, id: str, after: int = 0) -> list[dict]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a nonnegative integer")
        with self._connection() as db:
            self._get(db, id)
            return [{"id": row["id"], "task_id": row["task_id"], "kind": row["kind"],
                     "data": json.loads(row["data"]), "created_at": row["created_at"]}
                    for row in db.execute("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id", (id, after))]

    def decide_permission(self, task_id: str, permission_id: str, option_id: str, note: str) -> dict:
        """Resolve exactly one current native permission under the write lock.

        Reading the request, validating its option, recording the decision and
        writing its event are one transaction: competing managers cannot both
        approve it or accidentally decide a replacement permission request.
        """
        if not isinstance(note, str) or not note.strip():
            raise ValueError("An approval or rejection requires a concrete manager note")
        if not isinstance(permission_id, str) or not permission_id or not isinstance(option_id, str) or not option_id:
            raise ValueError("Use the actual current permission and option identifiers")
        with self._transaction() as db:
            task = self._get(db, task_id)
            permission = task.get("permission")
            if task["status"] != "waiting_permission" or not isinstance(permission, dict) or permission.get("id") != permission_id:
                raise ValueError("Permission is no longer pending; inspect the current task")
            if permission.get("decision") is not None:
                raise ValueError("This permission already has a decision")
            options = permission.get("options")
            matches = [item for item in options if isinstance(item, dict) and item.get("optionId") == option_id] if isinstance(options, list) else []
            if len(matches) != 1 or matches[0].get("kind") not in ("allow_once", "reject_once"):
                raise ValueError("Only an actual one-time option may be selected")
            option = matches[0]
            permission["decision"] = {"option_id": option_id, "note": note}
            return self._save(db, task, "permission_decided", {
                "permission_id": permission_id, "option_id": option_id,
                "kind": option["kind"], "note": note, "attempt": task["attempt"],
            })

    def rework_task(self, id: str, feedback: str) -> dict:
        if not isinstance(feedback, str) or not feedback.strip():
            raise ValueError("Rework feedback is required")
        with self._transaction() as db:
            task = self._get(db, id)
            if task["status"] not in {"needs_review", "failed", "blocked", "cancelled"} or not task["stop_confirmed"]:
                raise ValueError("Rework requires a finished task and explicit stopped confirmation")
            previous = {"attempt": task["attempt"], "status": task["status"], "result": task["result"], "error": task["error"]}
            task.update(attempt=task["attempt"] + 1, status="queued", feedback=feedback,
                        result=None, error=None, permission=None, worker_id=None, stop_confirmed=False)
            return self._save(db, task, "rework", {"previous": previous, "feedback": feedback, "attempt": task["attempt"]})

    def accept_task(self, id: str, note: str, evidence: list[str]) -> dict:
        if not isinstance(note, str) or not note.strip() or not isinstance(evidence, list) or not evidence or any(not isinstance(item, str) or not item.strip() for item in evidence):
            raise ValueError("Acceptance requires a note and nonempty independent verification evidence")
        with self._transaction() as db:
            task = self._get(db, id)
            if task["status"] != "needs_review" or not task["stop_confirmed"]:
                raise ValueError("Acceptance requires a finished result awaiting review")
            task["status"] = "accepted"
            return self._save(db, task, "accepted", {"note": note, "evidence": evidence, "attempt": task["attempt"]})

    def request_cancel(self, id: str) -> dict:
        with self._transaction() as db:
            task = self._get(db, id)
            if task["status"] == "queued":
                task.update(status="cancelled", stop_confirmed=True)
            elif task["status"] == "uncertain":
                # A request cannot make interrupted execution certain.
                self._event(db, id, "cancel_requested", {"attempt": task["attempt"], "remote_stopped": False})
                return task
            elif task["status"] in {"running", "waiting_permission", "cancel_requested"} or (task["worker_id"] and not task["stop_confirmed"]):
                task.update(status="cancel_requested", stop_confirmed=False)
            else:
                return task
            return self._save(db, task, "cancel_requested", {"attempt": task["attempt"], "remote_stopped": task["stop_confirmed"]})

    def recover_interrupted(self) -> int:
        recovered = 0
        with self._transaction() as db:
            for task in self._all(db):
                if task["status"] in {"running", "waiting_permission", "cancel_requested"}:
                    task.update(status="uncertain", stop_confirmed=False,
                                error="Gateway interrupted execution. Inspect the original WorkBuddy session and confirm it stopped; task was not replayed.")
                    self._save(db, task, "interrupted", {"attempt": task["attempt"], "replayed": False})
                    recovered += 1
        return recovered

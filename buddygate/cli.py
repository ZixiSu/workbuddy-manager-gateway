"""Command and tool surface for the manager. No public HTTP listener."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .discovery import discover
from .runtime import state_directory
from .store import Store


def store() -> Store:
    return Store(state_directory() / "tasks.sqlite3")


def start_worker():
    directory = state_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "buddygate", "worker"],
            cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, env=os.environ.copy(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    return process.pid


def approve_task(database: Store, task_id: str, permission_id: str,
                 option_id: str, note: str):
    return database.decide_permission(task_id, permission_id, option_id, note)


def resolve_task(database: Store, task_id: str, note: str, evidence: list[str]):
    if not note.strip() or not evidence or any(not item.strip() for item in evidence):
        raise ValueError("Resolving an interrupted task requires a note and actual stop-check evidence")
    task = database.get_task(task_id)
    if task["status"] != "uncertain":
        raise ValueError("Only an uncertain task may be manually reconciled")
    result = database.update_task(task_id, expected_attempt=task["attempt"], status="blocked",
                                  stop_confirmed=True, error=note, permission=None)
    database.add_event(task_id, "manager_reconciled", {"note": note, "evidence": evidence,
                       "warning": "Manager assertion recorded; gateway cannot prove external side effects stopped"})
    return result


def invoke(name: str, args: dict):
    if name == "buddy_doctor":
        return {"version": __version__, "executor": discover(),
                "state_directory": str(state_directory()), "usage": "unavailable",
                "execution": "WorkBuddy bundled CLI in an independent background ACP session"}
    database = store()
    if name == "buddy_stop":
        if any(t["status"] in ("queued", "running", "waiting_permission", "cancel_requested") for t in database.list_tasks()):
            raise ValueError("Worker has unfinished tasks; cancel or inspect them before stopping the idle worker")
        (state_directory() / "stop.request").write_text("stop", encoding="utf-8")
        return {"stop_requested": True, "note": "Idle worker will exit within one second. No WorkBuddy desktop window is affected."}
    if name == "buddy_submit":
        result = database.submit_task(args["workspace"], args["objective"], args.get("constraints", []),
                                      args.get("idempotency_key"), args.get("model"), args.get("timeout_seconds", 900))
        if args.get("start", True) and result["status"] == "queued":
            start_worker()
        return result
    if name == "buddy_get":
        return database.get_task(args["task_id"])
    if name == "buddy_list":
        return database.list_tasks()
    if name == "buddy_status":
        tasks = database.list_tasks()
        counts = {status: sum(item["status"] == status for item in tasks) for status in sorted({item["status"] for item in tasks})}
        return {"version": __version__, "counts": counts, "executor": discover(), "usage": "unavailable"}
    if name == "buddy_events":
        return database.get_events(args["task_id"], args.get("after", 0))
    if name == "buddy_rework":
        result = database.rework_task(args["task_id"], args["feedback"])
        if args.get("start", True):
            start_worker()
        return result
    if name == "buddy_accept":
        return database.accept_task(args["task_id"], args["note"], args["evidence"])
    if name == "buddy_cancel":
        return database.request_cancel(args["task_id"])
    if name == "buddy_approve":
        return approve_task(database, args["task_id"], args["permission_id"], args["option_id"], args["note"])
    if name == "buddy_resolve":
        return resolve_task(database, args["task_id"], args["note"], args["evidence"])
    raise ValueError("Unknown gateway tool")


def main():
    parser = argparse.ArgumentParser(prog="buddygate", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status", "list", "mcp", "stop"):
        commands.add_parser(name)
    submit = commands.add_parser("submit")
    submit.add_argument("--workspace", required=True)
    submit.add_argument("--objective", required=True)
    submit.add_argument("--constraint", action="append", default=[])
    submit.add_argument("--key")
    submit.add_argument("--model")
    submit.add_argument("--timeout", type=int, default=900)
    submit.add_argument("--no-start", action="store_true")
    for name in ("get", "events", "cancel", "rework", "accept", "approve", "resolve"):
        command = commands.add_parser(name)
        command.add_argument("task_id")
        if name == "events":
            command.add_argument("--after", type=int, default=0)
        if name == "rework":
            command.add_argument("--feedback", required=True)
            command.add_argument("--no-start", action="store_true")
        if name in ("accept", "approve", "resolve"):
            command.add_argument("--note", required=True)
        if name in ("accept", "resolve"):
            command.add_argument("--evidence", action="append", required=True)
        if name == "approve":
            command.add_argument("--permission-id", required=True)
            command.add_argument("--option", required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--idle-exit", action="store_true")
    values = vars(parser.parse_args())
    command = values.pop("command")
    try:
        if command == "mcp":
            from .mcp import serve
            serve()
            return
        if command == "worker":
            from .worker import run_worker
            result = run_worker(**values)
        else:
            if command == "submit":
                values = {"workspace": values["workspace"], "objective": values["objective"],
                          "constraints": values["constraint"], "idempotency_key": values["key"],
                          "model": values["model"], "timeout_seconds": values["timeout"], "start": not values["no_start"]}
            elif command == "rework":
                values["start"] = not values.pop("no_start")
            elif command == "approve":
                values["option_id"] = values.pop("option")
            result = invoke("buddy_" + command, values)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

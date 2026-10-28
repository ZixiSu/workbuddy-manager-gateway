"""Command and tool surface for the manager. No public HTTP listener."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .discovery import discover
from .runtime import state_directory
from .store import BACKEND_CLI, BACKEND_DESKTOP, Store
from .summary import brief_task, summarize_status, summarize_task, usage_view


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


def desktop_connect(executable: str, port: int, save: bool = False) -> dict:
    """Verify ownership and identity; persist the connection only when asked."""
    from .desktop import DesktopClient, DesktopConnection, check_ownership, http_json, listener_rows, renderer_candidates

    connection = DesktopConnection(executable=executable, port=port)
    verification = check_ownership(listener_rows(connection.port), connection.executable)
    candidates = renderer_candidates(http_json(connection.port, "/json/list"), connection.port)
    client = DesktopClient(connection)
    try:
        metadata = client.open()
    finally:
        client.close()
    result = {"peer_verified": True, "verification": verification, "page_candidates": len(candidates),
              "identity": metadata, "saved": False,
              "account_material": "none copied; the gateway never reads the debug ticket or chat history here"}
    if save:
        result["saved_to"] = str(connection.save())
        result["saved"] = True
    else:
        result["next_step"] = "Preview only. Re-run with --save to store this connection in the private state directory."
    return result


def desktop_configured() -> dict:
    """Cheap, offline view of the saved connection for routine status calls."""
    from .desktop import DesktopConnection, DesktopUnavailable

    try:
        connection = DesktopConnection.load()
    except DesktopUnavailable as error:
        return {"configured": False, "detail": str(error), "live_check": "buddy_desktop_status"}
    return {"configured": True, "port": connection.port, "host": connection.host,
            "executable": connection.executable, "live_check": "buddy_desktop_status"}


def desktop_status() -> dict:
    """Read-only check of the configured connection. Never calls a model."""
    from .desktop import DesktopClient, DesktopConnection, DesktopUnavailable

    try:
        connection = DesktopConnection.load()
    except DesktopUnavailable as error:
        return {"configured": False, "verified": False, "detail": str(error)}
    client = DesktopClient(connection)
    try:
        metadata = client.open()
        return {"configured": True, "verified": True, "connection": connection.to_dict(),
                "renderer": metadata, "model_calls": 0}
    except DesktopUnavailable as error:
        return {"configured": True, "verified": False, "connection": connection.to_dict(), "detail": str(error)}
    except Exception as error:  # noqa: BLE001 - a status check must not crash the caller
        return {"configured": True, "verified": False, "connection": connection.to_dict(),
                "detail": f"Renderer check failed: {type(error).__name__}"}
    finally:
        client.close()


def desktop_launch(execute: bool = False) -> dict:
    from .desktop import DesktopConnection
    from .desktop_launch import launch

    return launch(DesktopConnection.load(), execute=execute)


def invoke(name: str, args: dict):
    if name == "buddy_doctor":
        return {"version": __version__, "executor": discover(),
                "state_directory": str(state_directory()),
                "default_backend": BACKEND_DESKTOP,
                "compatibility_backend": BACKEND_CLI,
                "usage": {
                    "native": "one request's final usage snapshot: input/total/context plus "
                              "prompt, completion and prompt-cache hit/miss/write counters; not a bill and "
                              "not proven to accumulate every internal model round",
                    "transcript": "sum of the per-model-round providerData.usage records this request "
                                  "persisted in the local conversation transcript, deduplicated by message id; "
                                  "only persisted calls, never an account total and not a bill",
                    "codex": "difference of two token_count snapshots of one supplied thread file; "
                             "output_generation is output tokens, uncached_input is input minus cached input",
                    "output_cache_billing": "fields that were not observed are reported as null, never as zero",
                },
                "stop_confirmation": "a terminal request only counts as stopped when the desktop reports both "
                                     "hasActiveChildAgents and hasRunningBackgroundTasks as false",
                "execution": "visible desktop conversation by default; the independent CLI session is an explicit compatibility mode"}
    if name == "buddy_desktop_connect":
        return desktop_connect(args["executable"], args["port"], bool(args.get("save")))
    if name == "buddy_desktop_status":
        return desktop_status()
    if name == "buddy_desktop_launch":
        return desktop_launch(bool(args.get("execute_launch")))
    if name == "buddy_codex_usage":
        from .meter import codex_snapshot
        return codex_snapshot(args["thread"])
    if name == "buddy_usage":
        from .usage_ledger import ledger_for_task

        database = store()
        task = database.get_task(args["task_id"])
        refreshed = None
        view = None
        if args.get("refresh"):
            ledger = ledger_for_task(task, config_dir=args.get("config_dir"))
            if ledger is None:
                refreshed = {"refreshed": False, "prompt_resent": False,
                             "reason": "This task has no visible desktop conversation to read usage for."}
            else:
                usage = dict(task.get("usage") or {"native": None, "codex": None, "combined": None,
                                                   "notes": []})
                notes = usage.get("notes")
                usage["notes"] = [item for item in notes if isinstance(item, str)] if isinstance(notes, list) else []
                usage["transcript"] = ledger
                if task["status"] == "accepted":
                    # Acceptance froze this record; the ledger is computed for
                    # inspection but nothing is written back.
                    view = usage_view(usage)
                    refreshed = {"refreshed": False, "stored": False, "read_only": True,
                                 "available": ledger.get("available"), "rounds": ledger.get("rounds"),
                                 "prompt_resent": False,
                                 "reason": "This task is accepted, so its recorded usage is not modified; the "
                                           "transcript was read once for inspection and nothing was written."}
                else:
                    usage["transcript_refreshed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    task = database.update_task(task["id"], usage=usage)
                    database.add_event(task["id"], "usage_refreshed", {
                        "available": ledger.get("available"), "rounds": ledger.get("rounds"),
                        "reason": ledger.get("reason"), "read_only": True, "prompt_resent": False})
                    refreshed = {"refreshed": True, "stored": True, "available": ledger.get("available"),
                                 "rounds": ledger.get("rounds"), "prompt_resent": False}
        native = (task.get("native") or {}).get("conversation_id")
        return {"task_id": task["id"], "status": task["status"], "conversation_id": native,
                "native_request_id": (task.get("native") or {}).get("native_request_id"),
                "refresh": refreshed, "usage": view if view is not None else usage_view(task.get("usage"))}
    database = store()
    if name == "buddy_stop":
        if any(t["status"] in ("queued", "running", "waiting_permission", "cancel_requested") for t in database.list_tasks()):
            raise ValueError("Worker has unfinished tasks; cancel or inspect them before stopping the idle worker")
        (state_directory() / "stop.request").write_text("stop", encoding="utf-8")
        return {"stop_requested": True, "note": "Idle worker will exit within one second. No WorkBuddy desktop window is affected."}
    if name == "buddy_submit":
        result = database.submit_task(args["workspace"], args["objective"], args.get("constraints", []),
                                      args.get("idempotency_key"), args.get("model"),
                                      args.get("timeout_seconds", 900),
                                      backend=args.get("backend") or BACKEND_DESKTOP,
                                      codex_thread=args.get("codex_thread"))
        if args.get("start", True) and result["status"] == "queued":
            start_worker()
        return summarize_task(result)
    if name == "buddy_get":
        task = database.get_task(args["task_id"])
        return task if args.get("full") else summarize_task(task)
    if name == "buddy_list":
        return [brief_task(task) for task in database.list_tasks()]
    if name == "buddy_status":
        return {"version": __version__, "executor": discover(), "tasks": summarize_status(database),
                "desktop": desktop_configured()}
    if name == "buddy_events":
        return database.get_events(args["task_id"], args.get("after", 0))
    if name == "buddy_rework":
        result = database.rework_task(args["task_id"], args["feedback"])
        if args.get("start", True):
            start_worker()
        return summarize_task(result)
    if name == "buddy_accept":
        return summarize_task(database.accept_task(args["task_id"], args["note"], args["evidence"]))
    if name == "buddy_cancel":
        return summarize_task(database.request_cancel(args["task_id"]))
    if name == "buddy_approve":
        return summarize_task(approve_task(database, args["task_id"], args["permission_id"], args["option_id"], args["note"]))
    if name == "buddy_resolve":
        return summarize_task(resolve_task(database, args["task_id"], args["note"], args["evidence"]))
    raise ValueError("Unknown gateway tool")


def main():
    parser = argparse.ArgumentParser(prog="buddygate", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status", "list", "mcp", "stop", "desktop-status"):
        commands.add_parser(name)
    submit = commands.add_parser("submit")
    submit.add_argument("--workspace", required=True)
    submit.add_argument("--objective", required=True)
    submit.add_argument("--constraint", action="append", default=[])
    submit.add_argument("--key")
    submit.add_argument("--model")
    submit.add_argument("--timeout", type=int, default=900)
    submit.add_argument("--backend", choices=[BACKEND_DESKTOP, BACKEND_CLI], default=BACKEND_DESKTOP,
                        help="desktop_visible (default) or the explicit cli_acp compatibility mode")
    submit.add_argument("--codex-thread", help="explicit Codex thread file used only for the manager-side usage snapshot")
    submit.add_argument("--no-start", action="store_true")
    desktop_connect_command = commands.add_parser("desktop-connect")
    desktop_connect_command.add_argument("--executable", required=True)
    desktop_connect_command.add_argument("--port", type=int, required=True)
    desktop_connect_command.add_argument("--save", action="store_true",
                                         help="persist the verified connection in the private state directory")
    desktop_launch_command = commands.add_parser("desktop-launch")
    desktop_launch_command.add_argument("--execute-launch", action="store_true",
                                        help="start the installed app in the background if it is not running")
    usage = commands.add_parser("codex-usage")
    usage.add_argument("--thread", required=True)
    for name in ("get", "events", "cancel", "rework", "accept", "approve", "resolve", "usage"):
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
        if name == "get":
            command.add_argument("--full", action="store_true")
        if name == "usage":
            command.add_argument("--refresh", action="store_true",
                                 help="re-read the persisted transcript read-only; no prompt is resent")
            command.add_argument("--config-dir",
                                 help="verified WorkBuddy config directory (workbuddy.db + projects)")
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
                          "model": values["model"], "timeout_seconds": values["timeout"],
                          "backend": values["backend"], "codex_thread": values["codex_thread"],
                          "start": not values["no_start"]}
            elif command == "rework":
                values["start"] = not values.pop("no_start")
            elif command == "approve":
                values["option_id"] = values.pop("option")
            elif command == "desktop-connect":
                result = invoke("buddy_desktop_connect", dict(values))
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            elif command == "desktop-launch":
                result = invoke("buddy_desktop_launch", {"execute_launch": values["execute_launch"]})
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            elif command == "codex-usage":
                result = invoke("buddy_codex_usage", {"thread": values["thread"]})
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            if command == "desktop-status":
                result = invoke("buddy_desktop_status", {})
            else:
                result = invoke("buddy_" + command, values)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

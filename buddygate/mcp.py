"""Small stdio MCP server using newline JSON-RPC. Standard library only.

Long waits run in a bounded set of worker threads, so ``ping``, a cancellation
notification and read-only queries on the same connection keep answering while a
wait is pending. Waiting only reads the local task record: it makes no model
call, sends no prompt and never accepts a result.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time

from . import __version__
from .cli import invoke

MAX_CONCURRENT_WAITS = 4


def spec(name, description, properties=None, required=None):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}}


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
BOOLEAN = {"type": "boolean"}
TASK = {"task_id": STRING}
WAIT_SECONDS = {"type": "number", "minimum": 1, "maximum": 3600}
POLL_INTERVAL = {"type": "number", "minimum": 0.05, "maximum": 30}
WAIT_NAMES = frozenset({"buddy_run", "buddy_wait", "buddy_rework_and_wait"})

TOOLS = [
    spec("buddy_doctor", "Read-only installation check. Does not start WorkBuddy or make a model request."),
    spec("buddy_status", "Short status: task counts, backend split, recent result summaries and whether a desktop connection is saved. No model request."),
    spec("buddy_stop", "Request the idle local worker to exit. Refuses while tasks are queued or active."),
    spec("buddy_run", "Preferred for routine delegation: submit once and wait locally until the attempt is reviewable or needs a decision, then return one compact summary. No model request is made while waiting, so do not poll buddy_get, sleep and re-check, or resubmit. This is not acceptance: after it returns, read the actual artifacts before deciding. The host must allow a tool timeout longer than wait_seconds (tool_timeout_sec 1800-3600 is typical); a 60 s host timeout truncates the wait and the task keeps running unchanged.",
         {"workspace": STRING, "objective": STRING, "constraints": STRINGS, "idempotency_key": STRING,
          "model": STRING, "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 86400},
          "backend": {"type": "string", "enum": ["desktop_visible", "cli_acp"]},
          "codex_thread": STRING, "wait_seconds": WAIT_SECONDS, "poll_interval": POLL_INTERVAL},
         ["workspace", "objective"]),
    spec("buddy_wait", "Wait for an existing task without creating, cancelling, approving or accepting anything. Outcomes: ready_for_review, needs_attention (permission pending or blocked), uncertain, failed, cancelled, already_accepted, wait_expired, wait_cancelled, attempt_changed, not_found. wait_expired leaves the task untouched and its ID usable for another wait; never resubmit the objective because a wait expired.",
         {**TASK, "attempt": {"type": "integer", "minimum": 1}, "wait_seconds": WAIT_SECONDS,
          "poll_interval": POLL_INTERVAL}, ["task_id"]),
    spec("buddy_rework_and_wait", "Rework the same visible conversation as a new attempt, then wait locally for that attempt only. Requires the previous attempt confirmed stopped; accepts nothing.",
         {**TASK, "feedback": STRING, "wait_seconds": WAIT_SECONDS, "poll_interval": POLL_INTERVAL},
         ["task_id", "feedback"]),
    spec("buddy_submit", "Queue WorkBuddy execution in a visible desktop conversation by default. The independent CLI session requires backend=cli_acp and is never selected automatically. Preserve the returned ID; never resubmit an uncertain task. Prefer buddy_run unless a non-blocking submit is really wanted.",
         {"workspace": STRING, "objective": STRING, "constraints": STRINGS, "idempotency_key": STRING,
          "model": STRING, "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 86400},
          "backend": {"type": "string", "enum": ["desktop_visible", "cli_acp"]},
          "codex_thread": STRING}, ["workspace", "objective"]),
    spec("buddy_get", "Short task status with the native conversation ID, latest result excerpt, activity flags and what acceptance was actually recorded. stop_confirmed requires both desktop activity flags false. Use full=true for the raw record, buddy_events for tool detail. For a running attempt use buddy_wait instead of polling this repeatedly.",
         {**TASK, "full": BOOLEAN}, ["task_id"]),
    spec("buddy_list", "Read local persisted task summaries; no model request."),
    spec("buddy_events", "Read visible tool events after an event ID. Private reasoning is not recorded.",
         {**TASK, "after": {"type": "integer", "minimum": 0}}, ["task_id"]),
    spec("buddy_rework", "Run a new attempt in the same visible conversation, with a new request ID, preserving objective and constraints. Requires previous execution confirmed stopped.",
         {**TASK, "feedback": STRING}, ["task_id", "feedback"]),
    spec("buddy_approve", "Select a current one-time permission option after checking user authorization and actual tool details. Never grants permanent permission or approves unknown requests.",
         {**TASK, "permission_id": STRING, "option_id": STRING, "note": STRING}, ["task_id", "permission_id", "option_id", "note"]),
    spec("buddy_cancel", "Request cooperative stop. An active task may continue until WorkBuddy confirms a terminal response. This is not a wait cancellation: a pending buddy_wait is stopped by a notifications/cancelled message and leaves the task running.", TASK, ["task_id"]),
    spec("buddy_accept", "Record manager acceptance only after actual content/source checks; the gateway does not check business correctness.",
         {**TASK, "note": STRING, "evidence": STRINGS}, ["task_id", "note", "evidence"]),
    spec("buddy_resolve", "Manually reconcile an uncertain task only after verifying its process/session and external actions stopped. Stores manager evidence, does not prove it.",
         {**TASK, "note": STRING, "evidence": STRINGS}, ["task_id", "note", "evidence"]),
    spec("buddy_usage", "Token usage for one task, in three separate meters: the desktop request's last usage snapshot, the per-model-round counters persisted in the conversation transcript (deduplicated by message id and filtered to the canonical request id observed for that dispatch), and the Codex thread snapshot difference. Without that canonical id an ambiguous conversation stays unavailable instead of being guessed. Missing stays null; non-token fields such as cost are never converted; no price or saving ratio is derived. refresh=true re-reads the transcript read-only without resending any prompt, and an accepted task is read without being modified.",
         {**TASK, "refresh": BOOLEAN}, ["task_id"]),
    spec("buddy_codex_usage", "Read token_count snapshots from exactly one supplied Codex thread file. Never scans or reads other chats. A delta refuses to mix two files or a counter reset.",
         {"thread": STRING}, ["thread"]),
    spec("buddy_desktop_status", "Live read-only check: OS listener ownership, loopback binding, single native renderer identity. No model call, no account material."),
    spec("buddy_desktop_connect", "Verify a WorkBuddy local debug entry and optionally save it to the private state directory. Preview unless save=true.",
         {"executable": STRING, "port": {"type": "integer", "minimum": 1024, "maximum": 65535}, "save": BOOLEAN},
         ["executable", "port"]),
    spec("buddy_desktop_launch", "Preview how the installed app would be started. Only executes with execute_launch=true and only when no instance is running; never kills or restarts a running app.",
         {"execute_launch": BOOLEAN}),
]

INSTRUCTIONS = (
    "Manager delegates to WorkBuddy and independently checks artifacts. Prefer buddy_run for routine delegation: "
    "one call submits and waits locally until the attempt is reviewable or needs attention, and makes no model "
    "request while waiting. Do not poll buddy_get every few seconds or sleep-and-check between calls; that spends "
    "manager tokens without progress. After buddy_run returns, read the actual artifacts before accepting; waiting "
    "is not acceptance. The host tool timeout must exceed wait_seconds (tool_timeout_sec 1800-3600 is typical); a "
    "shorter host timeout truncates the wait and leaves the task running unchanged."
)


def handle(message):
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("MCP params must be an object")
    if method == "initialize":
        version = params.get("protocolVersion", "2024-11-05")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18"):
            version = "2025-06-18"
        return {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "workbuddy-manager-gateway", "version": __version__},
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        definition = next((tool for tool in TOOLS if tool["name"] == name), None)
        if not definition:
            raise ValueError("Unknown tool")
        return tool_result(name, definition, args)
    raise ValueError("Method not found")


def tool_result(name, definition, args, cancel_event=None):
    try:
        validate_arguments(args, definition["inputSchema"])
        result = invoke(name, args, cancel_event=cancel_event)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        return {"content": [{"type": "text", "text": str(error)}], "isError": True}


def validate_arguments(args, schema):
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be an object")
    if set(args) - set(schema["properties"]):
        raise ValueError("Unexpected tool argument")
    if any(key not in args for key in schema["required"]):
        raise ValueError("Missing required tool argument")
    for key, value in args.items():
        rule = schema["properties"][key]
        kind = rule["type"]
        if kind == "string" and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if kind == "boolean" and not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        if kind == "string" and rule.get("enum") and value not in rule["enum"]:
            raise ValueError(f"{key} must be one of: " + ", ".join(rule["enum"]))
        if kind == "integer" and type(value) is not int:
            raise ValueError(f"{key} must be an integer")
        if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError(f"{key} must be a number")
        if kind in ("integer", "number") and not math.isfinite(value):
            # JSON allows the NaN/Infinity literals; a NaN compares false against
            # both bounds and would otherwise reach the waiter as a valid number.
            raise ValueError(f"{key} must be a finite number")
        if kind in ("integer", "number"):
            if rule.get("minimum") is not None and value < rule["minimum"]:
                raise ValueError(f"{key} is below its minimum")
            if rule.get("maximum") is not None and value > rule["maximum"]:
                raise ValueError(f"{key} is above its maximum")
        if kind == "array" and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
            raise ValueError(f"{key} must be an array of strings")


def _utf8(stream, errors):
    """Make one standard stream speak UTF-8 before any protocol byte is read.

    MCP stdio is UTF-8 by specification, so the host's locale and
    ``PYTHONIOENCODING`` must not decide it. Streams without ``reconfigure``
    (in-process test doubles such as ``StringIO``) are left untouched, and a
    stream that is already detached or buffered keeps its own encoding instead
    of aborting the server.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors=errors)
    except (AttributeError, OSError, ValueError):
        return


class _ProtocolWriter:
    """One locked stdout writer: concurrent replies must stay whole lines."""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()
        self._closed = False

    def send(self, message: dict) -> bool:
        line = json.dumps(message, ensure_ascii=False) + "\n"
        with self._lock:
            if self._closed:
                return False
            try:
                self._stream.write(line)
                self._stream.flush()
                return True
            except (ValueError, OSError):
                # The host closed the pipe; drop the write instead of crashing.
                self._closed = True
                return False

    def close(self) -> None:
        with self._lock:
            self._closed = True


class _WaitSlots:
    """A bounded set of long waits, each cancellable by its own request id.

    JSON-RPC ids are matched by type as well as value: the number ``1`` and the
    string ``"1"`` are different requests and must be cancellable separately.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self._room = threading.BoundedSemaphore(limit)
        self._lock = threading.Lock()
        self._events = {}

    @staticmethod
    def key(request_id):
        if request_id is None or isinstance(request_id, (str, int, float)):
            return (type(request_id).__name__, request_id)
        return (type(request_id).__name__, repr(request_id))

    def reserve(self, request_id, event) -> str:
        """Return ``ok``, ``duplicate`` (same in-flight id) or ``busy`` (no room)."""
        key = self.key(request_id)
        with self._lock:
            if key in self._events:
                return "duplicate"
        if not self._room.acquire(blocking=False):
            return "busy"
        with self._lock:
            if key in self._events:
                self._room.release()
                return "duplicate"
            self._events[key] = event
        return "ok"

    def release(self, request_id, event) -> bool:
        """Free a slot only for the wait that actually reserved it."""
        key = self.key(request_id)
        with self._lock:
            if self._events.get(key) is not event:
                return False
            del self._events[key]
        self._room.release()
        return True

    def cancel(self, request_id) -> bool:
        with self._lock:
            event = self._events.get(self.key(request_id))
        if event is None:
            return False
        event.set()
        return True

    def cancel_all(self) -> None:
        with self._lock:
            events = list(self._events.values())
        for event in events:
            event.set()

    def pending(self) -> int:
        with self._lock:
            return len(self._events)


def _response(request_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": message}}


def _wait_in_background(request_id, event, writer, slots, definition, args):
    try:
        response = _response(request_id, tool_result(definition["name"], definition, args, event))
    except BaseException as error:  # noqa: BLE001 - a wait thread must not kill the transport
        response = _error(request_id, f"Wait failed: {type(error).__name__}")
    finally:
        slots.release(request_id, event)
    writer.send(response)


def serve():
    # Configure before the first read. stdin replaces undecodable bytes so one
    # malformed line becomes a JSON-RPC error instead of ending the connection;
    # stdout replaces an unencodable lone surrogate, which keeps the
    # newline-framed JSON parseable (a backslash escape would not); stderr is
    # diagnostics only, never part of the protocol.
    _utf8(sys.stdin, "replace")
    _utf8(sys.stdout, "replace")
    _utf8(sys.stderr, "backslashreplace")
    writer = _ProtocolWriter(sys.stdout)
    slots = _WaitSlots(MAX_CONCURRENT_WAITS)
    threads = []
    for line in sys.stdin:
        request_id = None
        try:
            if len(line) > 2_000_000:
                raise ValueError("MCP request too large")
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("MCP message must be an object")
            request_id = message.get("id")
            if "id" not in message:
                # Only cancellation is acted on, and only for this connection's
                # wait: the WorkBuddy task itself is never cancelled here.
                if message.get("method") == "notifications/cancelled":
                    params = message.get("params")
                    if isinstance(params, dict):
                        slots.cancel(params.get("requestId"))
                continue
            if message.get("method") == "tools/call":
                params = message.get("params")
                name = params.get("name") if isinstance(params, dict) else None
                definition = next((tool for tool in TOOLS if tool["name"] == name), None)
                if definition is not None and name in WAIT_NAMES:
                    event = threading.Event()
                    state = slots.reserve(request_id, event)
                    if state != "ok":
                        message = (f"Request id {request_id!r} already has a wait in flight; this duplicate call "
                                   "did not start and nothing was dispatched."
                                   if state == "duplicate" else
                                   f"Too many concurrent waits (limit {MAX_CONCURRENT_WAITS}); this call did "
                                   "not start and nothing was dispatched. Let one wait return, or use "
                                   "buddy_get for a status read.")
                        writer.send(_error(request_id, message))
                        continue
                    # Keep the observation set bounded: finished waits are already
                    # released from the slots, so their threads can be dropped.
                    threads = [item for item in threads if item.is_alive()]
                    thread = threading.Thread(
                        target=_wait_in_background,
                        args=(request_id, event, writer, slots, definition, params.get("arguments", {})),
                        name="wait", daemon=True)
                    thread.start()
                    threads.append(thread)
                    continue
            writer.send(_response(request_id, handle(message)))
        except (ValueError, KeyError, OSError, RuntimeError, TypeError) as error:
            writer.send(_error(request_id, str(error)))
    # stdin closed: stop observing, then let the process exit promptly.
    slots.cancel_all()
    writer.close()
    deadline = time.monotonic() + 5
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))

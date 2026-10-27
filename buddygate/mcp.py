"""Small stdio MCP server using newline JSON-RPC. Standard library only."""
from __future__ import annotations

import json
import sys

from . import __version__
from .cli import invoke


def spec(name, description, properties=None, required=None):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}}


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
TASK = {"task_id": STRING}
TOOLS = [
    spec("buddy_doctor", "Read-only installation check. Does not start WorkBuddy or make a model request."),
    spec("buddy_status", "Read installation metadata and task counts; unknown usage stays unavailable."),
    spec("buddy_stop", "Request the idle local worker to exit. Refuses while tasks are queued or active."),
    spec("buddy_submit", "Queue actual WorkBuddy execution in a dedicated background CLI session. Preserve returned ID; never resubmit an uncertain task.",
         {"workspace": STRING, "objective": STRING, "constraints": STRINGS, "idempotency_key": STRING,
          "model": STRING, "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 86400}}, ["workspace", "objective"]),
    spec("buddy_get", "Read task state, result and any pending permission. needs_review is not acceptance.", TASK, ["task_id"]),
    spec("buddy_list", "Read local persisted tasks; no model request."),
    spec("buddy_events", "Read visible tool events after an event ID. Private reasoning is not recorded.",
         {**TASK, "after": {"type": "integer", "minimum": 0}}, ["task_id"]),
    spec("buddy_rework", "Run a new attempt in the same WorkBuddy session, preserving objective and constraints. Requires previous execution confirmed stopped.",
         {**TASK, "feedback": STRING}, ["task_id", "feedback"]),
    spec("buddy_approve", "Select a current one-time permission option after checking user authorization and actual tool details. Never grants permanent permission.",
         {**TASK, "permission_id": STRING, "option_id": STRING, "note": STRING}, ["task_id", "permission_id", "option_id", "note"]),
    spec("buddy_cancel", "Request cooperative stop. Active task may continue until WorkBuddy confirms a terminal response.", TASK, ["task_id"]),
    spec("buddy_accept", "Record manager acceptance only after actual content/source checks; the gateway does not check business correctness.",
         {**TASK, "note": STRING, "evidence": STRINGS}, ["task_id", "note", "evidence"]),
    spec("buddy_resolve", "Manually reconcile an uncertain task only after verifying its process/session and external actions stopped. Stores manager evidence, does not prove it.",
         {**TASK, "note": STRING, "evidence": STRINGS}, ["task_id", "note", "evidence"]),
]


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
                "instructions": "Manager delegates to WorkBuddy and independently checks artifacts. User authorization never expands through delegation."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        definition = next((tool for tool in TOOLS if tool["name"] == name), None)
        if not definition:
            raise ValueError("Unknown tool")
        try:
            validate_arguments(args, definition["inputSchema"])
            result = invoke(name, args)
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            return {"content": [{"type": "text", "text": str(error)}], "isError": True}
    raise ValueError("Method not found")


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
        if kind == "integer" and (type(value) is not int or value < rule.get("minimum", value) or value > rule.get("maximum", value)):
            raise ValueError(f"{key} is outside its integer range")
        if kind == "array" and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
            raise ValueError(f"{key} must be an array of strings")


def serve():
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
                continue
            result = handle(message)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (ValueError, TypeError) as error:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": str(error)}}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()

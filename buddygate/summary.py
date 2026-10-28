"""Compact manager-facing views. Prompts and full tool streams stay in events."""

from __future__ import annotations

from .store import Store


def _clip(value, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def usage_view(usage: dict | None) -> dict:
    """Usage block as stored, including explicit absences; never summed."""
    if not isinstance(usage, dict):
        return {"native": None, "codex": None, "transcript": None, "combined": None,
                "note": "No usage was recorded for this attempt; missing data is not zero."}
    native = usage.get("native") if isinstance(usage.get("native"), dict) else None
    codex = usage.get("codex") if isinstance(usage.get("codex"), dict) else None
    ledger = usage.get("transcript") if isinstance(usage.get("transcript"), dict) else None
    return {
        "native": None if native is None else {
            "source": native.get("source"),
            "scope": native.get("scope"),
            "input_tokens": native.get("input_tokens"),
            "total_tokens": native.get("total_tokens"),
            "context_window": native.get("context_window"),
            "prompt_tokens": native.get("prompt_tokens"),
            "output_tokens": native.get("output_tokens"),
            "prompt_cache_hit_tokens": native.get("prompt_cache_hit_tokens"),
            "prompt_cache_miss_tokens": native.get("prompt_cache_miss_tokens"),
            "prompt_cache_write_tokens": native.get("prompt_cache_write_tokens"),
            "context_breakdown": native.get("context_breakdown"),
            "context_breakdown_check": native.get("context_breakdown_check"),
            "cached_input_tokens": native.get("cached_input_tokens"),
            "billed_tokens": native.get("billed_tokens"),
            "observed_fields": native.get("observed_fields"),
            "observed_detail_fields": native.get("observed_detail_fields"),
            "observed_context_fields": native.get("observed_context_fields"),
            "ignored_fields": native.get("ignored_fields"),
            "observed_invalid_fields": native.get("observed_invalid_fields"),
            "detail_arithmetic_check": native.get("detail_arithmetic_check"),
            "notes": native.get("notes"),
        },
        "codex": None if codex is None else {
            "source": codex.get("source"),
            "scope": codex.get("scope"),
            "thread_file": codex.get("thread_file"),
            "window": codex.get("window"),
            "delta": codex.get("delta"),
            "output_generation_tokens": codex.get("output_generation_tokens"),
            "uncached_input_tokens": codex.get("uncached_input_tokens"),
            "reset_fields": codex.get("reset_fields"),
            "error": codex.get("error"),
            "notes": codex.get("notes"),
        },
        "transcript": None if ledger is None else {
            "source": ledger.get("source"),
            "scope": ledger.get("scope"),
            "available": ledger.get("available"),
            "reason": ledger.get("reason"),
            "attributed_by": ledger.get("attributed_by"),
            "identity": ledger.get("identity"),
            "rounds": ledger.get("rounds"),
            "tokens": ledger.get("tokens"),
            "window": ledger.get("window"),
            "model_ids": ledger.get("model_ids"),
            "completeness": ledger.get("completeness"),
            "diagnostics": ledger.get("diagnostics"),
            "snapshot_alignment": ledger.get("snapshot_alignment"),
            "notes": ledger.get("notes"),
        },
        "combined": None,
        "note": "Three meters stay separate (request snapshot, transcript rounds, Codex thread "
                "window); no combined token count, price or saving ratio is computed.",
    }


def acceptance_view(task: dict) -> dict:
    """What was actually recorded about acceptance; the gateway never accepts by itself."""
    status = task.get("status")
    if status == "accepted":
        return {
            "status": "accepted_by_manager",
            "detail": "The manager's acceptance note and evidence are stored in this task's events "
                      "(kind 'accepted'). The gateway did not verify business correctness and did not "
                      "decide the acceptance.",
        }
    if status == "needs_review":
        return {
            "status": "not_accepted_yet",
            "detail": "The attempt finished and is awaiting review. needs_review is not acceptance; "
                      "check the actual artifacts first.",
        }
    return {
        "status": "not_accepted",
        "detail": "Only a finished attempt awaiting review can be accepted; nothing was accepted here.",
    }


def summarize_task(task: dict, text_limit: int = 1200) -> dict:
    """Short status, native chat ID and latest result summary for a manager."""
    native = task.get("native") if isinstance(task.get("native"), dict) else {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    text = result.get("text") if isinstance(result.get("text"), str) else ""
    permission = task.get("permission") if isinstance(task.get("permission"), dict) else None
    desktop = task.get("backend") == "desktop_visible"
    return {
        "id": task.get("id"),
        "status": task.get("status"),
        "backend": task.get("backend"),
        "attempt": task.get("attempt"),
        "stop_confirmed": task.get("stop_confirmed"),
        "stop_confirmed_basis": result.get("stop_confirmed_basis"),
        "activity": result.get("activity") or native.get("activity"),
        "updated_at": task.get("updated_at"),
        "objective": _clip(task.get("objective"), 300),
        "workspace": task.get("workspace"),
        # A CLI compatibility session ID is not a native conversation ID.
        "conversation_id": native.get("conversation_id") if desktop else None,
        "session_id": task.get("session_id"),
        "request_id": native.get("request_id"),
        "native_request_id": native.get("native_request_id"),
        "native_request_id_source": native.get("native_request_id_source"),
        "native_request_identity_conflict": native.get("native_request_identity_conflict"),
        "client_request_id_missing": native.get("client_request_id_missing"),
        "dispatch": native.get("dispatch"),
        "observation_resumed": native.get("observation_resumed"),
        "native_state": result.get("state"),
        "model": result.get("model") or task.get("model"),
        "result_excerpt": _clip(text[-text_limit:], text_limit),
        "result_chars": len(text),
        "error": _clip(task.get("error"), 800),
        "permission": None if permission is None else {
            "id": permission.get("id"),
            "options": [{"optionId": item.get("optionId"), "kind": item.get("kind"), "name": item.get("name")}
                        for item in permission.get("options") or [] if isinstance(item, dict)],
            "decided": bool(permission.get("decision")),
            "source": permission.get("source"),
        },
        "usage": usage_view(task.get("usage")),
        "detail_hint": "Use buddy_events for tool-level detail; the full dispatch prompt is not repeated here.",
        "acceptance": acceptance_view(task),
    }


def brief_task(task: dict) -> dict:
    """One line per task for routine status calls."""
    native = task.get("native") if isinstance(task.get("native"), dict) else {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    text = result.get("text") if isinstance(result.get("text"), str) else ""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return {
        "id": task.get("id"),
        "status": task.get("status"),
        "backend": task.get("backend"),
        "conversation_id": native.get("conversation_id"),
        "stop_confirmed": task.get("stop_confirmed"),
        "updated_at": task.get("updated_at"),
        "result_first_line": _clip(first_line, 160),
        "error": _clip(task.get("error"), 160),
    }


def summarize_status(database: Store, recent: int = 3) -> dict:
    tasks = database.list_tasks()
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    # ``list_tasks`` returns rowid order, so the tail is the most recently created.
    return {
        "counts": counts,
        "backends": {name: sum(task.get("backend") == name for task in tasks)
                     for name in sorted({task.get("backend") or "unknown" for task in tasks})},
        "recent": [brief_task(task) for task in tasks[-recent:]],
        "detail": "buddy_get TASK_ID for one task; buddy_events for tool-level detail.",
    }

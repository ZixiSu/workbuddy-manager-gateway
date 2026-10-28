"""Local waiting for one dispatched attempt. No model calls, no acceptance.

The manager spends one tool call and waits here; this module only reads the same
SQLite task record that the worker writes. It never resends a prompt, never
resolves a permission, never accepts a result, and never turns a timeout into a
completion. ``wait_expired`` and ``attempt_changed`` are observations, not
outcomes of the WorkBuddy task.
"""

from __future__ import annotations

from datetime import datetime
import time

from .store import Store

WAIT_TOOLS = ("buddy_run", "buddy_wait", "buddy_rework_and_wait")

READY_FOR_REVIEW = "ready_for_review"
ALREADY_ACCEPTED = "already_accepted"
NEEDS_ATTENTION = "needs_attention"
UNCERTAIN = "uncertain"
FAILED = "failed"
CANCELLED = "cancelled"
WAIT_EXPIRED = "wait_expired"
WAIT_CANCELLED = "wait_cancelled"
ATTEMPT_CHANGED = "attempt_changed"
NOT_FOUND = "not_found"

DEFAULT_WAIT_SECONDS = 900
MAX_WAIT_SECONDS = 3600
DEFAULT_POLL_SECONDS = 1.0
MIN_POLL_SECONDS = 0.05
MAX_POLL_SECONDS = 30.0

# Statuses that end the wait because the attempt itself finished.
_FINISHED = {
    "uncertain": UNCERTAIN,
    "failed": FAILED,
    "cancelled": CANCELLED,
    "blocked": NEEDS_ATTENTION,
}

# Honest process exit codes for the CLI; 0 means "review it now" or "already reviewed".
EXIT_OK = 0
EXIT_ATTENTION = 10
EXIT_UNCERTAIN = 11
EXIT_FAILED = 12
EXIT_CANCELLED = 13
EXIT_EXPIRED = 14
EXIT_ATTEMPT_CHANGED = 15
EXIT_NOT_FOUND = 16
EXIT_WAIT_CANCELLED = 17
EXIT_CODES = {
    READY_FOR_REVIEW: EXIT_OK,
    ALREADY_ACCEPTED: EXIT_OK,
    NEEDS_ATTENTION: EXIT_ATTENTION,
    UNCERTAIN: EXIT_UNCERTAIN,
    FAILED: EXIT_FAILED,
    CANCELLED: EXIT_CANCELLED,
    WAIT_EXPIRED: EXIT_EXPIRED,
    ATTEMPT_CHANGED: EXIT_ATTEMPT_CHANGED,
    NOT_FOUND: EXIT_NOT_FOUND,
    WAIT_CANCELLED: EXIT_WAIT_CANCELLED,
}

_NEXT_STEP = {
    READY_FOR_REVIEW: "The attempt is reviewable. Read the actual artifacts in the workspace and the task's "
                      "events, then decide; this wait did not accept anything.",
    ALREADY_ACCEPTED: "This task was already accepted. It was read only; nothing was modified.",
    NEEDS_ATTENTION: "A decision or unblocking step is pending. Inspect the task (permission, error and events) "
                     "and act explicitly; waiting neither approved nor rejected anything.",
    UNCERTAIN: "Execution stop is not confirmed. Do not resend the objective; inspect the visible conversation, "
               "then reconcile with evidence or rework deliberately.",
    FAILED: "The attempt failed. Read the error and events before deciding whether a rework is justified.",
    CANCELLED: "The attempt was cancelled. Nothing was resent.",
    WAIT_EXPIRED: "The wait expired; the task is unchanged and still has its own timeout. Call buddy_wait again "
                  "with the same task_id instead of submitting the objective again.",
    WAIT_CANCELLED: "Waiting stopped. The WorkBuddy task was not cancelled or modified.",
    ATTEMPT_CHANGED: "Another round changed this task's attempt, so this wait did not absorb it. Inspect the "
                     "current attempt and wait on that one explicitly.",
    NOT_FOUND: "No such task exists in this state directory.",
}


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clip(value, limit: int) -> str | None:
    """Short excerpt only; a full result is available through buddy_get."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def stop_certainty(task: dict) -> str:
    """Confirm only what the record actually proves."""
    if task.get("stop_confirmed") is True:
        return "confirmed"
    if task.get("status") in {"queued", "running", "waiting_permission", "cancel_requested"}:
        return "not_stopped"
    return "unconfirmed"


def classify(task: dict) -> tuple[str | None, str | None]:
    """Map one record to (outcome, detail); outcome None means keep waiting."""
    status = task.get("status")
    if status == "accepted":
        return ALREADY_ACCEPTED, None
    if status == "needs_review":
        if task.get("stop_confirmed") is True:
            return READY_FOR_REVIEW, None
        # A finished text without a confirmed stop is not a reviewable result.
        return None, "review_recorded_but_stop_unconfirmed"
    if status == "waiting_permission":
        return NEEDS_ATTENTION, "permission_pending"
    if status in _FINISHED:
        return _FINISHED[status], None
    return None, None


def validate_wait(wait_seconds, poll_interval) -> tuple:
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not 0 < wait_seconds <= MAX_WAIT_SECONDS:
        raise ValueError(f"wait_seconds must be a number in (0, {MAX_WAIT_SECONDS}]")
    if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) \
            or not MIN_POLL_SECONDS <= poll_interval <= MAX_POLL_SECONDS:
        raise ValueError(f"poll_interval must be a number in [{MIN_POLL_SECONDS}, {MAX_POLL_SECONDS}] seconds")
    return float(wait_seconds), float(poll_interval)


def wait_for_task(database: Store, task_id: str, attempt: int | None = None,
                  wait_seconds: float = DEFAULT_WAIT_SECONDS,
                  poll_interval: float = DEFAULT_POLL_SECONDS,
                  cancel_event=None, clock=time.monotonic, sleep=time.sleep) -> dict:
    """Read one attempt's record until it is reviewable, finished, or the wait expires.

    ``attempt`` binds the wait to the round it was started for. If the record
    moves to another attempt, the wait reports ``attempt_changed`` instead of
    silently collecting the next round's result.
    """
    wait_seconds, poll_interval = validate_wait(wait_seconds, poll_interval)
    if attempt is not None and (isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1):
        raise ValueError("attempt must be a positive integer or None")
    started = clock()
    deadline = started + wait_seconds
    polls = 0
    last = None
    detail = None
    while True:
        polls += 1
        try:
            task = database.get_task(task_id)
        except (KeyError, ValueError):
            report = _report(NOT_FOUND, None, started, polls, clock, detail="no_such_task", attempt=attempt)
            # Echo the requested id: nothing exists to read it back from.
            report["task_id"] = task_id
            return report
        if attempt is not None and task.get("attempt") != attempt:
            report = _report(ATTEMPT_CHANGED, task, started, polls, clock,
                             detail=f"bound to attempt {attempt}, task is at attempt {task.get('attempt')}",
                             attempt=attempt)
            # Report the round this wait was bound to, not the one it refused to
            # absorb, and never hand back the other round's text.
            report["attempt"] = attempt
            report["current_attempt"] = task.get("attempt")
            report["snapshot"] = None
            return report
        last = task
        outcome, detail = classify(task)
        if outcome is not None:
            return _report(outcome, task, started, polls, clock, detail=detail, attempt=attempt)
        remaining = deadline - clock()
        if remaining <= 0:
            return _report(WAIT_EXPIRED, task, started, polls, clock,
                           detail=detail or "still_active", attempt=attempt)
        # Waiting is cancellable without touching the WorkBuddy task.
        if cancel_event is not None:
            if cancel_event.wait(min(poll_interval, remaining)):
                return _report(WAIT_CANCELLED, task, started, polls, clock,
                               detail="stopped_observing", attempt=attempt)
        else:
            sleep(min(poll_interval, remaining))


def _report(outcome, task, started, polls, clock, detail, attempt) -> dict:
    status = task.get("status") if isinstance(task, dict) else None
    return {
        "outcome": outcome,
        "settled": outcome not in {WAIT_EXPIRED, WAIT_CANCELLED},
        "task_id": (task or {}).get("id"),
        "attempt": (task or {}).get("attempt", attempt),
        "current_attempt": (task or {}).get("attempt"),
        # The record exactly as this wait observed it; the payload is built from
        # this frozen copy so a later rework cannot slip into the answer.
        "snapshot": task,
        "status": status,
        "stop_confirmed": (task or {}).get("stop_confirmed"),
        "stop_certainty": stop_certainty(task) if isinstance(task, dict) else "unknown",
        "elapsed_seconds": round(max(0.0, clock() - started), 3),
        "polls": polls,
        "model_calls_during_wait": 0,
        "detail": detail,
        "observed_at": _now_text(),
    }


def _permission_view(task: dict) -> dict | None:
    permission = task.get("permission")
    if task.get("status") != "waiting_permission" or not isinstance(permission, dict):
        return None
    return {
        "id": permission.get("id"),
        "options": [{"optionId": item.get("optionId"), "kind": item.get("kind"), "name": item.get("name")}
                    for item in permission.get("options") or [] if isinstance(item, dict)],
        "decided": bool(permission.get("decision")),
        "note": "Waiting did not approve or reject this request.",
    }


def wait_options(args: dict) -> tuple:
    """Validate the wait arguments alone, so callers can do it before any side effect."""
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be an object")
    return validate_wait(args.get("wait_seconds", DEFAULT_WAIT_SECONDS),
                         args.get("poll_interval", DEFAULT_POLL_SECONDS))


def build_payload(database: Store, report: dict, mode: str, task: dict | None = None) -> dict:
    """One compact answer for the manager; the prompt and tool stream stay in events.

    Content comes from the wait's own frozen snapshot, never from a fresh read of
    the result, so a rework landing after the last observation cannot decorate
    this round's outcome with the next round's text. One cheap attempt check is
    still made here, because the record can move between the last poll and this
    assembly: if it did, the answer is ``attempt_changed`` and carries no result.
    """
    outcome = report["outcome"]
    detail = report.get("detail")
    bound = report.get("attempt")
    current = None
    if outcome != ALREADY_ACCEPTED and isinstance(bound, int) and not isinstance(bound, bool):
        try:
            current = database.get_task(report["task_id"])
        except (KeyError, ValueError):
            current = None
        if current is not None and current.get("attempt") != bound:
            outcome = ATTEMPT_CHANGED
            detail = f"bound to attempt {bound}, task is at attempt {current.get('attempt')}"
            task = None
    if task is None and outcome != ATTEMPT_CHANGED:
        task = report.get("snapshot")
    task = task if isinstance(task, dict) else {}
    done = task.get("result") if isinstance(task.get("result"), dict) else {}
    text = done.get("text") if isinstance(done.get("text"), str) else ""
    native = task.get("native") if isinstance(task.get("native"), dict) else {}
    if outcome == ATTEMPT_CHANGED:
        text = ""  # that text belongs to another round
    status = current.get("status") if outcome == ATTEMPT_CHANGED and current is not None else report.get("status")
    stop_confirmed = (current.get("stop_confirmed") if outcome == ATTEMPT_CHANGED and current is not None
                      else report.get("stop_confirmed"))
    certainty = (stop_certainty(current) if outcome == ATTEMPT_CHANGED and current is not None
                 else report.get("stop_certainty"))
    payload = {
        "mode": mode,
        "outcome": outcome,
        "settled": report["settled"],
        "task_id": report.get("task_id"),
        "attempt": bound,
        "current_attempt": (current.get("attempt")
                            if outcome == ATTEMPT_CHANGED and current is not None
                            else report.get("current_attempt")),
        "status": status,
        "stop_confirmed": stop_confirmed,
        "stop_certainty": certainty,
        "detail": detail,
        "waited_seconds": report["elapsed_seconds"],
        "polls": report["polls"],
        "model_calls_during_wait": 0,
        "review_ready": outcome == READY_FOR_REVIEW,
        "accepted": outcome == ALREADY_ACCEPTED,
        "conversation_id": native.get("conversation_id") if task.get("backend") == "desktop_visible" else None,
        "request_id": native.get("request_id"),
        "native_request_id": native.get("native_request_id"),
        "result_excerpt": _clip(text[-800:], 800),
        "result_chars": len(text),
        "permission_pending": _permission_view(task),
        "error": _clip(task.get("error"), 400),
        "next_step": _NEXT_STEP.get(outcome, "Inspect the task record."),
        # Explicit non-replay: the dispatch prompt and tool stream are not returned here.
        "prompt_resent": False,
        "tool_stream_included": False,
        "observed_at": report.get("observed_at"),
    }
    return payload


def exit_code(outcome: str) -> int:
    return EXIT_CODES.get(outcome, EXIT_FAILED)

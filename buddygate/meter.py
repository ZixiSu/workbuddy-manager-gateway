"""Token accounting with explicit provenance, scope and gaps.

Two independent meters exist and are never combined:

* ``workbuddy_native`` — the counters a desktop conversation request reported on
  its own ``usage`` object. Two groups are kept apart:

  - top-level context figures (``inputTokens``/``totalTokens``/``contextWindow``);
  - the observed ``usage.details`` snapshot (``promptTokens``,
    ``completionTokens``, ``promptCacheHitTokens``, ``promptCacheMissTokens``,
    ``promptCacheWriteTokens``).

  The scope is that request's **final** ``usage.details`` snapshot. Whether it
  accumulates every internal model round is **not verified**, so neither the
  details nor the top-level totals are a whole-task bill.
* ``codex_thread`` — the manager's own thread ``token_count`` events, read only
  from one explicitly supplied file. A task usage is the difference between the
  snapshots taken around the dispatch, over that file's time window.

Neither meter produces a price, a saving ratio or a guaranteed cost bound.
Fields that were not observed stay ``None``; they are never filled with zero.
"""

from __future__ import annotations

import json
from datetime import datetime
import math
import os
from pathlib import Path

NATIVE_SOURCE = "workbuddy_native_request_usage"
CODEX_SOURCE = "codex_thread_token_count"

# Only these top-level counters have been observed on a native conversation request.
_NATIVE_TOP = {
    "inputTokens": "input_tokens",
    "totalTokens": "total_tokens",
    "contextWindow": "context_window",
}

# Only these nested counters have been observed in the same request's usage.details.
_NATIVE_DETAIL = {
    "promptTokens": "prompt_tokens",
    "completionTokens": "output_tokens",
    "promptCacheHitTokens": "prompt_cache_hit_tokens",
    "promptCacheMissTokens": "prompt_cache_miss_tokens",
    "promptCacheWriteTokens": "prompt_cache_write_tokens",
}

# A context-composition breakdown observed next to the totals on some requests.
_CONTEXT_PARTS = ("systemPrompt", "conversation", "tools", "mcp", "skills")

NATIVE_SCOPE = ("final usage.details snapshot of one desktop conversation request; "
                "whether it accumulates every internal model round of the task is not verified")

NATIVE_NOTES = (
    "Scope is the request's final usage.details snapshot; it is not proven to be a whole-task "
    "cumulative consumption.",
    "Top-level totalTokens is a context figure from that same snapshot, not a whole-task bill.",
    "Not a bill: no price, cache pricing, currency or account quota is derived from these counters.",
    "cost/estimatedCost were not parsed: a non-token field is named but its value is never converted.",
    "byCategory, when present, is a context-composition breakdown of that request; it is not generated "
    "output and not a bill.",
    "cached_input_tokens stays null because only the desktop's promptCacheHitTokens counter was "
    "observed; it is reported under its own name instead of being relabelled.",
    "reasoning, uncached-output and billed counters were not observed and remain null.",
)


def _number(value):
    """A nonnegative integral token count; rejects bools, NaN, infinities and fractions."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
    if value < 0:
        return None
    return int(value)


def _split_usage(usage) -> tuple[dict, dict, dict, list, list, list, list]:
    """Accept the helper's structured usage or a flat mapping; whitelist both.

    Returns ``(top, details, context, observed, observed_detail, ignored, invalid)``.
    ``ignored`` names fields that were present but are not token counters (for
    example ``cost``); their values are never copied. ``invalid`` names counters
    that were present but not usable numbers.
    """
    if not isinstance(usage, dict):
        raw_top, raw_detail, raw_context, ignored, invalid = {}, {}, {}, [], []
    elif isinstance(usage.get("top"), dict) or isinstance(usage.get("details"), dict):
        raw_top = usage.get("top") if isinstance(usage.get("top"), dict) else {}
        raw_detail = usage.get("details") if isinstance(usage.get("details"), dict) else {}
        raw_context = usage.get("byCategory") if isinstance(usage.get("byCategory"), dict) else {}
        ignored = [str(item)[:60] for item in usage.get("ignored_fields") or [] if isinstance(item, str)]
        invalid = [str(item)[:60] for item in usage.get("invalid_fields") or [] if isinstance(item, str)]
    else:
        raw_top, raw_detail, raw_context, ignored, invalid = usage, {}, {}, [], []
    top = {name: _number(raw_top.get(key)) for key, name in _NATIVE_TOP.items()}
    details = {name: _number(raw_detail.get(key)) for key, name in _NATIVE_DETAIL.items()}
    context = {name: _number(raw_context.get(name)) for name in _CONTEXT_PARTS}
    observed = sorted(key for key in _NATIVE_TOP if key in raw_top)
    observed_detail = sorted(key for key in _NATIVE_DETAIL if key in raw_detail)
    if not ignored and isinstance(usage, dict):
        ignored = sorted(key for key in usage
                         if key not in _NATIVE_TOP and key not in ("top", "details", "byCategory"))
    return top, details, context, observed, observed_detail, ignored, invalid


def native_usage(request: dict | None) -> dict:
    """Normalise one native request's reported counters without inventing fields."""
    usage = request.get("usage") if isinstance(request, dict) else None
    top, details, context, observed, observed_detail, ignored, invalid = _split_usage(usage)

    prompt = details.get("prompt_tokens")
    hit = details.get("prompt_cache_hit_tokens")
    miss = details.get("prompt_cache_miss_tokens")
    write = details.get("prompt_cache_write_tokens")
    check = None
    if prompt is not None and hit is not None and miss is not None:
        expected = hit + miss + (write or 0)
        check = {
            "prompt_split_consistent": prompt == expected,
            "expected": expected,
            "note": "Arithmetic check of the reported detail counters only; it does not prove the "
                    "counters are complete, billable or cumulative.",
        }

    breakdown = {name: value for name, value in context.items() if value is not None}
    breakdown_check = None
    if breakdown and top["input_tokens"] is not None:
        total = sum(breakdown.values())
        breakdown_check = {
            "sum": total,
            "matches_input_tokens": total == top["input_tokens"],
            "note": "Arithmetic check of the reported context breakdown only; it does not prove the "
                    "breakdown is complete or that it is consumed output.",
        }

    return {
        "source": NATIVE_SOURCE,
        "scope": NATIVE_SCOPE,
        "input_tokens": top["input_tokens"],
        "total_tokens": top["total_tokens"],
        "context_window": top["context_window"],
        "prompt_tokens": details["prompt_tokens"],
        "output_tokens": details["output_tokens"],
        "prompt_cache_hit_tokens": details["prompt_cache_hit_tokens"],
        "prompt_cache_miss_tokens": details["prompt_cache_miss_tokens"],
        "prompt_cache_write_tokens": details["prompt_cache_write_tokens"],
        "context_breakdown": breakdown or None,
        "context_breakdown_check": breakdown_check,
        # Never relabel a details counter as a field that was not observed.
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "billed_tokens": None,
        "billed_amount": None,
        "observed_fields": observed,
        "observed_detail_fields": observed_detail,
        "observed_context_fields": sorted(breakdown),
        "ignored_fields": ignored,
        "observed_invalid_fields": invalid,
        "detail_arithmetic_check": check,
        "notes": list(NATIVE_NOTES),
    }


def _empty_snapshot(path: str) -> dict:
    return {"source": CODEX_SOURCE, "thread_file": path, "events": 0, "latest": None,
            "window": {"first": None, "last": None},
            "scope": "one explicitly supplied Codex thread file only; another chat is never read",
            "tokens": None}


def codex_snapshot(path: str, max_bytes: int = 512 * 1024 * 1024) -> dict:
    """Read ``token_count`` events from exactly one supplied thread file."""
    candidate = Path(path).expanduser()
    result = _empty_snapshot(str(candidate))
    if not candidate.is_absolute() or not candidate.is_file():
        result["error"] = "Thread file is missing or not an absolute path"
        return result
    try:
        if candidate.stat().st_size > max_bytes:
            result["error"] = "Thread file exceeds the readable size bound"
            return result
        first = last = None
        tokens = None
        count = 0
        with candidate.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if '"token_count"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                totals = info.get("total_token_usage") if isinstance(info, dict) else None
                if not isinstance(totals, dict):
                    continue
                count += 1
                stamp = record.get("timestamp")
                stamp = stamp if isinstance(stamp, str) else None
                first = first or stamp
                last = stamp or last
                tokens = {key: _number(totals.get(key)) for key in (
                    "input_tokens", "cached_input_tokens", "output_tokens",
                    "reasoning_output_tokens", "total_tokens")}
        result.update(events=count, tokens=tokens,
                      window={"first": first, "last": last},
                      scope="whole supplied thread file up to its last token_count event; other chats are never read; not a single delegated task")
        return result
    except OSError:
        result["error"] = "Thread file could not be read"
        return result


def _stamp(snapshot: dict, edge: str):
    window = snapshot.get("window") if isinstance(snapshot.get("window"), dict) else {}
    value = window.get(edge)
    return value if isinstance(value, str) and value else None


def codex_delta(before: dict | None, after: dict | None) -> dict:
    """Difference between two snapshots of the same thread file.

    Refuses to subtract across different files or across a counter reset. Every
    channel is reported separately: output generation is ``output_tokens`` and
    the uncached input is ``input_tokens - cached_input_tokens``. Neither is
    called a bill, and the window is the thread's time range, not a causal
    attribution to this gateway task.
    """
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    before_tokens = before.get("tokens") if isinstance(before.get("tokens"), dict) else None
    after_tokens = after.get("tokens") if isinstance(after.get("tokens"), dict) else None
    before_file = before.get("thread_file") if isinstance(before.get("thread_file"), str) else None
    after_file = after.get("thread_file") if isinstance(after.get("thread_file"), str) else None

    result = {
        "source": CODEX_SOURCE,
        "scope": "difference between two token_count snapshots of the same supplied thread file, "
                 "covering the dispatch window; not a causal bill and not one delegated task",
        "thread_file": after_file or before_file,
        "window": {"first": _stamp(before, "last"), "last": _stamp(after, "last")},
        "delta": None,
        "output_generation_tokens": None,
        "uncached_input_tokens": None,
        "notes": [
            "Window starts at the before-snapshot's last token_count event, not at the file's first event.",
            "output_generation_tokens is the output_tokens increment only.",
            "uncached_input_tokens is input_tokens minus cached_input_tokens; it is not generation.",
            "reasoning_output_tokens is reported separately and is never added to output_tokens here.",
            "The window covers the whole manager thread during the dispatch, so it includes any other work in that file.",
            "No price, saving ratio or billing total is inferred.",
        ],
    }

    if not before_tokens or not after_tokens:
        result["error"] = "One or both snapshots have no token counters; no delta is computed"
        return result
    if before_file and after_file and os.path.normcase(before_file) != os.path.normcase(after_file):
        result["error"] = "The two snapshots come from different thread files; refusing to mix them"
        return result

    delta = {}
    negative = []
    for key in ("input_tokens", "cached_input_tokens", "output_tokens",
                "reasoning_output_tokens", "total_tokens"):
        start, end = _number(before_tokens.get(key)), _number(after_tokens.get(key))
        if start is None or end is None:
            delta[key] = None
            continue
        if end < start:
            negative.append(key)
        delta[key] = end - start

    before_events = before.get("events")
    if isinstance(before_events, int) and not isinstance(before_events, bool) \
            and isinstance(after.get("events"), int) and after["events"] < before_events:
        negative.append("events")

    if negative:
        result["reset_fields"] = sorted(set(negative))
        result["error"] = ("A counter decreased between the snapshots (reset or rotated thread file); "
                           "the delta is treated as unavailable rather than adjusted")
        return result

    result["delta"] = delta
    result["output_generation_tokens"] = delta["output_tokens"]
    if delta["input_tokens"] is not None and delta["cached_input_tokens"] is not None:
        uncached = delta["input_tokens"] - delta["cached_input_tokens"]
        if uncached < 0:
            result["reset_fields"] = ["uncached_input_tokens"]
            result["delta"] = None
            result["output_generation_tokens"] = None
            result["error"] = "Cached input exceeds total input in the delta; the delta is treated as unavailable"
            return result
        result["uncached_input_tokens"] = uncached
    return result


def usage_record(native: dict | None = None, codex: dict | None = None) -> dict:
    """Assemble the stored usage block; missing meters stay explicitly absent."""
    record = {
        "native": native,
        "codex": codex,
        "combined": None,
        "notes": [
            "Meters are labelled by source and are not summed into one token count.",
            "Absent channels (output, cache, billing) stay null rather than zero.",
            "Native scope is one request's final usage.details snapshot; Codex scope is the thread time window.",
        ],
    }
    record["recorded_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return record

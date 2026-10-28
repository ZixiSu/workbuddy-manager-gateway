"""Per-model-round usage persisted in the local transcript of one request.

The desktop request object only exposes the **last** ``usage`` snapshot, so a
task that internally made several model rounds would be under-reported. The
local transcript of the same conversation persists one ``providerData.usage``
object per assistant round, which can be summed for exactly one dispatch.

Boundaries enforced here, not merely documented:

* the conversation must be one the gateway dispatched, and only rounds whose
  ``providerData.conversationRequestId`` matches that dispatch are counted;
* the file is located from a verified WorkBuddy config directory
  (``workbuddy.db`` plus ``projects``) and the session's own ``cwd``; a bounded
  one-level lookup by the exact ``<conversationId>.jsonl`` name is the fallback;
* links, junctions, path escapes, several matches and oversized files or lines
  are refused rather than guessed;
* only IDs, a model name and whitelisted numeric counters are read. Message
  text, reasoning text, tool arguments and results, and account material are
  never copied, returned or logged.

Absent evidence stays ``None``. A count is never fabricated from a snapshot,
and a failed or retried call that persisted nothing is invisible rather than
assumed.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from .store import BACKEND_DESKTOP

LEDGER_SOURCE = "workbuddy_transcript_provider_usage"
LEDGER_SCOPE = (
    "per-model-round providerData.usage records of exactly one native request in one local "
    "conversation transcript; auxiliary model calls that persisted no usage record are excluded"
)

_NOTES = (
    "Counts only usage records physically persisted in this conversation's transcript; a failed or "
    "retried model call that wrote no record cannot be seen here and is not estimated.",
    "This is not an official bill, not an account-level total, and not proof that every call was persisted.",
    "Rounds are deduplicated by providerData.messageId; identical duplicates are counted once and a "
    "conflicting duplicate makes that round unusable instead of being averaged.",
    "cache_miss_tokens is the transcript's own counter when present, otherwise derived as input - "
    "cached and flagged as derived.",
    "reasoning_tokens_separate is never added to output_tokens; whether output already includes it is "
    "unproven.",
    "Non-token fields (credit, cost, cache_read/cache_creation, cache write) are named only and never "
    "converted or summed.",
    "Kept separate from the desktop request snapshot and from the Codex thread meter; nothing is combined.",
)

_DOES_NOT_INCLUDE = (
    "model calls that persisted no usage record (failed or retried calls, and auxiliary calls such as "
    "automatic title generation)",
    "account, plan or credit totals; no account data is read",
    "price, credit units or a bill of any kind",
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_IDENTIFIER = re.compile(r"^[0-9a-z_-]{8,64}$")

# Top-level counters read from providerData.usage.
_TOP_COUNTERS = ("inputTokens", "outputTokens", "totalTokens", "requests")
# Consumption channels: missing values make the recorded sum incomplete.
_CORE_CHANNELS = ("input_tokens", "output_tokens", "total_tokens", "requests")
# Nested counters observed in providerData.usage on some builds.
_DETAIL_SOURCES = (
    ("inputTokensDetails", "cached_tokens", "usage.inputTokensDetails[0].cached_tokens"),
    ("outputTokensDetails", "reasoning_tokens", "usage.outputTokensDetails[0].reasoning_tokens"),
)
# Counters observed under providerData.rawUsage on other builds (named, not re-labelled).
_RAW_COUNTERS = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_thinking_tokens")


class LedgerUnavailable(Exception):
    """A path, size or shape problem that must be reported, never estimated."""


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 32 * 1024 * 1024
    max_line_bytes: int = 256 * 1024
    max_rounds: int = 512
    max_project_entries: int = 400


def _number(value):
    """A nonnegative integral count; rejects bools, NaN, infinities and fractions."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
    if value < 0:
        return None
    return int(value)


def _identifier(value):
    """A bounded, dash/underscore-safe identifier or ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if _IDENTIFIER.match(text) else None


def _counter(container, key):
    """Return ``(value, invalid)``; ``invalid`` means present but unusable."""
    if not isinstance(container, dict) or key not in container:
        return None, False
    value = _number(container.get(key))
    return value, value is None


def normalize_conversation_id(value):
    """A conversation UUID, or ``LedgerUnavailable`` if it could build a bad path."""
    if not isinstance(value, str) or not value.strip():
        raise LedgerUnavailable("no conversation id was supplied")
    text = value.strip().lower()
    if not _UUID.match(text):
        raise LedgerUnavailable("the conversation id is not a UUID; refusing to build a path from it")
    return text


def compress_workspace_path_name(cwd):
    """The transcript directory name for a workspace path.

    Observed rule: ``/``, ``\\`` and ``:`` become ``-``, leading/trailing ``-``
    are dropped and runs collapse. Over 255 UTF-8 bytes the name is truncated to
    180 bytes and suffixed with a djb2-base36 digest; that long-name branch is
    implemented from the documented rule and is not exercised on this machine.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        raise LedgerUnavailable("the session has no workspace path to build a transcript directory name")
    key = re.sub(r"[/\\:]", "-", cwd)
    key = re.sub(r"-+", "-", key).strip("-")
    if not key:
        raise LedgerUnavailable("the workspace path produced an empty transcript directory name")
    if len(key.encode("utf-8")) > 255:
        digest = _djb2_base36(key)
        key = key.encode("utf-8")[:180].decode("utf-8", "ignore") + "-" + digest
    return key


def _djb2_base36(value):
    digest = 5381
    for byte in value.encode("utf-8"):
        digest = ((digest * 33) + byte) & 0xFFFFFFFF
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if digest == 0:
        return "0"
    out = []
    while digest:
        digest, remainder = divmod(digest, 36)
        out.append(digits[remainder])
    return "".join(reversed(out))


def config_dir_candidates():
    """Finite, conventional locations only; no filesystem walk."""
    home = Path.home()
    candidates = []
    configured = os.environ.get("BUDDYGATE_WORKBUDDY_CONFIG_DIR")
    if configured:
        candidates.append(Path(configured))
    candidates.append(home / ".workbuddy")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.extend((Path(appdata) / "WorkBuddy", Path(appdata) / "workbuddy"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "WorkBuddy")
    candidates.append(home / "Library" / "Application Support" / "WorkBuddy")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "workbuddy")
    candidates.append(home / ".config" / "workbuddy")
    return candidates


def resolve_config_dir(explicit=None):
    """A directory that really holds ``workbuddy.db`` and ``projects`` and is not a link."""
    if explicit is not None:
        if not isinstance(explicit, (str, os.PathLike)) or not str(explicit).strip():
            raise LedgerUnavailable("the supplied config directory is empty")
        candidates = [Path(explicit).expanduser()]
    else:
        candidates = config_dir_candidates()
    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            if not (candidate / "workbuddy.db").is_file() or not (candidate / "projects").is_dir():
                continue
        except OSError:
            continue
        real = Path(os.path.realpath(candidate))
        if os.path.normcase(str(real)) != os.path.normcase(str(candidate.resolve())):
            continue
        return candidate
    return None


def session_cwd(config_dir, conversation_id):
    """The session's own ``cwd`` from a read-only ``WHERE id = ?`` lookup."""
    database = Path(config_dir) / "workbuddy.db"
    uri = "file:" + database.as_posix() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=3)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT cwd FROM sessions WHERE id = ?", (conversation_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return row[0] if row and isinstance(row[0], str) and row[0].strip() else None


def _verified_file(path, projects_root):
    """Resolve one transcript file, refusing links, escapes and non-regular files."""
    try:
        listing = os.lstat(path)
    except OSError:
        raise LedgerUnavailable("the transcript file could not be inspected") from None
    if stat.S_ISLNK(listing.st_mode):
        raise LedgerUnavailable("the transcript path is a link; refusing to follow it")
    if not stat.S_ISREG(listing.st_mode):
        raise LedgerUnavailable("the transcript path is not a regular file")
    real = Path(os.path.realpath(path))
    root = Path(os.path.realpath(projects_root))
    try:
        relative = real.relative_to(root)
    except ValueError:
        raise LedgerUnavailable("the transcript path escapes the projects directory") from None
    if len(relative.parts) != 2:
        raise LedgerUnavailable("the transcript is not directly inside one project directory")
    return real


def locate_transcript(config_dir, conversation_id, cwd=None, limits=None):
    """Exactly one transcript for this conversation, or a refusal."""
    limits = limits or Limits()
    projects = Path(config_dir) / "projects"
    name = conversation_id + ".jsonl"
    found = []
    if cwd:
        exact = projects / compress_workspace_path_name(cwd) / name
        try:
            if exact.is_file():
                found.append(exact)
        except OSError:
            pass
    scanned = 0
    try:
        entries = sorted(projects.iterdir())
    except OSError:
        raise LedgerUnavailable("the projects directory could not be listed") from None
    for entry in entries[: limits.max_project_entries]:
        scanned += 1
        if entry.is_symlink() or not entry.is_dir():
            continue
        candidate = entry / name
        if candidate in found:
            continue
        try:
            if candidate.is_file():
                found.append(candidate)
        except OSError:
            continue
    if not found:
        raise LedgerUnavailable("this conversation has no transcript file; no conversation content was read")
    unique = []
    for candidate in found:
        resolved = _verified_file(candidate, projects)
        if resolved not in unique:
            unique.append(resolved)
    if len(unique) > 1:
        raise LedgerUnavailable("several transcript files match this conversation id; refusing to choose")
    return unique[0]


def _round_from(provider, usage, record):
    """Whitelisted identifiers and counters for one record, plus invalid field names."""
    invalid = []
    values = {}
    for key in _TOP_COUNTERS:
        value, bad = _counter(usage, key)
        values[key] = value
        if bad:
            invalid.append("usage." + key)

    cache_hit = None
    cache_hit_source = None
    for holder, key, label in _DETAIL_SOURCES[:1]:
        nested = usage.get(holder)
        if isinstance(nested, list) and nested and isinstance(nested[0], dict):
            cache_hit = _number(nested[0].get(key))
            if cache_hit is not None:
                cache_hit_source = label
    reasoning = None
    reasoning_source = None
    holder, key, label = _DETAIL_SOURCES[1]
    nested = usage.get(holder)
    if isinstance(nested, list) and nested and isinstance(nested[0], dict):
        reasoning = _number(nested[0].get(key))
        if reasoning is not None:
            reasoning_source = label

    raw = provider.get("rawUsage") if isinstance(provider.get("rawUsage"), dict) else {}
    if cache_hit is None:
        cache_hit, bad = _counter(raw, "prompt_cache_hit_tokens")
        cache_hit_source = "rawUsage.prompt_cache_hit_tokens" if cache_hit is not None else None
        if bad:
            invalid.append("rawUsage.prompt_cache_hit_tokens")
    if reasoning is None:
        reasoning, bad = _counter(raw, "completion_thinking_tokens")
        reasoning_source = "rawUsage.completion_thinking_tokens" if reasoning is not None else None
        if bad:
            invalid.append("rawUsage.completion_thinking_tokens")
    miss_reported, bad = _counter(raw, "prompt_cache_miss_tokens")
    if bad:
        invalid.append("rawUsage.prompt_cache_miss_tokens")

    miss = None
    derived = None
    hit_exceeds_input = False
    if miss_reported is not None:
        miss, derived = miss_reported, False
    elif values["inputTokens"] is not None and cache_hit is not None:
        if cache_hit <= values["inputTokens"]:
            miss, derived = values["inputTokens"] - cache_hit, True
        else:
            hit_exceeds_input = True

    total, output = values["totalTokens"], values["outputTokens"]
    inp = values["inputTokens"]
    identity = None if (total is None or inp is None or output is None) else total == inp + output
    return {
        "message_id": _identifier(provider.get("messageId")),
        "conversation_request_id": _identifier(provider.get("conversationRequestId")),
        "model_id": (provider.get("requestModelId")[:80]
                     if isinstance(provider.get("requestModelId"), str) else None),
        "timestamp_ms": _number(record.get("timestamp")),
        "input_tokens": inp,
        "output_tokens": output,
        "total_tokens": total,
        "requests": values["requests"],
        "cache_hit_tokens": cache_hit,
        "cache_hit_source": cache_hit_source,
        "cache_miss_tokens": miss,
        "cache_miss_derived": derived,
        "cache_miss_reported_tokens": miss_reported,
        "reasoning_tokens_separate": reasoning,
        "reasoning_source": reasoning_source,
        "total_identity_ok": identity,
        "cache_hit_exceeds_input": hit_exceeds_input or None,
        "invalid_fields": invalid,
    }


def _scan(path, limits):
    """Read whitelisted counters, ignoring everything else in the file."""
    diagnostics = {
        "lines_read": 0,
        "skipped_lines": 0,
        "parse_errors": 0,
        "oversized_lines": 0,
        "rounds_without_usage": 0,
        "unkeyed_usage_records": 0,
        "duplicate_identical_rounds": 0,
        "conflicting_message_ids": 0,
        "compacted_records": 0,
        "invalid_fields": [],
        "truncated": False,
    }
    rounds = []
    request_ids = set()
    user_request_markers = 0
    try:
        size = os.stat(path).st_size
    except OSError:
        raise LedgerUnavailable("the transcript size could not be read") from None
    if size > limits.max_bytes:
        raise LedgerUnavailable("the transcript is larger than the readable size bound")
    try:
        stream = path.open("rb")
    except OSError:
        raise LedgerUnavailable("the transcript could not be opened") from None
    with stream:
        while True:
            if len(rounds) >= limits.max_rounds:
                diagnostics["truncated"] = True
                break
            chunk = stream.readline(limits.max_line_bytes + 1)
            if not chunk:
                break
            if len(chunk) > limits.max_line_bytes and not chunk.endswith(b"\n"):
                diagnostics["oversized_lines"] += 1
                while True:  # discard the remainder of this physical line unparsed
                    more = stream.readline(1 << 20)
                    if not more or more.endswith(b"\n"):
                        break
                continue
            diagnostics["lines_read"] += 1
            if b'"providerData"' not in chunk:
                diagnostics["skipped_lines"] += 1
                continue
            try:
                record = json.loads(chunk.decode("utf-8", "replace"))
            except ValueError:
                diagnostics["parse_errors"] += 1
                continue
            if not isinstance(record, dict):
                diagnostics["parse_errors"] += 1
                continue
            if (record.get("isCompacted") is True or record.get("isSummary") is True
                    or record.get("isReplayForkNotice") is True):
                diagnostics["compacted_records"] += 1
            provider = record.get("providerData")
            if not isinstance(provider, dict):
                diagnostics["skipped_lines"] += 1
                continue
            request_id = _identifier(provider.get("conversationRequestId"))
            if request_id:
                request_ids.add(request_id)
            if provider.get("startsNewUserRequest") is True:
                user_request_markers += 1
            usage = provider.get("usage")
            if not isinstance(usage, dict):
                diagnostics["rounds_without_usage"] += 1
                continue
            item = _round_from(provider, usage, record)
            if not item["message_id"]:
                diagnostics["unkeyed_usage_records"] += 1
                continue
            for name in item["invalid_fields"]:
                if name not in diagnostics["invalid_fields"]:
                    diagnostics["invalid_fields"].append(name)
            rounds.append(item)
    return rounds, diagnostics, request_ids, user_request_markers


def _same_counters(left, right):
    keys = ("input_tokens", "output_tokens", "total_tokens", "requests", "cache_hit_tokens",
            "reasoning_tokens_separate", "cache_miss_tokens")
    return all(left.get(key) == right.get(key) for key in keys)


def _dedupe(rounds, diagnostics):
    """One usable round per message id; a conflicting duplicate is not averaged."""
    seen = {}
    order = []
    for item in rounds:
        key = item["message_id"]
        if key not in seen:
            seen[key] = item
            order.append(key)
            continue
        if _same_counters(seen[key], item):
            diagnostics["duplicate_identical_rounds"] += 1
            continue
        fields = sorted(name for name in ("input_tokens", "output_tokens", "total_tokens", "requests",
                                          "cache_hit_tokens", "reasoning_tokens_separate")
                        if seen[key].get(name) != item.get(name))
        seen[key]["conflict_fields"] = fields
        seen[key]["usable"] = False
        diagnostics["conflicting_message_ids"] += 1
    for key in order:
        seen[key].setdefault("usable", True)
    return [seen[key] for key in order]


def _attributed(rounds, native_request_id, request_ids, markers):
    target = _identifier(native_request_id)
    if target:
        selected = [item for item in rounds if item["conversation_request_id"] == target]
        if not selected:
            raise LedgerUnavailable(
                "no persisted round belongs to this native request id; rounds of another request "
                "were not counted")
        return selected, "native_request_id"
    if len(request_ids) == 1 and markers <= 1:
        only = next(iter(request_ids))
        selected = [item for item in rounds if item["conversation_request_id"] == only]
        if not selected:
            raise LedgerUnavailable("the only request in this conversation has no usable round")
        return selected, "single_request_in_conversation"
    if not request_ids:
        raise LedgerUnavailable("no round carries a conversation request id; attribution is impossible")
    raise LedgerUnavailable(
        f"this conversation holds {len(request_ids)} requests and no native request id is persisted; "
        "refusing to guess which rounds belong to this dispatch")


def _sum_channel(rounds, key):
    present = [item[key] for item in rounds if item.get(key) is not None]
    if not present:
        return None, bool(rounds)
    return sum(present), len(present) < len(rounds)


def _alignment(selected, api_snapshot):
    if not selected or not isinstance(api_snapshot, dict):
        return None
    last = selected[-1]
    pairs = (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
             ("cache_hit_tokens", "prompt_cache_hit_tokens"),
             ("cache_miss_tokens", "prompt_cache_miss_tokens"))
    checks = {}
    for ledger_key, api_key in pairs:
        api_value = _number(api_snapshot.get(api_key))
        if api_value is None:
            continue
        ledger_value = last.get(ledger_key)
        checks[ledger_key] = {"api": api_value, "last_round": ledger_value,
                              "equal": ledger_value == api_value}
    if not checks:
        return None
    return {
        "compared_with": "last attributed round",
        "checks": checks,
        "aligned": all(item["equal"] for item in checks.values()),
        "note": "The desktop request object reports its last usage snapshot, so it is compared with the "
                "last attributed round rather than the sum. A mismatch is recorded as a diagnostic and "
                "is not attributed; auxiliary calls or different accounting could explain it.",
    }


def _empty(conversation_id):
    return {
        "source": LEDGER_SOURCE,
        "scope": LEDGER_SCOPE,
        "available": False,
        "conversation_id": conversation_id if isinstance(conversation_id, str) else None,
        "native_request_id": None,
        "attributed_by": None,
        "identity": {"native_request_id": None, "source": None, "client_request_id_is_canonical": False},
        "rounds": None,
        "tokens": None,
        "per_round": None,
        "model_ids": None,
        "window": {"first_ms": None, "last_ms": None},
        "completeness": {"status": "unavailable", "only_persisted_calls": True,
                         "gaps": [], "warnings": [], "does_not_include": list(_DOES_NOT_INCLUDE)},
        "diagnostics": None,
        "snapshot_alignment": None,
        "transcript_file": None,
        "config_dir": None,
        "reason": None,
        "notes": list(_NOTES),
    }


def unavailable_ledger(reason, conversation_id=None):
    """A report that names why nothing could be counted; no file is read."""
    result = _empty(conversation_id)
    result["reason"] = str(reason)
    return result


def transcript_usage(conversation_id, native_request_id=None, config_dir=None,
                     api_snapshot=None, limits=None, native_request_id_source=None):
    """Sum the persisted per-round counters of one dispatched request.

    Returns a report in every case: ``available`` is false with a ``reason`` when
    the transcript cannot be located, verified or attributed. Missing data is
    never replaced by zero or by the request's last snapshot.
    """
    limits = limits or Limits()
    result = _empty(conversation_id)
    try:
        cid = normalize_conversation_id(conversation_id)
        directory = resolve_config_dir(config_dir)
        if directory is None:
            raise LedgerUnavailable(
                "no verified WorkBuddy config directory (workbuddy.db and projects) was found")
        result["config_dir"] = str(directory)
        cwd = session_cwd(directory, cid)
        path = locate_transcript(directory, cid, cwd, limits)
        result["transcript_file"] = str(path)
        rounds, diagnostics, request_ids, markers = _scan(path, limits)
        deduped = _dedupe(rounds, diagnostics)
        selected, attribution = _attributed(deduped, native_request_id, request_ids, markers)
    except LedgerUnavailable as error:
        result["reason"] = str(error)
        return result

    usable = [item for item in selected if item.get("usable", True)]
    chosen = selected[0]["conversation_request_id"]
    other_ids = sorted({item["conversation_request_id"] for item in deduped
                        if item["conversation_request_id"] and item["conversation_request_id"] != chosen})
    other_rounds = sum(1 for item in deduped
                       if item["conversation_request_id"] and item["conversation_request_id"] != chosen)
    unattributed_rounds = sum(1 for item in deduped if not item["conversation_request_id"])

    channels = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "total_tokens": "total_tokens",
        "requests": "requests",
        "cache_hit_tokens": "cache_hit_tokens",
        "reasoning_tokens_separate": "reasoning_tokens_separate",
    }
    tokens = {}
    missing = []
    absent_detail = []
    for name, key in channels.items():
        value, gap = _sum_channel(usable, key)
        tokens[name] = value
        if gap:
            # Core consumption channels make the sum incomplete; detail channels
            # that a build simply does not report are a warning, not a fake zero.
            (missing if name in _CORE_CHANNELS else absent_detail).append(name)
    miss_values = [item["cache_miss_tokens"] for item in usable if item["cache_miss_tokens"] is not None]
    if not miss_values:
        tokens["cache_miss_tokens"] = None
        absent_detail.append("cache_miss_tokens")
        cache_miss_derived = None
    else:
        tokens["cache_miss_tokens"] = sum(miss_values)
        if len(miss_values) < len(usable):
            absent_detail.append("cache_miss_tokens")
        flags = {item["cache_miss_derived"] for item in usable if item["cache_miss_tokens"] is not None}
        cache_miss_derived = True if flags == {True} else (False if flags == {False} else None)

    gaps = []
    if diagnostics["parse_errors"]:
        gaps.append(f"{diagnostics['parse_errors']} unparsed line(s)")
    if diagnostics["oversized_lines"]:
        gaps.append(f"{diagnostics['oversized_lines']} oversized line(s) skipped")
    if diagnostics["unkeyed_usage_records"]:
        gaps.append(f"{diagnostics['unkeyed_usage_records']} usage record(s) without a message id")
    if diagnostics["conflicting_message_ids"]:
        gaps.append(f"{diagnostics['conflicting_message_ids']} conflicting duplicate message id(s)")
    if diagnostics["compacted_records"]:
        gaps.append(f"{diagnostics['compacted_records']} compacted or summary record(s)")
    if diagnostics["truncated"]:
        gaps.append("reading stopped at the round bound")
    if missing:
        gaps.append("missing channel(s): " + ", ".join(sorted(set(missing))))
    if diagnostics["invalid_fields"]:
        gaps.append("invalid counter(s): " + ", ".join(diagnostics["invalid_fields"]))

    warnings = []
    if absent_detail:
        warnings.append("detail channel(s) not reported for every round: "
                        + ", ".join(sorted(set(absent_detail))) + " (kept null, not zero)")
    if other_rounds:
        warnings.append(f"{other_rounds} round(s) of other requests in the same conversation were excluded")
    if unattributed_rounds:
        warnings.append(f"{unattributed_rounds} usage round(s) carried no conversation request id and "
                        "were excluded")
    if diagnostics["duplicate_identical_rounds"]:
        warnings.append(f"{diagnostics['duplicate_identical_rounds']} identical duplicate round(s) "
                        "were counted once")
    alignment = _alignment(usable, api_snapshot)
    if alignment is not None and not alignment["aligned"]:
        warnings.append("the desktop request snapshot does not equal the last attributed round")
    if sum(1 for item in usable if item["total_identity_ok"] is False):
        warnings.append("some rounds do not satisfy totalTokens = input + output")

    timestamps = [item["timestamp_ms"] for item in usable if item["timestamp_ms"] is not None]
    source = native_request_id_source if native_request_id_source in ("conversationRequestId", "id") else None
    result.update(
        available=True,
        native_request_id=_identifier(native_request_id),
        attributed_by=attribution,
        identity={"native_request_id": _identifier(native_request_id), "source": source,
                  "client_request_id_is_canonical": False},
        rounds=len(usable),
        tokens=tokens,
        per_round=[{key: value for key, value in item.items() if key != "usable"} for item in usable],
        model_ids=sorted({item["model_id"] for item in usable if item["model_id"]}),
        window={"first_ms": min(timestamps) if timestamps else None,
                "last_ms": max(timestamps) if timestamps else None},
        diagnostics={**diagnostics, "excluded_rounds": other_rounds + unattributed_rounds,
                     "other_request_ids": other_ids, "cache_miss_derived": cache_miss_derived},
        snapshot_alignment=alignment,
        completeness={
            "status": "partial" if gaps else "all_recorded_rounds",
            "only_persisted_calls": True,
            "gaps": gaps,
            "warnings": warnings,
            "does_not_include": list(_DOES_NOT_INCLUDE),
        },
    )
    return result


def ledger_for_task(task, config_dir=None, api_snapshot=None, limits=None):
    """The transcript ledger for a visible desktop task, or ``None`` if not applicable."""
    if not isinstance(task, dict) or task.get("backend") != BACKEND_DESKTOP:
        return None
    native = task.get("native") if isinstance(task.get("native"), dict) else {}
    conversation_id = native.get("conversation_id") or task.get("session_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    if isinstance(native.get("native_request_identity_conflict"), dict):
        # Two canonical ids were reported for one dispatch; counting either would
        # be a guess, and a single-request fallback could count another request.
        return unavailable_ledger(
            "Two different canonical request ids were observed for this dispatch; per-round usage is not "
            "attributed. Nothing was resent and no round was counted.",
            conversation_id=conversation_id)
    if api_snapshot is None:
        usage = task.get("usage") if isinstance(task.get("usage"), dict) else {}
        api_snapshot = usage.get("native") if isinstance(usage.get("native"), dict) else None
    return transcript_usage(conversation_id, native_request_id=native.get("native_request_id"),
                            config_dir=config_dir, api_snapshot=api_snapshot, limits=limits,
                            native_request_id_source=native.get("native_request_id_source"))

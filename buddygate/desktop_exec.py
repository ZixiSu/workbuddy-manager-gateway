"""Execute one task as a visible desktop conversation, observed by request ID.

Rules enforced here:

* identifiers are persisted before any side effect, so a restart can never
  repeat a dispatch;
* the prompt is sent at most once per attempt; an unknown outcome becomes
  ``uncertain`` and is never retried automatically;
* permission requests are only surfaced to the manager. Nothing is approved
  automatically, and an unrecognised request stays unresolved;
* a terminal request state alone does not prove that child agents or background
  tasks stopped. ``stop_confirmed`` is only set when the desktop reports both
  activity flags as false; active or unknown flags keep the task uncertain;
* the canonical request id is persisted as soon as it is observed: the desktop
  drops ``clientRequestId`` across an application restart, so a dispatch that was
  never recorded is treated as unidentifiable instead of being matched by time;
* usage is recorded with its source and gaps, never merged into one number.
"""
from __future__ import annotations

import re
import time
from typing import Any

from .desktop import DesktopClient, DesktopConnection, DesktopError, DesktopUnavailable, uuid7
from .meter import codex_delta, codex_snapshot, native_usage, usage_record
from .redact import sanitize
from .store import BACKEND_DESKTOP, Store
from .usage_ledger import transcript_usage, unavailable_ledger

_TERMINAL = {"completed", "failed", "error", "cancelled", "canceled", "aborted"}
_FAILED = {"failed", "error"}
_CANCELLED = {"cancelled", "canceled", "aborted"}
_DECIDABLE = {"allow_once", "reject_once"}
_ACTIVITY_FIELDS = ("hasActiveChildAgents", "hasRunningBackgroundTasks")
_RESULT_TEXT_LIMIT = 20000
_CANONICAL_ID = re.compile(r"^[0-9a-z_-]{8,64}$")


def canonical_id(value):
    """A bounded native request id, or ``None``.

    The identifier the gateway sends as ``clientRequestId`` is its own UUID and
    is deliberately not treated as the canonical id.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _CANONICAL_ID.match(text) else None


def observed_identity(observed: dict | None) -> dict | None:
    """The canonical request id one status observation reported, with its evidence."""
    if not isinstance(observed, dict) or observed.get("requestSeen") is not True:
        return None
    canonical = canonical_id(observed.get("nativeRequestId"))
    if canonical is None:
        return None
    source = observed.get("nativeRequestIdSource")
    matched_by = observed.get("matchBy")
    return {
        "native_request_id": canonical,
        "source": source if source in ("conversationRequestId", "id") else None,
        "matched_by": matched_by if matched_by in ("client_request_id", "native_request_id") else None,
    }


def activity_status(observed: dict | None) -> dict:
    """Tri-state view of the desktop's own background-activity flags.

    ``clear`` requires both flags to be literally ``false``. A missing flag is
    ``unknown`` and is never read as "nothing is running".
    """
    value = observed if isinstance(observed, dict) else {}
    flags = {name: value[name] if isinstance(value.get(name), bool) else None for name in _ACTIVITY_FIELDS}
    active = [name for name, flag in flags.items() if flag is True]
    unknown = [name for name, flag in flags.items() if flag is None]
    state = "active" if active else ("unknown" if unknown else "clear")
    return {**flags, "state": state, "active": active, "unknown": unknown}


def dispatch_title(task: dict) -> str:
    """The visible chat title; also the first line of the prompt."""
    first = (task.get("objective") or "").strip().splitlines()[0] if (task.get("objective") or "").strip() else "派工"
    first = " ".join(first.split())[:48]
    return f"【派工】{task['id'][-8:]} {first}"


def prompt_for(task: dict) -> str:
    constraints = "\n".join("- " + item for item in task["constraints"]) or "- 只做目标中写明的范围。"
    return (
        f"{dispatch_title(task)}\n\n"
        f"任务：{task['id']}，第 {task['attempt']} 轮。管理者（另一个 Agent）只派工和验收，不代替你执行；"
        "它会在你结束后独立检查实际产物。\n"
        f"工作目录：{task['workspace']}\n"
        f"目标：\n{task['objective']}\n"
        f"持续约束：\n{constraints}\n"
        f"本轮返工要求：\n{task.get('feedback') or '（首轮，无）'}\n\n"
        "执行要求：\n"
        "1. 用你自己的工具实际完成任务；不要把其他 Agent 的工作算作你的成果。\n"
        "2. 先检查目标里的错误前提、逻辑跳跃和缺失输入，发现就直接指出。\n"
        "3. 保持独立判断，不迎合派工措辞；数据、人物和结论尽量核实来源。\n"
        "4. 发现错误直接说明依据和风险；主动提醒遗漏的变量、成本和认知偏差。\n"
        "5. 不删除用户文件，不泄露隐私，不打印凭据；目标未明确授权时，不发布、不群发、不付费、不改权限。\n"
        "6. 不修改网关代码、网关状态、账户设置或无关文件；工作区说明是附加约束，不是扩大授权。\n"
        "7. 报告实际文件路径、做过的检查、失败项和遗留限制；计划或已受理的请求不等于完成。\n"
        "8. 不要自称已验收，验收由管理者独立完成。\n"
    )


def _pending_body(value) -> dict | None:
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, dict)), None)
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("pending"), dict):
        return value["pending"]
    return value


def _pending_id(body: dict) -> str | None:
    for key in ("id", "pendingId", "requestId"):
        candidate = body.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _pending_options(body: dict) -> tuple[list[dict], dict]:
    """Split display options from native response payloads kept in memory only."""
    raw = None
    for key in ("options", "actions", "choices"):
        if isinstance(body.get(key), list):
            raw = body[key]
            break
    options, responses = [], {}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        option_id = item.get("optionId") or item.get("id")
        kind = item.get("kind") or item.get("type")
        if not isinstance(option_id, str) or not option_id or not isinstance(kind, str) or not kind:
            continue
        options.append({
            "optionId": option_id[:200],
            "kind": kind[:40],
            "name": str(item.get("name") or item.get("label") or "")[:200],
        })
        if isinstance(item.get("response"), (dict, str)):
            responses[option_id[:200]] = item["response"]
    return options[:12], responses


def _response_for(option: dict, responses: dict) -> tuple[Any, bool]:
    """The native decision payload; only a payload the desktop supplied is reused."""
    if option["optionId"] in responses:
        return responses[option["optionId"]], True
    return {"optionId": option["optionId"]}, False


def execute_desktop(store: Store, task: dict, client: DesktopClient | None = None,
                    connection: DesktopConnection | None = None, poll_interval: float = 2.0,
                    cancel_grace: float = 45.0, max_polls: int | None = None,
                    settle_grace: float = 30.0, config_dir: str | None = None,
                    ledger: bool = True) -> dict:
    task_id, attempt = task["id"], task["attempt"]
    deadline = time.monotonic() + task["timeout_seconds"]
    native = dict(task.get("native") or {})
    # Read the original state before anything is written into ``native``.
    previous_conversation = native.get("conversation_id") or task.get("session_id")
    reused_conversation = bool(previous_conversation)
    conversation_id = previous_conversation or str(uuid7())
    owns_client = client is None
    if client is None:
        client = DesktopClient(connection or DesktopConnection.load())

    def update(**changes):
        return store.update_task(task_id, expected_attempt=attempt, **changes)

    def event(kind, data):
        return store.add_event(task_id, kind, sanitize(data))

    def record_identity(value):
        """Persist the canonical request id the first time an observation reports it.

        Later evidence must never replace it: if a different canonical id appears
        for this dispatch, usage attribution stops instead of choosing one.
        """
        identity = observed_identity(value)
        if identity is None:
            return
        if value.get("matchBy") == "native_request_id" and not native.get("client_request_id_missing"):
            native["client_request_id_missing"] = True
            event("client_request_id_missing", {
                "matched_by": "native_request_id",
                "note": "The desktop no longer reports clientRequestId for this request; the saved "
                        "canonical request id was used to keep observing. Nothing was resent.",
            })
        stored = canonical_id(native.get("native_request_id"))
        if stored is None:
            if native.get("native_request_identity_conflict"):
                return
            native.update(native_request_id=identity["native_request_id"],
                          native_request_id_source=identity["source"],
                          native_request_id_matched_by=identity["matched_by"],
                          native_request_id_saved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            update(native=native)
            event("native_request_identity_saved", {
                "native_request_id": identity["native_request_id"], "source": identity["source"],
                "matched_by": identity["matched_by"],
                "client_request_id_is_canonical": False,
                "note": "This is the server-side request identity, not the client request UUID; it is "
                        "kept for usage attribution and is never used to dispatch.",
            })
            return
        if stored == identity["native_request_id"] or native.get("native_request_identity_conflict"):
            return
        native["native_request_identity_conflict"] = {
            "stored": stored, "observed": identity["native_request_id"],
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "matched_by": identity["matched_by"],
            "note": "Two different canonical request ids were reported for this dispatch. The stored id is "
                    "kept, per-round usage is not attributed, and nothing was resent.",
        }
        update(native=native)
        event("native_request_identity_conflict", native["native_request_identity_conflict"])

    if native.get("dispatch") in ("reserved", "sending", "sent", "unknown"):
        # Persisted intent without a reconciled outcome: never dispatch again.
        update(status="uncertain", stop_confirmed=False,
               error="This attempt already reserved or sent a desktop request; it was not resent. "
                     "Inspect the visible conversation and use rework for a new attempt.")
        event("dispatch_refused", {"dispatch": native["dispatch"], "conversation_id": conversation_id, "replayed": False})
        if owns_client:
            client.close()
        return store.get_task(task_id)

    try:
        metadata = client.open()
    except DesktopUnavailable as error:
        update(status="blocked", error=str(error)[:4000], stop_confirmed=True, permission=None,
               result={"text": "", "backend": BACKEND_DESKTOP, "attempt": attempt, "dispatched": False})
        event("execution_blocked", {"reason": str(error)[:1000], "dispatched": False})
        if owns_client:
            client.close()
        return store.get_task(task_id)
    except DesktopError as error:
        update(status="blocked", error=str(error)[:4000], stop_confirmed=True, permission=None,
               result={"text": "", "backend": BACKEND_DESKTOP, "attempt": attempt, "dispatched": False})
        event("execution_blocked", {"reason": str(error)[:1000], "dispatched": False})
        if owns_client:
            client.close()
        return store.get_task(task_id)

    native.update({
        "backend": BACKEND_DESKTOP,
        "conversation_id": conversation_id,
        "request_id": uuid7(),
        "dispatch": "reserved",
        "attempt": attempt,
        "renderer": {"app_version": metadata.get("app_version"), "platform": metadata.get("platform"),
                     "port": metadata.get("port"), "peer_verified": metadata.get("peer_verified")},
        "reserved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    # A new request id was just reserved, so the previous attempt's identity must
    # not survive into this attempt's observation or usage attribution.
    for stale in ("native_request_id", "native_request_id_source", "native_request_id_matched_by",
                  "native_request_id_saved_at", "native_request_identity_conflict",
                  "client_request_id_missing", "activity"):
        native.pop(stale, None)
    reused = reused_conversation
    # Persist the identifiers and the reserved dispatch before any side effect.
    update(session_id=conversation_id, native=native)
    event("dispatch_reserved", {"conversation_id": conversation_id, "request_id": native["request_id"],
                                "attempt": attempt, "reused_conversation": reused})

    created = None
    if not reused:
        try:
            created = client.create_conversation(conversation_id, dispatch_title(task), task["workspace"], task.get("model"))
        except (DesktopError, DesktopUnavailable) as error:
            update(status="blocked", stop_confirmed=True, permission=None,
                   error="No prompt was dispatched; the visible conversation could not be confirmed: " + str(error)[:2000],
                   result={"text": "", "backend": BACKEND_DESKTOP, "attempt": attempt, "dispatched": False,
                           "conversation_id": conversation_id})
            event("conversation_create_failed", {"reason": str(error)[:1000], "dispatched": False,
                                                 "conversation_id": conversation_id, "created_again": False})
            if owns_client:
                client.close()
            return store.get_task(task_id)
        event("conversation_created", {"conversation_id": conversation_id, "state": (created or {}).get("state"),
                                       "model_reported": (created or {}).get("model"),
                                       "confirmed_by": (created or {}).get("confirmed_by") or "create"})

    codex_before = codex_snapshot(task["codex_thread"]) if task.get("codex_thread") else None
    if codex_before is not None:
        event("codex_snapshot", {"phase": "before", "events": codex_before.get("events"),
                                 "window": codex_before.get("window"), "error": codex_before.get("error")})

    try:
        sent = client.send_prompt(conversation_id, prompt_for(task), native["request_id"])
    except (DesktopError, DesktopUnavailable) as error:
        native["dispatch"] = "unknown"
        update(status="uncertain", stop_confirmed=False, native=native, permission=None,
               error="The prompt may or may not have reached the desktop. It was not resent: " + str(error)[:2000],
               result={"text": "", "backend": BACKEND_DESKTOP, "attempt": attempt, "dispatched": "unknown"})
        event("dispatch_unknown", {"reason": str(error)[:1000], "replayed": False})
        if owns_client:
            client.close()
        return store.get_task(task_id)

    if not isinstance(sent, dict) or sent.get("dispatched") is not True:
        native["dispatch"] = "unknown"
        update(status="uncertain", stop_confirmed=False, native=native, permission=None,
               error="The desktop did not confirm the dispatch. It was not resent.",
               result={"text": "", "backend": BACKEND_DESKTOP, "attempt": attempt, "dispatched": "unknown"})
        event("dispatch_unknown", {"reason": "No dispatch confirmation", "replayed": False})
        if owns_client:
            client.close()
        return store.get_task(task_id)

    native.update(dispatch="sent", sent_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    current = store.get_task(task_id)
    if current["status"] == "cancel_requested":
        # A cancellation that arrived during the send is not overwritten.
        update(native=native)
    else:
        update(status="running", native=native)
    event("prompt_dispatched", {"conversation_id": conversation_id, "request_id": native["request_id"],
                                "attempt": attempt, "sent_once": True})

    request_seen = False
    observed = None
    cancel_sent = False
    cancel_time = None
    permission: dict | None = None
    responses: dict = {}
    told_unresolvable = False
    told_unseen = False
    told_identity_lost = False
    settle_deadline = None
    polls = 0

    try:
        while True:
            polls += 1
            if max_polls is not None and polls > max_polls:
                raise TimeoutError("Simulation poll bound reached")
            current = store.get_task(task_id)
            if current["status"] == "cancel_requested" and not cancel_sent:
                try:
                    client.cancel_conversation(conversation_id)
                    cancel_sent, cancel_time = True, time.monotonic()
                    event("cancel_sent", {"conversation_id": conversation_id, "remote_stopped": False})
                except (DesktopError, DesktopUnavailable) as error:
                    event("cancel_failed", {"reason": str(error)[:500]})
            if cancel_sent and cancel_time is not None and time.monotonic() - cancel_time > cancel_grace:
                native["dispatch"] = "unknown"
                update(status="uncertain", stop_confirmed=False, native=native, permission=None,
                       error="Cancellation was requested but the desktop did not confirm a terminal state; "
                             "the run may still be active. Inspect the visible conversation.")
                event("cancel_unconfirmed", {"grace_seconds": cancel_grace, "remote_stopped": False})
                return store.get_task(task_id)

            if permission is not None:
                decision = ((store.get_task(task_id).get("permission") or {}).get("decision"))
                if decision:
                    option = next((item for item in permission["options"]
                                   if item["optionId"] == decision.get("option_id")), None)
                    if option is not None:
                        payload, supplied = _response_for(option, responses)
                        try:
                            client.resolve_pending(conversation_id, permission["id"], payload)
                            event("permission_resolved", {"permission_id": permission["id"],
                                                          "option_id": option["optionId"],
                                                          "kind": option.get("kind"),
                                                          "explicit_response_payload": supplied})
                        except (DesktopError, DesktopUnavailable) as error:
                            event("permission_resolve_failed", {"permission_id": permission["id"],
                                                                "reason": str(error)[:500]})
                    permission = None
                    if store.get_task(task_id)["status"] == "waiting_permission":
                        update(status="running", permission=None)

            try:
                pending_value = client.pending(conversation_id)
            except (DesktopError, DesktopUnavailable):
                pending_value = None
            body = _pending_body(pending_value)
            if body is not None:
                pending_id = _pending_id(body)
                options, responses = _pending_options(body)
                decidable = [item for item in options if item["kind"] in _DECIDABLE]
                if pending_id and decidable and permission is None:
                    permission = {"id": pending_id, "options": options, "decision": None,
                                  "source": "workbuddy_desktop_pending",
                                  "tool_call": sanitize({key: body[key] for key in ("title", "toolName", "tool", "description")
                                                         if key in body}),
                                  "note": "Native desktop permission request. Only a one-time option may be selected; "
                                          "nothing is approved automatically."}
                    update(status="waiting_permission", permission=permission)
                    event("permission_requested", permission)
                elif pending_id and not decidable and not told_unresolvable:
                    told_unresolvable = True
                    event("permission_requires_desktop_decision", {
                        "pending_id": pending_id,
                        "option_kinds": [item["kind"] for item in options][:12],
                        "reason": "This request shape has no one-time option the gateway can select; "
                                  "decide it in the WorkBuddy desktop window. Nothing was approved.",
                    })
            elif permission is not None:
                permission = None
                if store.get_task(task_id)["status"] == "waiting_permission":
                    update(status="running", permission=None)
                event("permission_cleared", {"reason": "No pending request reported by the desktop"})

            try:
                observed = client.observe(conversation_id, native["request_id"],
                                          native.get("native_request_id"))
            except (DesktopError, DesktopUnavailable) as error:
                observed = {"requestSeen": False, "state": None, "observe_error": str(error)[:500]}
            record_identity(observed)
            if not observed.get("requestSeen") and not told_identity_lost:
                listed = observed.get("requestCount")
                without = observed.get("requestsWithoutClientRequestId")
                if isinstance(listed, int) and listed > 0 and without == listed:
                    told_identity_lost = True
                    event("request_identity_lost", {
                        "request_count": listed, "client_request_id_absent": True,
                        "native_request_id_saved": canonical_id(native.get("native_request_id")) is not None,
                        "note": "No listed request of this conversation carries clientRequestId and this "
                                "dispatch has no saved canonical id. The run is not matched, nothing was "
                                "resent, and usage stays unavailable rather than being guessed.",
                    })
            if not observed.get("requestSeen") and not told_unseen and time.monotonic() > deadline - task["timeout_seconds"] + 60:
                told_unseen = True
                event("request_not_yet_visible", {"conversation_state": observed.get("conversationState"),
                                                  "request_count": observed.get("requestCount")})
            state = str(observed.get("state") or "").lower()
            if observed.get("requestSeen") and state in _TERMINAL:
                activity = activity_status(observed)
                if activity["state"] == "clear":
                    break
                # A terminal request is not proof that side effects stopped.
                if settle_deadline is None:
                    settle_deadline = time.monotonic() + max(0.0, settle_grace)
                    event("terminal_activity_unconfirmed", {
                        "state": state, **activity, "grace_seconds": settle_grace,
                        "note": "The request reached a terminal state, but the desktop does not report every "
                                "child agent and background task as stopped. Continuing to observe.",
                    })
                if time.monotonic() > settle_deadline:
                    native["activity"] = activity
                    text = str(observed.get("assistant") or "")[:_RESULT_TEXT_LIMIT]
                    update(status="uncertain", stop_confirmed=False, native=native, permission=None,
                           error=f"WorkBuddy reported request state '{state}', but child-agent/background-task "
                                 f"activity was not confirmed stopped ({activity['state']}). The gateway did not "
                                 "claim a stop; inspect the visible conversation before rework or acceptance.",
                           result={"text": text, "backend": BACKEND_DESKTOP, "attempt": attempt,
                                   "conversation_id": conversation_id, "request_id": native["request_id"],
                                   "state": state, "activity": activity})
                    event("stop_unconfirmed", {"state": state, **activity, "stop_confirmed": False})
                    return store.get_task(task_id)
            if time.monotonic() > deadline:
                raise TimeoutError("The desktop request did not reach a terminal state before the deadline")
            time.sleep(poll_interval)
    except TimeoutError as error:
        native["dispatch"] = "unknown"
        update(status="uncertain", stop_confirmed=False, native=native, permission=None,
               error=f"Observation ended without a terminal state: {error}. The run may still be active in the desktop.",
               result={"text": "", "backend": BACKEND_DESKTOP, "attempt": attempt,
                       "conversation_id": conversation_id, "request_id": native["request_id"],
                       "state": (observed or {}).get("state")})
        event("observation_timeout", {"state": (observed or {}).get("state"),
                                      "request_seen": bool((observed or {}).get("requestSeen")),
                                      "may_still_be_running": True})
        return store.get_task(task_id)
    except Exception as error:  # noqa: BLE001 - any observation failure is an unknown outcome
        native["dispatch"] = "unknown"
        update(status="uncertain", stop_confirmed=False, native=native, permission=None,
               error="Observation failed; the run may still be active: " + str(error)[:2000],
               result={"text": "", "backend": BACKEND_DESKTOP, "attempt": attempt,
                       "conversation_id": conversation_id, "request_id": native["request_id"]})
        event("observation_error", {"reason": str(error)[:1000], "may_still_be_running": True})
        return store.get_task(task_id)
    finally:
        if owns_client:
            client.close()

    state = str(observed.get("state") or "").lower()
    activity = activity_status(observed)
    native["activity"] = activity
    # The server-side request identity is what the transcript also records; it is
    # saved once and never overwritten by a later observation.
    record_identity(observed)
    text = str(observed.get("assistant") or "")[:_RESULT_TEXT_LIMIT]
    result = {
        "text": text,
        "backend": BACKEND_DESKTOP,
        "conversation_id": conversation_id,
        "request_id": native["request_id"],
        "state": state,
        "attempt": attempt,
        "conversation_state": observed.get("conversationState"),
        "activity": activity,
        "stop_confirmed_basis": "terminal request plus both desktop activity flags reported false",
        "model": {"requested": sanitize(observed.get("requestedModel")),
                  "response": sanitize(observed.get("responseModel")),
                  "gateway_requested": task.get("model")},
    }
    codex_after = codex_snapshot(task["codex_thread"]) if task.get("codex_thread") else None
    usage = usage_record(native_usage(observed) if observed.get("requestSeen") else None,
                         codex_delta(codex_before, codex_after) if task.get("codex_thread") else None)
    if not task.get("codex_thread"):
        usage["codex"] = None
        usage["notes"].append("No Codex thread file was supplied, so manager-side usage is not reported.")
    if ledger:
        # Read-only sum of the rounds this request persisted. Kept separate from
        # the request's own snapshot, which only reports its last round.
        conflict = native.get("native_request_identity_conflict")
        if conflict:
            usage["transcript"] = unavailable_ledger(
                "Two different canonical request ids were reported for this dispatch, so per-round usage "
                "cannot be attributed; no round was counted and nothing was resent.",
                conversation_id=conversation_id)
        else:
            usage["transcript"] = transcript_usage(
                conversation_id, native_request_id=native.get("native_request_id"),
                config_dir=config_dir, api_snapshot=usage.get("native"),
                native_request_id_source=native.get("native_request_id_source"))
        usage["notes"].append(
            "The transcript ledger counts per-round usage records of this request from the local "
            "transcript; it is not merged with the request snapshot or the Codex thread meter, and a "
            "re-read with 'buddygate usage <task> --refresh' may include rounds written later.")

    if _CANCELLED & {state}:
        update(status="cancelled", result=result, stop_confirmed=True, permission=None, usage=usage, native=native)
    elif state in _FAILED:
        update(status="blocked", result=result, stop_confirmed=True, permission=None, usage=usage, native=native,
               error=f"WorkBuddy reported request state '{state}'; inspect the visible conversation and its events.")
    elif store.get_task(task_id)["status"] == "cancel_requested":
        update(status="cancelled", result=result, stop_confirmed=True, permission=None, usage=usage, native=native,
               error="WorkBuddy reported a terminal response after cancellation; inspect the result for effects "
                     "that already happened.")
    elif not text.strip():
        update(status="blocked", result=result, stop_confirmed=True, permission=None, usage=usage, native=native,
               error="The request completed without visible assistant text; inspect the visible conversation.")
    else:
        update(status="needs_review", result=result, stop_confirmed=True, permission=None, usage=usage, native=native)
    event("execution_finished", {"state": state, "review": "pending", "activity": activity,
                                 "stop_confirmed": True,
                                 "usage_sources": [item for item in ("native", "codex", "transcript")
                                                   if usage.get(item) is not None],
                                 "transcript_rounds": (usage.get("transcript") or {}).get("rounds"),
                                 "text_length": len(text)})
    return store.get_task(task_id)

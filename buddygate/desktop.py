"""Visible desktop conversations over an OS-verified local debug port.

The gateway only talks to a port that the operating system reports as a
listener owned by the configured WorkBuddy executable, bound exclusively to
``127.0.0.1``, and to exactly one renderer that identifies itself as the native
WorkBuddy desktop. No browser, no global keyboard or mouse, no other renderer,
no account ticket.

Ownership verification is implemented for Windows only. On other platforms the
backend reports that it cannot verify ownership instead of pretending to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

from .redact import safe_text, sanitize
from .runtime import state_directory

DESKTOP_BACKEND = "desktop_visible"
CONFIG_NAME = "desktop-connection.json"
HELPER_NAME = "desktop_console.mjs"
_WS_PAGE = re.compile(r"^/devtools/page/[A-Za-z0-9-]+$")
_MAX_TARGETS = 2_000_000
_MAX_ERROR = 2_000


class DesktopUnavailable(RuntimeError):
    """A precondition is missing; no prompt was dispatched."""


class DesktopError(RuntimeError):
    """A desktop operation failed; its side effect may be unknown."""


def uuid7() -> str:
    """A time-ordered UUIDv7 for native client request identifiers."""
    milliseconds = time.time_ns() // 1_000_000
    raw = bytearray(secrets.token_bytes(16))
    raw[0:6] = milliseconds.to_bytes(6, "big")
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


@dataclass
class DesktopConnection:
    """Private connection facts for one installed WorkBuddy desktop."""

    executable: str
    port: int
    host: str = "127.0.0.1"
    arguments: list[str] = field(default_factory=lambda: ["--remote-debugging-address=127.0.0.1"])

    def __post_init__(self):
        if self.host != "127.0.0.1":
            raise DesktopUnavailable("The desktop backend only accepts host 127.0.0.1")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1024 <= self.port <= 65535:
            raise DesktopUnavailable("The desktop debug port must be an integer between 1024 and 65535")
        candidate = Path(self.executable).expanduser()
        if not candidate.is_absolute() or not candidate.is_file():
            raise DesktopUnavailable("The configured WorkBuddy executable is missing")
        self.executable = str(candidate.resolve())
        if not isinstance(self.arguments, list) or any(not isinstance(item, str) for item in self.arguments):
            raise DesktopUnavailable("Launch arguments must be a list of strings")
        for item in self.arguments:
            if item != "--remote-debugging-address=127.0.0.1":
                raise DesktopUnavailable("Only the loopback remote-debugging argument is supported")

    def to_dict(self) -> dict:
        return {"executable": self.executable, "port": self.port, "host": self.host,
                "arguments": list(self.arguments), "persistent_system_changes": False,
                "saved_at": datetime.now().astimezone().isoformat(timespec="seconds")}

    @classmethod
    def from_dict(cls, data: dict) -> "DesktopConnection":
        if not isinstance(data, dict):
            raise DesktopUnavailable("Desktop connection configuration is not an object")
        return cls(executable=str(data.get("executable") or ""), port=data.get("port"),
                   host=data.get("host", "127.0.0.1"), arguments=list(data.get("arguments") or []))

    @classmethod
    def load(cls, path: Path | None = None) -> "DesktopConnection":
        target = Path(path) if path is not None else state_directory() / CONFIG_NAME
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise DesktopUnavailable(
                "No desktop connection is configured. Run 'buddygate desktop-connect --executable <WorkBuddy.exe> "
                "--port <port> --save' once; the gateway never guesses a port or uses another program's listener."
            ) from None
        except (OSError, ValueError):
            raise DesktopUnavailable("Desktop connection configuration is unreadable; run desktop-connect again") from None
        return cls.from_dict(data)

    def save(self, path: Path | None = None) -> Path:
        target = Path(path) if path is not None else state_directory() / CONFIG_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return target


# --- operating-system ownership ---------------------------------------------


def _windows_listener_rows(port: int) -> list[dict]:
    import ctypes
    from ctypes import wintypes

    library = ctypes.WinDLL("iphlpapi", use_last_error=True)
    library.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                           wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    library.GetExtendedTcpTable.restype = wintypes.DWORD
    rows: list[dict] = []
    for family, row_size in ((2, 24), (23, 56)):  # AF_INET, AF_INET6
        size = wintypes.DWORD(0)
        error = library.GetExtendedTcpTable(None, ctypes.byref(size), False, family, 3, 0)
        if error not in (0, 122):
            raise DesktopUnavailable("The operating system refused a TCP listener table query")
        if not size.value:
            continue
        buffer = ctypes.create_string_buffer(size.value)
        error = library.GetExtendedTcpTable(buffer, ctypes.byref(size), False, family, 3, 0)
        if error != 0:
            raise DesktopUnavailable("The operating system refused a TCP listener table query")
        raw = buffer.raw
        count = struct.unpack_from("<I", raw, 0)[0]
        offset = 4
        for _ in range(max(0, count)):
            if offset + row_size > len(raw):
                break
            if family == 2:
                _state, address, local_port, _ra, _rp, pid = struct.unpack_from("<6I", raw, offset)
                text = socket.inet_ntoa(struct.pack("<I", address))
            else:
                address = raw[offset:offset + 16]
                _scope, local_port = struct.unpack_from("<II", raw, offset + 16)
                pid = struct.unpack_from("<I", raw, offset + 52)[0]
                text = socket.inet_ntop(socket.AF_INET6, address)
            offset += row_size
            if socket.ntohs(local_port & 0xFFFF) == port:
                rows.append({"address": text, "pid": pid})
    return rows


def _windows_process_image(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        image = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
            return None
        return image.value
    finally:
        kernel.CloseHandle(handle)


def listener_rows(port: int) -> list[dict]:
    """Listeners on ``port`` with their owning executable, from the OS only."""
    if os.name != "nt":
        raise DesktopUnavailable(
            "Listener ownership verification requires Windows process metadata; "
            "other platforms can run the simulation tests but are not claimed as supported."
        )
    rows = []
    for row in _windows_listener_rows(port):
        rows.append({"address": row["address"], "pid": row["pid"],
                     "path": _windows_process_image(row["pid"])})
    return rows


def check_ownership(rows: list[dict], executable: str | Path) -> dict:
    """Refuse anything that is not exclusively loopback and WorkBuddy-owned."""
    expected = os.path.normcase(os.path.realpath(str(executable)))
    if not rows:
        raise DesktopUnavailable(
            "Nothing is listening on the configured local port. The WorkBuddy debug entry may be disabled; "
            "restart WorkBuddy normally instead of trying another program's port."
        )
    addresses = {str(row.get("address")) for row in rows}
    if addresses != {"127.0.0.1"}:
        raise DesktopUnavailable(
            "The configured port is not bound exclusively to 127.0.0.1; refusing to connect: "
            + ", ".join(sorted(addresses))
        )
    for row in rows:
        path = row.get("path")
        if not isinstance(path, str) or not path:
            raise DesktopUnavailable("The owning process of the local port could not be identified")
        if os.path.normcase(os.path.realpath(path)) != expected:
            raise DesktopUnavailable(
                "The local port is owned by a different program than the configured WorkBuddy executable"
            )
    return {"peer_verified": True, "addresses": sorted(addresses),
            "pids": sorted({int(row["pid"]) for row in rows if isinstance(row.get("pid"), int)})}


# --- local renderer discovery ------------------------------------------------


def http_json(port: int, path: str = "/json/list") -> Any:
    if path not in ("/json/list", "/json/version"):
        raise DesktopUnavailable("Only the local renderer listings are read")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            if response.status != 200:
                raise DesktopUnavailable("The local debug endpoint returned an unexpected status")
            raw = response.read(_MAX_TARGETS + 1)
    except (urllib.error.URLError, OSError, ValueError):
        raise DesktopUnavailable("The local debug endpoint is unreachable") from None
    if len(raw) > _MAX_TARGETS:
        raise DesktopUnavailable("The local renderer listing is larger than the accepted bound")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise DesktopUnavailable("The local renderer listing is not valid JSON") from None


def valid_page(url: str, port: int) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "ws" and parsed.hostname == "127.0.0.1" and parsed.port == port
                and bool(_WS_PAGE.match(parsed.path))
                and not (parsed.username or parsed.password or parsed.query or parsed.fragment))
    except ValueError:
        return False


def renderer_candidates(targets: Any, port: int) -> list[str]:
    if not isinstance(targets, list):
        raise DesktopUnavailable("The local renderer listing has an unexpected shape")
    urls = []
    for item in targets:
        if not isinstance(item, dict) or item.get("type") != "page":
            continue
        url = item.get("webSocketDebuggerUrl")
        if isinstance(url, str) and valid_page(url, port):
            urls.append(url)
    return urls


# --- Node helper transport ---------------------------------------------------


def node_executable() -> str:
    found = shutil.which("node")
    if not found:
        raise DesktopUnavailable("Node.js was not found on PATH; the local renderer helper cannot start")
    return found


class Bridge:
    """One JSONL conversation with the local renderer helper process."""

    def __init__(self, node: str, helper: Path, timeout: float = 25.0):
        self.timeout = timeout
        self.serial = 0
        self.messages: queue.Queue = queue.Queue()
        self.process = subprocess.Popen(
            [node, str(helper)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        assert self.process.stdout
        try:
            for line in self.process.stdout:
                if len(line) > _MAX_TARGETS:
                    self.messages.put({"error": "Renderer response exceeded the accepted bound"})
                    continue
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    self.messages.put({"error": "Renderer helper returned invalid JSON"})
        except (OSError, ValueError):
            pass
        finally:
            self.messages.put({"error": "Renderer helper exited"})

    def call(self, op: str, ws: str, **arguments) -> dict:
        if self.process.poll() is not None:
            raise DesktopError("The renderer helper is not running")
        assert self.process.stdin
        self.serial += 1
        packet = {"id": self.serial, "op": op, "ws": ws, "args": sanitize(arguments)}
        try:
            self.process.stdin.write(json.dumps(packet, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise DesktopError("The renderer helper refused the request") from None
        try:
            message = self.messages.get(timeout=self.timeout)
        except queue.Empty:
            raise DesktopError(
                "The renderer operation timed out; the outcome is unknown and must not be replayed"
            ) from None
        if not isinstance(message, dict):
            raise DesktopError("The renderer helper returned an unexpected message")
        if message.get("error"):
            raise DesktopError(safe_text(str(message["error"]))[:_MAX_ERROR])
        value = message.get("value")
        return value if isinstance(value, dict) else {"value": value}

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass


# --- high-level client -------------------------------------------------------


class DesktopClient:
    """Verified single-renderer operations on one desktop conversation."""

    def __init__(self, connection: DesktopConnection, node: str | None = None,
                 helper: Path | None = None, bridge_factory=None, checker=None, lister=None):
        self.connection = connection
        self.node = node
        self.helper = Path(helper) if helper is not None else Path(__file__).with_name(HELPER_NAME)
        self._bridge_factory = bridge_factory or (lambda node, helper: Bridge(node, helper))
        self._checker = checker or (lambda port: listener_rows(port))
        self._lister = lister or (lambda port: http_json(port, "/json/list"))
        self.bridge: Bridge | None = None
        self.metadata: dict = {"peer_verified": False, "app_version": None, "platform": None,
                               "renderers_checked": 0, "port": connection.port}

    def open(self) -> dict:
        if self.bridge is not None:
            return self.metadata
        rows = self._checker(self.connection.port)
        self.metadata.update(check_ownership(rows, self.connection.executable))
        targets = self._lister(self.connection.port)
        candidates = renderer_candidates(targets, self.connection.port)
        if not candidates:
            raise DesktopUnavailable("The verified port exposed no WorkBuddy renderer page")
        node = self.node or node_executable()
        bridge = self._bridge_factory(node, self.helper)
        verified = []
        try:
            for url in candidates:
                try:
                    identity = bridge.call("identity", url)
                except DesktopError:
                    continue
                if identity.get("native") is True and isinstance(identity.get("version"), str):
                    verified.append((url, identity))
            if len(verified) != 1:
                raise DesktopUnavailable(
                    "Expected exactly one verified native WorkBuddy renderer; found "
                    f"{len(verified)} of {len(candidates)} candidate page(s)"
                )
        except BaseException:
            bridge.close()
            raise
        self.bridge = bridge
        self.ws, identity = verified[0]
        self.metadata.update(app_version=identity.get("version"), platform=identity.get("platform"),
                             renderers_checked=len(candidates))
        return self.metadata

    def _call(self, op: str, **arguments) -> dict:
        if self.bridge is None:
            self.open()
        assert self.bridge is not None
        return self.bridge.call(op, self.ws, **arguments)

    def create_conversation(self, conversation_id: str, title: str, cwd: str, model: str | None) -> dict:
        """Create the visible conversation exactly once.

        A create call whose answer is unusable is **not** retried: the reserved
        ID is read back instead. Creating again could produce a second visible
        chat, and changing the ID would abandon the identifier already persisted.
        """
        value = self._call("create", conversationId=conversation_id, title=title, cwd=cwd, model=model)
        if isinstance(value, dict) and isinstance(value.get("id"), str) and value["id"]:
            return value
        try:
            observed = self._call("conversation", conversationId=conversation_id)
        except (DesktopError, DesktopUnavailable) as error:
            raise DesktopError(
                "The conversation creation outcome is unknown; it was not created again and the reserved "
                "conversation ID is kept: " + str(error)[:300]
            ) from None
        if observed.get("exists") is True:
            return {"id": observed.get("id") or conversation_id, "state": observed.get("state"),
                    "model": None, "confirmed_by": "get"}
        raise DesktopError(
            "The desktop did not confirm the new conversation and it was not created again; "
            "the reserved conversation ID is kept: " + conversation_id[:100]
        )

    def send_prompt(self, conversation_id: str, text: str, request_id: str) -> dict:
        return self._call("send", conversationId=conversation_id, text=text, requestId=request_id)

    def observe(self, conversation_id: str, request_id: str, native_request_id: str | None = None) -> dict:
        """Observe one request.

        ``native_request_id`` is only passed after it was observed earlier: the
        desktop drops ``clientRequestId`` from its request listing across an
        application restart, and the canonical id is then the only evidence that
        survives. Without it an observation stays "not seen" rather than
        matching some other request.
        """
        return self._call("status", conversationId=conversation_id, requestId=request_id,
                          nativeRequestId=native_request_id)

    def pending(self, conversation_id: str) -> dict:
        return self._call("pending", conversationId=conversation_id)

    def resolve_pending(self, conversation_id: str, pending_id: str, response: Any) -> dict:
        return self._call("resolve", conversationId=conversation_id, pendingId=pending_id, response=response)

    def cancel_conversation(self, conversation_id: str) -> dict:
        return self._call("cancel", conversationId=conversation_id)

    def close(self):
        if self.bridge is not None:
            self.bridge.close()
            self.bridge = None

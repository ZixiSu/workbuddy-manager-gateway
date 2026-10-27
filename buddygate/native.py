"""Use the installed WorkBuddy IPC client without exporting account credentials."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
from types import ModuleType

from .discovery import discover

_METHOD = "c:wb.request/http.fetch"
_IMPORT_LOCK = threading.Lock()
_LIBRARY_RELATIVE = Path("resources/plugins/workbuddy-builtin/skills/library")


class NativeError(RuntimeError):
    """A native transport failure whose message contains no account material."""


def _read_endpoint(config_dir: Path) -> tuple[str, str]:
    try:
        root = config_dir.expanduser().resolve(strict=True)
        endpoint_file = (root / "wbipc" / "endpoint.json").resolve(strict=True)
        if not root.is_dir() or not endpoint_file.is_relative_to(root):
            raise NativeError("WorkBuddy IPC discovery path escapes its configuration directory")
        if endpoint_file.stat().st_size > 65_536:
            raise NativeError("WorkBuddy IPC discovery file exceeds 64 KiB")
        with endpoint_file.open("rb") as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            raise NativeError("WorkBuddy IPC discovery file exceeds 64 KiB")
        data = json.loads(raw.decode("utf-8-sig"))
        endpoint, ticket = data.get("endpoint"), data.get("ticket")
        if not isinstance(endpoint, str) or not re.fullmatch(r"\\\\\.\\pipe\\wbipc-[a-fA-F0-9]+", endpoint):
            raise NativeError("WorkBuddy IPC discovery endpoint is not a recognized Windows named pipe")
        if not isinstance(ticket, str) or not 16 <= len(ticket) <= 4096:
            raise NativeError("WorkBuddy IPC discovery ticket metadata is invalid")
        return endpoint, ticket
    except NativeError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        raise NativeError("WorkBuddy IPC discovery is unavailable or invalid; keep WorkBuddy running and logged in") from None


def _verify_pipe_peer(endpoint: str, executable: Path) -> bool:
    """Verify a pipe peer using Windows process metadata; never inspect argv."""
    if os.name != "nt":
        raise NativeError("The native WorkBuddy backend currently verifies Windows named pipes only")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(endpoint, 0xC0000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise NativeError("WorkBuddy native IPC pipe is unavailable")
    try:
        pid = wintypes.ULONG()
        if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
            raise NativeError("Unable to verify the WorkBuddy IPC server process")
        process = kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            raise NativeError("Unable to verify the WorkBuddy IPC server executable")
        try:
            image = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(image))
            if not kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(length)):
                raise NativeError("Unable to verify the WorkBuddy IPC server executable")
            if os.path.normcase(os.path.realpath(image.value)) != os.path.normcase(str(executable.resolve())):
                raise NativeError("Native IPC server does not belong to the discovered WorkBuddy installation")
        finally:
            kernel.CloseHandle(process)
    finally:
        kernel.CloseHandle(handle)
    return True


def _load_ipc_library(library: Path) -> ModuleType:
    """Import app-owned code in a private module namespace, without copying it."""
    source = library / "workbuddy_ipc" / "__init__.py"
    if not source.is_file():
        raise NativeError("Installed WorkBuddy IPC library is missing; set BUDDYGATE_IPC_LIBRARY to its library parent")
    name = "_buddygate_installed_ipc_" + hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
    with _IMPORT_LOCK:
        existing = sys.modules.get(name)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(name, source, submodule_search_locations=[str(source.parent)])
        if spec is None or spec.loader is None:
            raise NativeError("Installed WorkBuddy IPC library cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise NativeError("Installed WorkBuddy IPC library failed to load") from None
        if getattr(module, "PROTOCOL_VERSION", None) != 1 or not callable(getattr(module, "connect", None)):
            sys.modules.pop(name, None)
            raise NativeError("Installed WorkBuddy IPC library uses an unsupported protocol")
        return module


def _safe_code(error: BaseException) -> str:
    code = getattr(error, "code", "E_NATIVE")
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else "E_NATIVE"


class NativeClient:
    """A serialized, authenticated connection to the desktop's HTTP proxy pipe."""

    def __init__(self, discovery: dict | None = None, config_dir: Path | str | None = None):
        self._discovery = discovery
        self._config_dir = Path(config_dir) if config_dir is not None else Path(os.environ.get("BUDDYGATE_WORKBUDDY_CONFIG_DIR") or Path.home() / ".workbuddy")
        self._lock = threading.RLock()
        self._client = None
        self._pipe = None
        self._closed = False
        self.metadata = {"protocol": 1, "library_available": False, "verified_backend": None, "peer_verified": False}

    def _connect(self) -> None:
        if self._closed:
            raise NativeError("WorkBuddy native IPC client is closed")
        if self._client is not None:
            return
        found = self._discovery if self._discovery is not None else discover()
        if not isinstance(found, dict) or not found.get("supported") or not found.get("cli"):
            raise NativeError("WorkBuddy installation is unavailable; inspect installation discovery first")
        try:
            cli = Path(found["cli"]).resolve(strict=True)
            if not cli.is_file() or cli.parent.name.lower() != "bin" or cli.parent.parent.name.lower() != "cli":
                raise NativeError("Native IPC requires WorkBuddy's installed cli/bin/codebuddy entry")
            executable = cli.parents[4] / "WorkBuddy.exe"
            if not executable.is_file():
                raise NativeError("The discovered WorkBuddy desktop executable is missing")
            override = os.environ.get("BUDDYGATE_IPC_LIBRARY")
            library = Path(override).expanduser().resolve(strict=True) if override else cli.parents[2] / _LIBRARY_RELATIVE
        except NativeError:
            raise
        except (OSError, ValueError, TypeError, IndexError):
            raise NativeError("WorkBuddy installation paths or IPC library override are invalid") from None
        module = _load_ipc_library(library)
        self.metadata["library_available"] = True
        endpoint, ticket = _read_endpoint(self._config_dir)
        if not _verify_pipe_peer(endpoint, executable):
            raise NativeError("WorkBuddy native IPC server identity could not be verified")
        self.metadata["peer_verified"] = True
        client = None
        try:
            # The installed client verifies the server's HMAC proof before it
            # proves ticket possession. Ticket values never enter logs or state.
            client = module.connect(client_id="buddygate", client_kind="local-agent", client_version="0.1.0", endpoint=endpoint, ticket=ticket)
            pipe = client.get_pipe("wb.request")
            if "http.fetch" not in pipe.methods:
                raise NativeError("WorkBuddy native HTTP proxy capability is unavailable")
            self._client, self._pipe = client, pipe
            self.metadata["verified_backend"] = "workbuddy_wbipc"
        except Exception as error:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            if isinstance(error, NativeError):
                raise
            raise NativeError(f"WorkBuddy native IPC connection failed ({_safe_code(error)})") from None
        finally:
            ticket = ""

    def __enter__(self):
        with self._lock:
            self._connect()
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    def call(self, method: str, params: dict, timeout=55) -> dict:
        if method != _METHOD:
            raise NativeError("Only the WorkBuddy native wb.request/http.fetch method is supported")
        if not isinstance(params, dict):
            raise NativeError("WorkBuddy native request parameters must be an object")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 55:
            raise NativeError("WorkBuddy native request timeout must be greater than zero and at most 55 seconds")
        with self._lock:
            self._connect()
            try:
                result = self._pipe.invoke("http.fetch", params, timeout=timeout)
            except Exception as error:
                raise NativeError(f"WorkBuddy native IPC request failed ({_safe_code(error)})") from None
            if not isinstance(result, dict):
                raise NativeError("WorkBuddy native IPC returned an invalid response object")
            return result

    def close(self) -> None:
        with self._lock:
            self._closed = True
            client, self._client, self._pipe = self._client, None, None
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

"""Bounded stdio ACP transport. Never invokes a shell or opens a desktop window."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from typing import Callable


class AcpError(RuntimeError):
    pass


class AcpClient:
    def __init__(self, command: list[str], cwd: str, on_update: Callable,
                 on_permission: Callable, env: dict | None = None):
        self.on_update, self.on_permission = on_update, on_permission
        self.pending: dict[int, queue.Queue] = {}
        self.serial, self.lock, self.write_lock = 0, threading.Lock(), threading.Lock()
        self.stderr = deque(maxlen=30)
        self.diagnostics = deque(maxlen=20)
        self.closed = False
        self.process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _send(self, value: dict):
        with self.write_lock:
            if self.closed or self.process.poll() is not None:
                raise AcpError("WorkBuddy ACP process is not running")
            assert self.process.stdin
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 30):
        with self.lock:
            self.serial += 1
            serial = self.serial
            response: queue.Queue = queue.Queue(maxsize=1)
            self.pending[serial] = response
        try:
            self._send({"jsonrpc": "2.0", "id": serial, "method": method, "params": params})
            try:
                value = response.get(timeout=max(0.01, timeout))
            except queue.Empty as error:
                raise TimeoutError(f"ACP {method} timed out; it must not be replayed automatically") from error
            if isinstance(value, BaseException):
                raise value
            if "error" in value:
                detail = value["error"]
                raise AcpError(str(detail.get("message", "ACP request rejected")) if isinstance(detail, dict) else "ACP request rejected")
            return value.get("result")
        finally:
            with self.lock:
                self.pending.pop(serial, None)

    def notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _handle_server_request(self, value: dict):
        try:
            if value["method"] == "session/request_permission":
                result = self.on_permission(value.get("params", {}))
                self._send({"jsonrpc": "2.0", "id": value["id"], "result": result})
            else:
                self._send({"jsonrpc": "2.0", "id": value["id"], "error": {
                    "code": -32601, "message": "Unsupported client capability; no implicit permission granted"}})
        except (OSError, AcpError, ValueError, TypeError, AttributeError):
            pass

    def _read(self):
        assert self.process.stdout
        try:
            for line in self.process.stdout:
                if len(line) > 4_000_000:
                    continue
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    # WorkBuddy --serve banners can contain access-bearing URLs.
                    # Keep only a count, never their contents in errors or logs.
                    self.diagnostics.append("Non-JSON startup output omitted")
                    continue
                if not isinstance(value, dict):
                    continue
                if "id" in value and not isinstance(value["id"], (int, str, type(None))):
                    continue
                if "method" in value and "id" in value:
                    threading.Thread(target=self._handle_server_request, args=(value,), daemon=True).start()
                elif value.get("method") == "session/update":
                    params = value.get("params", {})
                    if isinstance(params, dict):
                        try:
                            self.on_update(params)
                        except (ValueError, TypeError, AttributeError):
                            pass
                elif "id" in value:
                    with self.lock:
                        target = self.pending.get(value["id"])
                    if target:
                        target.put_nowait(value)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            with self.lock:
                for target in self.pending.values():
                    try:
                        target.put_nowait(AcpError("WorkBuddy ACP transport closed; inspect task before retrying"))
                    except queue.Full:
                        pass

    def _drain_stderr(self):
        assert self.process.stderr
        for line in self.process.stderr:
            self.stderr.append(line.rstrip()[:2000])

    def close(self):
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        # Exiting the ACP process does not establish that all external actions stopped.
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

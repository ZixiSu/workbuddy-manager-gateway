"""Account-aware local transport for the bundled CLI, without account-token access.

Protocol and architecture reference: MIT-licensed jiang198012/workbuddian,
workbuddyNative.ts / workbuddyModels.ts at commit 568e7596. This is an
independent standard-library implementation, not a model-provider API service.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qsl, urlsplit


MAX_BODY = 655_360
_FETCH = "c:wb.request/http.fetch"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_RESPONSE_HEADERS = frozenset({"content-type", "etag", "last-modified", "retry-after", "x-request-id"})
_MODEL_FIELDS = frozenset({
    "id", "name", "vendor", "maxInputTokens", "maxOutputTokens", "maxAllowedSize",
    "temperature", "top_p", "top_k", "repetition_penalty", "supportsImages",
    "supportsReasoning", "supportsToolCall", "supportsToolCalls", "onlyReasoning",
    "disabledMultimodal", "canDisableThinking", "credits", "reasoning",
    "contextWindow", "relatedModels", "isDefault",
})
_DEFAULT_FIELDS = ("defaultModel", "defaultModelId", "default_model", "default_model_id", "model")


class BrokerError(RuntimeError):
    """A deliberately redacted protocol or transport failure."""


def _decode_body(response):
    if not isinstance(response, dict) or isinstance(response.get("status"), bool):
        raise BrokerError("Invalid native account response")
    status = response.get("status")
    if not isinstance(status, int) or not 100 <= status <= 599:
        raise BrokerError("Invalid native account response")
    encoded = response.get("body_b64")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_BODY + 2) // 3) * 4:
        raise BrokerError("Native response exceeds the local transport limit")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise BrokerError("Invalid native account response") from None
    if len(body) > MAX_BODY:
        raise BrokerError("Native response exceeds the local transport limit")
    headers = response.get("headers", {})
    if not isinstance(headers, dict):
        raise BrokerError("Invalid native account response")
    return status, headers, body


def _identifier(value):
    return isinstance(value, str) and _MODEL_ID.fullmatch(value) is not None


def _merge_catalog(product, payload):
    """Keep installed auxiliary configuration; admit only account CLI models."""
    if not isinstance(product, dict) or not isinstance(payload, dict) or payload.get("code") != 0:
        raise BrokerError("Account model catalog is unavailable")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BrokerError("Account model catalog is unavailable")
    models, agents = data.get("models"), data.get("agents")
    if not isinstance(models, list) or len(models) > 512 or not isinstance(agents, list):
        raise BrokerError("Account model catalog is incompatible")
    account_cli = next((a for a in agents if isinstance(a, dict) and a.get("name") == "cli"), None)
    if not account_cli or not isinstance(account_cli.get("models"), list) or not 1 <= len(account_cli["models"]) <= 256:
        raise BrokerError("Account CLI model list is unavailable")
    listed = account_cli["models"]
    if any(not _identifier(mid) for mid in listed) or len(set(listed)) != len(listed):
        raise BrokerError("Account CLI model list is incompatible")
    account = {}
    for model in models:
        if not isinstance(model, dict) or not _identifier(model.get("id")) or model["id"] in account:
            raise BrokerError("Account model catalog is incompatible")
        account[model["id"]] = model
    if any(mid not in account for mid in listed):
        raise BrokerError("Account CLI model list is incomplete")
    usable = [mid for mid in listed if account[mid].get("disabled") is not True
              and account[mid].get("supportsToolCall") is not False
              and account[mid].get("supportsToolCalls") is not False]
    if not usable:
        raise BrokerError("No usable account CLI tool model is available")
    default = "auto" if "auto" in usable else None
    if default is None:
        default = next((account_cli.get(key) for key in _DEFAULT_FIELDS if account_cli.get(key) in usable), None)
    if default is None:
        default = next((mid for mid in usable if account[mid].get("isDefault") is True), usable[0])
    installed_models, installed_agents = product.get("models"), product.get("agents")
    if not isinstance(installed_models, list) or not isinstance(installed_agents, list):
        raise BrokerError("Installed product configuration is incompatible")
    config = copy.deepcopy(product)
    merged = {}
    for model in config["models"]:
        if not isinstance(model, dict) or not _identifier(model.get("id")):
            raise BrokerError("Installed model configuration is incompatible")
        merged[model["id"]] = model
    # A disabled or explicitly non-tool account model must not leak back from an
    # older bundled definition; unrelated bundled helper models are retained.
    for mid in listed:
        if mid not in usable:
            merged.pop(mid, None)
    for mid in usable:
        source = account[mid]
        definition = {key: copy.deepcopy(value) for key, value in source.items() if key in _MODEL_FIELDS}
        if not isinstance(definition.get("name"), str) or not definition["name"].strip():
            raise BrokerError("Account model definition is incompatible")
        merged[mid] = {**merged.get(mid, {}), **definition}
    cli = next((a for a in config["agents"] if isinstance(a, dict) and a.get("name") == "cli"), None)
    if cli is None:
        raise BrokerError("Installed CLI agent configuration is unavailable")
    cli["models"] = usable
    for key in _DEFAULT_FIELDS:
        if key in cli:
            cli[key] = default
    config["models"] = list(merged.values())
    return config, tuple(usable), default


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def handle_error(self, *_):
        # Peer disconnects must not emit local paths or request context through
        # socketserver's default traceback logger.
        pass


class NativeBroker:
    """Context-managed loopback HTTP transport backed by a connected NativeClient.

    No credentials are copied from the desktop. ``nonce`` and the gateway
    password authenticate only this process's temporary local connections.
    """

    def __init__(self, native, discovery: dict, profile_dir):
        self.native = native
        self.discovery = discovery
        self.profile_dir = Path(profile_dir).expanduser().absolute()
        self.origin = ""
        self.nonce = secrets.token_hex(32)
        self.product_config = {}
        self.default_model = None
        self.available_models = ()
        self._gateway_password = secrets.token_hex(32)
        self._server = None
        self._thread = None
        self._slots = threading.BoundedSemaphore(8)
        self.request_metadata = []
        self._metadata_lock = threading.Lock()

    def __enter__(self):
        if self._server is not None:
            raise BrokerError("Local transport is already running")
        try:
            cli = Path(self.discovery["cli"])
            product_path = cli.parent.parent / "product.json"
            if product_path.stat().st_size > 1_048_576:
                raise BrokerError("Installed product configuration is too large")
            product = json.loads(product_path.read_text(encoding="utf-8-sig"))
            response = self.native.call(_FETCH, {"method": "GET", "path": "/console/enterprises/personal/models",
                                                 "headers": {"Accept": "application/json"}}, timeout=35)
            status, _, body = _decode_body(response)
            if status != 200:
                raise BrokerError("Account model catalog is unavailable")
            payload = json.loads(body.decode("utf-8"))
            self.product_config, self.available_models, self.default_model = _merge_catalog(product, payload)
            # Validate serialization before any server is exposed. Catalog
            # account metadata and credentials are never merged into config.
            json.dumps(self.product_config, allow_nan=False)
            self._server = _Server(("127.0.0.1", 0), self._handler())
            self.origin = "http://127.0.0.1:" + str(self._server.server_port)
            self.product_config.update({"endpoint": self.origin, "networkEnvironment": "external",
                                        "authentication": {"type": "custom-token", "attributes": {
                                            "tokenType": "bearerToken", "token": self.nonce}}})
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            kwargs={"poll_interval": 0.05}, daemon=True,
                                            name="buddygate-local-account-transport")
            self._thread.start()
            return self
        except Exception:
            self.close()
            raise BrokerError("Account-aware local transport could not be prepared; no task was sent") from None

    def __exit__(self, *_):
        self.close()

    def close(self):
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            if thread is not None and thread.is_alive():
                server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def child_environment(self, base_env=None, model=None):
        if self._server is None:
            raise BrokerError("Local transport is not running")
        selected = self.default_model if model is None else model
        if selected not in self.available_models:
            raise BrokerError("Requested model is not available for account CLI tool execution")
        if self.profile_dir.is_symlink():
            raise BrokerError("Worker profile directory is not trusted")
        # Windows desktop execution identities may be unable to reopen Python's
        # synthesized 0700 ACL. The private profile inherits its parent's ACL;
        # Unix profiles still receive an explicit owner-only mode.
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o777 if os.name == "nt" else 0o700)
        original = dict(os.environ if base_env is None else base_env)
        no_proxy = next((str(v) for k, v in original.items() if k.upper() == "NO_PROXY"), "")
        prefixes = ("CODEBUDDY_", "WORKBUDDY_", "ACC_PRODUCT_CONFIG", "ACC_USER_", "ANTHROPIC_", "OPENAI_")
        env = {k: v for k, v in original.items() if not k.upper().startswith(prefixes)
               and k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
        local_no_proxy = ",".join(dict.fromkeys(["127.0.0.1", "localhost", "::1"] +
                                                [part.strip() for part in no_proxy.split(",") if part.strip()]))
        config = copy.deepcopy(self.product_config)
        cli = next(a for a in config["agents"] if isinstance(a, dict) and a.get("name") == "cli")
        for key in _DEFAULT_FIELDS:
            if key in cli:
                cli[key] = selected
        env.update({
            "ELECTRON_RUN_AS_NODE": "1", "CODEBUDDY_FORCE_HEADLESS_BUNDLE": "1",
            "CODEBUDDY_CONFIG_DIR": str(self.profile_dir), "WORKBUDDY_CONFIG_DIR": str(self.profile_dir),
            "CODEBUDDY_AUTH_TOKEN": self.nonce, "CODEBUDDY_BASE_URL": self.origin + "/v2",
            "CODEBUDDY_INTERNET_ENVIRONMENT": "external", "CODEBUDDY_CREDENTIALS_IN_MEMORY": "1",
            "CODEBUDDY_DISABLE_LOCAL_STORAGE": "1", "CODEBUDDY_DISABLE_PRODUCT_CACHE": "1",
            "CODEBUDDY_DISABLE_CUSTOM_MODELS_FILE": "1", "CODEBUDDY_API_KEY_DISABLED": "1",
            "CODEBUDDY_API_KEY_HELPER_DISABLED": "1", "CODEBUDDY_MAX_RETRIES": "0",
            "CODEBUDDY_RETRY_WATCHDOG": "0", "CODEBUDDY_GATEWAY_AUTH": "password",
            "CODEBUDDY_GATEWAY_PASSWORD": self._gateway_password, "CODEBUDDY_GATEWAY_DISABLE_API_DOCS": "1",
            "SERVER__PORT": "0", "SERVER__HOST": "127.0.0.1", "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1", "ACC_PRODUCT_CONFIG_V3": json.dumps(config, ensure_ascii=False, allow_nan=False),
            "NO_PROXY": local_no_proxy, "no_proxy": local_no_proxy,
        })
        if self.discovery.get("force_lite_wb") is True:
            env["CODEBUDDY_FORCE_LITE_WB_BUNDLE"] = "1"
        return env

    def _handler(self):
        broker = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, *_):
                pass

            def log_error(self, *_):
                pass

            def handle_expect_100(self):
                self.reply(417, b"Expectation not supported")
                return False

            def reply(self, status, body=b"", headers=None):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if self.command != "HEAD":
                    self.wfile.write(body)

            def relay(self):
                try:
                    authorization = self.headers.get_all("Authorization", [])
                    if self.headers.get_all("Origin") or len(authorization) != 1 or not hmac.compare_digest(
                            authorization[0], "Bearer " + broker.nonce):
                        self.reply(401)
                        return
                    if self.headers.get("Host") != urlsplit(broker.origin).netloc:
                        self.reply(400)
                        return
                    if any(ord(c) <= 32 or ord(c) == 127 or c in "\\#" for c in self.path):
                        self.reply(400)
                        return
                    url = urlsplit(self.path)
                    segments = url.path.split("/")
                    if url.scheme or url.netloc or url.fragment or not url.path.startswith("/v2/") or "%" in url.path or "//" in url.path or any(
                            segment in {".", ".."} for segment in segments) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", url.path):
                        self.reply(400)
                        return
                    # Verified against actual WorkBuddy CLI requests. Do not
                    # turn the account channel into a general account-write API.
                    if self.command != "POST" or url.path != "/v2/chat/completions" or url.query:
                        self.reply(403, b"Only the verified model completion endpoint is available")
                        return
                    pairs = parse_qsl(url.query, keep_blank_values=True, max_num_fields=128)
                    if len({key for key, _ in pairs}) != len(pairs):
                        self.reply(400)
                        return
                    lengths = self.headers.get_all("Content-Length", [])
                    if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
                        self.reply(400)
                        return
                    length = int(lengths[0]) if lengths else 0
                    if length < 0:
                        self.reply(400)
                        return
                    if length > MAX_BODY:
                        self.reply(413)
                        return
                    headers = {}
                    for key in ("accept", "content-type"):
                        value = self.headers.get(key)
                        if value is not None:
                            if len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                                self.reply(400)
                                return
                            headers[key] = value
                    body = self.rfile.read(length) if length else b""
                    if len(body) != length:
                        self.reply(400)
                        return
                    if not broker._slots.acquire(blocking=False):
                        self.reply(429)
                        return
                    try:
                        params = {"method": self.command, "path": url.path, "headers": headers}
                        if pairs:
                            params["query"] = dict(pairs)
                        if body:
                            params["body_b64"] = base64.b64encode(body).decode("ascii")
                        with broker._metadata_lock:
                            entry = {"method": self.command, "path": url.path}
                            if len(broker.request_metadata) < 128:
                                broker.request_metadata.append(entry)
                        response = broker.native.call(_FETCH, params, timeout=55)
                        entry["status"] = response.get("status") if isinstance(response, dict) else None
                    finally:
                        broker._slots.release()
                    status, response_headers, response_body = _decode_body(response)
                    if 300 <= status < 400:
                        self.reply(502, b"Native redirects are not followed")
                        return
                    clean = {}
                    for key, value in response_headers.items():
                        if isinstance(key, str) and key.lower() in _RESPONSE_HEADERS and isinstance(value, str):
                            if len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                                continue
                            clean[key.lower()] = value
                    self.reply(status, response_body, clean)
                except (ValueError, OverflowError):
                    self.reply(400)
                except Exception:
                    # Neither native error text nor credential-bearing startup
                    # output is returned. A non-retry response avoids replay.
                    self.reply(422, b'{"error":{"type":"workbuddy_local_transport","code":"NATIVE_REQUEST_FAILED"}}',
                               {"content-type": "application/json"})

            do_GET = relay
            do_HEAD = relay
            do_POST = relay
            do_PUT = relay
            do_PATCH = relay
            do_DELETE = relay

        return Handler

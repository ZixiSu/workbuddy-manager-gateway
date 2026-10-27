"""Native bridge tests use a fake installed library, never a model account."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import uuid

from buddygate.native import NativeClient, NativeError, _read_endpoint


class NativeClientTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(tempfile.gettempdir()).resolve()
        self.root = scratch / ("buddygate-native-" + uuid.uuid4().hex)
        self.root.mkdir()
        if self.root.resolve().parent != scratch:
            raise RuntimeError("Test directory escaped its scratch root")
        self.addCleanup(shutil.rmtree, self.root)
        self.installation = self.root / "WorkBuddy"
        self.cli = self.installation / "resources/app.asar.unpacked/cli/bin/codebuddy"
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text("test entry, never executed", encoding="utf8")
        (self.installation / "WorkBuddy.exe").write_text("test executable, never executed", encoding="utf8")
        self.config = self.root / "profile"
        self.endpoint = self.config / "wbipc/endpoint.json"
        self.endpoint.parent.mkdir(parents=True)
        self.endpoint.write_text(json.dumps({"endpoint": r"\\.\pipe\wbipc-123abc", "ticket": "unit-test-ticket-value-123"}), encoding="utf8")
        self.discovery = {"supported": True, "cli": str(self.cli)}
        self.pipe = MagicMock()
        self.pipe.methods = ["http.fetch"]
        self.pipe.invoke.return_value = {"status": 200, "headers": {}, "body_b64": "e30="}
        self.connection = MagicMock()
        self.connection.get_pipe.return_value = self.pipe
        self.module = MagicMock()
        self.module.PROTOCOL_VERSION = 1
        self.module.connect.return_value = self.connection
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def mocked_backend(self):
        load = patch("buddygate.native._load_ipc_library", return_value=self.module)
        verify = patch("buddygate.native._verify_pipe_peer", return_value=True)
        return load, verify

    def test_context_reuses_verified_connection_and_only_http_pipe(self):
        load, verify = self.mocked_backend()
        params = {"method": "GET", "path": "/console/enterprises/personal/models"}
        with load, verify as peer:
            with NativeClient(self.discovery, self.config) as native:
                self.assertEqual(native.call("c:wb.request/http.fetch", params, timeout=35)["status"], 200)
                native.call("c:wb.request/http.fetch", params)
                self.assertEqual(native.metadata["verified_backend"], "workbuddy_wbipc")
                self.assertTrue(native.metadata["peer_verified"])
                self.assertNotIn("ticket", json.dumps(native.metadata))
            native.close()
        self.module.connect.assert_called_once()
        self.connection.get_pipe.assert_called_once_with("wb.request")
        self.assertEqual(self.pipe.invoke.call_count, 2)
        self.connection.close.assert_called_once()
        peer.assert_called_once_with(r"\\.\pipe\wbipc-123abc", self.installation / "WorkBuddy.exe")

    def test_rejects_unknown_method_before_connecting(self):
        native = NativeClient(self.discovery, self.config)
        with patch("buddygate.native._load_ipc_library") as load:
            with self.assertRaises(NativeError):
                native.call("c:wb.wecom-a2a/authorize", {})
        load.assert_not_called()

    def test_rejects_invalid_request_and_unbounded_timeout(self):
        native = NativeClient(self.discovery, self.config)
        for params, timeout in (([], 55), ({}, None), ({}, True), ({}, 0), ({}, 56), ({}, float("inf"))):
            with self.assertRaises(NativeError):
                native.call("c:wb.request/http.fetch", params, timeout)

    def test_wrong_server_identity_never_authenticates(self):
        with patch("buddygate.native._load_ipc_library", return_value=self.module), patch("buddygate.native._verify_pipe_peer", return_value=False):
            with self.assertRaises(NativeError):
                NativeClient(self.discovery, self.config).__enter__()
        self.module.connect.assert_not_called()

    def test_explicit_profile_and_library_do_not_use_installation_home_as_profile(self):
        library = self.root / "installed-library"
        library.mkdir()
        load, verify = self.mocked_backend()
        with patch.dict(os.environ, {"BUDDYGATE_WORKBUDDY_CONFIG_DIR": str(self.config), "BUDDYGATE_IPC_LIBRARY": str(library), "WORKBUDDY_HOME": str(self.root / "wrong-profile")}), load as loaded, verify:
            with NativeClient(self.discovery):
                pass
        loaded.assert_called_once_with(library.resolve())

    def test_missing_installed_library_is_a_clear_failure(self):
        with self.assertRaisesRegex(NativeError, "IPC library is missing"):
            NativeClient(self.discovery, self.config).__enter__()

    def test_invalid_discovery_blocks_native_connection(self):
        with self.assertRaises(NativeError):
            NativeClient({"supported": False}, self.config).__enter__()

    def test_missing_host_capability_closes_new_connection(self):
        self.pipe.methods = []
        load, verify = self.mocked_backend()
        with load, verify, self.assertRaises(NativeError):
            NativeClient(self.discovery, self.config).__enter__()
        self.connection.close.assert_called_once()

    def test_native_error_discards_secret_bearing_original_message(self):
        self.pipe.invoke.side_effect = RuntimeError("private-account-value-must-not-appear")
        load, verify = self.mocked_backend()
        with load, verify:
            with NativeClient(self.discovery, self.config) as native:
                with self.assertRaises(NativeError) as caught:
                    native.call("c:wb.request/http.fetch", {})
        self.assertNotIn("private-account-value", str(caught.exception))

    def test_closed_client_cannot_implicitly_reconnect(self):
        native = NativeClient(self.discovery, self.config)
        native.close()
        with self.assertRaisesRegex(NativeError, "closed"):
            native.call("c:wb.request/http.fetch", {})

    def test_oversized_or_non_pipe_endpoint_is_rejected(self):
        self.endpoint.write_bytes(b"x" * 65_537)
        with self.assertRaisesRegex(NativeError, "64 KiB"):
            _read_endpoint(self.config)
        self.endpoint.write_text(json.dumps({"endpoint": "http://127.0.0.1", "ticket": "unit-test-ticket-value-123"}), encoding="utf8")
        with self.assertRaisesRegex(NativeError, "named pipe"):
            _read_endpoint(self.config)

    def test_resolved_endpoint_cannot_escape_configuration_directory(self):
        config = self.config.resolve()
        outside = self.root / "elsewhere/endpoint.json"
        def resolved(path, strict=False):
            return outside if path.name == "endpoint.json" else config
        with patch.object(Path, "resolve", autospec=True, side_effect=resolved):
            with self.assertRaisesRegex(NativeError, "escapes"):
                _read_endpoint(config)


if __name__ == "__main__":
    unittest.main()

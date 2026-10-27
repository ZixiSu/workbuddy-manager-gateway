"""Local fake-native transport checks; no installed WorkBuddy or account used."""
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from urllib.parse import urlsplit

from buddygate.broker import BrokerError, MAX_BODY, NativeBroker


def response(status=200, body=b"{}", headers=None):
    return {"status": status, "headers": headers or {"content-type": "application/json"},
            "body_b64": base64.b64encode(body).decode("ascii")}


class FakeNative:
    def __init__(self, catalog):
        self.catalog = catalog
        self.calls = []
        self.result = response()

    def call(self, method, params, timeout=55):
        self.calls.append((method, copy.deepcopy(params), timeout))
        if params["path"] == "/console/enterprises/personal/models":
            return response(body=json.dumps(self.catalog).encode())
        if isinstance(self.result, Exception):
            raise self.result
        return copy.deepcopy(self.result)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.environ.get("BUDDYGATE_TEST_TEMP", tempfile.gettempdir())).resolve()
        self.root = self.base / ("buddygate-broker-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup_root)
        root = self.root
        cli = root / "cli" / "bin" / "codebuddy"
        cli.parent.mkdir(parents=True)
        cli.write_text("not executed", encoding="utf-8")
        product = {"productName": "FixtureWorkBuddy", "auxiliary": {"keep": True},
                   "models": [{"id": "helper", "name": "Helper", "auxiliary": True},
                              {"id": "disabled", "name": "Old disabled"}],
                   "agents": [{"name": "cli", "models": ["old-model"], "defaultModel": "old-model"},
                              {"name": "helper", "models": ["helper"]}]}
        (cli.parent.parent / "product.json").write_text(json.dumps(product), encoding="utf-8")
        self.catalog = {"code": 0, "data": {
            "endpoint": "https://unused.invalid", "private_account_metadata": "not-copied",
            "models": [{"id": "tool-b", "name": "Tool B", "supportsToolCall": True},
                       {"id": "disabled", "name": "Disabled", "disabled": True},
                       {"id": "no-tools", "name": "No tools", "supportsToolCall": False},
                       {"id": "tool-a", "name": "Tool A", "supportsToolCall": True, "isDefault": True}],
            "agents": [{"name": "cli", "models": ["tool-b", "disabled", "no-tools", "tool-a"]}]}}
        self.native = FakeNative(self.catalog)
        self.discovery = {"cli": str(cli)}
        self.profile = root / "private-profile"

    def cleanup_root(self):
        resolved = self.root.resolve()
        if resolved.parent != self.base or not resolved.name.startswith("buddygate-broker-test-"):
            raise RuntimeError("Refusing cleanup outside the allocated test directory")
        shutil.rmtree(resolved)

    def start(self):
        broker = NativeBroker(self.native, self.discovery, self.profile)
        return self.enterContext(broker)

    def request(self, broker, path="/v2/chat/completions", body=None, headers=None, method="POST"):
        target = urlsplit(broker.origin)
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            result = connection.getresponse()
            return result.status, dict(result.getheaders()), result.read()
        finally:
            connection.close()

    def test_account_models_filtered_and_auxiliary_configuration_preserved(self):
        broker = self.start()
        self.assertEqual(broker.default_model, "tool-a")
        self.assertEqual(broker.available_models, ("tool-b", "tool-a"))
        cli = next(a for a in broker.product_config["agents"] if a["name"] == "cli")
        self.assertEqual(cli["models"], ["tool-b", "tool-a"])
        self.assertTrue(broker.product_config["auxiliary"]["keep"])
        ids = [m["id"] for m in broker.product_config["models"]]
        self.assertIn("helper", ids)
        self.assertNotIn("disabled", ids)
        self.assertNotIn("no-tools", ids)
        self.assertEqual(broker.product_config["endpoint"], broker.origin)
        self.assertNotIn("private_account_metadata", json.dumps(broker.product_config))
        self.assertEqual(self.native.calls[0][2], 35)

    def test_authentication_and_origin_are_checked_before_native_forwarding(self):
        broker = self.start()
        for headers in ({}, {"Authorization": "Bearer wrong"},
                        {"Authorization": "Bearer " + broker.nonce, "Origin": "http://example.invalid"}):
            with self.subTest(headers=tuple(headers)):
                self.assertEqual(self.request(broker, headers=headers)[0], 401)
        self.assertEqual(len(self.native.calls), 1)

    def test_only_required_headers_forwarded_and_native_errors_are_not_replayed(self):
        broker = self.start()
        self.native.result = response(403, b'{"error":"denied"}',
                                      {"Content-Type": "application/json", "Set-Cookie": "never-forward", "Location": "/other"})
        headers = {"Authorization": "Bearer " + broker.nonce, "Cookie": "do-not-forward-secret",
                   "X-Api-Key": "do-not-forward-secret", "Accept": "application/json", "Content-Type": "application/json"}
        status, returned, body = self.request(broker, "/v2/chat/completions", b'{"a":1}', headers)
        self.assertEqual((status, body), (403, b'{"error":"denied"}'))
        method, forwarded, timeout = self.native.calls[-1]
        self.assertEqual(method, "c:wb.request/http.fetch")
        self.assertEqual(timeout, 55)
        self.assertEqual(forwarded["headers"], {"accept": "application/json", "content-type": "application/json"})
        self.assertNotIn("query", forwarded)
        self.assertNotIn("authorization", forwarded["headers"])
        self.assertNotIn(broker.nonce, json.dumps(forwarded))
        self.assertEqual(base64.b64decode(forwarded["body_b64"]), b'{"a":1}')
        self.assertNotIn("do-not-forward-secret", json.dumps(forwarded))
        self.assertNotIn("Set-Cookie", returned)
        self.assertNotIn("Location", returned)
        self.assertEqual(len(self.native.calls), 2)
        self.native.result = RuntimeError("private account detail must not escape")
        status, _, body = self.request(broker, headers={"Authorization": "Bearer " + broker.nonce})
        self.assertEqual(status, 422)
        self.assertNotIn(b"private account", body)
        self.assertEqual(len(self.native.calls), 3)

    def test_endpoint_allowlist_blocks_other_methods_routes_and_query_without_native_calls(self):
        broker = self.start()
        auth = {"Authorization": "Bearer " + broker.nonce}
        for method, path in (("PUT", "/v2/chat/completions"),
                             ("DELETE", "/v2/chat/completions"),
                             ("GET", "/v2/chat/completions"),
                             ("POST", "/v2/messages"),
                             ("POST", "/v2/billing/example"),
                             ("POST", "/v2/chat/completions?mode=one")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(broker, path, headers=auth, method=method)[0], 403)
                # Only the initialization catalog read is allowed to reach the
                # native account channel; rejected requests have no side effect.
                self.assertEqual(len(self.native.calls), 1)

    def test_paths_redirects_and_oversized_bodies_are_rejected(self):
        broker = self.start()
        auth = {"Authorization": "Bearer " + broker.nonce}
        for path in ("http://example.invalid/v2/chat", "//example.invalid/v2/chat", "/console/enterprises/personal/models",
                     "/v2/../other", "/v2/%2e%2e/other", "/v2/%252e%252e/other", "/v2//other"):
            with self.subTest(path=path):
                self.assertEqual(self.request(broker, path, headers=auth)[0], 400)
        # Declare the oversized body without flooding a connection the server
        # correctly closes before reading it (Windows can reset unread data).
        oversized = {**auth, "Content-Length": str(MAX_BODY + 1)}
        self.assertEqual(self.request(broker, body=b"", headers=oversized)[0], 413)
        self.assertEqual(len(self.native.calls), 1)
        self.native.result = response(302, b"", {"Location": "https://example.invalid"})
        self.assertEqual(self.request(broker, headers=auth)[0], 502)
        self.native.result = response(body=b"x" * (MAX_BODY + 1))
        self.assertEqual(self.request(broker, headers=auth)[0], 422)
        self.assertEqual(len(self.native.calls), 3)

    def test_child_environment_has_private_memory_credentials_and_no_proxy_or_old_account(self):
        self.discovery["force_lite_wb"] = True
        broker = self.start()
        env = broker.child_environment({"PATH": "keep-path", "HTTP_PROXY": "http://unused.invalid",
                                        "https_proxy": "http://unused.invalid", "All_Proxy": "http://unused.invalid",
                                        "CODEBUDDY_OLD": "old", "WORKBUDDY_HOST_SECRET": "old",
                                        "ACC_USER_NAME": "old", "OPENAI_API_KEY": "old", "ANTHROPIC_API_KEY": "old",
                                        "NO_PROXY": "internal.invalid"}, model="tool-b")
        self.assertEqual(env["PATH"], "keep-path")
        for key in ("HTTP_PROXY", "https_proxy", "All_Proxy", "CODEBUDDY_OLD", "WORKBUDDY_HOST_SECRET",
                    "ACC_USER_NAME", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            self.assertNotIn(key, env)
        self.assertEqual(env["CODEBUDDY_BASE_URL"], broker.origin + "/v2")
        self.assertEqual(env["CODEBUDDY_FORCE_LITE_WB_BUNDLE"], "1")
        self.assertEqual(env["CODEBUDDY_AUTH_TOKEN"], broker.nonce)
        self.assertNotEqual(env["CODEBUDDY_GATEWAY_PASSWORD"], broker.nonce)
        config = json.loads(env["ACC_PRODUCT_CONFIG_V3"])
        self.assertEqual(config["authentication"]["attributes"]["token"], broker.nonce)
        self.assertEqual(config["endpoint"], broker.origin)
        self.assertIn("127.0.0.1", env["NO_PROXY"])
        self.assertEqual(env["NO_PROXY"], env["no_proxy"])
        self.assertEqual(list(self.profile.iterdir()), [])
        with self.assertRaises(BrokerError):
            broker.child_environment({}, model="disabled")

    def test_auto_then_agent_default_then_account_default_model_priority(self):
        # Each context uses the same fake installation but a separate local
        # endpoint. These are synthetic model IDs, not paid-provider choices.
        data = self.catalog["data"]
        data["models"].append({"id": "auto", "name": "Auto", "supportsToolCall": True})
        data["agents"][0]["models"].append("auto")
        with NativeBroker(self.native, self.discovery, self.profile) as broker:
            self.assertEqual(broker.default_model, "auto")
        data["agents"][0]["models"].remove("auto")
        data["agents"][0]["defaultModel"] = "tool-b"
        with NativeBroker(self.native, self.discovery, self.profile) as broker:
            self.assertEqual(broker.default_model, "tool-b")
        data["agents"][0]["defaultModel"] = "disabled"
        with NativeBroker(self.native, self.discovery, self.profile) as broker:
            self.assertEqual(broker.default_model, "tool-a")

    def test_catalog_failure_is_redacted_before_server_or_task_starts(self):
        self.catalog["data"]["agents"][0]["models"] = ["absent-model"]
        broker = NativeBroker(self.native, self.discovery, self.profile)
        with self.assertRaisesRegex(BrokerError, "no task was sent"):
            with broker:
                self.fail("An incomplete account catalog must not expose a server")
        self.assertEqual(broker.origin, "")
        self.assertEqual(len(self.native.calls), 1)


if __name__ == "__main__":
    unittest.main()

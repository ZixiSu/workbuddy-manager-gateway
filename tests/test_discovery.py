"""Discovery must use installation metadata without executing an agent."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

from buddygate.discovery import _display_icon_path, discover


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        # Ordinary mkdir inherits the ACL of the allowed test scratch directory.
        # Windows sandbox tokens cannot use tempfile's explicit mode-0700 ACL.
        scratch = Path(tempfile.gettempdir()).resolve()
        self.root = scratch / ("buddygate-discovery-" + uuid.uuid4().hex)
        self.root.mkdir()
        if self.root.resolve().parent != scratch:
            raise RuntimeError("Test directory escaped its scratch root")
        self.addCleanup(shutil.rmtree, self.root)
        self.node = self.root / "node.exe"
        self.node.write_text("never executed", encoding="utf-8")

    def make_installation(self, name="WorkBuddy", version="2.161.0", lite=True):
        installation = self.root / name
        package = installation / "resources" / "app.asar.unpacked" / "cli"
        (package / "bin").mkdir(parents=True)
        cli = package / "bin" / "codebuddy"
        cli.write_text("never executed", encoding="utf-8")
        (package / "package.json").write_text(json.dumps({"version": "0.0.0", "publishConfig": {"customPackage": {"version": version}}}), encoding="utf-8")
        if lite:
            (package / "dist").mkdir()
            (package / "dist" / "codebuddy-lite-wb.mjs").write_text("never executed", encoding="utf-8")
        return installation, cli

    def test_explicit_paths_take_precedence_and_read_packaged_version(self):
        _, cli = self.make_installation()
        with patch.dict(os.environ, {"BUDDYGATE_NODE": str(self.node), "BUDDYGATE_CLI": str(cli)}, clear=True), patch("buddygate.discovery._default_installations", side_effect=AssertionError("must not search")), patch("buddygate.discovery.shutil.which", side_effect=AssertionError("must not search")):
            found = discover()
        self.assertTrue(found["supported"])
        self.assertEqual(found["cli"], str(cli.resolve()))
        self.assertEqual(found["version"], "2.161.0")
        self.assertTrue(found["force_lite_wb"])
        self.assertEqual(found["errors"], [])

    def test_invalid_explicit_cli_does_not_fall_back(self):
        installation, _ = self.make_installation()
        with patch.dict(os.environ, {"BUDDYGATE_NODE": str(self.node), "BUDDYGATE_CLI": str(self.root / "missing")}, clear=True), patch("buddygate.discovery._default_installations", return_value=[(installation, None)]) as search:
            found = discover()
        self.assertFalse(found["supported"])
        self.assertIsNone(found["cli"])
        self.assertIn("BUDDYGATE_CLI", found["errors"][0])
        search.assert_not_called()

    def test_documented_cli_variable_precedes_alias(self):
        _, cli = self.make_installation()
        with patch.dict(os.environ, {"BUDDYGATE_NODE": str(self.node), "WORKBUDDY_CLI_PATH": str(cli), "BUDDYGATE_CLI": str(self.root / "missing")}, clear=True):
            found = discover()
        self.assertTrue(found["supported"])
        self.assertEqual(found["cli"], str(cli.resolve()))

    def test_explicit_installation_root_precedes_default_locations(self):
        installation, cli = self.make_installation()
        with patch.dict(os.environ, {"BUDDYGATE_NODE": str(self.node), "WORKBUDDY_HOME": str(installation)}, clear=True), patch("buddygate.discovery._default_installations", side_effect=AssertionError("must not search")):
            found = discover()
        self.assertTrue(found["supported"])
        self.assertEqual(found["cli"], str(cli.resolve()))

    def test_invalid_explicit_node_does_not_fall_back(self):
        _, cli = self.make_installation()
        with patch.dict(os.environ, {"BUDDYGATE_NODE": str(self.root / "missing"), "BUDDYGATE_CLI": str(cli)}, clear=True), patch("buddygate.discovery.shutil.which", return_value=str(self.node)) as search:
            found = discover()
        self.assertFalse(found["supported"])
        self.assertIsNone(found["node"])
        search.assert_not_called()

    def test_registry_candidate_and_node_on_path(self):
        installation, cli = self.make_installation(lite=False)
        with patch.dict(os.environ, {}, clear=True), patch("buddygate.discovery._registry_installations", return_value=[(self.root / "missing", None), (installation, "5.8.3")]), patch("buddygate.discovery.shutil.which", return_value=str(self.node)):
            found = discover()
        self.assertTrue(found["supported"])
        self.assertEqual(found["cli"], str(cli.resolve()))
        self.assertEqual(found["desktop_version"], "5.8.3")
        self.assertFalse(found["force_lite_wb"])

    def test_missing_installation_has_useful_errors(self):
        with patch.dict(os.environ, {}, clear=True), patch("buddygate.discovery._default_installations", return_value=[]), patch("buddygate.discovery.shutil.which", return_value=None):
            found = discover()
        self.assertFalse(found["supported"])
        self.assertEqual(len(found["errors"]), 2)
        self.assertIsNone(found["version"])

    def test_malformed_package_metadata_does_not_hide_valid_paths(self):
        _, cli = self.make_installation()
        (cli.parent.parent / "package.json").write_text("not JSON", encoding="utf-8")
        with patch.dict(os.environ, {"BUDDYGATE_NODE": str(self.node), "BUDDYGATE_CLI": str(cli)}, clear=True):
            found = discover()
        self.assertTrue(found["supported"])
        self.assertIsNone(found["version"])

    def test_display_icon_is_parsed_without_shell_interpretation(self):
        exe = str(self.root / "Work Buddy" / "WorkBuddy.exe")
        self.assertEqual(_display_icon_path('"' + exe + '",0'), Path(exe))
        self.assertEqual(_display_icon_path(exe + ",0"), Path(exe))
        self.assertIsNone(_display_icon_path('"unterminated.exe'))
        self.assertIsNone(_display_icon_path("cmd.exe /c something"))


if __name__ == "__main__":
    unittest.main()

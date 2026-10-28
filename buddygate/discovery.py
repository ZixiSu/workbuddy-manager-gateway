"""Find installed executors without starting them or reading account settings."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
from typing import Any


def _display_icon_path(value: str) -> Path | None:
    """Parse an uninstall entry's icon path as data, never as a command."""
    value = os.path.expandvars(value.strip())
    if value.startswith('"'):
        end = value.find('"', 1)
        if end < 0:
            return None
        value = value[1:end]
    else:
        value = re.sub(r",\s*-?\d+\s*$", "", value)
    if not value.lower().endswith(".exe"):
        return None
    return Path(value)


def _registry_installations() -> list[tuple[Path, str | None]]:
    """Read only WorkBuddy uninstall metadata; registry commands are never run."""
    try:
        import winreg
    except ImportError:
        return []

    result: list[tuple[Path, str | None]] = []
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    views = {0, getattr(winreg, "KEY_WOW64_32KEY", 0), getattr(winreg, "KEY_WOW64_64KEY", 0)}
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            try:
                with winreg.OpenKey(hive, uninstall, 0, winreg.KEY_READ | view) as root:
                    for index in range(4096):
                        try:
                            child_name = winreg.EnumKey(root, index)
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(root, child_name) as entry:
                                name = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                                if not re.search(r"\bWorkBuddy\b", name, re.IGNORECASE):
                                    continue
                                try:
                                    version = str(winreg.QueryValueEx(entry, "DisplayVersion")[0])
                                except OSError:
                                    version = None
                                try:
                                    location = winreg.QueryValueEx(entry, "InstallLocation")[0]
                                    if isinstance(location, str) and location.strip():
                                        result.append((Path(os.path.expandvars(location.strip().strip('"'))), version))
                                except OSError:
                                    pass
                                try:
                                    icon = winreg.QueryValueEx(entry, "DisplayIcon")[0]
                                    executable = _display_icon_path(icon) if isinstance(icon, str) else None
                                    if executable is not None:
                                        result.append((executable.parent, version))
                                except OSError:
                                    pass
                        except OSError:
                            continue
            except OSError:
                continue
    return result


def _default_installations() -> list[tuple[Path, str | None]]:
    candidates = _registry_installations()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.extend((Path(local) / relative, None) for relative in ("Programs/WorkBuddy", "WorkBuddy"))
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(variable):
            candidates.append((Path(os.environ[variable]) / "WorkBuddy", None))
    # A non-standard install location must be given explicitly (WORKBUDDY_HOME or
    # BUDDYGATE_CLI); no drive letter or user path is baked into the source.
    return candidates


def _existing_file(value: str) -> str | None:
    try:
        candidate = Path(os.path.expandvars(value)).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def _package_metadata(cli: str) -> tuple[str | None, bool]:
    root = Path(cli).parent.parent
    force_lite = (root / "dist" / "codebuddy-lite-wb.mjs").is_file()
    package = root / "package.json"
    version = None
    try:
        if package.stat().st_size <= 1_048_576:
            data = json.loads(package.read_text(encoding="utf-8-sig"))
            custom = data.get("publishConfig", {}).get("customPackage", {})
            candidate = custom.get("version") or data.get("version")
            if isinstance(candidate, str) and re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,79}", candidate):
                version = candidate
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return version, force_lite


def discover() -> dict[str, Any]:
    """Return installation facts, not a claim that login or model execution works.

    BUDDYGATE_NODE and WORKBUDDY_CLI_PATH (or BUDDYGATE_CLI) take precedence.
    WORKBUDDY_HOME may select an installation root, not an account data folder.
    An invalid explicit
    override fails closed instead of silently selecting another installation.
    This function never opens user settings, credentials, session history,
    desktop applications, network connections, or subprocesses.
    """
    result: dict[str, Any] = {
        "node": None,
        "cli": None,
        "version": None,
        "supported": False,
        "errors": [],
        "force_lite_wb": False,
    }
    node_override = os.environ.get("BUDDYGATE_NODE")
    node_candidate = node_override if node_override is not None else shutil.which("node")
    if node_candidate:
        result["node"] = _existing_file(node_candidate)
    if result["node"] is None:
        result["errors"].append("BUDDYGATE_NODE is not an existing file." if node_override is not None else "Node.js was not found on PATH; set BUDDYGATE_NODE to its executable.")

    cli_variable = "WORKBUDDY_CLI_PATH" if "WORKBUDDY_CLI_PATH" in os.environ else "BUDDYGATE_CLI"
    cli_override = os.environ.get(cli_variable)
    if cli_override is not None:
        result["cli"] = _existing_file(cli_override) if cli_override else None
        if result["cli"] is None:
            result["errors"].append(f"{cli_variable} is not an existing file.")
    else:
        installation_override = os.environ.get("WORKBUDDY_HOME")
        installations = [(Path(installation_override).expanduser(), None)] if installation_override else _default_installations()
        seen: set[str] = set()
        for installation, desktop_version in installations:
            root_key = os.path.normcase(str(installation))
            if root_key in seen:
                continue
            seen.add(root_key)
            for relative in ("resources/app.asar.unpacked/cli/bin/codebuddy", "resources/app/cli/bin/codebuddy"):
                cli = _existing_file(str(installation / relative))
                if cli:
                    result["cli"] = cli
                    if desktop_version and re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,79}", desktop_version):
                        result["desktop_version"] = desktop_version
                    break
            if result["cli"]:
                break
        if result["cli"] is None:
            result["errors"].append("WorkBuddy's bundled CLI was not found; set WORKBUDDY_CLI_PATH to cli/bin/codebuddy or WORKBUDDY_HOME to the installation root.")

    if result["cli"]:
        result["version"], result["force_lite_wb"] = _package_metadata(result["cli"])
    result["supported"] = bool(result["node"] and result["cli"])
    return result

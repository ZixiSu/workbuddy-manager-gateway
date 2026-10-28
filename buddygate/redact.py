"""Remove credential-shaped material from anything the manager can read."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit


def safe_text(value: str) -> str:
    value = re.sub(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic)\s+)?)[^\s,\"']+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)((?:access_token|refresh_token|client_secret|api_key|password|sessionToken|ticket|token)\s*[\"']?\s*[:=]\s*[\"']?)[^\s,\"']+", r"\1[REDACTED]", value)
    value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "[REDACTED]", value)
    def scrub_url(match):
        try:
            url = urlsplit(match[0])
            if url.query or url.fragment or url.username or url.password:
                return urlunsplit((url.scheme, url.hostname or "", url.path, "[REDACTED]", ""))
        except ValueError:
            return "[REDACTED_URL]"
        return match[0]
    return re.sub(r"https?://[^\s<>\"']+", scrub_url, value)


_SENSITIVE_KEY = re.compile(r"(?i)^(?:authorization|password|token|ticket|access_?token|refresh_?token|session_?token|api_?key|client_?secret|cookie|credential)$")


def sanitize(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else sanitize(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return safe_text(value) if isinstance(value, str) else value


def error_summary(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return safe_text(value)[:4000]
    if isinstance(value, dict):
        return {key: error_summary(item) for key, item in value.items()
                if key in {"code", "message", "category", "httpStatus", "status", "bizCode", "reason", "error", "type"}}
    return value if value is None or isinstance(value, (int, float, bool)) else "Error details omitted"


def protocol_errors(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if "error" in str(key).lower() and item:
                summary = error_summary(item)
                if summary:
                    found.append(summary)
            elif isinstance(item, (dict, list)):
                found.extend(protocol_errors(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(protocol_errors(item))
    return found[:10]

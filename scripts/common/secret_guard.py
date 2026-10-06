#!/usr/bin/env python3
"""Deny-by-default secret scanner for managed repo config and templates.

This is a bounded gate, not a proof of absence. It flags obvious credential
material in managed source files and in resolved values about to be written by
bootstrap. It never inspects keychains, credential stores, or arbitrary personal
directories, and it never reports the matching value.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# High-signal credential shapes. Kept specific to avoid false positives on
# ordinary configuration such as host:port values or env-var *names*.
SECRET_PATTERNS = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|A3T[A-Z0-9])[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
)

# key = "literal" assignments whose value is long enough to be a real secret.
ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd|credential)s?\b"
    r"\s*[:=]\s*[\"']?([^\s\"'{}<>\[\]]{12,})"
)

# Values that are references/placeholders, not secrets.
PLACEHOLDER = re.compile(
    r"""^(?:
        \$\{?[A-Za-z_][A-Za-z0-9_]*\}? |
        \{\{[^}]+\}\} |
        <[^>]+> |
        (?:REDACTED|CHANGEME|PLACEHOLDER|EXAMPLE|NONE|NULL|UNSET) |
        [xX*.]{4,} |
        -+
    )$""",
    re.VERBOSE,
)


class SecretFinding:
    __slots__ = ("location", "kind", "line")

    def __init__(self, location: str, kind: str, line: int) -> None:
        self.location = location
        self.kind = kind
        self.line = line

    def __str__(self) -> str:
        return f"{self.location}:{self.line} ({self.kind})"

    def __repr__(self) -> str:
        return f"SecretFinding({self.location!r}, {self.kind!r}, {self.line!r})"


def _looks_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER.match(value)) or value.startswith("http://") or value.startswith("https://")


def scan_text(text: str, location: str = "<text>") -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if match and not _looks_placeholder(match.group(0)):
                findings.append(SecretFinding(location, kind, lineno))
        match = ASSIGNMENT.search(line)
        if match and not _looks_placeholder(match.group(2)):
            findings.append(SecretFinding(location, "assigned_secret", lineno))
    return findings


def scan_paths(paths: Iterable[Any]) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_text(text, str(path)))
    return findings


def scan_values(value: Any, location: str = "<value>") -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    if isinstance(value, str):
        findings.extend(scan_text(value, location))
    elif isinstance(value, dict):
        for key, item in value.items():
            findings.extend(scan_values(item, f"{location}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            findings.extend(scan_values(item, f"{location}[{index}]"))
    return findings


def describe(findings: list[SecretFinding]) -> str:
    return "; ".join(str(finding) for finding in findings)

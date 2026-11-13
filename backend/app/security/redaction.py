"""Secret detection and redaction.

Applied to everything that leaves a trusted boundary: tool arguments and results shown to
the model, command output, log events, and audit details.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_SECRET_KEY_RE = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization"
    r"|client[_-]?secret|encryption[_-]?key|signing[_-]?key|auth)",
    re.IGNORECASE,
)
# Key names that contain a secret-ish word but are not secret values.
_SAFE_KEY_RE = re.compile(
    r"(token_(usage|count|hint|type)|(input|output|total|max)_tokens|_at$|^author|approval_id|^tokens?$"
    r"|secret_files|is_secret|has_token|token_configured|password_hash_scheme)",
    re.IGNORECASE,
)

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?(?:-----END (?:[A-Z ]+ )?PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
    ),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")),
]

# key = "value" / key: value assignments whose key looks secret. Values that are obviously
# references (env lookups, placeholders, empty) are left alone so code stays readable.
_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?P<key>\b[\w.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key
        |client[_-]?secret|encryption[_-]?key|signing[_-]?key|credentials?)[\w.-]*)
    (?P<quote_key>["']?)
    (?P<sep>\s*[:=]\s*)
    (?P<q>["']?)
    (?P<value>[^\s"'`,;)}\]]{6,})
    (?P=q)
    """
)
_REFERENCE_VALUE_RE = re.compile(
    r"(?i)^(os\.|process\.|env\b|getenv|settings\.|config\.|self\.|none$|null$|true$|false$|undefined$"
    r"|\$\{|\$\(|<|\{\{|str$|string$|secretstr|field\(|example|changeme|change-me|placeholder|your[_-]|xxx"
    r"|import\.meta|request\.|data\.|payload\.|body\.|form\.|args\.|kwargs|params\.|user\.|token$|\[redacted)"
)


def _assignment_sub(match: re.Match[str]) -> str:
    value = match.group("value")
    if _REFERENCE_VALUE_RE.search(value) or _SAFE_KEY_RE.search(match.group("key")):
        return match.group(0)
    # Identifier-like values (function calls, attribute access) are code, not literals.
    if not match.group("q") and re.fullmatch(r"[A-Za-z_][\w.]*\(?", value):
        return match.group(0)
    q = match.group("q")
    return f"{match.group('key')}{match.group('quote_key')}{match.group('sep')}{q}{REDACTED}{q}"


def redact_text(text: str) -> str:
    """Mask secret-looking substrings. Idempotent."""
    if not text:
        return text
    for name, pattern in _PATTERNS:
        if name == "url_credentials":
            text = pattern.sub(rf"\1{REDACTED}@", text)
        elif name == "bearer":
            text = pattern.sub(rf"\1 {REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return _ASSIGNMENT_RE.sub(_assignment_sub, text)


def contains_secret(text: str) -> bool:
    return redact_text(text) != text


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key)) and not _SAFE_KEY_RE.search(key)


def redact_value(value: Any, *, key: str | None = None, max_string: int | None = None) -> Any:
    """Recursively redact strings in JSON-like data; values under secret-looking keys are masked."""
    if key is not None and isinstance(value, str | bytes) and is_secret_key(key):
        return REDACTED if value else value
    if isinstance(value, str):
        out = redact_text(value)
        if max_string is not None and len(out) > max_string:
            out = out[:max_string] + f"... [truncated {len(out) - max_string} chars]"
        return out
    if isinstance(value, dict):
        return {str(k): redact_value(v, key=str(k), max_string=max_string) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(v, max_string=max_string) for v in value]
    return value

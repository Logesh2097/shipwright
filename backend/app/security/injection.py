"""Heuristic detection of prompt-injection attempts in untrusted text.

This is a tripwire for visibility, not a defence: authorisation never depends on the
model resisting such text. Detected patterns are surfaced to the developer and audited.
"""

from __future__ import annotations

import re

UNTRUSTED_OPEN = "<untrusted_repository_content>"
UNTRUSTED_CLOSE = "</untrusted_repository_content>"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "override_instructions",
        re.compile(
            r"(?i)\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your|system)\b"
            r".{0,40}\b(instructions?|prompts?|rules?|guidelines?|directions?)\b"
        ),
    ),
    ("role_reassignment", re.compile(r"(?i)\byou are (now|no longer)\b.{0,80}\b(assistant|agent|ai|model)\b")),
    ("new_instructions", re.compile(r"(?i)\b(new|updated|real|actual)\s+(system\s+)?(instructions?|prompt)\s*:")),
    ("fake_system_tag", re.compile(r"(?i)<\s*/?\s*(system|assistant|developer)\s*>|\[\s*(system|inst)\s*\]")),
    (
        "addressed_to_ai",
        re.compile(
            r"(?i)\b(ai|llm|assistant|agent|claude|gpt|copilot)\b[^.\n]{0,60}"
            r"\b(must|should|needs? to|is required to|have to)\b"
            r"[^.\n]{0,80}\b(delete|remove|exfiltrate|send|upload|push|commit|approve|disable|run|execute|curl|wget)\b"
        ),
    ),
    (
        "approval_bypass",
        re.compile(r"(?i)\b(without|skip|bypass|no need for|do not (ask|wait) for)\b.{0,40}\b(approval|review|confirmation|permission)\b"),
    ),
    (
        "secret_exfiltration",
        re.compile(
            r"(?i)\b(print|show|reveal|send|post|include|output|leak)\b.{0,60}"
            r"\b(api[_ -]?keys?|secrets?|tokens?|passwords?|credentials?|\.env|system prompt)\b"
        ),
    ),
    ("delimiter_escape", re.compile(r"(?i)</?\s*untrusted_repository_content\s*>")),
]


def scan_for_injection(text: str, *, limit: int = 200_000) -> list[str]:
    """Return the names of injection patterns found in ``text``."""
    if not text:
        return []
    sample = text[:limit]
    return [name for name, pattern in _PATTERNS if pattern.search(sample)]


def wrap_untrusted(text: str) -> str:
    """Delimit untrusted content, neutralising any attempt to close the delimiter early."""
    cleaned = re.sub(r"(?i)<(/?)\s*untrusted_repository_content\s*>", r"<\1untrusted-content-tag-removed>", text)
    return f"{UNTRUSTED_OPEN}\n{cleaned}\n{UNTRUSTED_CLOSE}"

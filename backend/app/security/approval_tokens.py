"""Short-lived signed tokens that authorise one specific write tool call.

The backend mints a token after a human approval; the MCP server verifies it. The token
binds the tool name, the exact arguments, the workspace root and an expiry, so it cannot
be replayed for a different call. The model never sees these tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

META_KEY = "asea/approval-token"


class ApprovalTokenError(Exception):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def args_digest(tool: str, arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json({"tool": tool, "arguments": arguments})).hexdigest()


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def mint_approval_token(
    secret: str,
    *,
    tool: str,
    arguments: dict[str, Any],
    workspace: str,
    approval_id: str,
    ttl_seconds: int = 120,
    now: float | None = None,
) -> str:
    claims = {
        "tool": tool,
        "digest": args_digest(tool, arguments),
        "workspace": workspace,
        "approval_id": approval_id,
        "exp": int((now if now is not None else time.time()) + ttl_seconds),
    }
    body = base64.urlsafe_b64encode(canonical_json(claims))
    return f"{body.decode()}.{_sign(secret, body)}"


def verify_approval_token(
    secret: str,
    token: str | None,
    *,
    tool: str,
    arguments: dict[str, Any],
    workspace: str,
    now: float | None = None,
) -> dict[str, Any]:
    if not secret:
        raise ApprovalTokenError("approval verification is not configured on this server")
    if not token or not isinstance(token, str) or token.count(".") != 1:
        raise ApprovalTokenError("this operation requires an approval token")
    body_text, signature = token.split(".")
    body = body_text.encode()
    if not hmac.compare_digest(_sign(secret, body), signature):
        raise ApprovalTokenError("approval token signature is invalid")
    try:
        claims: dict[str, Any] = json.loads(base64.urlsafe_b64decode(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ApprovalTokenError("approval token is malformed") from exc
    if claims.get("exp", 0) < (now if now is not None else time.time()):
        raise ApprovalTokenError("approval token has expired")
    if claims.get("tool") != tool:
        raise ApprovalTokenError("approval token was issued for a different tool")
    if claims.get("workspace") != workspace:
        raise ApprovalTokenError("approval token was issued for a different workspace")
    if claims.get("digest") != args_digest(tool, arguments):
        raise ApprovalTokenError("approval token does not match these arguments")
    return claims

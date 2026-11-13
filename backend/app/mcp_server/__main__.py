"""Entry point: ``python -m app.mcp_server``.

Environment:
  ASEA_WORKSPACE_ROOT    repository this server is confined to
  ASEA_APPROVAL_SECRET   key used to verify approval tokens for write tools
  ASEA_GITHUB_TOKEN      token for the github toolset
  ASEA_MCP_HTTP_TOKEN    bearer token required in streamable-http mode
  SANDBOX_*              sandbox settings (see app/sandbox/runner.py)
"""

from __future__ import annotations

import argparse
import hmac
import os
import sys

from app.logs import configure_logging
from app.mcp_server.context import ALL_TOOLSETS, ServerContext
from app.mcp_server.server import build_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.mcp_server", description="ASEA development tools MCP server")
    parser.add_argument("--toolsets", default="fs,git,terminal,testing", help=f"comma-separated: {','.join(ALL_TOOLSETS)}")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"))
    toolsets = tuple(t.strip() for t in args.toolsets.split(",") if t.strip())
    context = ServerContext.from_env(toolsets)

    if args.transport == "stdio":
        try:
            server = build_server(context)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        server.run("stdio")
        return 0

    # Streamable HTTP is for external MCP clients. It always requires a bearer token.
    http_token = os.environ.get("ASEA_MCP_HTTP_TOKEN", "")
    if len(http_token) < 24:
        print("error: ASEA_MCP_HTTP_TOKEN (at least 24 characters) is required for streamable-http", file=sys.stderr)
        return 2

    from mcp.server.auth.provider import AccessToken
    from mcp.server.auth.settings import AuthSettings

    class StaticTokenVerifier:
        async def verify_token(self, token: str) -> AccessToken | None:
            if hmac.compare_digest(token.encode(), http_token.encode()):
                return AccessToken(token=token, client_id="asea-http-client", scopes=["tools"])
            return None

    base_url = f"http://{args.host}:{args.port}"
    try:
        server = build_server(
            context,
            token_verifier=StaticTokenVerifier(),
            auth=AuthSettings(issuer_url=base_url, resource_server_url=f"{base_url}/mcp", required_scopes=["tools"]),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    server.run("streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

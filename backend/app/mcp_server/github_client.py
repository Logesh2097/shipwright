"""Minimal GitHub REST client with explicit rate-limit handling."""

from __future__ import annotations

import time
from typing import Any

import httpx

API_VERSION = "2022-11-28"


class GitHubError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not token:
            raise GitHubError("github_not_configured", "No GitHub token is configured")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "asea-agent",
            },
        )
        self.last_rate_limit: dict[str, Any] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None
    ) -> Any:
        try:
            response = await self._client.request(
                method, path, params={k: v for k, v in (params or {}).items() if v is not None}, json=json_body
            )
        except httpx.TimeoutException as exc:
            raise GitHubError("github_timeout", "GitHub did not respond in time") from exc
        except httpx.HTTPError as exc:
            raise GitHubError("github_unreachable", f"Could not reach GitHub: {type(exc).__name__}") from exc

        headers = response.headers
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        self.last_rate_limit = {
            "remaining": int(remaining) if remaining and remaining.isdigit() else None,
            "reset_at": int(reset) if reset and reset.isdigit() else None,
            "resource": headers.get("x-ratelimit-resource"),
        }

        if response.status_code in (403, 429):
            retry_after = headers.get("retry-after")
            if response.status_code == 429 or remaining == "0" or retry_after:
                wait = int(retry_after) if retry_after and retry_after.isdigit() else None
                if wait is None and reset and reset.isdigit():
                    wait = max(0, int(reset) - int(time.time()))
                raise GitHubError(
                    "rate_limited",
                    "GitHub rate limit reached" + (f"; retry in about {wait} seconds" if wait is not None else ""),
                    status=response.status_code,
                    details={"retry_after_seconds": wait, "reset_at": self.last_rate_limit["reset_at"]},
                )
        if response.status_code >= 400:
            message = ""
            try:
                body = response.json()
                message = str(body.get("message", "")) if isinstance(body, dict) else ""
                errors = body.get("errors") if isinstance(body, dict) else None
                if errors:
                    message += " " + "; ".join(
                        str(e.get("message") or e.get("code") or e) if isinstance(e, dict) else str(e) for e in errors[:5]
                    )
            except ValueError:
                message = response.text[:200]
            code = {
                401: "github_unauthorized",
                403: "github_forbidden",
                404: "github_not_found",
                422: "github_validation_failed",
            }.get(response.status_code, "github_error")
            raise GitHubError(code, message.strip() or f"GitHub returned {response.status_code}", status=response.status_code)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError("github_error", "GitHub returned a non-JSON response") from exc


# --- response shaping: keep what is useful, drop the rest ------------------------------


def repo_summary(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "full_name": r.get("full_name"),
        "private": r.get("private"),
        "description": r.get("description"),
        "default_branch": r.get("default_branch"),
        "language": r.get("language"),
        "html_url": r.get("html_url"),
        "open_issues_count": r.get("open_issues_count"),
        "stargazers_count": r.get("stargazers_count"),
        "archived": r.get("archived"),
        "pushed_at": r.get("pushed_at"),
        "permissions": r.get("permissions"),
    }


def issue_summary(i: dict[str, Any], *, body: bool = False) -> dict[str, Any]:
    out = {
        "number": i.get("number"),
        "title": i.get("title"),
        "state": i.get("state"),
        "user": (i.get("user") or {}).get("login"),
        "labels": [label.get("name") for label in i.get("labels", []) if isinstance(label, dict)],
        "comments": i.get("comments"),
        "created_at": i.get("created_at"),
        "updated_at": i.get("updated_at"),
        "html_url": i.get("html_url"),
    }
    if body:
        out["body"] = (i.get("body") or "")[:20000]
    return out


def pull_summary(p: dict[str, Any], *, body: bool = False) -> dict[str, Any]:
    out = {
        "number": p.get("number"),
        "title": p.get("title"),
        "state": p.get("state"),
        "draft": p.get("draft"),
        "user": (p.get("user") or {}).get("login"),
        "head": (p.get("head") or {}).get("ref"),
        "base": (p.get("base") or {}).get("ref"),
        "merged": p.get("merged"),
        "mergeable_state": p.get("mergeable_state"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "html_url": p.get("html_url"),
    }
    if body:
        out["body"] = (p.get("body") or "")[:20000]
        out.update({k: p.get(k) for k in ("commits", "additions", "deletions", "changed_files")})
    return out


def commit_summary(c: dict[str, Any]) -> dict[str, Any]:
    commit = c.get("commit") or {}
    author = commit.get("author") or {}
    return {
        "sha": c.get("sha"),
        "message": (commit.get("message") or "")[:500],
        "author": author.get("name"),
        "date": author.get("date"),
        "html_url": c.get("html_url"),
    }

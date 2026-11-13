"""GitHub credential management and read access (through the GitHub MCP server)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep, SettingsDep, client_ip
from app.db.models import UserCredential
from app.errors import AppError, Forbidden, NotFound, RateLimited, ServiceUnavailable, Unauthenticated, UpstreamError
from app.mcp_client.gateway import CallContext
from app.mcp_server.github_client import GitHubClient, GitHubError
from app.runtime import get_runtime
from app.schemas.api import GitHubCredentialIn, GitHubStatus
from app.security.auth import encrypt_secret
from app.services.access import audit

router = APIRouter(prefix="/github", tags=["github"])
_OWNER = Annotated[str, Query()]


def _raise(code: str | None, message: str | None) -> None:
    message = message or "GitHub request failed"
    if code == "github_not_configured":
        raise AppError("Add a GitHub token in Settings first", code=code, status_code=409)
    if code == "rate_limited":
        raise RateLimited(message, code="github_rate_limited")
    if code == "github_not_found":
        raise NotFound(message, code=code)
    if code == "github_unauthorized":
        raise AppError("GitHub rejected the stored token; it may have expired or been revoked", code=code, status_code=409)
    if code == "github_forbidden":
        raise Forbidden(f"The GitHub token does not permit this: {message}", code=code)
    if code == "mcp_unavailable":
        raise ServiceUnavailable(message, code=code)
    raise UpstreamError(message, code=code or "github_error")


async def _call(user: CurrentUser, tool: str, arguments: dict[str, Any]) -> Any:
    if user.role == "viewer":
        # Viewers have no project-independent GitHub access through this service.
        raise Forbidden("Viewers cannot use the GitHub integration")
    ctx = CallContext(repository=None, role="developer", user_id=user.id, actor="user")
    result = await get_runtime().gateway.call(tool, arguments, ctx)
    if not result.ok:
        _raise(result.error_code, result.error_message)
    return result.data


@router.get("/status", response_model=GitHubStatus)
async def status(user: CurrentUser, session: SessionDep) -> GitHubStatus:
    credential = await session.scalar(select(UserCredential).where(UserCredential.user_id == user.id, UserCredential.provider == "github"))
    if credential is None:
        return GitHubStatus(configured=False)
    return GitHubStatus(configured=True, hint=credential.hint, login=credential.meta.get("login"), updated_at=credential.updated_at)


@router.put("/credential", response_model=GitHubStatus)
async def set_credential(
    body: GitHubCredentialIn, user: CurrentUser, session: SessionDep, settings: SettingsDep, request: Request
) -> GitHubStatus:
    """Store a fine-grained personal access token. It is validated, encrypted, and never returned."""
    if user.role == "viewer":
        raise Forbidden("Viewers cannot store credentials")
    token = body.token.strip()
    client = GitHubClient(token, base_url=settings.github_api_url, transport=getattr(request.app.state, "github_transport", None))
    try:
        account = await client.request("GET", "/user")
    except GitHubError as exc:
        if exc.code == "github_unauthorized":
            raise Unauthenticated("GitHub rejected this token", code="github_token_invalid") from exc
        _raise(exc.code, exc.message)
        raise  # pragma: no cover - _raise always raises
    finally:
        await client.aclose()
    login = account.get("login") if isinstance(account, dict) else None
    credential = await session.scalar(select(UserCredential).where(UserCredential.user_id == user.id, UserCredential.provider == "github"))
    if credential is None:
        credential = UserCredential(user_id=user.id, provider="github", encrypted_secret="")
        session.add(credential)
    credential.encrypted_secret = encrypt_secret(settings, token)
    credential.hint = f"…{token[-4:]}"
    credential.meta = {"login": login}
    await audit(session, "github.credential_set", actor=user, resource_type="user", resource_id=user.id,
                details={"login": login}, ip=client_ip(request))  # fmt: skip
    await session.commit()
    await session.refresh(credential)
    # Running GitHub tool servers hold the old token in their environment.
    await get_runtime().mcp.close_matching(user_id=str(user.id))
    return GitHubStatus(configured=True, hint=credential.hint, login=login, updated_at=credential.updated_at)


@router.delete("/credential", status_code=204)
async def delete_credential(user: CurrentUser, session: SessionDep, request: Request) -> Response:
    credential = await session.scalar(select(UserCredential).where(UserCredential.user_id == user.id, UserCredential.provider == "github"))
    if credential is not None:
        await session.delete(credential)
        await audit(session, "github.credential_delete", actor=user, resource_type="user", resource_id=user.id, ip=client_ip(request))
        await session.commit()
        await get_runtime().mcp.close_matching(user_id=str(user.id))
    return Response(status_code=204)


@router.get("/repositories")
async def repositories(user: CurrentUser, page: Annotated[int, Query(ge=1, le=100)] = 1) -> Any:
    return await _call(user, "github_list_repositories", {"page": page, "per_page": 30})


@router.get("/repositories/{owner}/{repo}")
async def repository(owner: str, repo: str, user: CurrentUser) -> Any:
    return await _call(user, "github_get_repository", {"full_name": f"{owner}/{repo}"})


@router.get("/repositories/{owner}/{repo}/issues")
async def issues(
    owner: str, repo: str, user: CurrentUser,
    state: Annotated[str, Query(pattern="^(open|closed|all)$")] = "open", page: Annotated[int, Query(ge=1, le=100)] = 1,
) -> Any:  # fmt: skip
    return await _call(user, "github_list_issues", {"full_name": f"{owner}/{repo}", "state": state, "page": page})


@router.get("/repositories/{owner}/{repo}/issues/{number}")
async def issue(owner: str, repo: str, number: int, user: CurrentUser) -> Any:
    return await _call(user, "github_get_issue", {"full_name": f"{owner}/{repo}", "number": number})


@router.get("/repositories/{owner}/{repo}/pulls")
async def pulls(
    owner: str, repo: str, user: CurrentUser,
    state: Annotated[str, Query(pattern="^(open|closed|all)$")] = "open", page: Annotated[int, Query(ge=1, le=100)] = 1,
) -> Any:  # fmt: skip
    return await _call(user, "github_list_pull_requests", {"full_name": f"{owner}/{repo}", "state": state, "page": page})


@router.get("/repositories/{owner}/{repo}/pulls/{number}")
async def pull(owner: str, repo: str, number: int, user: CurrentUser) -> Any:
    return await _call(user, "github_get_pull_request", {"full_name": f"{owner}/{repo}", "number": number})


@router.get("/repositories/{owner}/{repo}/branches")
async def branches(owner: str, repo: str, user: CurrentUser) -> Any:
    return await _call(user, "github_list_branches", {"full_name": f"{owner}/{repo}"})


@router.get("/repositories/{owner}/{repo}/commits")
async def commits(owner: str, repo: str, user: CurrentUser, branch: Annotated[str | None, Query(max_length=200)] = None) -> Any:
    return await _call(user, "github_list_commits", {"full_name": f"{owner}/{repo}", "branch": branch})

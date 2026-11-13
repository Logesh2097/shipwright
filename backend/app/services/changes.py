"""Change sets: proposals, diffs, safe application, stale detection and recovery.

Nothing here writes to the repository directly. Application and reversal go through the
Tool Gateway to the MCP server, which re-checks the file hash and the approval token.
"""

from __future__ import annotations

import difflib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import ChangeSet, FileChange, Repository
from app.repo_intel.workspace import ABSENT, Workspace, WorkspaceError, sha256_text
from app.security.paths import PathNotAllowed, normalize_relative

if TYPE_CHECKING:
    from app.mcp_client.gateway import CallContext, ToolGateway

MAX_PROPOSAL_FILES = 40


class ProposalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def unified_diff(path: str, old: str | None, new: str | None) -> str:
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = (new or "").splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="/dev/null" if old is None else f"a/{path}",
        tofile="/dev/null" if new is None else f"b/{path}",
        n=3,
    )
    out: list[str] = []
    for line in diff:
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def diff_stats(diff: str) -> tuple[int, int]:
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return added, removed


async def get_change_set(session: AsyncSession, change_set_id: uuid.UUID, *, lock: bool = False) -> ChangeSet | None:
    stmt = select(ChangeSet).where(ChangeSet.id == change_set_id).options(selectinload(ChangeSet.files))
    if lock:
        stmt = stmt.with_for_update(of=ChangeSet)
    return await session.scalar(stmt)


async def _current_text(session: AsyncSession, change_set: ChangeSet, ws: Workspace, path: str) -> tuple[FileChange | None, str | None]:
    """Text the next edit applies to: the pending proposal if there is one, else the file on disk."""
    existing = await session.scalar(select(FileChange).where(FileChange.change_set_id == change_set.id, FileChange.path == path))
    if existing is not None:
        return existing, existing.new_content
    try:
        if ws.file_hash(path) == ABSENT:
            return None, None
        return None, ws.read_text_raw(path)
    except WorkspaceError as exc:
        raise ProposalError(exc.code, str(exc)) from exc


async def _record(
    session: AsyncSession, change_set: ChangeSet, ws: Workspace, path: str, existing: FileChange | None, new_content: str | None
) -> FileChange:
    if change_set.status != "draft":
        raise ProposalError("change_set_closed", "This change set can no longer be modified")
    if existing is None:
        count = len((await session.scalars(select(FileChange.id).where(FileChange.change_set_id == change_set.id))).all())
        if count >= MAX_PROPOSAL_FILES:
            raise ProposalError("too_many_files", f"A change set is limited to {MAX_PROPOSAL_FILES} files")
        base_hash = ws.file_hash(path)
        original = None if base_hash == ABSENT else ws.read_text_raw(path)
        if original is not None and sha256_text(original) != base_hash:
            # Decoding was lossy; writing it back would corrupt the file.
            raise ProposalError("not_utf8", f"{path} is not valid UTF-8 text and cannot be edited")
        change = FileChange(
            change_set_id=change_set.id,
            path=path,
            base_hash=None if base_hash == ABSENT else base_hash,
            original_content=original,
            change_type="create" if original is None else "modify",
        )
        session.add(change)
    else:
        change = existing
        original = change.original_content
    if new_content is None:
        if original is None:
            # A file created and then deleted within the same proposal: drop it entirely.
            await session.delete(change)
            await session.flush()
            return change
        change.change_type = "delete"
    else:
        if len(new_content.encode()) > ws.max_file_bytes:
            raise ProposalError("too_large", "The resulting file exceeds the size limit")
        change.change_type = "create" if original is None else "modify"
        if original is not None and new_content == original:
            if existing is not None:
                await session.delete(change)
            else:
                session.expunge(change)
            await session.flush()
            raise ProposalError("no_change", "The edit does not change the file")
    change.new_content = new_content
    change.new_hash = sha256_text(new_content) if new_content is not None else None
    change.diff = unified_diff(path, original, new_content)
    change.status = "pending"
    await session.flush()
    return change


def _check_path(ws: Workspace, path: str) -> str:
    try:
        normalized = normalize_relative(path)
        if normalized == ".":
            raise PathNotAllowed("A file path is required")
        ws.resolve(normalized)
        return normalized
    except PathNotAllowed as exc:
        raise ProposalError(exc.reason, str(exc)) from exc


async def propose_edit(
    session: AsyncSession, change_set: ChangeSet, ws: Workspace, path: str, old_text: str, new_text: str
) -> dict[str, Any]:
    path = _check_path(ws, path)
    existing, current = await _current_text(session, change_set, ws, path)
    if current is None:
        raise ProposalError("not_found", f"{path} does not exist; use propose_create_file for new files")
    if not old_text:
        raise ProposalError("invalid_edit", "old_text must not be empty")
    occurrences = current.count(old_text)
    if occurrences == 0:
        raise ProposalError(
            "no_match",
            f"old_text was not found in {path}. It must match the file exactly, including whitespace and indentation. "
            "Read the file again and copy the text to replace.",
        )
    if occurrences > 1:
        raise ProposalError(
            "ambiguous_match", f"old_text occurs {occurrences} times in {path}; include more surrounding lines to make it unique"
        )
    change = await _record(session, change_set, ws, path, existing, current.replace(old_text, new_text, 1))
    added, removed = diff_stats(change.diff)
    return {"path": path, "change_type": change.change_type, "lines_added": added, "lines_removed": removed}


async def propose_create_file(session: AsyncSession, change_set: ChangeSet, ws: Workspace, path: str, content: str) -> dict[str, Any]:
    path = _check_path(ws, path)
    existing, current = await _current_text(session, change_set, ws, path)
    if current is not None:
        raise ProposalError("already_exists", f"{path} already exists; use propose_edit to change it")
    change = await _record(session, change_set, ws, path, existing, content)
    return {"path": path, "change_type": change.change_type, "lines_added": content.count("\n") + 1}


async def propose_delete_file(session: AsyncSession, change_set: ChangeSet, ws: Workspace, path: str) -> dict[str, Any]:
    path = _check_path(ws, path)
    existing, current = await _current_text(session, change_set, ws, path)
    if current is None:
        raise ProposalError("not_found", f"{path} does not exist")
    await _record(session, change_set, ws, path, existing, None)
    return {"path": path, "change_type": "delete", "note": "File deletion needs explicit confirmation from the developer"}


def stale_files(ws: Workspace, change_set: ChangeSet) -> list[dict[str, str]]:
    """Files whose on-disk content no longer matches what the proposal was based on."""
    stale = []
    for change in change_set.files:
        try:
            actual = ws.file_hash(change.path)
        except (WorkspaceError, PathNotAllowed) as exc:
            stale.append({"path": change.path, "reason": str(exc)})
            continue
        expected = change.base_hash or ABSENT
        if actual != expected:
            reason = (
                "file was created after the proposal" if expected == ABSENT
                else "file was deleted after the proposal" if actual == ABSENT
                else "file was modified after the proposal"
            )  # fmt: skip
            stale.append({"path": change.path, "reason": reason})
    return stale


async def apply_change_set(
    session: AsyncSession, gateway: ToolGateway, repository: Repository, change_set: ChangeSet, ctx: CallContext
) -> dict[str, Any]:
    """Apply an approved change set. All-or-nothing: on any failure, files already written are restored."""
    ws = Workspace(Path(repository.local_path), max_file_bytes=gateway.settings.max_file_bytes)
    stale = stale_files(ws, change_set)
    if stale:
        change_set.status = "stale"
        for change in change_set.files:
            if any(s["path"] == change.path for s in stale):
                change.status = "stale"
        await session.flush()
        return {"status": "stale", "stale_files": stale, "applied": []}

    applied: list[FileChange] = []
    failure: dict[str, Any] | None = None
    for change in change_set.files:
        result = await _write(gateway, ctx, change.path, change.new_content, change.base_hash or ABSENT)
        if result.ok:
            change.status = "applied"
            change.error = None
            applied.append(change)
            continue
        change.status = "stale" if result.error_code == "stale" else "failed"
        change.error = result.error_message
        failure = {"path": change.path, "code": result.error_code, "message": result.error_message}
        break

    if failure is None:
        change_set.status = "applied"
        change_set.applied_at = datetime.now(UTC)
        await session.flush()
        return {"status": "applied", "applied": [c.path for c in applied]}

    rolled_back: list[str] = []
    rollback_failed: list[str] = []
    for change in reversed(applied):
        undo = await _write(gateway, ctx, change.path, change.original_content, change.new_hash or ABSENT)
        if undo.ok:
            change.status = "pending"
            rolled_back.append(change.path)
        else:
            change.status = "applied"
            change.error = f"rollback failed: {undo.error_message}"
            rollback_failed.append(change.path)
    change_set.status = "stale" if failure["code"] == "stale" and not rollback_failed else "failed"
    await session.flush()
    return {
        "status": change_set.status,
        "failure": failure,
        "applied": rollback_failed,
        "rolled_back": rolled_back,
        "rollback_failed": rollback_failed,
    }


async def revert_change_set(
    session: AsyncSession, gateway: ToolGateway, repository: Repository, change_set: ChangeSet, ctx: CallContext
) -> dict[str, Any]:
    """Restore files the agent changed, but only where they are still exactly as the agent left them."""
    reverted: list[str] = []
    conflicts: list[dict[str, str]] = []
    for change in change_set.files:
        if change.status != "applied":
            continue
        result = await _write(gateway, ctx, change.path, change.original_content, change.new_hash or ABSENT)
        if result.ok:
            change.status = "reverted"
            change.error = None
            reverted.append(change.path)
        else:
            change.status = "revert_conflict" if result.error_code in ("stale", "not_found") else "applied"
            change.error = (
                "The file was changed after the agent wrote it, so it was left untouched"
                if result.error_code in ("stale", "not_found") else result.error_message
            )  # fmt: skip
            conflicts.append({"path": change.path, "reason": change.error or "revert failed"})
    if reverted and not conflicts:
        change_set.status = "reverted"
    elif reverted:
        change_set.status = "partially_reverted"
    await session.flush()
    return {"status": change_set.status, "reverted": reverted, "conflicts": conflicts}


async def _write(gateway: ToolGateway, ctx: CallContext, path: str, content: str | None, expected: str) -> Any:
    if content is None:
        return await gateway.call("fs_delete_file", {"path": path, "expected_sha256": expected}, ctx)
    return await gateway.call("fs_write_file", {"path": path, "content": content, "expected_sha256": expected}, ctx)

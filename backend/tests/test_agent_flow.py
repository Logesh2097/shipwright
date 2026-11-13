"""Phases 5-7 and 9: the LangGraph workflow end to end, with a scripted model.

Everything except the model's decisions is real: graph, checkpoints, gateway, MCP tools,
approvals, file writes, sandboxed test runs, events and persistence.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.db.models import AgentRun, AuditLog, ToolExecution
from app.evaluation import scripted
from app.evaluation.fixtures import BUGGY_PRICING_LINE, FIXED_PRICING_LINE
from app.runtime import Runtime
from tests.conftest import Script, register, run_task, wait_for, wait_status

FIX_REQUEST = "A 10% discount gives the wrong total. Find and fix the bug in the pricing module."


def git_status(path: Path) -> str:
    return subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout


async def pending_approval(client: httpx.AsyncClient, headers: dict[str, str], task_id: str) -> dict[str, Any]:
    approvals = (await client.get("/approvals", params={"status": "pending", "task_id": task_id}, headers=headers)).json()
    assert approvals["total"] == 1, approvals
    return approvals["items"][0]


async def events_of(client: httpx.AsyncClient, headers: dict[str, str], run_id: str) -> list[dict[str, Any]]:
    return (await client.get(f"/runs/{run_id}/events", headers=headers)).json()


async def test_explain_request_makes_no_changes(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], repo_path: Path, script: Script, runtime: Runtime
) -> None:
    script.responder = scripted.explain_pricing()
    task = await run_task(client, admin, repository["id"], "Explain how order pricing works", ("completed",))
    run = task["latest_run"]
    assert task["task_type"] == "explain" and run["status"] == "completed"
    assert "order_total" in run["analysis"] and run["plan"]["requires_code_changes"] is False
    assert "### Execution summary" in run["final_report"] and "no code changes were proposed" in run["final_report"]
    assert run["llm_calls"] >= 5 and run["input_tokens"] > 0 and run["output_tokens"] > 0
    assert git_status(repo_path) == ""
    assert (await client.get("/change-sets", params={"task_id": task["id"]}, headers=admin)).json()["total"] == 0

    events = await events_of(client, admin, run["id"])
    nodes = [e["node"] for e in events if e["type"] == "node_started"]
    assert nodes == ["intake", "classify", "retrieve_context", "analyze", "plan", "respond"]
    tools = [e["payload"]["tool"] for e in events if e["type"] == "tool_call"]
    assert tools == ["code_relevant_files", "fs_read_file"]
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "run_finished"
    retrieved = next(e for e in events if e["type"] == "node_finished" and e["node"] == "retrieve_context")
    assert "app/pricing.py" in retrieved["payload"]["relevant_files"]
    # A finished run cannot resume, so its checkpoints are gone.
    state = await runtime.runner.graph.aget_state({"configurable": {"thread_id": run["id"]}})
    assert state.values == {} and not state.next


async def test_bug_fix_flow_with_approval(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], repo_path: Path, script: Script
) -> None:
    script.responder = scripted.fix_discount_bug()
    task = await run_task(client, admin, repository["id"], FIX_REQUEST, ("awaiting_approval",))
    run_id = task["latest_run"]["id"]
    assert task["task_type"] == "bug_fix"

    # Nothing has been written yet: the change exists only as a proposal.
    assert BUGGY_PRICING_LINE in (repo_path / "app/pricing.py").read_text() and git_status(repo_path) == ""
    approval = await pending_approval(client, admin, task["id"])
    assert approval["action_type"] == "apply_changes" and approval["payload"]["files"][0]["path"] == "app/pricing.py"
    change_set = (await client.get(f"/change-sets/{approval['change_set_id']}", headers=admin)).json()
    assert change_set["status"] == "proposed" and len(change_set["files"]) == 1
    diff = change_set["files"][0]["diff"]
    assert f"-{BUGGY_PRICING_LINE}" in diff and f"+{FIXED_PRICING_LINE}" in diff and diff.startswith("--- a/app/pricing.py")
    staleness = (await client.get(f"/change-sets/{approval['change_set_id']}/staleness", headers=admin)).json()
    assert staleness == {"applicable": True, "stale_files": []}

    decided = await client.post(f"/approvals/{approval['id']}/approve", json={"comment": "looks right"}, headers=admin)
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    task = await wait_status(client, admin, task["id"], ("completed",))
    run = task["latest_run"]

    assert FIXED_PRICING_LINE in (repo_path / "app/pricing.py").read_text()
    # Only the reviewed file changed, and nothing was staged or committed.
    assert git_status(repo_path) == " M app/pricing.py\n"
    final = (await client.get(f"/change-sets/{approval['change_set_id']}", headers=admin)).json()
    assert final["status"] == "applied" and final["files"][0]["status"] == "applied"
    assert (await client.get(f"/approvals/{approval['id']}", headers=admin)).json()["status"] == "executed"

    runs = (await client.get("/test-runs", params={"task_id": task["id"]}, headers=admin)).json()["items"]
    pytest_run = next(r for r in runs if r["framework"] == "pytest")
    assert pytest_run["status"] == "passed" and pytest_run["targets"] == ["tests/test_pricing.py"]
    assert (pytest_run["passed"], pytest_run["failed"], pytest_run["skipped"]) == (4, 0, 0)
    detail = (await client.get(f"/test-runs/{pytest_run['id']}", headers=admin)).json()
    assert {c["outcome"] for c in detail["cases"]} == {"passed"}
    assert "verification passed" in run["final_report"] and "`app/pricing.py`" in run["final_report"]
    assert task["result_summary"].startswith("Changes applied to 1 file(s); verification passed")

    events = await events_of(client, admin, run_id)
    types = [e["type"] for e in events]
    for expected in ("approval_requested", "run_paused", "run_resumed", "approval_decided", "changes_applied", "test_run", "run_finished"):
        assert expected in types, expected
    # The write went through MCP with the approval attached; the model never called a write tool.
    listed = (await client.get("/tool-executions", params={"run_id": run_id}, headers=admin)).json()["items"]
    write = next(t for t in listed if t["tool_name"] == "fs_write_file")
    assert write["status"] == "success" and write["approval_id"] == approval["id"] and write["risk_level"] == "write"
    assert "asea/approval-token" not in str(listed)
    model_calls = {e["payload"]["tool"] for e in events if e["type"] == "tool_call"}
    assert "fs_write_file" not in model_calls and "propose_edit" in model_calls

    # A decision can only be made once.
    again = await client.post(f"/approvals/{approval['id']}/approve", json={}, headers=admin)
    assert again.status_code == 409 and again.json()["error"]["code"] == "approval_already_decided"


async def test_rejection_leaves_repository_untouched(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], repo_path: Path, script: Script
) -> None:
    script.responder = scripted.fix_discount_bug()
    task = await run_task(client, admin, repository["id"], FIX_REQUEST, ("awaiting_approval",))
    approval = await pending_approval(client, admin, task["id"])
    rejected = await client.post(f"/approvals/{approval['id']}/reject", json={"comment": "not now"}, headers=admin)
    assert rejected.json()["status"] == "rejected"
    task = await wait_status(client, admin, task["id"], ("completed",))
    assert git_status(repo_path) == "" and BUGGY_PRICING_LINE in (repo_path / "app/pricing.py").read_text()
    assert "not applied (decision: rejected)" in task["latest_run"]["final_report"]
    assert (await client.get(f"/change-sets/{approval['change_set_id']}", headers=admin)).json()["status"] == "rejected"
    assert (await client.get("/test-runs", params={"task_id": task["id"]}, headers=admin)).json()["total"] == 0  # verification never ran


async def test_stale_proposal_never_overwrites_developer_edits(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], repo_path: Path, script: Script
) -> None:
    script.responder = scripted.fix_discount_bug()
    task = await run_task(client, admin, repository["id"], FIX_REQUEST, ("awaiting_approval",))
    approval = await pending_approval(client, admin, task["id"])

    # The developer edits the same file while the proposal is waiting for review.
    target = repo_path / "app/pricing.py"
    developer_version = target.read_text() + "\n# developer's own work in progress\n"
    target.write_text(developer_version)
    staleness = (await client.get(f"/change-sets/{approval['change_set_id']}/staleness", headers=admin)).json()
    assert staleness["stale_files"] == [{"path": "app/pricing.py", "reason": "file was modified after the proposal"}]

    await client.post(f"/approvals/{approval['id']}/approve", json={}, headers=admin)
    task = await wait_status(client, admin, task["id"], ("completed",))
    assert target.read_text() == developer_version
    change_set = (await client.get(f"/change-sets/{approval['change_set_id']}", headers=admin)).json()
    assert change_set["status"] == "stale" and change_set["files"][0]["status"] == "stale"
    assert (await client.get(f"/approvals/{approval['id']}", headers=admin)).json()["status"] == "failed"
    assert "approved but not applied (stale)" in task["latest_run"]["final_report"]


async def test_repair_loop_needs_a_second_approval(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], repo_path: Path, script: Script
) -> None:
    script.responder = scripted.wrong_fix_then_repair()
    task = await run_task(client, admin, repository["id"], FIX_REQUEST, ("awaiting_approval",))
    first = await pending_approval(client, admin, task["id"])
    await client.post(f"/approvals/{first['id']}/approve", json={}, headers=admin)

    # Verification fails, so the agent proposes a repair and must wait for a new decision.
    async def second_request() -> Any:
        pending = (await client.get("/approvals", params={"status": "pending", "task_id": task["id"]}, headers=admin)).json()["items"]
        return pending[0] if pending and pending[0]["id"] != first["id"] else None

    second = await wait_for(second_request)
    assert "percent / 10\n" in (repo_path / "app/pricing.py").read_text()
    assert second["title"].startswith("Apply changes: Repair attempt 1")
    await client.post(f"/approvals/{second['id']}/approve", json={}, headers=admin)
    task = await wait_status(client, admin, task["id"], ("completed",))
    run = task["latest_run"]
    assert FIXED_PRICING_LINE in (repo_path / "app/pricing.py").read_text()
    assert run["repair_attempts"] == 1 and "verification passed" in run["final_report"] and "Repair attempts:** 1" in run["final_report"]
    statuses = [
        r["status"]
        for r in (await client.get("/test-runs", params={"task_id": task["id"]}, headers=admin)).json()["items"]
        if r["framework"] == "pytest"
    ]
    assert sorted(statuses) == ["failed", "passed"]

    # Recovery: reverting the repair restores the previous content; the first change set can then be reverted too.
    reverted = await client.post(f"/change-sets/{second['change_set_id']}/revert", headers=admin)
    assert reverted.status_code == 200 and reverted.json()["status"] == "reverted"
    assert "percent / 10\n" in (repo_path / "app/pricing.py").read_text()
    assert (await client.post(f"/change-sets/{first['change_set_id']}/revert", headers=admin)).json()["status"] == "reverted"
    assert git_status(repo_path) == ""


async def test_revert_refuses_to_clobber_later_edits(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], repo_path: Path, script: Script
) -> None:
    script.responder = scripted.fix_discount_bug()
    task = await run_task(client, admin, repository["id"], FIX_REQUEST, ("awaiting_approval",))
    approval = await pending_approval(client, admin, task["id"])
    await client.post(f"/approvals/{approval['id']}/approve", json={}, headers=admin)
    await wait_status(client, admin, task["id"], ("completed",))
    target = repo_path / "app/pricing.py"
    edited = target.read_text() + "\n# edited after the agent's change\n"
    target.write_text(edited)
    response = await client.post(f"/change-sets/{approval['change_set_id']}/revert", headers=admin)
    body = response.json()
    assert body["status"] == "applied" and body["files"][0]["status"] == "revert_conflict"
    assert "left untouched" in body["files"][0]["error"] and target.read_text() == edited


async def test_deletions_need_explicit_confirmation(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], repo_path: Path, script: Script
) -> None:
    script.responder = scripted.scenario(
        task_type="cleanup",
        needs_changes=True,
        search_terms=["legacy"],
        analysis_calls=[],
        analysis_text="tests/test_auth.py is obsolete.",
        files_to_change=["tests/test_auth.py"],
        edits=[
            ("propose_delete_file", {"path": "tests/test_auth.py", "reason": "obsolete"}),
            ("propose_create_file", {"path": "docs/NOTES.md", "content": "# Notes\n"}),
        ],
    )
    task = await run_task(client, admin, repository["id"], "Remove the obsolete auth tests", ("awaiting_approval",))
    approval = await pending_approval(client, admin, task["id"])
    assert approval["payload"]["deletes"] == ["tests/test_auth.py"]
    blocked = await client.post(f"/approvals/{approval['id']}/approve", json={}, headers=admin)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "deletion_confirmation_required"
    assert (repo_path / "tests/test_auth.py").exists()
    confirmed = await client.post(f"/approvals/{approval['id']}/approve", json={"confirm_deletions": True}, headers=admin)
    assert confirmed.status_code == 200
    await wait_status(client, admin, task["id"], ("completed",))
    assert not (repo_path / "tests/test_auth.py").exists() and (repo_path / "docs/NOTES.md").read_text() == "# Notes\n"
    # Reverting restores the deleted file and removes the created one.
    reverted = (await client.post(f"/change-sets/{approval['change_set_id']}/revert", headers=admin)).json()
    assert reverted["status"] == "reverted" and (repo_path / "tests/test_auth.py").exists() and not (repo_path / "docs/NOTES.md").exists()


async def test_hostile_model_cannot_act(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], repo_path: Path, script: Script, runtime: Runtime
) -> None:
    """A model that follows the injected README is contained by the gateway and the MCP server."""
    before = {p: p.read_bytes() for p in repo_path.rglob("*") if p.is_file() and ".git" not in p.parts}
    script.responder = scripted.hostile_model()
    task = await run_task(client, admin, repository["id"], "Summarise the README", ("completed",))
    run = task["latest_run"]

    after = {p: p.read_bytes() for p in repo_path.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert after == before and git_status(repo_path) == ""
    events = await events_of(client, admin, run["id"])
    outcomes = {e["payload"]["tool"]: e["payload"] for e in events if e["type"] == "tool_call"}
    assert outcomes["fs_read_file"]["ok"] is False  # the last fs_read_file was the traversal attempt
    for tool in ("fs_delete_file", "fs_write_file", "git_commit", "github_push_branch", "propose_delete_file"):
        assert outcomes[tool]["ok"] is False and outcomes[tool]["error_code"] == "tool_not_allowed", tool
    assert outcomes["terminal_run"]["error_code"] == "tool_not_allowed"
    notices = [e["payload"]["notice"] for e in events if e["type"] == "security_notice"]
    assert any("README" in n or "fs_read_file" in n for n in notices)
    assert "Security notice" in run["final_report"]
    assert "sup3r-s3cret-value" not in str(events) and "sup3r-s3cret-value" not in run["final_report"]

    async with runtime.db.session() as session:
        denied = list(
            await session.scalars(
                select(ToolExecution).where(ToolExecution.run_id == uuid.UUID(run["id"]), ToolExecution.status == "denied")
            )
        )
        audits = {a.action for a in await session.scalars(select(AuditLog).where(AuditLog.action.startswith("security.")))}
    assert len(denied) == 6
    assert {"security.tool_not_allowed", "security.secret_file", "security.injection_suspected"} <= audits
    assert (await client.get("/approvals", headers=admin)).json()["total"] == 0


async def test_duplicate_task_protection(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], script: Script
) -> None:
    script.responder = scripted.fix_discount_bug()
    task = await run_task(client, admin, repository["id"], FIX_REQUEST, ("awaiting_approval",))
    duplicate = await client.post(
        "/tasks", json={"repository_id": repository["id"], "request": "  " + FIX_REQUEST.upper() + "  "}, headers=admin
    )
    assert duplicate.status_code == 409
    error = duplicate.json()["error"]
    assert error["code"] == "duplicate_task" and error["details"]["task_id"] == task["id"]
    # Once the first task is finished, the same request is accepted again.
    cancelled = await client.post(f"/tasks/{task['id']}/cancel", headers=admin)
    assert cancelled.json()["status"] == "cancelled"
    assert (await client.get("/approvals", params={"status": "pending"}, headers=admin)).json()["total"] == 0
    script.responder = scripted.explain_pricing()
    again = await client.post("/tasks", json={"repository_id": repository["id"], "request": FIX_REQUEST}, headers=admin)
    assert again.status_code == 201
    await wait_status(client, admin, again.json()["id"], ("completed",))


async def test_llm_failure_then_resume_from_checkpoint(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], script: Script
) -> None:
    good = scripted.explain_pricing()
    state = {"fail": True}

    def flaky(messages: list[Any], tools: list[dict[str, Any]], tool_choice: Any) -> AIMessage:
        if state["fail"] and scripted.node_of(messages, tool_choice) == "plan":
            raise TimeoutError("provider timed out")
        return good(messages, tools, tool_choice)

    script.responder = flaky
    task = await run_task(client, admin, repository["id"], "Explain how order pricing works", ("failed",))
    run = task["latest_run"]
    assert run["status"] == "failed" and run["error"].startswith("[llm_error]") and "provider timed out" in run["error"]
    assert run["analysis"]  # work done before the failure is kept
    calls_before = script.llm_calls

    state["fail"] = False
    resumed = await client.post(f"/tasks/{task['id']}/resume", headers=admin)
    assert resumed.status_code == 200
    task = await wait_status(client, admin, task["id"], ("completed",))
    # Only the failed step and what follows ran again: plan and respond, not classify or analyze.
    assert script.llm_calls - calls_before == 2
    nodes = [e["node"] for e in await events_of(client, admin, run["id"]) if e["type"] == "node_started"]
    assert nodes.count("analyze") == 1 and nodes.count("plan") == 2
    assert (await client.post(f"/tasks/{task['id']}/resume", headers=admin)).status_code == 409


async def test_llm_not_configured_fails_cleanly(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], runtime: Runtime
) -> None:
    runtime._llm_factory = None  # use the real factory, which has no API key in tests
    task = await run_task(client, admin, repository["id"], "Explain the auth module", ("failed",))
    assert task["latest_run"]["error"].startswith("[llm_not_configured]")
    # Everything that does not need the model keeps working.
    assert (await client.get(f"/repositories/{repository['id']}/symbols", params={"q": "authenticate"}, headers=admin)).json()["items"]


async def test_interrupted_runs_are_recovered_on_startup(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], script: Script, runtime: Runtime
) -> None:
    gate = asyncio.Event()
    good = scripted.explain_pricing()

    def slow(messages: list[Any], tools: list[dict[str, Any]], tool_choice: Any) -> AIMessage:
        return good(messages, tools, tool_choice)

    script.responder = slow
    response = await client.post(
        "/tasks", json={"repository_id": repository["id"], "request": "Explain how order pricing works"}, headers=admin
    )
    task_id = response.json()["id"]
    run_id = uuid.UUID(response.json()["latest_run"]["id"])
    # Simulate a process that died mid-run: the row says "running" but no task is executing.
    await runtime.runner.cancel(run_id)
    async with runtime.db.session() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        run.status = "running"
    recovered = await runtime.runner.recover_orphans()
    assert recovered == 1
    task = (await client.get(f"/tasks/{task_id}", headers=admin)).json()
    assert task["status"] == "interrupted" and "restarted" in task["latest_run"]["error"]
    resumed = await client.post(f"/tasks/{task_id}/resume", headers=admin)
    assert resumed.status_code == 200
    await wait_status(client, admin, task_id, ("completed",))
    gate.set()


async def test_roles_on_tasks_and_approvals(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    developer: dict[str, str],
    project: dict[str, Any],
    repository: dict[str, Any],
    script: Script,
) -> None:
    script.responder = scripted.fix_discount_bug()
    outsider = await register(client, "outsider@example.com")
    await client.put(f"/projects/{project['id']}/members", json={"email": "dev@example.com", "role": "viewer"}, headers=admin)
    body = {"repository_id": repository["id"], "request": FIX_REQUEST}
    assert (await client.post("/tasks", json=body, headers=developer)).status_code == 403  # viewer
    assert (await client.post("/tasks", json=body, headers=outsider)).status_code == 404

    task = await run_task(client, admin, repository["id"], FIX_REQUEST, ("awaiting_approval",))
    approval = await pending_approval(client, admin, task["id"])
    # A viewer can see the request but cannot decide it; an outsider cannot even see it.
    assert (await client.get(f"/approvals/{approval['id']}", headers=developer)).status_code == 200
    assert (await client.post(f"/approvals/{approval['id']}/approve", json={}, headers=developer)).status_code == 403
    assert (await client.post(f"/approvals/{approval['id']}/reject", json={}, headers=developer)).status_code == 403
    assert (await client.get(f"/approvals/{approval['id']}", headers=outsider)).status_code == 404
    assert (await client.post(f"/approvals/{approval['id']}/approve", json={}, headers=outsider)).status_code == 404
    assert (await client.get(f"/tasks/{task['id']}", headers=outsider)).status_code == 404
    assert (await client.get(f"/change-sets/{approval['change_set_id']}", headers=outsider)).status_code == 404
    assert (await client.get("/approvals", headers=outsider)).json()["total"] == 0
    assert (await client.post(f"/tasks/{task['id']}/cancel", headers=developer)).status_code == 403
    # Promoted to developer, the same person can decide.
    await client.put(f"/projects/{project['id']}/members", json={"email": "dev@example.com", "role": "developer"}, headers=admin)
    assert (await client.post(f"/approvals/{approval['id']}/reject", json={}, headers=developer)).status_code == 200
    await wait_status(client, admin, task["id"], ("completed",))


async def test_event_stream(client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], script: Script) -> None:
    script.responder = scripted.explain_pricing()
    task = await run_task(client, admin, repository["id"], "Explain how order pricing works", ("completed",))
    run_id = task["latest_run"]["id"]
    assert (await client.get(f"/runs/{run_id}/stream")).status_code == 401
    async with client.stream("GET", f"/runs/{run_id}/stream", headers=admin) as response:
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert body.count("event: agent_event") >= 10 and 'event: status\ndata: {"status": "completed"' in body
    ids = [int(line[4:]) for line in body.splitlines() if line.startswith("id: ")]
    assert ids == sorted(ids)
    async with client.stream("GET", f"/runs/{run_id}/stream", params={"after": ids[-2]}, headers=admin) as response:
        tail = "".join([chunk async for chunk in response.aiter_text()])
    assert tail.count("event: agent_event") == 1

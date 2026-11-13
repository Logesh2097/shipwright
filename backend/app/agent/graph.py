"""The LangGraph workflow. See docs/agent-workflow.md.

One graph; each node has a narrow role, its own prompt and its own tool set. Nodes return
small state updates. Large artefacts (change sets, test runs) live in their own tables.
"""

from __future__ import annotations

import json
import operator
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import func, select

from app.agent import prompts
from app.agent.events import emit, update_run, update_task
from app.agent.llm import Usage, call_structured, invoke, message_text
from app.config import Settings
from app.db.models import Approval, ChangeSet, FileChange, Project, Repository, Task
from app.db.session import Database
from app.logs import get_logger
from app.mcp_client.gateway import CallContext, ToolGateway, format_for_model
from app.mcp_client.policy import CODE_INTEL_TOOLS, PROPOSE_TOOLS, READ_TOOLS, VERIFY_TOOLS
from app.repo_intel import search
from app.repo_intel.indexer import index_repository
from app.repo_intel.workspace import Workspace
from app.security.approval_tokens import canonical_json
from app.security.injection import scan_for_injection, wrap_untrusted
from app.security.redaction import redact_text
from app.services import changes, verification

log = get_logger(__name__)


def _sum_usage(left: dict[str, int] | None, right: dict[str, int] | None) -> dict[str, int]:
    total = dict(left or {})
    for key, value in (right or {}).items():
        total[key] = total.get(key, 0) + value
    return total


class AgentState(TypedDict, total=False):
    run_id: str
    task_id: str
    repository_id: str
    user_id: str
    request: str
    classification: dict[str, Any]
    repo_overview: str
    relevant_files: list[dict[str, Any]]
    analysis: str
    plan: dict[str, Any]
    change_set_id: str | None
    change_summary: str
    approval: dict[str, Any] | None
    apply_result: dict[str, Any] | None
    verification: dict[str, Any] | None
    repair_attempts: int
    applied_change_sets: Annotated[list[str], operator.add]
    final_report: str
    usage: Annotated[dict[str, int], _sum_usage]
    security_notices: Annotated[list[str], operator.add]


@dataclass
class AgentDeps:
    db: Database
    settings: Settings
    gateway: ToolGateway
    llm_factory: Callable[[], BaseChatModel]


NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]


def payload_hash(payload: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(payload)).hexdigest()


def build_graph(deps: AgentDeps) -> StateGraph[AgentState]:
    db, settings, gateway = deps.db, deps.settings, deps.gateway

    # --- helpers -----------------------------------------------------------------------

    def ids(state: AgentState) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        return (
            uuid.UUID(state["run_id"]), uuid.UUID(state["task_id"]),
            uuid.UUID(state["repository_id"]), uuid.UUID(state["user_id"]),
        )  # fmt: skip

    async def load_repository(state: AgentState) -> tuple[Repository, bool]:
        async with db.session() as session:
            repository = await session.get(Repository, uuid.UUID(state["repository_id"]))
            if repository is None:
                raise RuntimeError("The repository for this task no longer exists")
            project = await session.get(Project, repository.project_id)
            auto = bool((project.settings or {}).get("auto_run_tests", True)) if project else True
            return repository, auto

    def context(state: AgentState, repository: Repository, auto: bool, tools: frozenset[str] | None, **kwargs: Any) -> CallContext:
        run_id, _, _, user_id = ids(state)
        # The agent acts for the developer who created the task, with developer-level rights at most.
        return CallContext(
            repository=repository, role="developer", user_id=user_id, actor="agent", run_id=run_id,
            allowed_tools=tools, auto_run_tests=auto, **kwargs,
        )  # fmt: skip

    def node(name: str) -> Callable[[NodeFn], NodeFn]:
        def decorator(fn: NodeFn) -> NodeFn:
            async def wrapper(state: AgentState) -> dict[str, Any]:
                run_id = uuid.UUID(state["run_id"])
                await update_run(db, run_id, current_node=name, heartbeat_at=datetime.now(UTC))
                await emit(db, run_id, "node_started", node=name)
                try:
                    update = await fn(state)
                except Exception as exc:
                    if type(exc).__name__ != "GraphInterrupt":
                        await emit(db, run_id, "error", node=name, payload={"error": f"{type(exc).__name__}: {exc}"[:1000]})
                    raise
                await emit(db, run_id, "node_finished", node=name, payload=_event_summary(name, update))
                return update

            wrapper.__name__ = name
            return wrapper

        return decorator

    async def tool_loop(
        state: AgentState, *, name: str, system: str, user: str, tools: frozenset[str], ctx: CallContext
    ) -> tuple[str, Usage]:
        """Bounded think/act loop. Every tool call goes through the gateway."""
        run_id = uuid.UUID(state["run_id"])
        usage: Usage = {}
        assert ctx.repository is not None
        definitions = await gateway.tool_definitions(ctx.repository, tools)
        model = deps.llm_factory()
        bound = model.bind_tools(definitions) if definitions else model
        messages: list[BaseMessage] = [SystemMessage(system), HumanMessage(user)]
        empty_replies = 0
        for step in range(settings.agent_max_tool_steps):
            reply = await invoke(bound, messages, usage)
            messages.append(reply)
            await emit(db, run_id, "llm_call", node=name, payload={"step": step + 1, "tool_calls": [c["name"] for c in reply.tool_calls]})
            if not reply.tool_calls:
                text = message_text(reply).strip()
                if text or empty_replies >= 1:
                    return text, usage
                # Some models occasionally return an empty turn after a tool result. Ask once more
                # instead of passing an empty analysis or summary downstream.
                empty_replies += 1
                messages.pop()  # an empty assistant turn is not valid history for every provider
                messages.append(HumanMessage("Your last reply was empty. Continue: call a tool, or give your final written answer."))
                continue
            for call in reply.tool_calls:
                arguments = call.get("args") if isinstance(call.get("args"), dict) else {}
                result = await gateway.call(call["name"], arguments or {}, ctx)
                await emit(
                    db, run_id, "tool_call", node=name,
                    payload={
                        "tool": call["name"], "arguments": _brief(arguments or {}), "ok": result.ok,
                        "error_code": result.error_code, "duration_ms": result.duration_ms,
                    },
                )  # fmt: skip
                for notice in result.notices:
                    await emit(db, run_id, "security_notice", node=name, payload={"notice": notice})
                messages.append(
                    ToolMessage(content=format_for_model(result, settings.agent_tool_result_chars), tool_call_id=call.get("id") or "")
                )
            _trim(messages, settings.agent_context_chars)
        messages.append(
            HumanMessage("You have used the tool budget for this step. Do not call tools. Give your final answer now from what you have.")
        )
        final = await invoke(model, messages, usage)
        return message_text(final).strip(), usage

    # --- nodes -------------------------------------------------------------------------

    @node("intake")
    async def intake(state: AgentState) -> dict[str, Any]:
        run_id, *_ = ids(state)
        repository, _ = await load_repository(state)
        if repository.status not in ("ready", "indexing"):
            await index_repository(db, settings, repository.id)
        notices: list[str] = []
        findings = scan_for_injection(state["request"])
        if findings:
            notices.append(f"request text: resembles instructions aimed at the agent ({', '.join(findings)})")
            await emit(db, run_id, "security_notice", node="intake", payload={"notice": notices[0]})
        return {"security_notices": notices, "repair_attempts": state.get("repair_attempts", 0)}

    @node("classify")
    async def classify(state: AgentState) -> dict[str, Any]:
        run_id, task_id, *_ = ids(state)
        usage: Usage = {}
        result = await call_structured(
            deps.llm_factory(),
            prompts.Classification,
            [SystemMessage(prompts.CLASSIFY_SYSTEM), HumanMessage(f"Developer request:\n{state['request']}")],
            usage,
        )
        classification = result.model_dump()
        await update_run(db, run_id, classification=classification)
        await update_task(db, task_id, task_type=classification["task_type"])
        return {"classification": classification, "usage": usage}

    @node("retrieve_context")
    async def retrieve_context(state: AgentState) -> dict[str, Any]:
        repository, _ = await load_repository(state)
        async with db.session() as session:
            overview = await search.build_overview(session, repository)
            files = await search.relevant_files(
                session, repository, state["request"], extra_terms=state["classification"].get("search_terms", []), limit=15
            )
        return {"repo_overview": overview, "relevant_files": files}

    @node("analyze")
    async def analyze(state: AgentState) -> dict[str, Any]:
        run_id, *_ = ids(state)
        repository, auto = await load_repository(state)
        task_type = state["classification"]["task_type"]
        tools = READ_TOOLS | CODE_INTEL_TOOLS
        if task_type in ("bug_fix", "test_failure") and auto:
            tools = tools | VERIFY_TOOLS
        ctx = context(state, repository, auto, frozenset(tools))
        candidates = (
            "\n".join(f"- {f['path']} (score {f['score']}; {'; '.join(f['reasons'])})" for f in state.get("relevant_files", []))
            or "(no candidates found by the index; search for them)"
        )
        user = (
            f"Developer request:\n{state['request']}\n\n"
            f"Task type: {task_type}\n\n"
            f"Repository overview:\n{wrap_untrusted(state.get('repo_overview', ''))}\n\n"
            f"Candidate files from the index:\n{wrap_untrusted(candidates)}"
        )
        analysis, usage = await tool_loop(
            state, name="analyze", system=prompts.analysis_prompt_for(task_type), user=user, tools=frozenset(tools), ctx=ctx
        )
        analysis = redact_text(analysis)
        await update_run(db, run_id, analysis=analysis)
        return {"analysis": analysis, "usage": usage, "security_notices": ctx.notices}

    @node("plan")
    async def plan(state: AgentState) -> dict[str, Any]:
        run_id, *_ = ids(state)
        usage: Usage = {}
        classification = state["classification"]
        user = (
            f"Developer request:\n{state['request']}\n\n"
            f"Classification: {json.dumps(classification)}\n\n"
            f"Analysis:\n{wrap_untrusted(state.get('analysis', ''))}"
        )
        result = await call_structured(deps.llm_factory(), prompts.Plan, [SystemMessage(prompts.PLAN_SYSTEM), HumanMessage(user)], usage)
        plan_data = result.model_dump()
        await update_run(db, run_id, plan=plan_data)
        return {"plan": plan_data, "usage": usage}

    async def _proposal_step(state: AgentState, *, name: str, system: str, user: str, title: str) -> dict[str, Any]:
        run_id, task_id, repository_id, _ = ids(state)
        repository, auto = await load_repository(state)
        async with db.session() as session:
            head = repository.head_commit
            change_set = ChangeSet(
                run_id=run_id, task_id=task_id, repository_id=repository_id, title=title[:300], status="draft", base_commit=head
            )
            session.add(change_set)
            await session.flush()
            change_set_id = change_set.id
        tools = frozenset(READ_TOOLS | CODE_INTEL_TOOLS | PROPOSE_TOOLS)
        ctx = context(state, repository, auto, tools, change_set_id=change_set_id)
        summary, usage = await tool_loop(state, name=name, system=system, user=user, tools=tools, ctx=ctx)
        summary = redact_text(summary)
        async with db.session() as session:
            proposed = await changes.get_change_set(session, change_set_id)
            assert proposed is not None
            change_set = proposed
            if not change_set.files:
                await session.delete(change_set)
                return {"change_set_id": None, "change_summary": summary, "usage": usage, "security_notices": ctx.notices}
            change_set.status = "proposed"
            change_set.summary = summary[:20000]
            files = [{"path": f.path, "change_type": f.change_type} for f in change_set.files]
        await emit(db, run_id, "change_set_created", node=name, payload={"change_set_id": str(change_set_id), "files": files})
        return {"change_set_id": str(change_set_id), "change_summary": summary, "usage": usage, "security_notices": ctx.notices}

    @node("propose_changes")
    async def propose_changes(state: AgentState) -> dict[str, Any]:
        plan_data = state.get("plan", {})
        user = (
            f"Developer request:\n{state['request']}\n\n"
            f"Plan:\n{json.dumps(plan_data, indent=1)}\n\n"
            f"Analysis:\n{wrap_untrusted(state.get('analysis', ''))}"
        )
        title = plan_data.get("summary") or state["classification"].get("summary") or "Proposed changes"
        return await _proposal_step(state, name="propose_changes", system=prompts.MODIFY_SYSTEM, user=user, title=title)

    @node("repair")
    async def repair(state: AgentState) -> dict[str, Any]:
        run_id, *_ = ids(state)
        attempt = state.get("repair_attempts", 0) + 1
        await update_run(db, run_id, repair_attempts=attempt)
        failures = json.dumps((state.get("verification") or {}).get("runs", []), indent=1)[:12000]
        user = (
            f"Developer request:\n{state['request']}\n\n"
            f"What was changed:\n{wrap_untrusted(state.get('change_summary', ''))}\n\n"
            f"Verification results after applying the change (repair attempt {attempt} of {settings.agent_max_repair_attempts}):\n"
            f"{wrap_untrusted(failures)}"
        )
        update = await _proposal_step(state, name="repair", system=prompts.REPAIR_SYSTEM, user=user, title=f"Repair attempt {attempt}")
        return {**update, "repair_attempts": attempt}

    @node("request_approval")
    async def request_approval(state: AgentState) -> dict[str, Any]:
        run_id, task_id, repository_id, _ = ids(state)
        change_set_id = uuid.UUID(state["change_set_id"] or "")
        async with db.session() as session:
            # Re-running this node after a crash must not create a second request.
            existing = await session.scalar(
                select(Approval).where(
                    Approval.run_id == run_id, Approval.change_set_id == change_set_id, Approval.action_type == "apply_changes"
                )
            )
            if existing is not None:
                return {"approval": {"approval_id": str(existing.id), "decision": None}}
            change_set = await changes.get_change_set(session, change_set_id)
            assert change_set is not None
            files = []
            for change in change_set.files:
                added, removed = changes.diff_stats(change.diff)
                files.append({"path": change.path, "change_type": change.change_type, "lines_added": added, "lines_removed": removed})
            payload = {
                "change_set_id": str(change_set_id),
                "files": files,
                "deletes": [f["path"] for f in files if f["change_type"] == "delete"],
            }
            approval = Approval(
                repository_id=repository_id, task_id=task_id, run_id=run_id, change_set_id=change_set_id,
                action_type="apply_changes", title=f"Apply changes: {change_set.title}"[:300], payload=payload,
                payload_hash=payload_hash(payload),
                expires_at=datetime.now(UTC) + timedelta(hours=settings.approval_expiry_hours),
            )  # fmt: skip
            session.add(approval)
            await session.flush()
            approval_id = approval.id
        await emit(db, run_id, "approval_requested", node="request_approval",
                   payload={"approval_id": str(approval_id), "change_set_id": str(change_set_id), "files": files})  # fmt: skip
        return {"approval": {"approval_id": str(approval_id), "decision": None}}

    async def await_approval(state: AgentState) -> dict[str, Any]:
        # Nothing but the interrupt lives in this node, because it is re-executed on resume.
        pending = state.get("approval") or {}
        decision = interrupt({"type": "approval", "approval_id": pending.get("approval_id")})
        run_id = uuid.UUID(state["run_id"])
        chosen = str((decision or {}).get("decision", "rejected"))
        await emit(
            db, run_id, "approval_decided", node="await_approval", payload={"approval_id": pending.get("approval_id"), "decision": chosen}
        )
        return {"approval": {**pending, "decision": chosen, "comment": (decision or {}).get("comment")}}

    @node("apply_changes")
    async def apply_changes(state: AgentState) -> dict[str, Any]:
        run_id, _, repository_id, _ = ids(state)
        repository, auto = await load_repository(state)
        approval_id = uuid.UUID((state.get("approval") or {})["approval_id"])
        async with db.session() as session:
            # The decision is read from the database, never from the resume payload. No row lock:
            # tool-execution rows reference this approval and are inserted while it is in use.
            approval = await session.get(Approval, approval_id)
            change_set = await changes.get_change_set(session, uuid.UUID(state["change_set_id"] or ""))
            if approval is None or change_set is None:
                raise RuntimeError("Approval or change set is missing")
            if change_set.status == "applied":
                return {"apply_result": {"status": "applied", "applied": [f.path for f in change_set.files], "note": "already applied"}}
            if approval.status != "approved" or approval.change_set_id != change_set.id:
                return {"apply_result": {"status": "not_approved", "applied": []}}
            ctx = context(state, repository, auto, frozenset({"fs_write_file", "fs_delete_file"}), approval=approval)
            result = await changes.apply_change_set(session, gateway, repository, change_set, ctx)
            approval.status = "executed" if result["status"] == "applied" else "failed"
            approval.result = result
        await emit(db, run_id, "changes_applied", node="apply_changes", payload=result)
        if result["status"] == "applied":
            await index_repository(db, settings, repository_id)
            return {"apply_result": result, "applied_change_sets": [state["change_set_id"] or ""]}
        return {"apply_result": result}

    @node("verify")
    async def verify(state: AgentState) -> dict[str, Any]:
        run_id, task_id, _, _ = ids(state)
        repository, auto = await load_repository(state)
        changed = list((state.get("apply_result") or {}).get("applied", []))
        if not auto:
            return {
                "verification": {
                    "status": "not_executed",
                    "runs": [],
                    "reason": "Automatic test runs are disabled for this project. Run checks from the Tests screen.",
                }
            }
        ctx = context(state, repository, auto, frozenset(VERIFY_TOOLS))
        profile = repository.profile or {}
        runs: list[dict[str, Any]] = []
        async with db.session() as session:
            selections = [
                (fw, await verification.select_tests(session, repository, changed, fw["framework"], fw["directory"]))
                for fw in profile.get("test_frameworks", [])
            ]
        for framework, selection in selections:
            if not selection["run"]:
                continue
            run = await verification.run_check(
                gateway, ctx, tool=framework["framework"], kind="unit", targets=selection["targets"],
                directory=framework["directory"], task_id=task_id,
            )  # fmt: skip
            summary = verification.summarize_run(run) | {"selection_reason": selection["reason"]}
            runs.append(summary)
            await emit(
                db,
                run_id,
                "test_run",
                node="verify",
                payload={k: summary[k] for k in ("test_run_id", "framework", "kind", "status", "totals")},
            )
        for tool in profile.get("quality_tools", []):
            if tool["kind"] != "lint":
                continue
            targets = [
                p[len(tool["directory"]) + 1 :] if tool["directory"] != "." else p
                for p in changed
                if (tool["directory"] == "." or p.startswith(tool["directory"] + "/"))
                and (p.endswith(".py") if tool["tool"] == "ruff" else p.endswith((".js", ".ts", ".vue", ".jsx", ".tsx")))
                and (Path(repository.local_path) / p).is_file()
            ]
            if not targets:
                continue
            run = await verification.run_check(
                gateway, ctx, tool=tool["tool"], kind="lint", targets=targets, directory=tool["directory"], task_id=task_id
            )
            summary = verification.summarize_run(run)
            runs.append(summary)
            await emit(
                db,
                run_id,
                "test_run",
                node="verify",
                payload={k: summary[k] for k in ("test_run_id", "framework", "kind", "status", "totals")},
            )
        tests = [r for r in runs if r["kind"] != "lint"]
        if any(r["status"] in ("failed", "error", "timeout") for r in tests):
            status = "failed"
        elif any(r["status"] == "passed" for r in tests):
            status = "passed"
        else:
            status = "not_executed"
        lint_failed = any(r["kind"] == "lint" and r["status"] == "failed" for r in runs)
        return {"verification": {"status": status, "runs": runs, "lint_failed": lint_failed, "changed_files": changed}}

    @node("respond")
    async def respond(state: AgentState) -> dict[str, Any]:
        run_id, task_id, *_ = ids(state)
        usage: Usage = {}
        facts = await _facts(db, state)
        user = (
            f"Developer request:\n{state['request']}\n\n"
            f"Analysis:\n{wrap_untrusted(state.get('analysis', '(none)'))}\n\n"
            f"Plan:\n{json.dumps(state.get('plan', {}), indent=1)[:6000]}\n\n"
            f"Facts about what happened (authoritative):\n{json.dumps(facts, indent=1)[:10000]}"
        )
        reply = await invoke(deps.llm_factory(), [SystemMessage(prompts.RESPOND_SYSTEM), HumanMessage(user)], usage)
        report = redact_text(message_text(reply).strip()) + "\n\n" + _facts_markdown(facts)
        await update_run(db, run_id, final_report=report)
        await update_task(db, task_id, result_summary=facts["outcome"])
        return {"final_report": report, "usage": usage}

    # --- wiring ------------------------------------------------------------------------

    def after_plan(state: AgentState) -> str:
        plan_data = state.get("plan") or {}
        wants_changes = plan_data.get("requires_code_changes") and state["classification"].get("task_type") != "explain"
        return "propose_changes" if wants_changes else "respond"

    def after_proposal(state: AgentState) -> str:
        return "request_approval" if state.get("change_set_id") else "respond"

    def after_approval(state: AgentState) -> str:
        return "apply_changes" if (state.get("approval") or {}).get("decision") == "approved" else "respond"

    def after_apply(state: AgentState) -> str:
        return "verify" if (state.get("apply_result") or {}).get("status") == "applied" else "respond"

    def after_verify(state: AgentState) -> str:
        failed = (state.get("verification") or {}).get("status") == "failed"
        return "repair" if failed and state.get("repair_attempts", 0) < settings.agent_max_repair_attempts else "respond"

    graph: StateGraph[AgentState] = StateGraph(AgentState)
    for name, fn in [
        ("intake", intake), ("classify", classify), ("retrieve_context", retrieve_context), ("analyze", analyze),
        ("plan", plan), ("propose_changes", propose_changes), ("request_approval", request_approval),
        ("await_approval", await_approval), ("apply_changes", apply_changes), ("verify", verify),
        ("repair", repair), ("respond", respond),
    ]:  # fmt: skip
        graph.add_node(name, fn)  # type: ignore[call-overload]
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "classify")
    graph.add_edge("classify", "retrieve_context")
    graph.add_edge("retrieve_context", "analyze")
    graph.add_edge("analyze", "plan")
    graph.add_conditional_edges("plan", after_plan, ["propose_changes", "respond"])
    graph.add_conditional_edges("propose_changes", after_proposal, ["request_approval", "respond"])
    graph.add_edge("request_approval", "await_approval")
    graph.add_conditional_edges("await_approval", after_approval, ["apply_changes", "respond"])
    graph.add_conditional_edges("apply_changes", after_apply, ["verify", "respond"])
    graph.add_conditional_edges("verify", after_verify, ["repair", "respond"])
    graph.add_conditional_edges("repair", after_proposal, ["request_approval", "respond"])
    graph.add_edge("respond", END)
    return graph


# --- module-level helpers --------------------------------------------------------------


def _brief(arguments: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:160] + "..." if isinstance(v, str) and len(v) > 160 else v) for k, v in list(arguments.items())[:8]}


def _trim(messages: list[BaseMessage], budget: int) -> None:
    """Replace the oldest tool outputs with a placeholder once the conversation exceeds the budget."""
    total = sum(len(message_text(m)) for m in messages)
    for message in messages:
        if total <= budget:
            return
        if isinstance(message, ToolMessage) and len(message_text(message)) > 200:
            total -= len(message_text(message)) - 60
            message.content = "[earlier tool output omitted to save space; call the tool again if needed]"


def _event_summary(name: str, update: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if "classification" in update:
        summary["classification"] = update["classification"]
    if "relevant_files" in update:
        summary["relevant_files"] = [f["path"] for f in update["relevant_files"][:10]]
    if "analysis" in update:
        summary["analysis_chars"] = len(update["analysis"])
    if "plan" in update:
        summary["plan_summary"] = update["plan"].get("summary")
        summary["files_to_change"] = update["plan"].get("files_to_change")
    if "change_set_id" in update:
        summary["change_set_id"] = update["change_set_id"]
    if update.get("apply_result"):
        summary["apply_status"] = update["apply_result"].get("status")
    if update.get("verification"):
        summary["verification_status"] = update["verification"].get("status")
    if update.get("usage"):
        summary["usage"] = update["usage"]
    return summary


async def _facts(db: Database, state: AgentState) -> dict[str, Any]:
    """What actually happened, taken from the database rather than from the model's memory."""
    facts: dict[str, Any] = {
        "task_type": state["classification"].get("task_type"),
        "code_changes": "none proposed",
        "approval": None,
        "applied_files": [],
        "verification": state.get("verification"),
        "repair_attempts": state.get("repair_attempts", 0),
        "security_notices": list(dict.fromkeys(state.get("security_notices", [])))[:10],
    }
    run_id = uuid.UUID(state["run_id"])
    async with db.session() as session:
        sets = list(await session.scalars(select(ChangeSet).where(ChangeSet.run_id == run_id).order_by(ChangeSet.created_at)))
        facts["change_sets"] = []
        for change_set in sets:
            count = await session.scalar(select(func.count()).select_from(FileChange).where(FileChange.change_set_id == change_set.id))
            facts["change_sets"].append({"id": str(change_set.id), "title": change_set.title, "status": change_set.status, "files": count})
        task = await session.get(Task, uuid.UUID(state["task_id"]))
        facts["task_title"] = task.title if task else None
    approval = state.get("approval") or {}
    apply_result = state.get("apply_result") or {}
    verification_state = state.get("verification") or {}
    if facts["change_sets"]:
        facts["approval"] = approval.get("decision")
        facts["code_changes"] = facts["change_sets"][-1]["status"]
        facts["applied_files"] = apply_result.get("applied", [])
    applied_any = bool(state.get("applied_change_sets"))
    if not facts["change_sets"]:
        outcome = "Analysis completed; no code changes were proposed."
    elif approval.get("decision") != "approved" and not applied_any:
        outcome = f"Changes were proposed but not applied (decision: {approval.get('decision') or 'none'})."
    elif apply_result.get("status") in ("stale", "failed", "not_approved") and not applied_any:
        outcome = f"Changes were approved but not applied ({apply_result.get('status')}); the files had changed or a write failed."
    else:
        status = verification_state.get("status", "not_executed")
        tail = {
            "passed": "verification passed.",
            "failed": "verification FAILED.",
            "not_executed": "verification was not executed.",
        }.get(status, f"verification: {status}.")
        if verification_state.get("lint_failed"):
            tail = tail.rstrip(".") + "; the linter reported issues in the changed files (they may predate this change)."
        last = "" if approval.get("decision") == "approved" else " The latest repair proposal was not applied."
        outcome = f"Changes applied to {len(set(apply_result.get('applied', [])))} file(s); {tail}{last}"
    facts["outcome"] = outcome
    return facts


def _facts_markdown(facts: dict[str, Any]) -> str:
    lines = ["---", "### Execution summary", f"- **Outcome:** {facts['outcome']}"]
    for change_set in facts.get("change_sets", []):
        lines.append(f"- **Change set:** {change_set['title']} — {change_set['files']} file(s), status `{change_set['status']}`")
    if facts.get("applied_files"):
        lines.append("- **Files written:** " + ", ".join(f"`{p}`" for p in facts["applied_files"][:20]))
    verification_state = facts.get("verification") or {}
    for run in verification_state.get("runs", []):
        t = run["totals"]
        lines.append(
            f"- **{run['framework']} ({run['kind']}):** `{run['status']}` — {t['passed']} passed, {t['failed']} failed, "
            f"{t['skipped']} skipped, {t['error']} errors, {t['not_executed']} not executed"
            + (f" — {run['note']}" if run.get("note") else "")
        )
    if verification_state and not verification_state.get("runs"):
        lines.append(f"- **Verification:** not executed. {verification_state.get('reason', 'No matching test suite was found.')}")
    if facts.get("repair_attempts"):
        lines.append(f"- **Repair attempts:** {facts['repair_attempts']}")
    for notice in facts.get("security_notices", []):
        lines.append(f"- **Security notice:** {notice}")
    return "\n".join(lines)


__all__ = ["AgentDeps", "AgentState", "Workspace", "build_graph", "payload_hash"]

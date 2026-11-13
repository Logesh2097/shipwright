"""Demo data for a local installation: ``python -m app.seed``.

Creates one project owned by an existing account, registers the bundled sample
repository in it, and runs four tasks through the real workflow (graph, gateway, MCP
tools, sandbox) with canned model decisions, so no model quota is spent:

* an explanation, an unused-code report and a README summary whose scripted "model"
  attempts forbidden actions (all three finish);
* a bug fix that stops at the approval step, so there is a diff to review.

Nothing is approved here. The reports of these tasks come from a scripted stand-in
model, which the project description says. Run it while the backend is stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

# Scripted decisions need no pacing; this must be set before settings are loaded.
os.environ["LLM_REQUESTS_PER_MINUTE"] = "0"

from sqlalchemy import func, select

from app.agent.llm import ScriptedChatModel
from app.api.routes.tasks import dedupe_key
from app.config import get_settings
from app.db.models import AgentRun, Project, ProjectMember, Repository, Task, User
from app.db.session import get_database
from app.evaluation.dataset import get_cases
from app.evaluation.fixtures import create_sample_repository
from app.evaluation.harness import _wait
from app.evaluation.scripted import SCENARIOS
from app.logs import configure_logging
from app.repo_intel.indexer import index_repository, validate_repository_path
from app.runtime import Runtime
from app.services.access import audit

PROJECT_NAME = "Demo: Shop API"
PROJECT_DESCRIPTION = (
    "Sample data created by `python -m app.seed`. The tasks in this project were run with a scripted "
    "stand-in model, not the configured language model; tool calls, diffs and test results are real."
)
# The bug fix goes last and is left waiting for approval.
CASE_IDS = ["explain-pricing", "prompt-injection-readme", "find-unused-code", "fix-discount-bug"]


async def _seed(email: str | None) -> int:
    settings = get_settings()
    configure_logging("WARNING")
    if not settings.allowed_roots:
        print("REPOS_ALLOWED_ROOTS must be set: the sample repository is created inside the first allowed root.", file=sys.stderr)
        return 2
    db = get_database()
    try:
        async with db.session() as session:
            query = select(User).where(User.is_active.is_(True))
            query = query.where(User.email == email) if email else query.where(User.role == "admin").order_by(User.created_at)
            owner = await session.scalar(query.limit(1))
            if owner is None:
                print("No matching account. Start the application, register, then run this again (or pass --email).", file=sys.stderr)
                return 2
            owner_id, owner_email = owner.id, owner.email
            if await session.scalar(select(Project.id).where(Project.owner_id == owner_id, Project.name == PROJECT_NAME)):
                print(
                    f'"{PROJECT_NAME}" already exists for {owner_email}; nothing to do. Delete the project in the application to seed again.'
                )
                return 0
            busy = await session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.status.in_(("running", "queued"))))
            if busy:
                print("Agent runs are in progress. Stop the backend (or let them finish) before seeding.", file=sys.stderr)
                return 2

        repo_dir = Path(settings.allowed_roots[0]) / "shop-demo"
        if not repo_dir.exists():
            create_sample_repository(repo_dir)
        info = validate_repository_path(settings, str(repo_dir))

        async with db.session() as session:
            owner = await session.get(User, owner_id)
            project = Project(name=PROJECT_NAME, description=PROJECT_DESCRIPTION, owner_id=owner_id, settings={"auto_run_tests": True})
            project.members.append(ProjectMember(user_id=owner_id, role="owner"))
            session.add(project)
            await session.flush()
            repository = Repository(
                project_id=project.id, name="shop-demo", local_path=info.path, default_branch=info.default_branch, status="indexing"
            )
            session.add(repository)
            await session.flush()
            project_id, repository_id = project.id, repository.id
            await audit(session, "project.create", actor=owner, resource_type="project", resource_id=project_id,
                        details={"name": PROJECT_NAME, "source": "seed"})  # fmt: skip
            await audit(session, "repository.register", actor=owner, resource_type="repository", resource_id=repository_id,
                        details={"path": info.path, "source": "seed"})  # fmt: skip
        await index_repository(db, settings, repository_id)
        print(f"Project and repository created for {owner_email}: {info.path}")

        runtime = Runtime(settings, db)  # PostgreSQL checkpoints, so the backend can continue the waiting run
        await runtime.startup()
        try:
            for case in get_cases(CASE_IDS):
                runtime._llm_factory = lambda responder=SCENARIOS[case.id](): ScriptedChatModel(responder=responder)
                async with db.session() as session:
                    owner = await session.get(User, owner_id)
                    task = Task(
                        project_id=project_id, repository_id=repository_id, created_by=owner_id, title=case.request[:300],
                        request_text=case.request, dedupe_key=dedupe_key(repository_id, case.request), status="queued",
                    )  # fmt: skip
                    session.add(task)
                    await session.flush()
                    run_id = uuid.uuid4()
                    session.add(AgentRun(id=run_id, task_id=task.id, thread_id=str(run_id), status="queued"))
                    await audit(session, "task.create", actor=owner, resource_type="task", resource_id=task.id,
                                details={"repository_id": str(repository_id), "source": "seed"})  # fmt: skip
                await runtime.runner.start(run_id)
                status = await _wait(runtime, run_id, 300.0)
                print(f"  {status:18s} {case.request}")
        finally:
            await runtime.shutdown()
    finally:
        await db.dispose()
    print("Done. One task is waiting for your approval under Approvals.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", help="account that will own the demo project (default: the first administrator)")
    return asyncio.run(_seed(parser.parse_args().email))


if __name__ == "__main__":
    raise SystemExit(main())

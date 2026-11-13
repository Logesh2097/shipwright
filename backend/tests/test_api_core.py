"""Phase 1-2: health, authentication, users, projects, membership and isolation."""

from __future__ import annotations

from typing import Any

import httpx

from tests.conftest import PASSWORD, register


async def test_health_endpoints(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    checks = ready.json()["checks"]
    assert checks["database"] == "ok" and checks["llm"] == "not_configured" and checks["sandbox"]["mode"] == "local"


async def test_error_shape_and_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/projects")
    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "unauthenticated" and body["request_id"] == response.headers["x-request-id"]
    invalid = await client.post("/auth/register", json={"email": "nope", "password": "short"})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "validation_error"
    assert (await client.get("/does-not-exist")).json()["error"]["code"] == "not_found"


async def test_first_user_is_admin_then_developer(client: httpx.AsyncClient) -> None:
    admin = await register(client, "first@example.com")
    dev = await register(client, "second@example.com")
    assert (await client.get("/auth/me", headers=admin)).json()["role"] == "admin"
    assert (await client.get("/auth/me", headers=dev)).json()["role"] == "developer"
    duplicate = await client.post("/auth/register", json={"email": "FIRST@example.com", "password": PASSWORD})
    assert duplicate.status_code == 409
    assert (await client.get("/users", headers=dev)).status_code == 403
    users = (await client.get("/users", headers=admin)).json()
    assert users["total"] == 2 and all("password" not in str(u).lower() for u in users["items"])


async def test_service_account_does_not_take_the_admin_slot(client: httpx.AsyncClient, runtime: Any) -> None:
    from app.evaluation.harness import _ensure_eval_user

    await _ensure_eval_user(runtime)
    first_human = await register(client, "human@example.com")
    assert (await client.get("/auth/me", headers=first_human)).json()["role"] == "admin"
    blocked = await client.post("/auth/login", json={"email": "evaluation-runner@asea.local", "password": "anything-at-all"})
    assert blocked.status_code == 401


async def test_registration_can_be_closed(client: httpx.AsyncClient, settings: Any, monkeypatch: Any) -> None:
    await register(client, "first@example.com")
    monkeypatch.setattr(settings, "allow_registration", False)
    closed = await client.post("/auth/register", json={"email": "late@example.com", "password": PASSWORD})
    assert closed.status_code == 403 and closed.json()["error"]["code"] == "registration_disabled"


async def test_login_refresh_and_token_types(client: httpx.AsyncClient) -> None:
    await register(client, "a@example.com")
    tokens = (await client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})).json()
    refreshed = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    # A refresh token is not accepted as an access token, and vice versa.
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"})).status_code == 401
    assert (await client.post("/auth/refresh", json={"refresh_token": tokens["access_token"]})).status_code == 401
    assert (await client.get("/auth/me", headers={"Authorization": "Bearer garbage"})).status_code == 401


async def test_login_throttling(client: httpx.AsyncClient) -> None:
    await register(client, "a@example.com")
    for _ in range(5):
        bad = await client.post("/auth/login", json={"email": "a@example.com", "password": "wrong-password"})
        assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_credentials"
    blocked = await client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert blocked.status_code == 429


async def test_disabled_user_and_last_admin_protection(client: httpx.AsyncClient, admin: dict[str, str], developer: dict[str, str]) -> None:
    users = (await client.get("/users", headers=admin)).json()["items"]
    admin_id = next(u["id"] for u in users if u["role"] == "admin")
    dev_id = next(u["id"] for u in users if u["role"] == "developer")
    assert (await client.patch(f"/users/{admin_id}", json={"role": "developer"}, headers=admin)).status_code == 409
    assert (await client.patch(f"/users/{dev_id}", json={"is_active": False}, headers=admin)).status_code == 200
    assert (await client.get("/auth/me", headers=developer)).status_code == 401


async def test_project_crud_and_isolation(client: httpx.AsyncClient, admin: dict[str, str], developer: dict[str, str]) -> None:
    other = await register(client, "other@example.com")
    created = await client.post("/projects", json={"name": "Dev project"}, headers=developer)
    assert created.status_code == 201 and created.json()["my_role"] == "owner"
    project_id = created.json()["id"]
    assert (await client.post("/projects", json={"name": "Dev project"}, headers=developer)).status_code == 409

    # Another user cannot see it, and cannot tell that it exists.
    assert (await client.get(f"/projects/{project_id}", headers=other)).status_code == 404
    assert (await client.get("/projects", headers=other)).json()["total"] == 0
    assert (await client.patch(f"/projects/{project_id}", json={"name": "x"}, headers=other)).status_code == 404
    # Administrators see everything.
    assert (await client.get(f"/projects/{project_id}", headers=admin)).json()["my_role"] == "owner"

    updated = await client.patch(f"/projects/{project_id}", json={"settings": {"auto_run_tests": False}}, headers=developer)
    assert updated.json()["settings"]["auto_run_tests"] is False
    assert (await client.delete(f"/projects/{project_id}", headers=developer)).status_code == 204
    assert (await client.get(f"/projects/{project_id}", headers=developer)).status_code == 404


async def test_membership_roles(
    client: httpx.AsyncClient, admin: dict[str, str], developer: dict[str, str], project: dict[str, Any]
) -> None:
    pid = project["id"]
    assert (await client.get(f"/projects/{pid}", headers=developer)).status_code == 404
    missing = await client.put(f"/projects/{pid}/members", json={"email": "ghost@example.com", "role": "viewer"}, headers=admin)
    assert missing.status_code == 404
    added = await client.put(f"/projects/{pid}/members", json={"email": "dev@example.com", "role": "viewer"}, headers=admin)
    assert added.status_code == 200
    assert (await client.get(f"/projects/{pid}", headers=developer)).json()["my_role"] == "viewer"
    # A viewer can read but not manage.
    assert (await client.patch(f"/projects/{pid}", json={"name": "x"}, headers=developer)).status_code == 403
    assert (
        await client.put(f"/projects/{pid}/members", json={"email": "dev@example.com", "role": "owner"}, headers=developer)
    ).status_code == 403
    members = (await client.get(f"/projects/{pid}/members", headers=developer)).json()
    assert {m["role"] for m in members} == {"owner", "viewer"}
    # The only owner cannot be removed or demoted.
    owner_id = next(m["user_id"] for m in members if m["role"] == "owner")
    assert (await client.delete(f"/projects/{pid}/members/{owner_id}", headers=admin)).status_code == 409
    dev_id = next(m["user_id"] for m in members if m["role"] == "viewer")
    assert (await client.delete(f"/projects/{pid}/members/{dev_id}", headers=admin)).status_code == 204
    assert (await client.get(f"/projects/{pid}", headers=developer)).status_code == 404


async def test_audit_log_and_dashboard(
    client: httpx.AsyncClient, admin: dict[str, str], developer: dict[str, str], project: dict[str, Any]
) -> None:
    assert (await client.get("/audit-logs", headers=developer)).status_code == 403
    logs = (await client.get("/audit-logs", headers=admin)).json()
    actions = {entry["action"] for entry in logs["items"]}
    assert {"auth.register", "auth.login", "project.create"} <= actions
    summary = (await client.get("/dashboard/summary", headers=admin)).json()
    assert summary["projects"] == 1 and summary["repositories"] == 0 and summary["pending_approvals"] == 0
    assert summary["system"]["llm_configured"] is False
    assert (await client.get("/dashboard/summary", headers=developer)).json()["projects"] == 0
    system = (await client.get("/settings/system", headers=developer)).json()
    assert system["llm"]["configured"] is False and "secret" not in str(system).lower().replace("secret_files", "")
    levels = {e["command"]: e["level"] for e in system["command_policy_examples"]}
    assert levels["pytest -q"] == "safe" and levels["npm ci"] == "sensitive" and levels["rm -rf ."] == "denied"

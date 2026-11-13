"""Phase 3: registration, validation, indexing, exploration and code understanding."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from app.evaluation.fixtures import create_sample_repository
from app.repo_intel.symbols import extract_symbols
from tests.conftest import _REPOS_ROOT, register, wait_for


async def test_registration_validation(client: httpx.AsyncClient, admin: dict[str, str], project: dict[str, Any], repo_path: Path) -> None:
    url = f"/projects/{project['id']}/repositories"

    async def code(path: str) -> tuple[int, str]:
        response = await client.post(url, json={"local_path": path}, headers=admin)
        return response.status_code, response.json().get("error", {}).get("code", "")

    outside = Path(tempfile.mkdtemp(prefix="outside-"))
    create_sample_repository(outside / "r")
    assert await code(str(outside / "r")) == (400, "path_not_allowed")
    assert await code("relative/path") == (400, "invalid_repository")
    assert await code(str(_REPOS_ROOT)) == (400, "path_not_allowed")  # the root itself is not a repository
    assert await code(str(_REPOS_ROOT / "missing")) == (400, "invalid_repository")
    plain = _REPOS_ROOT / "plain-dir"
    plain.mkdir(exist_ok=True)
    assert await code(str(plain)) == (400, "not_a_git_repository")
    assert await code(str(repo_path / "app")) == (400, "not_a_git_repository")
    # A symlink inside the allowed root that points outside is resolved before the check.
    link = _REPOS_ROOT / "sneaky-link"
    if not link.exists():
        link.symlink_to(outside / "r")
    assert await code(str(link)) == (400, "path_not_allowed")

    unsafe = Path(tempfile.mkdtemp(prefix="unsafe-", dir=_REPOS_ROOT))
    unsafe.rmdir()
    create_sample_repository(unsafe)
    with (unsafe / ".git" / "config").open("a") as handle:
        handle.write("[core]\n\tfsmonitor = ./evil.sh\n")
    assert await code(str(unsafe)) == (400, "unsafe_git_config")

    ok = await client.post(url, json={"local_path": str(repo_path)}, headers=admin)
    assert ok.status_code == 201 and ok.json()["default_branch"] == "main"
    assert await code(str(repo_path)) == (409, "repository_exists")


async def test_only_owner_registers(
    client: httpx.AsyncClient, admin: dict[str, str], developer: dict[str, str], project: dict[str, Any], repo_path: Path
) -> None:
    await client.put(f"/projects/{project['id']}/members", json={"email": "dev@example.com", "role": "developer"}, headers=admin)
    response = await client.post(f"/projects/{project['id']}/repositories", json={"local_path": str(repo_path)}, headers=developer)
    assert response.status_code == 403


async def test_index_profile(client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any]) -> None:
    profile = repository["profile"]
    assert profile["kind"] == "fullstack"
    assert {"FastAPI", "Vue", "Vite", "Vitest", "pytest"} <= set(profile["frameworks"])
    assert profile["languages"]["python"] >= 6 and profile["languages"]["vue"] == 3 and profile["languages"]["typescript"] == 3
    assert {"framework": "pytest", "directory": "."} in profile["test_frameworks"]
    assert {"framework": "vitest", "directory": "frontend"} in profile["test_frameworks"]
    assert "app/main.py" in profile["entry_points"] and "frontend/src/main.ts" in profile["entry_points"]
    assert repository["head_commit"] and repository["last_indexed_at"]

    files = (await client.get(f"/repositories/{repository['id']}/files", headers=admin)).json()
    paths = {f["path"] for f in files["items"]}
    assert "app/pricing.py" in paths and ".env" not in paths and not any(p.startswith(".git/") for p in paths)
    assert next(f for f in files["items"] if f["path"] == "tests/test_pricing.py")["is_test"] is True


async def test_explorer_endpoints(client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any]) -> None:
    base = f"/repositories/{repository['id']}"
    tree = (await client.get(f"{base}/tree", headers=admin)).json()
    names = {e["name"] for e in tree["entries"]}
    assert {"app", "frontend", "tests", "README.md"} <= names and ".env" not in names and ".git" not in names
    assert tree["hidden_entries"] >= 2

    content = (await client.get(f"{base}/file", params={"path": "app/pricing.py"}, headers=admin)).json()
    assert "def apply_discount" in content["content"] and len(content["sha256"]) == 64

    for path, code in (
        (".env", "path_not_allowed"),
        ("../../etc/passwd", "path_not_allowed"),
        (".git/config", "path_not_allowed"),
        ("/etc/passwd", "path_not_allowed"),
    ):
        denied = await client.get(f"{base}/file", params={"path": path}, headers=admin)
        assert denied.status_code == 400 and denied.json()["error"]["code"] == code, path
    assert (await client.get(f"{base}/file", params={"path": "nope.py"}, headers=admin)).status_code == 404

    found = (await client.get(f"{base}/search", params={"q": "apply_discount"}, headers=admin)).json()
    assert {m["path"] for m in found["matches"]} == {"app/pricing.py", "tests/test_pricing.py"}
    secret = (await client.get(f"{base}/search", params={"q": "sup3r-s3cret"}, headers=admin)).json()
    assert secret["matches"] == []

    status = (await client.get(f"{base}/git/status", headers=admin)).json()
    assert status["branch"] == "main" and status["clean"] is True
    log = (await client.get(f"{base}/git/log", headers=admin)).json()
    assert log["commits"][0]["subject"] == "Initial sample project"


async def test_symbols_routes_relevance_and_context(client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any]) -> None:
    base = f"/repositories/{repository['id']}"
    symbols = (await client.get(f"{base}/symbols", params={"q": "apply_discount"}, headers=admin)).json()["items"]
    assert symbols[0]["path"] == "app/pricing.py" and symbols[0]["kind"] == "function"
    assert symbols[0]["signature"].startswith("def apply_discount(price: Decimal")
    components = (await client.get(f"{base}/symbols", params={"q": "LoginForm", "kind": "component"}, headers=admin)).json()["items"]
    assert components[0]["path"] == "frontend/src/components/LoginForm.vue" and components[0]["extra"]["emits"] == ["logged-in"]

    routes = (await client.get(f"{base}/routes", headers=admin)).json()["items"]
    found = {(r["method"], r["path"]) for r in routes}
    assert {("GET", "/health"), ("POST", "/login"), ("POST", "/register"), ("POST", "/orders/quote"), ("PAGE", "/login")} <= found

    ranked = (await client.get(f"{base}/relevant-files", params={"q": "Fix the bug in the customer login API"}, headers=admin)).json()[
        "items"
    ]
    top = [r["path"] for r in ranked[:4]]
    assert "app/auth.py" in top and "app/main.py" in top
    ranked = (await client.get(f"{base}/relevant-files", params={"q": "discount is calculated incorrectly"}, headers=admin)).json()["items"]
    assert ranked[0]["path"] == "app/pricing.py"

    context = (await client.get(f"{base}/context", headers=admin)).json()
    assert "FastAPI" in context["overview"] and "POST /login" in context["overview"] and "sup3r" not in context["overview"]


async def test_unused_code_detection(client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any]) -> None:
    result = (await client.get(f"/repositories/{repository['id']}/unused-code", headers=admin)).json()
    imports = {(i["path"], i["name"]) for i in result["unused_imports"]}
    assert ("app/pricing.py", "json") in imports
    assert ("frontend/src/api.ts", "ref") in imports
    assert ("frontend/src/components/LoginForm.vue", "ref") not in imports
    definitions = {(d["path"], d["name"]) for d in result["unused_definitions"]}
    assert ("app/pricing.py", "legacy_format_price") in definitions
    # Route handlers and functions that are referenced are not reported.
    assert not any(name in {"login", "health", "apply_discount", "authenticate"} for _, name in definitions)


async def test_reindex_is_incremental(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], repo_path: Path
) -> None:
    base = f"/repositories/{repository['id']}"
    (repo_path / "app" / "shipping.py").write_text("def shipping_cost(weight: float) -> float:\n    return weight * 2\n")
    (repo_path / "app" / "auth.py").unlink()
    assert (await client.post(f"{base}/index", headers=admin)).status_code == 202

    async def reindexed() -> Any:
        hits = (await client.get(f"{base}/symbols", params={"q": "shipping_cost"}, headers=admin)).json()["items"]
        return hits or None

    assert (await wait_for(reindexed))[0]["path"] == "app/shipping.py"
    assert (await client.get(f"{base}/symbols", params={"q": "authenticate"}, headers=admin)).json()["items"] == []


async def test_repository_isolation(client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any]) -> None:
    outsider = await register(client, "outsider@example.com")
    base = f"/repositories/{repository['id']}"
    for path in ("", "/tree", "/file?path=app/pricing.py", "/symbols?q=a", "/git/status", "/context", "/unused-code"):
        assert (await client.get(base + path, headers=outsider)).status_code == 404, path
    assert (await client.delete(base, headers=outsider)).status_code == 404


async def test_unregister_leaves_files(
    client: httpx.AsyncClient, admin: dict[str, str], repository: dict[str, Any], repo_path: Path
) -> None:
    assert (await client.delete(f"/repositories/{repository['id']}", headers=admin)).status_code == 204
    assert (repo_path / "app" / "pricing.py").exists()
    assert subprocess.run(["git", "-C", str(repo_path), "status", "--porcelain"], capture_output=True, check=True).stdout == b""


def test_symbol_extraction_details() -> None:
    py = extract_symbols(
        "x.py",
        "python",
        "import os\nfrom a.b import c as d\n\nclass A(Base):\n    async def m(self, x: int) -> str: ...\n\n@app.route('/r', methods=['PUT', 'POST'])\ndef r(): ...\n",
    )
    kinds = {(s.kind, s.name) for s in py}
    assert {
        ("import", "os"),
        ("import", "d"),
        ("class", "A"),
        ("method", "m"),
        ("function", "r"),
        ("route", "PUT /r"),
        ("route", "POST /r"),
    } <= kinds
    assert next(s for s in py if s.name == "m").signature == "async def m(self, x: int) -> str"
    ts = extract_symbols(
        "x.ts",
        "typescript",
        "import { a, type B as C } from './m'\nexport const add = (x: number, y: number): number => x + y\nexport default class Svc {\n  run() {}\n}\n"
        "export interface Shape { a: number }\nrouter.get('/items/:id', handler)\nfunction helper<T>(v: T) {\n  return v\n}\n",
    )
    names = {(s.kind, s.name) for s in ts}
    assert {
        ("function", "add"),
        ("class", "Svc"),
        ("class", "Shape"),
        ("route", "GET /items/:id"),
        ("function", "helper"),
        ("import", "a"),
        ("import", "C"),
    } <= names
    assert next(s for s in ts if s.name == "helper").end_line == 10
    assert extract_symbols("bad.py", "python", "def (:\n") == []

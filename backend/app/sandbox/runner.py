"""Isolated command execution.

``DockerSandbox`` runs each command in a throwaway container with no network, dropped
capabilities, a read-only root filesystem and resource limits. ``LocalSandbox`` is a
fallback for machines without Docker: it limits time, output and environment, but it does
NOT isolate the network or the filesystem, and must not be used with untrusted code.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

REPORT_PLACEHOLDER = "{report}"
_REPORT_BEGIN = "===ASEA-REPORT-BEGIN==="
_REPORT_END = "===ASEA-REPORT-END==="
_CONTAINER_REPORT = "/tmp/asea-report"  # noqa: S108 - path inside the sandbox container's private tmpfs


@dataclass(frozen=True)
class SandboxConfig:
    mode: Literal["auto", "docker", "local"] = "auto"
    image: str = "asea-sandbox:latest"
    memory_mb: int = 1024
    cpus: float = 1.0
    pids_limit: int = 256
    path_map: str = ""  # "container_prefix=host_prefix"
    max_output_bytes: int = 400_000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SandboxConfig:
        env = dict(os.environ) if env is None else env
        mode = env.get("SANDBOX_MODE", "auto")
        return cls(
            mode=mode if mode in ("auto", "docker", "local") else "auto",  # type: ignore[arg-type]
            image=env.get("SANDBOX_IMAGE", "asea-sandbox:latest"),
            memory_mb=int(env.get("SANDBOX_MEMORY_MB", "1024")),
            cpus=float(env.get("SANDBOX_CPUS", "1.0")),
            pids_limit=int(env.get("SANDBOX_PIDS_LIMIT", "256")),
            path_map=env.get("SANDBOX_PATH_MAP", ""),
        )

    def to_env(self) -> dict[str, str]:
        return {
            "SANDBOX_MODE": self.mode,
            "SANDBOX_IMAGE": self.image,
            "SANDBOX_MEMORY_MB": str(self.memory_mb),
            "SANDBOX_CPUS": str(self.cpus),
            "SANDBOX_PIDS_LIMIT": str(self.pids_limit),
            "SANDBOX_PATH_MAP": self.path_map,
        }


@dataclass
class SandboxRequest:
    argv: list[str]
    workdir: str = "."  # relative to the repository root, already validated by the caller
    timeout_seconds: float = 300.0
    network: bool = False
    writable: bool = False
    env: dict[str, str] = field(default_factory=dict)
    want_report: bool = False  # substitute {report} in argv and return that file's content
    prepare_python_env: bool = False


@dataclass
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    mode: str
    truncated: bool = False
    report: str | None = None
    error: str | None = None  # the sandbox itself failed to start

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


async def _read_capped(stream: asyncio.StreamReader | None, cap: int) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    size = 0
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if size < cap:
            chunks.append(chunk[: cap - size])
            size += len(chunks[-1])
            if len(chunk) > len(chunks[-1]):
                truncated = True
        else:
            truncated = True  # keep draining so the child is never blocked on a full pipe
    return b"".join(chunks), truncated


def _tail_biased(data: bytes, truncated: bool) -> str:
    text = data.decode("utf-8", errors="replace")
    return text + ("\n... [output truncated]" if truncated else "")


class Sandbox:
    mode = "base"

    def __init__(self, root: Path, config: SandboxConfig) -> None:
        self.root = Path(os.path.realpath(root))
        self.config = config

    async def run(self, request: SandboxRequest) -> SandboxResult:  # pragma: no cover - interface
        raise NotImplementedError

    async def _communicate(
        self, proc: asyncio.subprocess.Process, timeout: float, kill: contextlib.AbstractAsyncContextManager[None] | None = None
    ) -> tuple[bytes, bytes, bool, bool]:
        cap = self.config.max_output_bytes
        out_task = asyncio.create_task(_read_capped(proc.stdout, cap))
        err_task = asyncio.create_task(_read_capped(proc.stderr, cap))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
        except asyncio.CancelledError:
            await self._terminate(proc)
            out_task.cancel()
            err_task.cancel()
            raise
        if timed_out:
            await self._terminate(proc)
        (out, out_trunc), (err, err_trunc) = await asyncio.gather(out_task, err_task)
        return out, err, timed_out, out_trunc or err_trunc

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)


class LocalSandbox(Sandbox):
    """Restricted subprocess. No network or filesystem isolation."""

    mode = "local"

    def _env(self, workdir: Path, extra: dict[str, str]) -> dict[str, str]:
        path_parts: list[str] = []
        for base in (workdir, self.root):
            for candidate in (base / ".venv" / "bin", base / "venv" / "bin", base / "node_modules" / ".bin"):
                if candidate.is_dir() and str(candidate) not in path_parts:
                    path_parts.append(str(candidate))
        path_parts.append(str(Path(sys.executable).parent))
        node = shutil.which("node")
        if node:
            path_parts.append(str(Path(node).parent))
        path_parts += ["/usr/local/bin", "/usr/bin", "/bin"]
        env = {
            "PATH": os.pathsep.join(dict.fromkeys(path_parts)),
            "HOME": tempfile.gettempdir(),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "CI": "1",
            "NO_COLOR": "1",
            "npm_config_update_notifier": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
        }
        env.update(extra)
        return env

    async def run(self, request: SandboxRequest) -> SandboxResult:
        workdir = self.root / request.workdir if request.workdir != "." else self.root
        report_path: str | None = None
        argv = list(request.argv)
        tmpdir: str | None = None
        if request.want_report:
            tmpdir = tempfile.mkdtemp(prefix="asea-report-")
            report_path = os.path.join(tmpdir, "report")
            argv = [a.replace(REPORT_PLACEHOLDER, report_path) for a in argv]
        cpu_seconds = int(request.timeout_seconds) + 10

        def _limits() -> None:  # runs in the child before exec
            os.setsid()
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            fsize = 1024 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))

        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=self._env(workdir, request.env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=_limits,
            )
        except (FileNotFoundError, PermissionError) as exc:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            return SandboxResult(None, "", "", 0, False, self.mode, error=f"cannot start '{argv[0]}': {exc.strerror}")
        try:
            out, err, timed_out, truncated = await self._communicate(proc, request.timeout_seconds)
            report = None
            if report_path and os.path.isfile(report_path):
                with open(report_path, errors="replace") as handle:
                    report = handle.read(5_000_000)
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
        return SandboxResult(
            exit_code=None if timed_out else proc.returncode,
            stdout=_tail_biased(out, truncated),
            stderr=_tail_biased(err, False),
            duration_ms=int((time.perf_counter() - started) * 1000),
            timed_out=timed_out,
            mode=self.mode,
            truncated=truncated,
            report=report,
        )

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await super()._terminate(proc)


class DockerSandbox(Sandbox):
    mode = "docker"

    def _host_path(self, path: Path) -> str:
        """Translate a path seen by this process into the path the Docker daemon sees."""
        text = str(path)
        if "=" in self.config.path_map:
            inside, outside = self.config.path_map.split("=", 1)
            inside = inside.rstrip("/")
            if text == inside or text.startswith(inside + "/"):
                return outside.rstrip("/") + text[len(inside) :]
        return text

    @property
    def deps_volume(self) -> str:
        return "asea-deps-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:16]

    async def _deps_volume_exists(self) -> bool:
        """Read-only runs only mount the dependency volume if an approved install has created it."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "volume", "inspect", self.deps_volume, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return await asyncio.wait_for(proc.wait(), timeout=15) == 0
        except (TimeoutError, OSError):
            return False

    def build_command(self, request: SandboxRequest, name: str, *, mount_deps: bool = True) -> list[str]:
        cfg = self.config
        mount_mode = "rw" if request.writable else "ro"
        workdir = "/workspace" if request.workdir == "." else f"/workspace/{request.workdir}"
        env = {
            "HOME": "/tmp",  # noqa: S108
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "CI": "1",
            "NO_COLOR": "1",
            "npm_config_update_notifier": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
            "npm_config_cache": "/tmp/.npm",  # noqa: S108
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CACHE_DIR": "/tmp/.pip",  # noqa: S108
            **request.env,
        }
        if request.want_report:
            env["ASEA_REPORT_FILE"] = _CONTAINER_REPORT
        if request.prepare_python_env:
            env["ASEA_PREPARE_VENV"] = "1"
        command = [
            "docker", "run", "--rm", "--init", "--name", name,
            "--network", "bridge" if request.network else "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(cfg.pids_limit),
            "--memory", f"{cfg.memory_mb}m",
            "--memory-swap", f"{cfg.memory_mb}m",
            "--cpus", str(cfg.cpus),
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,exec,nosuid,size=1g,mode=1777",  # noqa: S108 - tmpfs private to the container
            "-v", f"{self._host_path(self.root)}:/workspace:{mount_mode}",
            "-w", workdir,
        ]  # fmt: skip
        if mount_deps:
            # Docker creates a missing named volume on first use, which is what an install needs.
            command += ["-v", f"{self.deps_volume}:/deps:{'rw' if request.writable else 'ro'}"]
        for key, value in env.items():
            command += ["-e", f"{key}={value}"]
        command.append(cfg.image)
        command += [a.replace(REPORT_PLACEHOLDER, _CONTAINER_REPORT) for a in request.argv]
        return command

    async def run(self, request: SandboxRequest) -> SandboxResult:
        name = f"asea-sbx-{uuid.uuid4().hex[:12]}"
        mount_deps = request.writable or await self._deps_volume_exists()
        command = self.build_command(request, name, mount_deps=mount_deps)
        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(*command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            return SandboxResult(None, "", "", 0, False, self.mode, error="docker executable not found")
        self._name = name
        out, err, timed_out, truncated = await self._communicate(proc, request.timeout_seconds + 15)
        stdout = out.decode("utf-8", errors="replace")
        stderr = err.decode("utf-8", errors="replace")
        report = None
        if _REPORT_BEGIN in stdout:
            head, _, rest = stdout.partition("\n" + _REPORT_BEGIN + "\n")
            body, _, tail = rest.partition("\n" + _REPORT_END)
            report, stdout = body, head + tail.lstrip("\n")
        error = None
        code = None if timed_out else proc.returncode
        # 125: docker itself failed (bad image, daemon error); 126/127: command not runnable in the image.
        if code == 125:
            error = "sandbox container could not be started: " + stderr.strip()[-500:]
        return SandboxResult(
            exit_code=code,
            stdout=stdout + ("\n... [output truncated]" if truncated else ""),
            stderr=stderr,
            duration_ms=int((time.perf_counter() - started) * 1000),
            timed_out=timed_out,
            mode=self.mode,
            truncated=truncated,
            report=report,
            error=error,
        )

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        name = getattr(self, "_name", None)
        if name:
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec("docker", "kill", name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), timeout=15)
        await super()._terminate(proc)


_docker_cache: dict[str, tuple[float, bool, str]] = {}


def docker_status(image: str, ttl: float = 30.0) -> tuple[bool, str]:
    """Whether the Docker daemon is reachable and the sandbox image exists (cached briefly)."""
    cached = _docker_cache.get(image)
    now = time.monotonic()
    if cached and now - cached[0] < ttl:
        return cached[1], cached[2]
    ok, detail = False, ""
    if shutil.which("docker") is None:
        detail = "docker executable not found"
    else:
        try:
            proc = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True, timeout=10, check=False
            )
            if proc.returncode == 0:
                ok, detail = True, "ready"
            else:
                message = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["unavailable"]
                detail = (
                    f"image '{image}' not found; build it with: docker compose --profile build build sandbox-image"
                    if "No such image" in message[0]
                    else f"docker unavailable: {message[0][:200]}"
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            detail = f"docker unavailable: {exc}"
    _docker_cache[image] = (now, ok, detail)
    return ok, detail


def resolve_mode(config: SandboxConfig) -> tuple[str, str]:
    """Effective sandbox mode and a human-readable explanation."""
    if config.mode == "local":
        return "local", "local mode configured (no network or filesystem isolation)"
    ok, detail = docker_status(config.image)
    if ok:
        return "docker", "docker sandbox ready"
    if config.mode == "docker":
        return "unavailable", detail
    return "local", f"falling back to local mode ({detail}); no network or filesystem isolation"


def create_sandbox(root: Path, config: SandboxConfig) -> Sandbox:
    mode, detail = resolve_mode(config)
    if mode == "docker":
        return DockerSandbox(root, config)
    if mode == "local":
        return LocalSandbox(root, config)
    raise RuntimeError(f"Sandbox is not available: {detail}")

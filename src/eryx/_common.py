"""Shared project I/O, subprocess and output contracts for Eryx."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any, Iterator

SCHEMA = "eryx-report/v1"
MIN_PYTHON = (3, 12)


class EryxError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def tool_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def tool_binary(name: str) -> str:
    """Find a required executable beside the selected project interpreter.

    Args:
        name (str): Executable filename.

    Returns:
        str: Absolute path to the executable.

    Raises:
        EryxError: The executable is absent from the interpreter environment.
    """
    path = Path(sys.executable).parent / name
    if not path.is_file():
        raise EryxError("MISSING_TOOL", f"Missing {name} in project environment; "
                        "run uv sync --locked --group dev.")
    return str(path)


def project_dir(value: str | Path) -> Path:
    root = Path(value).resolve()
    if not root.is_dir() or not (root / "pyproject.toml").is_file():
        raise EryxError("NOT_A_PROJECT", f"Expected a project with pyproject.toml: {root}")
    return root


def tool_revision() -> dict[str, Any]:
    """Identify this checkout, never the enclosing consumer repository."""
    root = tool_dir()
    result = capture(["git", "rev-parse", "--show-toplevel"], root, timeout=10)
    if result["exit_code"] != 0 or Path(result["stdout"].strip()).resolve() != root:
        return {"commit": None, "dirty": None}
    head = capture(["git", "rev-parse", "HEAD"], root, timeout=10)
    status = capture(["git", "status", "--porcelain", "--untracked-files=all"], root, timeout=10)
    return {"commit": head["stdout"].strip() if head["exit_code"] == 0 else None,
            "dirty": bool(status["stdout"]) if status["exit_code"] == 0 else None}


def require_runtime() -> None:
    if not sys.platform.startswith("linux"):
        raise EryxError("UNSUPPORTED_OS", "Only Linux is supported.")
    if sys.version_info < MIN_PYTHON:
        raise EryxError("UNSUPPORTED_PYTHON", "Use a project interpreter >= Python 3.12.")


def load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise EryxError("INVALID_CONFIG", f"Cannot read {path}: {exc}") from exc


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EryxError("INVALID_JSON", f"Cannot read {path}: {exc}") from exc


def safe_target(path: Path, root: Path) -> None:
    """Do not follow repository symlinks when writing managed files."""
    root = root.resolve()
    try:
        rel = path.absolute().relative_to(root)
    except ValueError as exc:
        raise EryxError("UNSAFE_PATH", f"Path is outside the repository: {path}") from exc
    cursor = root
    for part in rel.parts:
        if part in (".", ".."):
            raise EryxError("UNSAFE_PATH", str(path))
        cursor /= part
        if cursor.is_symlink():
            raise EryxError("SYMLINK_TARGET", f"Refusing to overwrite a symlink: {cursor}")


def atomic_write(path: Path, content: str | bytes, root: Path | None = None,
                 mode: int | None = None) -> bool:
    if root is not None:
        safe_target(path, root)
    data = content.encode("utf-8") if isinstance(content, str) else content
    if path.exists() and path.read_bytes() == data:
        if mode is not None:
            path.chmod(mode)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, previous_mode if mode is None else mode)
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
    return True


def write_json(path: Path, data: Any, root: Path | None = None) -> bool:
    return atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", root)


@contextlib.contextmanager
def repo_lock(root: Path) -> Iterator[None]:
    # All tool checkouts agree on a consumer-owned lock; never write into Eryx.
    lockfile = root / ".eryx/cache/operation.lock"
    safe_target(lockfile, root)
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EryxError("REPOSITORY_BUSY", "Another Eryx operation is running in this repository.") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def command_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Bootstrap uv's temporary environment is not the consumer's environment.
    env.pop("VIRTUAL_ENV", None)
    # Redirect analyzer caches without moving uv's potentially large package cache.
    env.setdefault("UV_CACHE_DIR", str(Path(env.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "uv"))
    env.update({"NO_COLOR": "1", "TERM": "dumb", "PYTHONUTF8": "1", "UV_NO_PROGRESS": "1",
                "PYRIGHT_PYTHON_FORCE_VERSION": "1.1.414", "PYTHONDONTWRITEBYTECODE": "1",
                "XDG_CACHE_HOME": str(root / ".eryx/cache"),
                "PYLINTHOME": str(root / ".eryx/cache/pylint")})
    env["PYTHONPATH"] = str(root / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def capture(argv: list[str], cwd: Path, timeout: int = 600,
            env: dict[str, str] | None = None) -> dict[str, Any]:
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env or command_env(cwd),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except OSError as exc:
        return {"argv": argv, "exit_code": None, "stdout": "", "stderr": str(exc),
                "duration_seconds": 0, "error": "TOOL_START_FAILED"}
    error = None
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        error = "TOOL_TIMEOUT"
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        raise
    return {"argv": argv, "exit_code": proc.returncode, "stdout": stdout, "stderr": stderr,
            "duration_seconds": round(time.monotonic() - start, 3), "error": error}


def run_required(argv: list[str], cwd: Path, timeout: int = 900) -> dict[str, Any]:
    result = capture(argv, cwd, timeout)
    if result["error"] or result["exit_code"] != 0:
        detail = (result["stderr"] or result["stdout"])[-8000:]
        raise EryxError(result["error"] or "COMMAND_FAILED", f"{' '.join(argv)}\n{detail}")
    return result


def emit_event(status: str, *, json_output: bool, **fields: Any) -> None:
    data = {"schema_version": "eryx-event/v1", "status": status, **fields}
    if json_output:
        print(json.dumps(data, ensure_ascii=False, sort_keys=True))
    else:
        print(status.upper())
        for key, value in fields.items():
            print(f"{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}")


def error_exit(exc: Exception, json_output: bool = False) -> int:
    emit_event("error", json_output=json_output,
               code=getattr(exc, "code", "INTERNAL_ERROR"), message=str(exc), exit_code=2)
    return 2

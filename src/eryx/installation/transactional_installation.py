"""Apply installation steps with rollback of consumer-owned managed files."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
from types import ModuleType
from typing import Any, override

from eryx._common import (EryxError, atomic_write, load_toml, read_json,
                          require_runtime, safe_target, write_json)
from eryx.installation.installation import Installation


class TransactionalInstallation(Installation):
    """Install pinned tools and leave a failure log after restoring managed files."""

    def __init__(self, entry: ModuleType) -> None:
        super().__init__()
        self._entry: ModuleType = entry

    @override
    def plan(self, root: Path, tool: Path, *, extras: list[str] | None = None
             ) -> dict[str, Any]:
        entry = self._entry
        specs = entry.dependency_specs(tool)
        previous = (read_json(root / ".eryx/installation.json")
                    if (root / ".eryx/installation.json").exists() else {})
        selected_extras = sorted(set(extras if extras is not None else previous.get("extras", [])))
        available = load_toml(root / "pyproject.toml")["project"].get("optional-dependencies", {})
        if any(extra not in available for extra in selected_extras):
            raise EryxError("UNKNOWN_EXTRA", f"Unknown project extra in {selected_extras}")
        managed = sorted([f".eryx/{name}" for name in entry.STATE_FILES] + [
            "pyproject.toml", "pyrightconfig.json", "uv.lock", "tach.toml", ".gitignore",
        ])
        return {"project": str(root), "dependencies": specs, "extras": selected_extras,
                "managed_files": managed,
                "architecture_initialization": "separate; no source layout is changed"}

    @override
    def install(self, root: Path, tool: Path, *, dry_run: bool = False,
                progress: bool = True, extras: list[str] | None = None) -> dict[str, Any]:
        entry = self._entry
        require_runtime()
        entry.validate_project(root)
        uv = shutil.which("uv")
        if not uv:
            raise EryxError("UV_NOT_FOUND", "uv must already be installed and available on PATH.")
        plan = self.plan(root, tool, extras=extras)
        saved = entry.snapshot(root, plan["managed_files"])
        specs = plan["dependencies"]
        selected_extras = plan["extras"]
        extra_args = [arg for extra in selected_extras for arg in ("--extra", extra)]
        if dry_run:
            return {"status": "planned", **plan, "exit_code": 0}
        safe_target(root / ".eryx/logs/install-probes", root)
        safe_target(root / ".eryx/logs/install.log", root)
        log_parts = []
        steps = ("resolve dependencies", "sync project environment",
                 "probe native tools and generate configuration")
        try:
            if progress:
                print("Installing Eryx configuration and exact dev dependencies...",
                      file=sys.stderr)
            entry.initialize_state(root, tool)
            from eryx.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
            policy = ConfiguredConsumerPolicy(root)
            policy.constructor_purity()
            if not policy.files()["production"]:
                raise EryxError("EMPTY_SOURCE_SCOPE", "No Python files under src/.")
            entry.prepare_project(root)
            commands = [
                [uv, "add", "--dev", "--no-sync", "--python", ">=3.12", *specs],
                [uv, "sync", "--locked", "--group", "dev", "--python", ">=3.12", *extra_args],
                [uv, "run", "--locked", "--no-sync", "--group", "dev", *extra_args,
                 "python", str(tool / "_audit.py"), "--project", str(root), "--json"],
            ]
            for index, argv in enumerate(commands, 1):
                if progress:
                    print(f"Step {index}/3: {steps[index - 1]}", file=sys.stderr)
                result = entry.capture(argv, root, timeout=1200)
                log_parts.append("$ " + " ".join(argv) + "\n" + result["stdout"]
                                 + "\n" + result["stderr"])
                atomic_write(root / ".eryx/logs/install.log", "\n\n".join(log_parts), root)
                if result["error"] or result["exit_code"] != 0:
                    raise EryxError(result["error"] or "INSTALL_STEP_FAILED",
                        f"Step {index} failed (exit {result['exit_code']}). "
                        + "See .eryx/logs/install.log and .eryx/logs/install-probes/.\n"
                        + (result["stdout"] + "\n" + result["stderr"])[-8000:])
            ignore_path = root / ".gitignore"
            old_ignore = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
            additions = [line for line in (
                "/.eryx/reports/", "/.eryx/logs/", "/.eryx/cache/", "/.tach/",
            ) if line not in old_ignore.splitlines()]
            if additions:
                atomic_write(ignore_path, old_ignore.rstrip("\n")
                             + ("\n" if old_ignore else "")
                             + "\n# Local Eryx reports, logs and native cache\n"
                             + "\n".join(additions) + "\n", root)
            audit_result = read_json(root / ".eryx/logs/install-probes/result.json")
            marker = root / ".eryx/architecture.ready.json"
            installation = {
                "eryx_version": read_json(tool / "tools.json")["eryx_version"],
                "extras": selected_extras, "dependencies": specs, "source_layout": "src",
                "mode": "strict",
                "architecture_initialization_required": not (
                    marker.exists() and read_json(marker).get("accepted")),
            }
            write_json(root / ".eryx/installation.json", installation, root)
            modified = [name for name, old in saved.items()
                        if ((root / name).read_bytes() if (root / name).exists() else None)
                        != (old[0] if old else None)]
            return {
                "status": "installed", "project": str(root), "modified_files": sorted(modified),
                "versions": audit_result["versions"], "smoke_tests": audit_result["smoke_tests"],
                "architecture_initialization_required": installation[
                    "architecture_initialization_required"],
                "instructions": str(tool / "README.md"),
                "agent_instructions": str(tool / "AGENTS.md"),
                "next_command": "uv run --locked --group dev python eryx/init_architecture.py",
                "exit_code": 0,
            }
        except BaseException as exc:
            errors = entry.rollback(root, saved)
            error_data = {
                "status": "error", "code": getattr(exc, "code", "INSTALL_FAILED"),
                "message": str(exc), "managed_files_rolled_back": not errors,
                "rollback_errors": errors,
                "environment_note": "The virtual environment/cache may have changed. "
                                    + "Re-sync the restored project with uv sync.",
                "exit_code": 2,
            }
            try:
                write_json(root / ".eryx/logs/install-error.json", error_data, root)
            except Exception:
                pass
            if errors:
                raise EryxError("ROLLBACK_INCOMPLETE", str(exc)
                                + "; rollback failures: " + "; ".join(errors)) from exc
            raise

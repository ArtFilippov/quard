"""An isolated Git-pinned runtime consumer for qraft regressions."""

from pathlib import Path
import shutil
import subprocess


def consumer(root: Path) -> tuple[Path, str]:
    """Install qraft from a committed public-style Git subdirectory.

    Args:
        root (Path): Temporary parent for the tool clone and consumer.

    Returns:
        tuple[Path, str]: Consumer path and pinned Git revision.
    """
    tool = root / "tool"
    shutil.copytree(Path(__file__).resolve().parents[1] / "runtime/qraft",
                    tool / "runtime/qraft")
    subprocess.run(["git", "init", "-q", str(tool)], check=True)
    subprocess.run(["git", "add", "."], cwd=tool, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.org", "commit", "-qm", "fixture"],
                   cwd=tool, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tool,
                                       text=True).strip()
    project = root / "consumer"
    project.mkdir()
    (project / "pyproject.toml").write_text(f'''\
[project]
name = "consumer"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["qraft==0.1.0"]
[tool.uv]
package = false
[tool.uv.sources]
qraft = {{ git = "{tool.as_uri()}", rev = "{revision}", subdirectory = "runtime/qraft" }}
''', encoding="utf-8")
    subprocess.run(["uv", "lock"], cwd=project, capture_output=True, text=True,
                   check=True)
    subprocess.run(["uv", "sync", "--locked"], cwd=project, capture_output=True,
                   text=True, check=True)
    return project, revision

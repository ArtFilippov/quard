"""Release matrix checks reject mislabeled and incomplete binary archives."""

from __future__ import annotations

from importlib import util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = util.spec_from_file_location("verify_clone_wheels", ROOT / "scripts/verify_clone_wheels.py")
assert spec is not None and spec.loader is not None
release = util.module_from_spec(spec)
spec.loader.exec_module(release)


class TestCloneWheelRelease(unittest.TestCase):
    def test_complete_binary_matrix_and_missing_target(self) -> None:
        with TemporaryDirectory(dir="/tmp/opencode") as tmp:
            directory = Path(tmp)
            for libc, arch in release.TARGETS:
                tag = f"cp312-abi3-{libc}_2_17_{arch}"
                path = directory / f"quard_clones-0.1.0-{tag}.whl"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("quard_clones/_native.abi3.so", b"binary")
                    archive.writestr("quard_clones-0.1.0.dist-info/WHEEL", f"Tag: {tag}\n")
            release.verify(directory, release.TARGETS)
            path.unlink()
            with self.assertRaisesRegex(ValueError, "Missing wheel targets"):
                release.verify(directory, release.TARGETS)

    def test_incompatible_abi_and_missing_native_extension(self) -> None:
        with TemporaryDirectory(dir="/tmp/opencode") as tmp:
            directory = Path(tmp)
            tag = "cp312-cp312-manylinux_2_17_x86_64"
            path = directory / f"quard_clones-0.1.0-{tag}.whl"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("quard_clones/_native.so", b"binary")
                archive.writestr("quard_clones-0.1.0.dist-info/WHEEL", f"Tag: {tag}\n")
            with self.assertRaisesRegex(ValueError, "Unexpected/duplicate wheel target"):
                release.verify(directory, {("manylinux", "x86_64")})
            path.unlink()
            tag = "cp312-abi3-manylinux_2_17_x86_64"
            path = directory / f"quard_clones-0.1.0-{tag}.whl"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("quard_clones-0.1.0.dist-info/WHEEL", f"Tag: {tag}\n")
            with self.assertRaisesRegex(ValueError, "Missing native extension"):
                release.verify(directory, {("manylinux", "x86_64")})

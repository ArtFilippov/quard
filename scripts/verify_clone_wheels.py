"""Fail a release if the binary matrix is incomplete or mislabeled."""

from __future__ import annotations

import argparse
from pathlib import Path
import zipfile

from packaging.utils import parse_wheel_filename

TARGETS = {(libc, arch) for libc in ("manylinux", "musllinux")
           for arch in ("x86_64", "aarch64")}


def verify(directory: Path, targets: set[tuple[str, str]]) -> None:
    """Inspect each artifact, extension and ABI tag; require exactly one per target."""
    seen: set[tuple[str, str]] = set()
    for wheel in directory.glob("*.whl"):
        name, version, _, tags = parse_wheel_filename(wheel.name)
        if name != "quard-clones" or str(version) != "0.1.0":
            raise ValueError(f"Unexpected distribution: {wheel.name}")
        matched = {(libc, arch) for libc, arch in targets if any(
            tag.interpreter == "cp312" and tag.abi == "abi3"
            and tag.platform.startswith(libc + "_")
            and tag.platform.endswith("_" + arch) for tag in tags)}
        if len(matched) != 1 or seen.intersection(matched):
            raise ValueError(f"Unexpected/duplicate wheel target: {wheel.name}")
        with zipfile.ZipFile(wheel) as archive:
            if not any(path.startswith("quard_clones/_native") and path.endswith(".so")
                       for path in archive.namelist()):
                raise ValueError(f"Missing native extension: {wheel.name}")
            metadata = [path for path in archive.namelist() if path.endswith(".dist-info/WHEEL")]
            if len(metadata) != 1:
                raise ValueError(f"Missing wheel metadata: {wheel.name}")
            lines = archive.read(metadata[0]).decode("utf-8").splitlines()
            recorded = {line[5:].strip() for line in lines if line.startswith("Tag: ")}
            if not recorded or not recorded.intersection(str(tag) for tag in tags):
                raise ValueError(f"Wheel filename/metadata tags disagree: {wheel.name}")
        seen.update(matched)
    if seen != targets:
        raise ValueError(f"Missing wheel targets: {sorted(targets - seen)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--arch", choices=("x86_64", "aarch64"))
    parser.add_argument("--libc", choices=("manylinux", "musllinux"))
    args = parser.parse_args()
    if bool(args.arch) != bool(args.libc):
        parser.error("--arch and --libc must be supplied together")
    verify(args.directory, {(args.libc, args.arch)} if args.arch else TARGETS)

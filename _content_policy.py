"""Path-only content exclusion policy shared by the anonymity workers and runner."""
from pathlib import PurePath


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".gz", ".bz2",
                    ".tar.bz2", ".tbz2", ".xz", ".tar.xz", ".txz",
                    ".7z", ".rar", ".zst", ".tar.zst")


def content_excluded(path: str) -> bool:
    """Decide from the filename alone, without reading or resolving the input."""
    name = PurePath(path).name.casefold()
    return name.endswith((".pth", *ARCHIVE_SUFFIXES))

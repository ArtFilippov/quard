"""Create a public, executable generated-policy consumer for full-check tests."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory

from offline_fixtures import create_repo
from quard._common import write_json

discover_plugins = import_module("quard.audit_utils").discover_plugins
configure = import_module("quard.policy_utils").configure
IDENTITY = import_module("quard.architecture_draft.generated_selection_utils").IDENTITY


def adoption_consumer(root: Path) -> None:
    """Create a small, executable, multi-package consumer.

    Args:
        root (Path): Temporary consumer root.
    """
    create_repo(root, ready=False)
    (root / "src/demo/domain/api.py").write_text("")
    (root / "tests/test_value.py").write_text("")
    settings = root / ".quard/config.toml"
    settings.write_text(settings.read_text().replace("dependency_types = false",
                                                     "dependency_types = true"))
    write_json(root / ".quard/architecture.generated.json", IDENTITY)
    for name in ("placeholder", "demo/records", "demo/records/envelopes", "demo/main",
                 "gateway", "gateway/client"):
        folder = root / "src" / name
        folder.mkdir(parents=True)
        (folder / "__init__.py").write_text("")
    group = root / "src/demo/records"
    (group / "service.py").write_text(
        "from typing import Protocol\n\nclass Service(Protocol):\n" +
        "    def first(self, value: str) -> str: ...\n" +
        "    def second(self, value: str) -> str: ...\n")
    sender = ("from typing import override\nfrom demo.records.service import Service\n\n"
              + "class Sender(Service):\n"
              + "    def __init__(self) -> None:\n        super().__init__()\n"
              + "        self._prefix: str = 'ready:'\n\n"
              + "    @override\n    def first(self, value: str) -> str:\n"
              + "        return self.second(value)\n\n"
              + "    @override\n    def second(self, value: str) -> str:\n"
              + "        return self._prefix + value\n")
    (group / "sender.py").write_text(sender)
    (group / "sender.pyi").write_text(
        "from typing import override\nfrom demo.records.service import Service\n\n" +
        "class Sender(Service):\n" +
        "    @override\n    def first(self, value: str) -> str: ...\n" +
        "    @override\n    def second(self, value: str) -> str: ...\n")
    (group / "identity_shape.py").write_text(
        "from typing import Protocol\n\nclass IdentityShape(Protocol):\n" +
        "    @property\n    def code(self) -> str: ...\n")
    (group / "entry_shape.py").write_text(
        "from typing import Protocol\nfrom demo.records.identity_shape import IdentityShape\n\n" +
        "class EntryShape(Protocol):\n" +
        "    @property\n    def identity(self) -> IdentityShape: ...\n")
    (group / "identity.py").write_text(
        "from typing import NamedTuple\n\nIdentity = NamedTuple('Identity', [('code', str)])\n")
    entry = ("from typing import NamedTuple\n"
             + "from demo.records.identity_shape import IdentityShape\n\n"
             + "Entry = NamedTuple('Entry', [('identity', IdentityShape)])\n")
    (group / "entry.py").write_text(entry)
    (group / "entry.pyi").write_text(entry)
    (group / "external_items.py").write_text(
        "from collections import UserList\n\nclass ExternalItems(UserList[str]):\n" +
        "    def first(self) -> str:\n        return self.last()\n\n" +
        "    def last(self) -> str:\n        return self[0]\n")
    (group / "external_items.pyi").write_text(
        "from collections import UserList\n\nclass ExternalItems(UserList[str]):\n" +
        "    def first(self) -> str: ...\n    def last(self) -> str: ...\n")
    (group / "__init__.py").write_text(
        "from .service import Service as Port\n__all__ = ['Port']\n")
    (group / "__init__.pyi").write_text((group / "__init__.py").read_text())
    (root / "src/demo/__init__.py").write_text(
        "from .records import Port as PublicPort\n__all__ = ['PublicPort']\n")
    (group / "envelopes/adapter.py").write_text(
        "from typing import override\nfrom demo.records.service import Service\n" +
        "from demo.records.sender import Sender\n\nclass Adapter(Service):\n" +
        "    @override\n    def first(self, value: str) -> str:\n" +
        "        return Sender().first(value)\n\n" +
        "    @override\n    def second(self, value: str) -> str:\n" +
        "        return self.first(value + '!')\n")
    (root / "src/demo/main/wire.py").write_text(
        "from demo.records.envelopes.adapter import Adapter\n" +
        "from demo.records.sender import Sender\n\n" +
        "def connect() -> str:\n    return Adapter().first(Sender().second('ok'))\n")
    (root / "src/gateway/client/value_utils.py").write_text(
        "from demo import PublicPort\n" +
        "import demo.records.entry_shape as shape\n\n" +
        "def read_value(value: shape.EntryShape, port: PublicPort) -> str:\n" +
        "    return port.first(value.identity.code)\n")
    (root / "src/gateway/client/value_utils.pyi").write_text(
        "from demo import PublicPort\n" +
        "import demo.records.entry_shape as shape\n\n" +
        "def read_value(value: shape.EntryShape, port: PublicPort) -> str: ...\n")
    (root / "src/gateway/client/main.py").write_text("")
    tests = root / "tests/demo/records"
    tests.mkdir(parents=True)
    (tests / "test_sender.py").write_text(
        "import unittest\nfrom demo.records.sender import Sender\n\n" +
        "class TestSender(unittest.TestCase):\n" +
        "    def test_first(self) -> None:\n" +
        "        self.assertEqual(Sender().first('ok'), 'ready:ok')\n")
    logs = root / ".quard/logs/install-probes"
    logs.mkdir(parents=True)
    plugins, catalog = discover_plugins(root, logs)
    write_json(root / ".quard/pylint-catalog.json", catalog)
    configure(root, initial=True, plugins=plugins, catalog=catalog)


@contextmanager
def temporary_consumer() -> Generator[Path]:
    """Provide a public adoption consumer in an isolated temporary directory.

    Yields:
        Path: Temporary consumer root.
    """
    with TemporaryDirectory(prefix="quard-public-adoption-") as directory:
        root = Path(directory)
        adoption_consumer(root)
        yield root

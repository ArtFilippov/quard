"""Real-Pylint regressions for the pinned architecture plugin's semantic bugs."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest


ERYX = Path(__file__).resolve().parents[1]
RULES = ["W9001", "W9003", "W9004", "W9006", "W9007", "W9009", "W9201", "W9202"]


class ArchitectureCompatTest(unittest.TestCase):
    def lint(self, files, layers, *, rules=RULES, patched=True, extra="", pylint_extra=""):
        with TemporaryDirectory(prefix="architecture-compat-") as directory:
            root = Path(directory)
            paths = []
            for name, code in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(textwrap.dedent(code).lstrip(), encoding="utf-8")
                paths.append(str(path))
            config = ('[tool.pylint.main]\nload-plugins = ["clean_architecture_linter"]\n'
                      'persistent = false\n[tool.pylint.messages_control]\ndisable = ["all"]\n'
                      f'enable = {json.dumps(rules + ["fatal", "astroid-error"])}\n'
                      + pylint_extra + '\n'
                      '[tool.clean-arch]\nproject_type = "generic"\n' + extra + '\n')
            for module, layer in layers.items():
                config += f'[[tool.clean-arch.layers]]\nmodule = "{module}"\nname = "{layer}"\n'
            (root / "pyproject.toml").write_text(config, encoding="utf-8")
            setup = (f'sys.path.insert(0, {str(ERYX)!r}); '
                     'from _architecture_compat import install_architecture_compat; '
                     'install_architecture_compat(); install_architecture_compat(); ') if patched else ""
            # A fresh process also isolates the plugin's singleton configuration.
            result = subprocess.run(
                [sys.executable, "-c", 'import sys; ' + setup
                 + 'from pylint.lint import Run; Run(sys.argv[1:])',
                 "--rcfile=pyproject.toml", "--output-format=json2", "--reports=no", "--score=no",
                 "--jobs=1", *paths],
                cwd=root, capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertIn(result.returncode, (0, 2, 4, 6, 8, 10, 12, 14), result.stdout + result.stderr)
            messages = json.loads(result.stdout)["messages"]
            self.assertFalse([m for m in messages if m["messageId"].startswith("F")], messages)
            return messages

    def test_inner_imports_and_future_features_use_explicit_layers(self):
        files = {
            "sample/__init__.py": "",
            "sample/contracts.py": "class Message: pass\n",
            "sample/consumer.py": """
                from __future__ import annotations
                from .contracts import Message
                import sample.contracts
                from sample import contracts
                import socket
                import external_io
            """,
            "external_io.py": "import socket\n",
        }
        layers = {"sample": "Domain"}
        native = self.lint(files, layers, rules=["W9004"], patched=False)
        fixed = self.lint(files, layers, rules=["W9004"])
        self.assertGreater(len(native), len(fixed))
        self.assertEqual([(m["line"], m["messageId"]) for m in fixed],
                         [(5, "W9004"), (6, "W9004")])
        self.assertIn("import socket", fixed[0]["message"])

    def test_relative_outer_imports_and_similarly_named_packages_still_fail(self):
        files = {
            "sample/__init__.py": "",
            "sample/consumer.py": """
                from . import transport
                from .transport import Client
                import sample.core_io
            """,
            "sample/transport.py": "class Client: pass\n",
            "sample/core_io.py": "",
        }
        layers = {"sample": "Domain", "sample.transport": "Infrastructure",
                  "sample.core": "Domain", "sample.core_io": "Infrastructure"}
        messages = self.lint(files, layers, rules=["W9001", "W9004"])
        self.assertEqual(sorted((m["line"], m["messageId"]) for m in messages),
                         [(line, rule) for line in range(1, 4) for rule in ("W9001", "W9004")])
        for message in messages:
            if message["messageId"] == "W9001":
                self.assertIn("Domain layer cannot import from Infrastructure", message["message"])

    def test_explicit_layer_overrides_import_directory_convention(self):
        files = {"sample/__init__.py": "", "sample/domain/__init__.py": "",
                 "sample/domain/transport.py": "class Driver: pass\n",
                 "sample/consumer.py": "from sample.domain.transport import Driver\n"}
        layers = {"sample": "Domain", "sample.domain.transport": "Infrastructure"}
        messages = self.lint(files, layers, rules=["W9001", "W9004"])
        self.assertEqual(sorted(m["messageId"] for m in messages), ["W9001", "W9004"])

    def test_explicit_inner_layer_beats_outer_directory_name(self):
        files = {"sample/__init__.py": "", "sample/infrastructure/__init__.py": "",
                 "sample/infrastructure/contracts.py": "class Entity: pass\n",
                 "sample/consumer.py": "from sample.infrastructure.contracts import Entity\n"}
        layers = {"sample": "Domain", "sample.infrastructure.contracts": "Domain"}
        native = self.lint(files, layers, rules=["W9001"], patched=False)
        self.assertEqual([m["messageId"] for m in native], ["W9001"])
        self.assertEqual(self.lint(files, layers, rules=["W9001"]), [])

    def test_layer_prefixes_end_at_a_module_segment(self):
        files = {"sample/__init__.py": "", "sample/core.py": "",
                 "sample/core_io.py": "class Entity: pass\n",
                 "sample/consumer.py": "from sample.core_io import Entity\n"}
        layers = {"sample": "Domain", "sample.core": "Interface"}
        native = self.lint(files, layers, rules=["W9001"], patched=False)
        self.assertEqual([m["messageId"] for m in native], ["W9001"])
        self.assertEqual(self.lint(files, layers, rules=["W9001"]), [])

    def test_native_dependency_matrix_is_not_relaxed_for_interfaces(self):
        files = {"sample/__init__.py": "", "sample/driver.py": "class Driver: pass\n",
                 "sample/entry.py": "from .driver import Driver\n"}
        layers = {"sample.driver": "Infrastructure", "sample.entry": "Interface"}
        messages = self.lint(files, layers, rules=["W9001"])
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("Interface layer cannot import from Infrastructure", messages[0]["message"])

    def test_explicit_bootstrap_wires_layers_but_repository_cannot_leak_raw_values(self):
        files = {
            "sample/__init__.py": "",
            "sample/entities.py": "class Entity: pass\n",
            "sample/use.py": "class UseCase: pass\n",
            "sample/views.py": "class View: pass\n",
            "sample/io.py": "class Connection: pass\n",
            "sample/repository.py": """
                from .io import Connection
                class Repository:
                    def fetch(self): return Connection()
            """,
            "sample/compose.py": """
                from .entities import Entity
                from .use import UseCase
                from .views import View
                from .io import Connection
                from . import wiring
                def create(): return Connection()
            """,
            "sample/wiring.py": "",
        }
        layers = {"sample.entities": "Domain", "sample.use": "UseCase", "sample.views": "Interface",
                  "sample.io": "Infrastructure", "sample.repository": "Infrastructure",
                  "sample.compose": "Bootstrap", "sample.wiring": "Bootstrap"}
        native = self.lint(files, layers, rules=["W9001", "W9007"], patched=False)
        messages = self.lint(files, layers, rules=["W9001", "W9007"])
        self.assertGreater(len(native), len(messages))
        self.assertEqual([(m["messageId"], m["obj"]) for m in messages],
                         [("W9007", "Repository.fetch")])
        self.assertIn(messages[0]["message"], [m["message"] for m in native])

    def test_no_inner_or_outer_layer_can_depend_on_bootstrap(self):
        files = {"sample/__init__.py": "", "sample/compose.py": "def create(): return 1\n"}
        layers = {"sample.compose": "Bootstrap"}
        for layer in ("Domain", "UseCase", "Interface", "Infrastructure"):
            module = layer.lower()
            files[f"sample/{module}.py"] = "from .compose import create\n"
            layers[f"sample.{module}"] = layer
        messages = self.lint(files, layers, rules=["W9001"])
        self.assertEqual(len(messages), 4, messages)
        for message in messages:
            self.assertIn("cannot import from Bootstrap layer", message["message"])

    def test_composition_spelling_alone_does_not_exempt_raw_returns(self):
        files = {"composition.py": """
            class Connection: pass
            def create(): return Connection()
        """}
        messages = self.lint(files, {"composition": "Interface"}, rules=["W9007"])
        self.assertEqual([(m["messageId"], m["obj"]) for m in messages], [("W9007", "create")])

    def test_bootstrap_repository_methods_still_enforce_the_domain_boundary(self):
        files = {
            "sample/__init__.py": "",
            "sample/contracts.py": """
                from typing import Protocol
                class Port(Protocol):
                    def fetch(self): ...
            """,
            "sample/io.py": "class Connection: pass\n",
            "sample/compose.py": """
                from .io import Connection
                from .contracts import Port
                class Factory:
                    def create(self): return Connection()
                class RecordsRepository:
                    def fetch(self): return Connection()
                class Implementation(Port):
                    def fetch(self): return Connection()
            """,
        }
        layers = {"sample.contracts": "Domain", "sample.io": "Infrastructure",
                  "sample.compose": "Bootstrap"}
        messages = self.lint(files, layers, rules=["W9007"])
        self.assertEqual([(m["messageId"], m["obj"]) for m in messages],
                         [("W9007", "RecordsRepository.fetch"), ("W9007", "Implementation.fetch")])

    def test_protocol_aliases_qualified_bases_and_inherited_contracts(self):
        files = {
            "sample/__init__.py": "",
            "sample/contracts.py": """
                import typing
                from typing import Protocol as Port
                class Channel(Port):
                    def send(self) -> int: ...
                class Named(typing.Protocol):
                    def name(self) -> str: ...
                class LooksLikeProtocol:
                    def send(self) -> int: return 1
                T = typing.TypeVar('T')
                class GenericChannel(Port[T]):
                    def send(self) -> T: ...
            """,
            "sample/adapters.py": """
                from .contracts import Channel, Named, LooksLikeProtocol, GenericChannel
                class Good(Channel):
                    def send(self): return 1
                class Derived(Good):
                    def send(self): return 2
                class AlsoGood(Named):
                    def name(self): return 'name'
                class Extra(Channel):
                    def send(self): return 1
                    def leak(self): return 2
                class Missing:
                    def send(self): return 1
                class Pretender(LooksLikeProtocol):
                    def send(self): return 1
                class GenericGood(GenericChannel[int]):
                    def send(self): return 1
            """,
        }
        layers = {"sample.contracts": "Domain", "sample.adapters": "Infrastructure"}
        native = self.lint(files, layers, rules=["W9201", "W9202"], patched=False)
        messages = self.lint(files, layers, rules=["W9201", "W9202"])
        self.assertGreater(len(native), len(messages))
        self.assertTrue(any(m["messageId"] == "W9202" for m in native), native)
        contracts = [m["message"] for m in messages if m["messageId"] == "W9201"]
        self.assertEqual(len(contracts), 3, messages)
        for name in ("Extra.leak (not in Protocol)", "Missing", "Pretender"):
            self.assertTrue(any(name in message for message in contracts), contracts)
        # A suffix alone does not turn the class into a typing.Protocol.
        self.assertFalse([m for m in messages if m["messageId"] == "W9202"], messages)

    def test_domain_spelling_does_not_override_a_protocols_configured_layer(self):
        files = {"sample/__init__.py": "", "sample/domain/__init__.py": "",
                 "sample/domain/contracts.py": """
                     from typing import Protocol
                     class Channel(Protocol):
                         def send(self): ...
                 """,
                 "sample/adapters.py": """
                     from .domain.contracts import Channel
                     class Driver(Channel):
                         def send(self): return 1
                 """}
        layers = {"sample.domain.contracts": "UseCase", "sample.adapters": "Infrastructure"}
        messages = self.lint(files, layers, rules=["W9201"])
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("Infrastructure class Driver", messages[0]["message"])

    def test_literals_from_infrastructure_are_not_infrastructure_types(self):
        files = {
            "sample/__init__.py": "",
            "sample/io.py": """
                def nothing(): return None
                def mapping(): return {}
                def sequence(): return []
                def pair(): return ()
                def text(): return 'text'
                class Connection: pass
                def connection(): return Connection()
                class Driver: pass
                def driver(): return Driver()
            """,
            "sample/use.py": """
                from .io import nothing, text, connection, driver
                value = nothing()
                label = text()
                raw = connection()
                adapter = driver()
            """,
        }
        layers = {"sample.io": "Infrastructure", "sample.use": "UseCase"}
        native = self.lint(files, layers, rules=["W9007", "W9009"], patched=False,
                           extra='infrastructure_modules = ["sample.io"]')
        messages = self.lint(files, layers, rules=["W9007", "W9009"],
                             extra='infrastructure_modules = ["sample.io"]')
        self.assertGreater(len(native), len(messages))
        for message in messages:
            self.assertIn(message["message"], [m["message"] for m in native])
        self.assertEqual(sorted((m["messageId"], m["line"]) for m in messages),
                         [("W9007", 7), ("W9007", 9), ("W9009", 4), ("W9009", 5)])

    def test_specific_domain_layer_overrides_infrastructure_package_origin(self):
        files = {
            "sample/__init__.py": "",
            "sample/records.py": "class Record: pass\n",
            "sample/driver.py": "class Driver: pass\n",
            "sample/repository.py": """
                from .records import Record
                from .driver import Driver
                def mapped(): return Record()
                def leaked(): return Driver()
            """,
            "sample/use.py": """
                from .records import Record
                from .driver import Driver
                entity = Record()
                concrete = Driver()
            """,
        }
        layers = {"sample": "Infrastructure", "sample.records": "Domain", "sample.use": "UseCase"}
        options = 'infrastructure_modules = ["sample"]'
        native = self.lint(files, layers, rules=["W9007", "W9009"], extra=options, patched=False)
        messages = self.lint(files, layers, rules=["W9007", "W9009"], extra=options)
        self.assertGreater(len(native), len(messages))
        self.assertEqual([(m["messageId"], m["obj"], m["line"]) for m in messages],
                         [("W9007", "leaked", 4), ("W9009", "", 4)])

    def test_domain_mapping_does_not_hide_external_raw_type_ancestry(self):
        files = {
            "external.py": "class Connection: pass\n",
            "domain.py": """
                from external import Connection
                class Disguised(Connection): pass
                def leak(): return Disguised()
            """,
        }
        messages = self.lint(files, {"domain": "Domain"}, rules=["W9007"])
        self.assertEqual([(m["messageId"], m["obj"]) for m in messages], [("W9007", "leak")])

    def test_mixed_inference_reports_the_offending_type_not_the_none_branch(self):
        files = {"adapter.py": """
            class Driver: pass
            def cached(ready):
                value = None
                if ready:
                    value = Driver()
                return value
        """}
        options = 'infrastructure_modules = ["adapter"]'
        native = self.lint(files, {"adapter": "Infrastructure"}, rules=["W9007"],
                           extra=options, patched=False)
        messages = self.lint(files, {"adapter": "Infrastructure"}, rules=["W9007"], extra=options)
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("Naked Return: NoneType", native[0]["message"])
        self.assertIn("Naked Return: Driver", messages[0]["message"])

    def test_internal_factories_require_a_real_and_precise_domain_contract(self):
        files = {
            "sample/__init__.py": "",
            "sample/contracts.py": """
                from typing import Protocol
                class Port(Protocol):
                    def run(self): ...
            """,
            "sample/runtime.py": """
                from typing import Any
                from .contracts import Port
                class Runtime(Port):
                    def run(self): return 1
                class Driver: pass
                class Session(Port):
                    def run(self): return 1
                def internal() -> Runtime: return Runtime()
                def abstract() -> Port: return Runtime()
                def vague() -> object: return Runtime()
                def unchecked() -> Any: return Runtime()
                def unannotated(): return Runtime()
                def wrong() -> Port: return Driver()
                def raw() -> Port: return Session()
                class Repository:
                    def leaked(self) -> Runtime: return Runtime()
                    def port(self) -> Port: return Runtime()
            """,
        }
        layers = {"sample.contracts": "Domain", "sample.runtime": "Infrastructure"}
        messages = self.lint(files, layers, rules=["W9007"],
                             extra='infrastructure_modules = ["sample.runtime"]')
        self.assertEqual([m["obj"] for m in messages],
                         ["vague", "unchecked", "unannotated", "wrong", "raw", "Repository.leaked"])

    def test_context_manager_self_return_is_not_raw_data_escape(self):
        files = {"adapter.py": """
            from typing import Self
            class Driver: pass
            class Context:
                def __enter__(self) -> Self: return self
                def __exit__(self, *args): return None
                def leaked(self) -> Self: return self
            class Wrong:
                def __enter__(self) -> Self: return Driver()
                def __exit__(self, *args): return None
            class Vague:
                def __enter__(self) -> object: return self
                def __exit__(self, *args): return None
            class Rebound:
                def __enter__(self) -> Self:
                    self = Driver()
                    return self
                def __exit__(self, *args): return None
            class Forward:
                def __enter__(self) -> 'Forward': return self
                def __exit__(self, *args): return None
        """}
        options = 'infrastructure_modules = ["adapter"]'
        native = self.lint(files, {"adapter": "Infrastructure"}, rules=["W9007"],
                           extra=options, patched=False)
        messages = self.lint(files, {"adapter": "Infrastructure"}, rules=["W9007"], extra=options)
        self.assertGreater(len(native), len(messages))
        self.assertEqual([m["obj"] for m in messages],
                         ["Context.leaked", "Wrong.__enter__", "Vague.__enter__", "Rebound.__enter__"])

    def test_only_selected_standard_library_symbols_are_resource_safe(self):
        files = {"domain.py": """
            from types import TracebackType, MappingProxyType
            from threading import Event, Lock, RLock, Thread
            from urllib.parse import urlsplit, urlunsplit
            from shlex import quote as escape
            from copy import deepcopy
            from socket import socket
            from subprocess import run
            from urllib.request import urlopen
            from concurrent.futures import ProcessPoolExecutor
            from shlex import shlex
            from copy import deepcopy, copy
        """}
        native = self.lint(files, {"domain": "Domain"}, rules=["W9004"], patched=False)
        messages = self.lint(files, {"domain": "Domain"}, rules=["W9004"])
        self.assertEqual(len(native), 11, native)
        self.assertEqual([m["line"] for m in messages], list(range(6, 12)))

    def test_qualified_stdlib_uses_are_checked_symbol_by_symbol(self):
        files = {
            "safe.py": """
                import shlex as escaping
                import urllib.parse
                value = escaping.quote('text')
                parts = urllib.parse.urlsplit('https://example.invalid')
                url = urllib.parse.urlunsplit(parts)
            """,
            "unsafe.py": """
                import shlex
                def parse(stream): return shlex.shlex(stream)
                def escaped(text): return shlex.quote(text)
            """,
            "opaque.py": """
                import copy
                def delegate(consumer): return consumer(copy)
            """,
        }
        layers = dict.fromkeys(("safe", "unsafe", "opaque"), "Domain")
        messages = self.lint(files, layers, rules=["W9004"])
        self.assertEqual([m["module"] for m in messages], ["unsafe", "opaque"])

    def test_stdlib_symbol_exception_does_not_override_an_explicit_outer_layer(self):
        files = {"domain.py": "from copy import deepcopy\n"}
        messages = self.lint(files, {"domain": "Domain", "copy": "Infrastructure"}, rules=["W9004"])
        self.assertEqual([m["messageId"] for m in messages], ["W9004"])

    def test_visibility_requires_an_actual_boundary_crossing(self):
        files = {
            "sample/__init__.py": "",
            "sample/entity.py": "class Entity:\n    _secret = 1\n",
            "sample/inside.py": "from .entity import Entity\nvalue = Entity()._secret\n",
            "sample/outside.py": "from .entity import Entity\nvalue = Entity()._secret\n",
        }
        layers = {"sample": "Domain", "sample.outside": "Interface"}
        native = self.lint(files, layers, rules=["W9003"], patched=False)
        messages = self.lint(files, layers, rules=["W9003"])
        self.assertEqual(len(native), 2, native)
        self.assertEqual(len(messages), 1, messages)
        self.assertTrue(messages[0]["path"].endswith("outside.py"), messages)

    def test_only_actual_super_hooks_are_exempt_from_visibility(self):
        files = {
            "adapter.py": """
                from threading import Thread
                class Worker(Thread):
                    def run(self): return super()._bootstrap()
                    def explicit(self): return super(Worker, self)._bootstrap()
                    def missing(self): return super()._missing_hook()
                    def unrelated(self): return Thread()._bootstrap()
            """,
            "pretender.py": """
                from threading import Thread
                def super(): return Thread()
                def unrelated(): return super()._bootstrap()
            """,
        }
        layers = {"adapter": "Infrastructure", "pretender": "Infrastructure"}
        native = self.lint(files, layers, rules=["W9003"], patched=False)
        messages = self.lint(files, layers, rules=["W9003"])
        self.assertEqual(len(native), 5, native)
        self.assertEqual([(m["messageId"], m["obj"]) for m in messages],
                         [("W9003", "Worker.missing"), ("W9003", "Worker.unrelated"),
                          ("W9003", "unrelated")])

    def test_constructors_and_namespaces_are_not_stranger_object_chains(self):
        files = {
            "sample/__init__.py": "",
            "sample/objects.py": """
                class Engine:
                    def run(self): return 1
                def get_engine(): return Engine()
            """,
            "sample/consumer.py": """
                import sample.objects
                from .objects import Engine
                def owned():
                    engine = Engine()
                    engine.run()
                    sample.objects.get_engine()
                def stranger(factory):
                    engine = factory.get_engine()
                    engine.run()
                def traversal(other):
                    other.engine.run()
                class Owner:
                    def __init__(self): self.engine = Engine()
                    def run(self): return self.engine.run()
            """,
        }
        native = self.lint(files, {}, rules=["W9006"], patched=False)
        messages = self.lint(files, {}, rules=["W9006"])
        self.assertEqual([m["line"] for m in native], [5, 6, 9, 11])
        self.assertEqual([(m["messageId"], m["line"]) for m in messages],
                         [("W9006", 9), ("W9006", 11)])
        for message in messages:
            self.assertIn(message["message"], [m["message"] for m in native])

    def test_concrete_stubs_still_report_pass_ellipsis_and_return_none(self):
        files = {"adapter.py": """
            class Adapter:
                def first(self): pass
                def second(self): ...
                def third(self): return None
                def fourth(self): return 1
        """}
        messages = self.lint(files, {"adapter": "Infrastructure"}, rules=["W9202"])
        self.assertEqual([m["line"] for m in messages], [2, 3, 4])

    def test_overload_declarations_are_not_concrete_stubs(self):
        files = {
            "adapter.py": """
                import typing
                from typing import overload as signature
                class Adapter:
                    @signature
                    def send(self, value: int) -> int: ...
                    @typing.overload
                    def send(self, value: str) -> str: ...
                    def send(self, value): return value
                    def missing(self): ...
            """,
            "pretender.py": """
                def overload(function): return function
                class Adapter:
                    @overload
                    def missing(self): ...
            """,
        }
        native = self.lint(files, {}, rules=["W9202"], patched=False)
        messages = self.lint(files, {}, rules=["W9202"])
        self.assertEqual(len(native), 4, native)
        self.assertEqual([(m["messageId"], m["obj"]) for m in messages],
                         [("W9202", "Adapter.missing"), ("W9202", "Adapter.missing")])

    def test_domain_bundle_annotations_preserve_stranger_chain_enforcement(self):
        files = {
            "sample/__init__.py": "",
            "sample/contracts.py": """
                from dataclasses import dataclass
                from typing import Protocol
                class Clock(Protocol):
                    def now(self) -> float: ...
                @dataclass(frozen=True)
                class Bundle:
                    clock: Clock
            """,
            "sample/consumer.py": """
                from dataclasses import dataclass
                from .contracts import Bundle
                def parameter(bundle: Bundle): return bundle.clock.now()
                def keyword(*, bundle: 'Bundle'): return bundle.clock.now()
                @dataclass
                class Consumer:
                    dependencies: Bundle
                    def run(self): return self.dependencies.clock.now()
                class Injected:
                    def __init__(self, dependencies: Bundle):
                        self.dependencies = dependencies
                    def run(self): return self.dependencies.clock.now()
                def stranger(factory):
                    other = factory.get_thing()
                    return other.engine.run()
                class Outer:
                    def run(self): return self.factory.engine.run()
                def unknown(other): return other.bundle.clock.now()
            """,
        }
        layers = {"sample.contracts": "Domain", "sample.consumer": "UseCase"}
        native = self.lint(files, layers, rules=["W9006"], patched=False)
        messages = self.lint(files, layers, rules=["W9006"])
        self.assertGreater(len(native), len(messages))
        self.assertEqual([(m["messageId"], m["obj"]) for m in messages],
                         [("W9006", "stranger"), ("W9006", "Outer.run"), ("W9006", "unknown")])

    def test_domain_annotation_does_not_overrule_known_infrastructure_or_union(self):
        files = {
            "sample/__init__.py": "",
            "sample/contracts.py": "class Bundle: pass\n",
            "sample/transport.py": "class Driver: pass\n",
            "sample/consumer.py": """
                from .contracts import Bundle
                from .transport import Driver
                value: Bundle = Driver()
                value.engine.run()
                def ambiguous(value: Bundle | Driver): return value.engine.run()
                def stranger(value: Driver): return value.engine.run()
            """,
        }
        layers = {"sample.contracts": "Domain", "sample.transport": "Infrastructure",
                  "sample.consumer": "UseCase"}
        messages = self.lint(files, layers, rules=["W9006"])
        self.assertEqual([m["line"] for m in messages], [4, 5, 6])

    def test_precise_domain_return_annotations_fill_uninferable_calls_only(self):
        files = {
            "sample/__init__.py": "",
            "sample/contracts.py": """
                from typing import Protocol
                class Message:
                    def render(self): return 'message'
                class Port(Protocol):
                    def fetch(self) -> Message: raise NotImplementedError
            """,
            "sample/driver.py": "class Driver: pass\n",
            "sample/consumer.py": """
                from .contracts import Message, Port
                from .driver import Driver
                def fetch(port: Port) -> Message: return port.fetch()
                def rendered(port: Port):
                    response = fetch(port)
                    return response.render()
                def direct(port: Port):
                    response = port.fetch()
                    return response.render()
                def lie() -> Message: return Driver()
                def bad():
                    response = lie()
                    return response.run()
                def vague(factory) -> object: return factory()
                def opaque(factory):
                    response = vague(factory)
                    return response.run()
            """,
        }
        layers = {"sample.contracts": "Domain", "sample.driver": "Infrastructure", "sample.consumer": "UseCase"}
        native = self.lint(files, layers, rules=["W9006"], patched=False)
        messages = self.lint(files, layers, rules=["W9006"])
        self.assertGreater(len(native), len(messages))
        self.assertEqual([m["obj"] for m in messages], ["bad", "opaque"])

    def test_inferred_builtin_scalars_are_values_not_stranger_object_graphs(self):
        files = {"adapter.py": """
            def binary(stream) -> bytes: return stream.read()
            def text(value):
                raw = binary(value)
                return raw.decode('utf-8')
            class Response:
                def decode(self): return 'response'
            def response(): return Response()
            def stranger():
                raw = response()
                return raw.decode()
        """}
        native = self.lint(files, {"adapter": "Infrastructure"}, rules=["W9006"], patched=False)
        messages = self.lint(files, {"adapter": "Infrastructure"}, rules=["W9006"])
        self.assertEqual([m["obj"] for m in native], ["text", "stranger"])
        self.assertEqual([m["obj"] for m in messages], ["stranger"])

    def test_domain_iteration_keeps_its_element_type_through_tuple_indexing(self):
        files = {
            "sample/__init__.py": "",
            "sample/messages.py": """
                from __future__ import annotations
                from collections.abc import Generator
                class Message:
                    origin: Message | None = None
                    def versions(self) -> Generator[Message]:
                        version = self
                        yield version
                        while version.origin is not None:
                            version = version.origin
                            yield version
                    def snapshot(self):
                        versions = tuple(self.versions())
                        root = versions[-1]
                        return root.origin.snapshot() if root.origin is not None else None
            """,
            "sample/consumer.py": """
                from .messages import Message
                def direct(values: tuple[Message, ...]):
                    item = values[-1]
                    return item.origin.snapshot()
                def unknown(factory):
                    values = tuple(factory())
                    item = values[-1]
                    return item.child.run()
            """,
        }
        layers = {"sample.messages": "Domain", "sample.consumer": "UseCase"}
        native = self.lint(files, layers, rules=["W9006"], patched=False)
        messages = self.lint(files, layers, rules=["W9006"])
        self.assertGreater(len(native), len(messages))
        self.assertEqual([m["obj"] for m in messages], ["unknown"])

    def test_pathlib_with_name_preserves_path_type_not_arbitrary_factory_types(self):
        files = {"adapter.py": """
            from pathlib import Path
            def cleanup(path: Path):
                temporary = path.with_name(path.name + '.tmp')
                temporary.unlink(missing_ok=True)
            class Stranger:
                def unlink(self): return 1
            class Pretender:
                def with_name(self, name): return Stranger()
            def foreign():
                source = Pretender()
                temporary = source.with_name('other')
                temporary.unlink()
            class Override(Path):
                def with_name(self, name): return Stranger()
            def overridden(path: Override):
                temporary = path.with_name('other')
                temporary.unlink()
        """}
        native = self.lint(files, {"adapter": "Infrastructure"}, rules=["W9006"], patched=False)
        messages = self.lint(files, {"adapter": "Infrastructure"}, rules=["W9006"])
        self.assertIn("cleanup", [m["obj"] for m in native])
        self.assertEqual([m["obj"] for m in messages], ["foreign", "overridden"])

    def test_native_abstract_protocol_declaration_avoids_e1111_but_concrete_empty_still_fails(self):
        code = """
            from abc import abstractmethod
            from collections.abc import Callable
            from dataclasses import dataclass
            from typing import Protocol
            class Journal(Protocol):
                DECORATOR
                def call[T](self, invoke: Callable[[], T]) -> T: ...
            @dataclass
            class Consumer:
                journal: Journal
                def run(self):
                    result = self.journal.call(lambda: 1)
                    return result
            def concrete() -> int: pass
            result = concrete()
        """
        plain = self.lint({"sample.py": code.replace("DECORATOR", "")}, {},
                          rules=["E1111"], patched=False)
        abstract = self.lint({"sample.py": code.replace("DECORATOR", "@abstractmethod")}, {},
                             rules=["E1111"], patched=False)
        self.assertEqual([m["obj"] for m in plain], ["Consumer.run", ""])
        self.assertEqual([(m["messageId"], m["obj"]) for m in abstract], [("E1111", "")])

    def test_inherited_framework_surface_requires_a_separate_domain_adapter(self):
        files = {
            "sample/__init__.py": "",
            "sample/contracts.py": """
                from typing import Protocol
                class Port(Protocol):
                    def execute(self): ...
            """,
            "framework.py": """
                class Framework:
                    def setup(self): return 1
                    def probe(self): return 2
            """,
            "sample/adapter.py": """
                from framework import Framework
                from .contracts import Port
                class Leaky(Framework, Port):
                    def execute(self): return self.probe()
                class Adapter(Port):
                    def __init__(self, framework: Framework): self._framework = framework
                    def execute(self): return self._framework.probe()
            """,
        }
        layers = {"sample.contracts": "Domain", "sample.adapter": "Infrastructure"}
        messages = self.lint(files, layers, rules=["W9201"])
        self.assertEqual(len(messages), 2, messages)
        self.assertTrue(all(m["path"] == "framework.py" for m in messages), messages)
        self.assertTrue(all("Leaky." in m["message"] for m in messages), messages)

    def test_r0903_exempts_protocol_declarations_not_concrete_implementations(self):
        files = {"sample.py": """
            import typing
            from typing import Protocol as Port
            class Named(typing.Protocol):
                def name(self): ...
            class Aliased(Port):
                def send(self): ...
            T = typing.TypeVar('T')
            class Generic(Port[T]):
                def send(self) -> T: ...
            class Concrete:
                def send(self): return 1
            class Implementation(Aliased):
                def send(self): return 1
            class FakeProtocol:
                def send(self): return 1
        """}
        native = self.lint(files, {}, rules=["R0903"], patched=False)
        messages = self.lint(files, {}, rules=["R0903"])
        self.assertGreater(len(native), len(messages))
        self.assertEqual([(m["messageId"], m["obj"]) for m in messages],
                         [("R0903", "Concrete"), ("R0903", "Implementation"),
                          ("R0903", "FakeProtocol")])

    def test_protocol_exemption_preserves_ancestor_and_maximum_method_limits(self):
        files = {"sample.py": """
            from typing import Protocol
            class Wide(Protocol):
                def one(self): ...
                def two(self): ...
                def three(self): ...
            class First: pass
            class Second(First): pass
            class Third(Second): pass
            class Fourth(Third): pass
            class Deep(Fourth): pass
        """}
        options = '[tool.pylint.design]\nmax-parents = 3\nmax-public-methods = 2\n'
        native = self.lint(files, {}, rules=["R0901", "R0904"], patched=False, pylint_extra=options)
        messages = self.lint(files, {}, rules=["R0901", "R0904"], pylint_extra=options)
        self.assertEqual([(m["messageId"], m["obj"], m["message"]) for m in messages],
                         [(m["messageId"], m["obj"], m["message"]) for m in native])
        self.assertEqual({m["messageId"] for m in messages}, {"R0901", "R0904"})

    def test_pylint_protocol_ellipsis_declares_a_return_contract(self):
        files = {"sample.py": '''
            import typing
            from dataclasses import dataclass
            from typing import Protocol as Port
            class Lifecycle(Port):
                def ready(self) -> bool: ...
            class Journal(Lifecycle, Port):
                def call[T](self, operation: str, invoke: typing.Callable[[], T]) -> T:
                    """Return the callback result after observation."""
                    ...
            class Qualified(typing.Protocol):
                def fetch(self) -> int: ...
            T = typing.TypeVar("T")
            class Generic(Port[T]):
                def fetch(self) -> T: ...
            @dataclass
            class Client:
                journal: Journal
                def execute(self):
                    _ = self.journal.call("exchange", lambda: 1)
            aliased = Journal.call
            first = aliased(object(), "exchange", lambda: 1)
            second = Qualified.fetch(object())
            third = Generic.fetch(object())
        '''}
        native = self.lint(files, {}, rules=["E1111", "E1128"], patched=False)
        self.assertTrue(native)
        self.assertTrue(all(m["messageId"] == "E1111" for m in native), native)
        self.assertEqual(self.lint(files, {}, rules=["E1111", "E1128"]), [])

    def test_protocol_return_fix_preserves_concrete_and_default_method_checks(self):
        files = {"sample.py": '''
            from typing import Protocol
            class Port(Protocol):
                def fetch(self) -> int: ...
                def default(self) -> int:
                    value = 1
                def nothing(self): return None
            class Implementation(Port):
                def fetch(self) -> int: ...
            class FakeProtocol:
                def fetch(self) -> int: ...
            def missing() -> int:
                value = 1
            first = Port.default(object())
            second = Port.nothing(object())
            third = Implementation.fetch(object())
            fourth = FakeProtocol.fetch(object())
            fifth = missing()
        '''}
        native = self.lint(files, {}, rules=["E1111", "E1128"], patched=False)
        messages = self.lint(files, {}, rules=["E1111", "E1128"])
        self.assertEqual([(m["line"], m["messageId"]) for m in messages],
                         [(m["line"], m["messageId"]) for m in native])
        self.assertEqual(sorted(m["messageId"] for m in messages), ["E1111"] * 4 + ["E1128"])


if __name__ == "__main__":
    unittest.main()

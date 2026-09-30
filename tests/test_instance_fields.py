"""Isolated consumer fixtures for field ownership and report failures."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX / "src"))

from eryx._policy import discover_files, read_scope
from eryx.check import execute_job, make_jobs


class InstanceFieldTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="eryx-fields-")
        self.root = Path(self.temp.name)
        (self.root / "src/pkg").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / ".eryx/reports/run").mkdir(parents=True)
        (self.root / ".eryx/scope.toml").write_text(
            'exclude_directory_names = ["generated"]\n'
            'test_directory_names = ["tests"]\n', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def put(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def job(self):
        files = discover_files(self.root, read_scope(self.root))
        return next(job for job in make_jobs(self.root, self.root / ".eryx/reports/run", files, False, [])
                    if job["name"] == "instance-fields")

    def run_worker(self):
        job = self.job()
        result = execute_job(job, self.root, self.root / ".eryx/reports/run", 30)
        self.assertTrue((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"),
                        (self.root / result["raw_output"]["stderr"]).read_text(encoding="utf-8"))
        raw = json.loads((self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8"))
        return result, raw

    def test_external_reads_writes_deletes_and_known_owners_in_both_profiles(self):
        self.put("src/pkg/model.py", '''\
class Record:
    declared: int
    plain = 1
    def __init__(self):
        self.value = 1
    def change(self, other: Record):
        self.value = other.value
        other.value = 2
    @property
    def public(self):
        return self.value
    def call(self):
        return self.value
''')
        self.put("src/pkg/use.py", '''\
from pkg.model import Record as Item
import pkg.model as model

def use(item: Item):
    a = item.value
    item.value = 2
    del item.value
    return item.public, item.call()

copy = Item()
copy.declared = 1
copy.plain += 1
model.Record().value
''')
        self.put("tests/test_use.py", '''\
from pkg.model import Record
def test_field():
    instance = Record()
    return instance.value
''')
        self.put("src/pkg/generated/ignored.py", "from pkg.model import Record\nx = Record().value\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual([(item["path"], item["line"], item["symbol"]) for item in result["diagnostics"]], [
            ("src/pkg/use.py", line, symbol) for line, symbol in (
                (5, "pkg.model.Record.value"), (6, "pkg.model.Record.value"),
                (7, "pkg.model.Record.value"), (11, "pkg.model.Record.declared"),
                (12, "pkg.model.Record.plain"), (13, "pkg.model.Record.value"))
        ] + [("tests/test_use.py", 4, "pkg.model.Record.value")])
        self.assertEqual(raw["diagnostics"][0]["rule"], "EXTERNAL_INSTANCE_FIELD")
        self.assertEqual(result["diagnostics"][0]["column"], 9)
        self.assertTrue(all(not item["is_infrastructure_error"] for item in result["diagnostics"]))

    def test_unknown_third_party_properties_methods_and_rebinding(self):
        self.put("src/pkg/model.py", '''\
class Model:
    def __init__(self): self.field = 1
    @property
    def property(self): return self.field
    def method(self): return self.field
    def peer(self, other: Model):
        return other.field
    public_via_builtin = property(lambda self: self.field)
''')
        self.put("tests/test_negative.py", '''\
from pkg.model import Model
from library import Foreign
def use(unknown, external: Foreign):
    unknown.field
    external.field
    Model().property
    Model().method()
    Model().public_via_builtin
    known = Model()
    known = unknown
    known.field
    known = Model()
    for known in objects:
        pass
    known.field
    known = Model()
    if condition:
        known = unknown
    known.field
''')
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["diagnostics"], raw["diagnostics"]),
                         ("passed", [], []))

    def test_stub_fields_and_properties(self):
        self.put("src/pkg/record.pyi", '''\
class Record:
    value: int
    @property
    def public(self) -> int: ...
    def use(self, other: Record) -> int: ...
''')
        self.put("tests/test_stub.py", '''\
from pkg.record import Record
def use(record: Record):
    return record.value, record.public
''')
        result, _ = self.run_worker()
        self.assertEqual((result["status"], [(d["path"], d["line"], d["symbol"])
                                             for d in result["diagnostics"]]),
                         ("violations", [("tests/test_stub.py", 3, "pkg.record.Record.value")]))

    def test_source_and_output_failures_are_infrastructure_errors(self):
        self.put("src/pkg/good.py", "class Good:\n    def __init__(self): self.value = 1\nx = Good().value\n")
        self.put("tests/broken.py", "class Broken(\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "INSTANCE_FIELDS_PARSE_ERROR"))
        self.assertEqual({item["rule"] for item in result["diagnostics"]},
                         {"INSTANCE_FIELDS_PARSE_ERROR", "EXTERNAL_INSTANCE_FIELD"})
        job = self.job()
        (self.root / "tests/broken.py").unlink()
        missing = execute_job(job, self.root, self.root / ".eryx/reports/run", 30)
        self.assertEqual((missing["status"], missing["error"]), ("error", "INSTANCE_FIELDS_PARSE_ERROR"))
        for payload, code in [("not json", 0), (json.dumps({
                "eryx_instance_fields_adapter": 1, "diagnostics": [], "execution_error": None}), 1),
                (json.dumps({"eryx_instance_fields_adapter": 1, "diagnostics": [
                    {"rule": "EXTERNAL_INSTANCE_FIELD", "message": "bad"}], "execution_error": None}), 1)]:
            with self.subTest(payload=payload):
                corrupt = execute_job(job, self.root, self.root / ".eryx/reports/run", 30,
                                      capture_fn=lambda *args: {"stdout": payload, "stderr": "",
                                                                "exit_code": code, "duration_seconds": 0,
                                                                "error": None})
                self.assertEqual(corrupt["status"], "error")


if __name__ == "__main__":
    unittest.main()

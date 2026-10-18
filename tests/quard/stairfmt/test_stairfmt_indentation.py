"""Autonomous indentation regressions for Stairfmt check and fix."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from typing import Any, override

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._policy import discover_files, read_scope
from quard.check import execute_job, make_jobs
from offline_fixtures import create_repo


class TestStairfmtIndentation(unittest.TestCase):
    """Verify indentation independently of call layouts and selection."""

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path(self.enterContext(TemporaryDirectory()))

    @override
    def setUp(self) -> None:
        create_repo(self.root)

    def put(self, name: str, source: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def run_fix(self, *paths: str) -> tuple[int, dict[str, Any]]:
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--fix",
                                  "--project", str(self.root), *paths], cwd=self.root,
                                 capture_output=True, text=True, check=False)
        report = json.loads((self.root / ".quard/reports/stairfmt/latest.json").read_text(
            encoding="utf-8"))
        return command.returncode, report

    def checked(self, source: str) -> dict[str, Any]:
        self.put("src/demo/working.py", source)
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--check",
                                  "--project", str(self.root), "--json", "src/demo/working.py"],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        return json.loads(command.stdout)

    def test_indent_tab_without_calls_is_a_positioned_job_violation(self):
        self.put("src/demo/working.py", "if True:\n\tvalue = 1\n")
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / ".quard/reports/run"
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item["name"] == "stairfmt")
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          [(item["rule"], item["line"], item["column"])
                           for item in result["diagnostics"]]),
                         ("violations", 1, [("ST010", 2, 1)]))

    def test_noncanonical_space_indent_is_reported_before_fix(self):
        source = 'if True:\n  value = 1\n'
        found = self.checked(source)
        fixed, report = self.run_fix('src/demo/working.py')
        self.assertEqual(([item['rule'] for item in found['remaining']],
                          fixed, report['remaining'],
                          (self.root / 'src/demo/working.py').read_text(encoding='utf-8')),
                         (['ST010'], 0, [], 'if True:\n    value = 1\n'))

    def test_noncanonical_continuation_indent_is_reported(self):
        report = self.checked('value = f(\n  a,\n  b\n)\n')
        self.assertEqual([item['rule'] for item in report['remaining']], ['ST010', 'ST010'])

    def test_continuation_and_closing_indent_converge_after_fix(self):
        source = 'if True:\n    value = f(\n      a,\n      b\n      )\n'
        found = self.checked(source)
        code, report = self.run_fix('src/demo/working.py')
        fixed = (self.root / 'src/demo/working.py').read_text(encoding='utf-8')
        again, repeat = self.run_fix('src/demo/working.py')
        self.assertEqual(([item['rule'] for item in found['remaining']], code,
                          report['remaining'], fixed, again, repeat['changed_files']),
                         (['ST010', 'ST010', 'ST010'], 0, [],
                          'if True:\n    value = f(\n        a, b\n    )\n', 0, []))

    def test_parenthesized_call_continuation_indent_is_fixed(self):
        source = 'value = (f(\n  a, b\n))\n'
        found = self.checked(source)
        code, report = self.run_fix('src/demo/working.py')
        fixed = (self.root / 'src/demo/working.py').read_text(encoding='utf-8')
        again, repeat = self.run_fix('src/demo/working.py')
        self.assertEqual(([item['rule'] for item in found['remaining']], code,
                          report['remaining'], fixed, again, repeat['changed_files']),
                         (['ST010'], 0, [], 'value = (f(\n    a, b\n))\n', 0, []))

    def test_binary_operands_use_expression_indentation(self):
        source = 'x = (g(\n  1, 2\n) +\n     h(\n         3, 4\n     ))\n'
        found = self.checked(source)
        code, report = self.run_fix('src/demo/working.py')
        fixed = (self.root / 'src/demo/working.py').read_text(encoding='utf-8')
        again, repeat = self.run_fix('src/demo/working.py')
        self.assertEqual(([item['line'] for item in found['remaining']
                           if item['rule'] == 'ST010'], code, report['remaining'], fixed,
                          again, repeat['changed_files']),
                         ([2, 3], 0, [],
                          'x = (g(\n         1, 2\n     ) +\n     h(\n         3, 4\n     ))\n', 0, []))

    def test_parenthesized_nested_call_has_its_own_indentation(self):
        source = 'value = outer(\n    (inner(\n      a, b\n    ))\n)\n'
        found = self.checked(source)
        code, report = self.run_fix('src/demo/working.py')
        fixed = (self.root / 'src/demo/working.py').read_text(encoding='utf-8')
        self.assertEqual(([item['line'] for item in found['remaining']
                           if item['rule'] == 'ST010'], code, report['remaining'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([3], 0, [], True))

    def test_form_feed_and_tab_in_indent_are_reported_and_removed(self):
        source = 'if True:\n\f\tvalue = 1\n'
        found = self.checked(source)
        fixed, report = self.run_fix('src/demo/working.py')
        self.assertEqual(([item['rule'] for item in found['remaining']],
                          fixed, report['remaining'],
                          (self.root / 'src/demo/working.py').read_text(encoding='utf-8')),
                         (['ST010'], 0, [], 'if True:\n    value = 1\n'))

    def test_indent_fix_preserves_mixed_blocks_and_literal_text(self):
        source = ('if True:\n\t# note\tkeep\n\tif True:\n\t    value = """first\n'
                  '\tsecond\n\t"""\n\t    formatted = f"""first\n\tsecond\n\t"""\n'
                  '\t    result = (\n\t\tvalue  # note\tstay\n'
                  '\t    )\nif False:\n        value = "spaces"\n')
        self.put("src/demo/working.py", source)
        code, report = self.run_fix("src/demo/working.py")
        fixed = (self.root / "src/demo/working.py").read_text(encoding="utf-8")
        again, repeat = self.run_fix("src/demo/working.py")
        self.assertEqual((code, report["status"], report["remaining"],
                          ast.dump(ast.parse(source)) == ast.dump(ast.parse(fixed)),
                          'f"""first\n\tsecond\n\t"""' in fixed, '# note\tkeep' in fixed,
                          '# note\tstay' in fixed, '    if True:\n        value' in fixed,
                          'if False:\n    value' in fixed, again, repeat["changed_files"],
                          self.checked(fixed)["remaining"]),
                         (0, "passed", [], True, True, True, True, True, True, 0, [], []))

    def test_noncanonical_indent_width_fails_before_writing_selected_files(self):
        self.put("src/demo/working.py", "if True:\n\tvalue = 1\n")
        self.put("src/demo/other.py", "if True:\n\tvalue = 2\n")
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--fix",
                                  "--indent", "2", "--project", str(self.root), "--json",
                                  "src/demo/working.py"], cwd=self.root,
                                 capture_output=True, text=True, check=False)
        report = json.loads(command.stdout)
        self.assertEqual((command.returncode, report["changed_files"],
                          (self.root / "src/demo/working.py").read_text(encoding="utf-8"),
                          (self.root / "src/demo/other.py").read_text(encoding="utf-8")),
                         (2, [], "if True:\n\tvalue = 1\n",
                          "if True:\n\tvalue = 2\n"))

    def test_noncanonical_indent_width_fails_check_too(self):
        self.put("src/demo/working.py", "value = 1\n")
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--check",
                                  "--indent", "2", "--project", str(self.root), "--json"],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        self.assertEqual((command.returncode, json.loads(command.stdout)["status"]), (2, "error"))

    def test_indent_check_ignores_tabs_inside_literals_and_comments(self):
        source = ('text = """first\n\tsecond\n"""\n'
                  'formatted = f"""first\n\tsecond\n"""\n# note\tkeep\n')
        report = self.checked(source)
        self.assertEqual((report["status"], report["remaining"]), ("passed", []))

    def test_template_text_tab_is_preserved(self):
        if sys.hexversion < 0x030E0000:
            self.skipTest('Template strings require Python 3.14')
        source = 'x = t"""hello\n\tworld"""\n'
        checked = self.checked(source)
        code, fixed = self.run_fix('src/demo/working.py')
        self.assertEqual((checked['remaining'], code, fixed['remaining'],
                          (self.root / 'src/demo/working.py').read_text(encoding='utf-8')),
                         ([], 0, [], source))

    def test_indent_fix_is_independent_of_blocked_calls(self):
        self.put("src/demo/working.py", 'if True:\n\tvalue = f(1,  # note\n\t    2)\n')
        code, report = self.run_fix("src/demo/working.py")
        fixed = (self.root / "src/demo/working.py").read_text(encoding="utf-8")
        self.assertEqual((code, report["status"], "\t" in fixed,
                          '# note' in fixed, {item["rule"] for item in report["remaining"]}),
                         (1, "violations", False, True, {"ST002", "ST004", "ST008"}))

    def test_call_and_tab_indentation_are_fixed_together(self):
        source = 'if True:\n\tvalue = f(g(1), h(2))\n'
        self.put("src/demo/working.py", source)
        code, report = self.run_fix("src/demo/working.py")
        fixed = (self.root / "src/demo/working.py").read_text(encoding="utf-8")
        self.assertEqual((code, report["remaining"],
                          ast.dump(ast.parse(source)) == ast.dump(ast.parse(fixed)),
                          fixed.startswith('if True:\n    value = f(\n')),
                         (0, [], True, True))

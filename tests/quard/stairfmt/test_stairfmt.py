"""Autonomous regressions for checking and fixing Stairfmt compositions."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from typing import Any, override

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._policy import discover_files, read_checks, read_scope
from quard.check import execute_job, make_jobs
from quard.check_runner.native_check_runner import NativeCheckRunner
from offline_fixtures import create_repo, fake_tools


class TestStairfmt(unittest.TestCase):
    """Exercise the consumer-facing check and its independent fix report.

    Args:
        methodName (str, default="runTest"): Test case name.
    """

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path.cwd()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        create_repo(self.root)

    def put(self, name: str, source: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def run_fix(self, *paths: str) -> tuple[int, dict[str, Any], str]:
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--fix",
                                  "--project", str(self.root), *paths], cwd=self.root,
                                 capture_output=True, text=True, check=False)
        report = json.loads((self.root / ".quard/reports/stairfmt/latest.json").read_text(
            encoding="utf-8"))
        return command.returncode, report, command.stdout

    def formatted(self, source: str) -> tuple[str, dict[str, Any]]:
        self.put("src/demo/working.py", source)
        _, report, _ = self.run_fix("src/demo/working.py")
        return (self.root / "src/demo/working.py").read_text(encoding="utf-8"), report

    def checked(self, source: str) -> dict[str, Any]:
        self.put("src/demo/working.py", source)
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--check",
                                  "--project", str(self.root), "--json", "src/demo/working.py"],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        return json.loads(command.stdout)

    def test_enabled_job_reports_positions_and_fix_command(self):
        self.put("src/demo/domain/api.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/domain/api.pyi", "x = f(g(1), h(2))\n")
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / ".quard/reports/run"
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item["name"] == "stairfmt")
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          {item["path"] for item in result["diagnostics"]},
                          all("stairfmt.py --fix --project ." in item["message"]
                              for item in result["diagnostics"])),
                         ("violations", 1, {"src/demo/domain/api.py"}, True), result)

    def test_parse_failure_preserves_other_file_findings(self):
        self.put("src/demo/domain/api.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/broken.py", "def x(:\n")
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / ".quard/reports/run"
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item["name"] == "stairfmt")
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          {item["rule"] for item in result["diagnostics"]},
                          all(item["is_infrastructure_error"] for item in result["diagnostics"]
                              if item["rule"] == "STAIRFMT_PARSE_ERROR")),
                          ("error", 2, {"ST001", "ST002", "ST004", "STAIRFMT_PARSE_ERROR"},
                           True), result)

    def test_deep_expression_reports_file_error_and_preserves_peer_findings(self):
        self.put("src/demo/domain/api.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/deep.py", "x = f(" + "+" * 1100 + "g(1))\n")
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / ".quard/reports/run"
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item["name"] == "stairfmt")
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          {item["path"] for item in result["diagnostics"] if item["path"]},
                          any(item["rule"] == "STAIRFMT_EXECUTION_ERROR"
                              and item["is_infrastructure_error"]
                              for item in result["diagnostics"])),
                         ("error", 2, {"src/demo/deep.py", "src/demo/domain/api.py"}, True), result)

    def test_deep_expression_does_not_prevent_peer_fix(self):
        source = "x = f(" + "+" * 1100 + "g(1))\n"
        self.put("src/demo/deep.py", source)
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        code, report, _ = self.run_fix()
        self.assertEqual((code, report["status"], report["changed_files"],
                          report["errors"][0]["path"],
                          (self.root / "src/demo/deep.py").read_text(encoding="utf-8"),
                          "x = f(\n" in (self.root / "src/demo/working.py").read_text(encoding="utf-8")),
                         (2, "error", ["src/demo/working.py"], "src/demo/deep.py", source, True))

    def test_direct_check_reports_deep_expression_without_traceback(self):
        report = self.checked("x = f(" + "+" * 1100 + "g(1))\n")
        self.assertEqual((report["status"], report["exit_code"],
                          report["errors"][0]["path"],
                          "слишком глубокое выражение" in report["errors"][0]["message"]),
                         ("error", 2, "src/demo/working.py", True))

    def test_partial_fix_reports_failure_and_keeps_success(self):
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/blocked.py", "x = f(g(1),  # note\n    h(2))\n")
        code, report, output = self.run_fix()
        self.assertEqual((code, report["status"], report["changed_files"],
                          report["warnings"][0]["line"], len(report["remaining"]),
                          "комментарий" in output,
                          f"{len(report['warnings'])} пропусков" in output,
                          "x = f(\n" in (self.root / "src/demo/working.py").read_text()),
                          (1, "violations", ["src/demo/working.py"], 1, 4, True, True, True))

    def test_parent_issue_repro_is_parseable_and_clean_after_fix(self):
        source = ('import json\n\ndef lambda_case(values):\n'
                  '    return list(map(lambda item: str(item), values))\n\n'
                  'def comprehension_case(values):\n'
                  '    return tuple(item.strip() for item in values if item.startswith("x"))\n\n'
                  'def expression_case(path):\n'
                  '    return path.write_text(json.dumps({"x": int(path.name)}) + "\\n", '
                  'encoding="utf-8")\n\n'
                  'def fstring_case(value):\n    return str(f"{value.upper()}")\n')
        fixed, report = self.formatted(source)
        self.assertEqual((ast.dump(ast.parse(source)), ast.dump(ast.parse(fixed)),
                          report["remaining"], report["warnings"],
                          self.formatted(fixed)[0], 'f"{value.upper()}"' in fixed),
                         (ast.dump(ast.parse(source)), ast.dump(ast.parse(source)),
                          [], [], fixed, True))

    def test_four_cases_with_tabs_comments_inline_calls_and_long_string(self):
        source = ('import json\n\n# note: keep this comment\n'
                  'def lambda_case(values):\n'
                  '\treturn list(map(lambda item: str(item), values))\n\n'
                  'def comprehension_case(values):\n'
                  '\treturn tuple(item.strip() for item in values if item.startswith("x"))\n\n'
                  'def expression_case(path):\n'
                  '\treturn path.write_text(json.dumps({"x": int(path.name)}) + "\\n", '
                  'encoding="utf-8")\n\n'
                  'def fstring_case(value):\n\treturn str(f"{value.upper()}")\n\n'
                  'description = "' + 'a long phrase ' * 15 + '"\n')
        self.put('src/demo/working.py', source)
        initial = self.checked(source)
        code, report, _ = self.run_fix('src/demo/working.py')
        fixed = (self.root / 'src/demo/working.py').read_text(encoding='utf-8')
        again, repeat, _ = self.run_fix('src/demo/working.py')
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / '.quard/reports/run'
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item['name'] == 'stairfmt')
        result = execute_job(job, self.root, directory, 30)
        archived = json.loads((self.root / report['report_path']).read_text(encoding='utf-8'))
        self.assertEqual(({'ST010', 'ST011'} <= {item['rule'] for item in initial['remaining']},
                          any(item['rule'] == 'ST001' for item in initial['remaining']),
                          code, report['remaining'], report['warnings'],
                          ast.dump(ast.parse(source)) == ast.dump(ast.parse(fixed)),
                          '# note: keep this comment' in fixed, '\treturn' in fixed,
                          'lambda item: str(item)' in fixed, again, repeat['changed_files'],
                          archived['exit_code'], result['status'], result['exit_code'],
                          result['diagnostics']),
                         (True, True, 0, [], [], True, True, False, True, 0, [], 0,
                          'passed', 0, []))

    def test_single_field_dictionary_and_comprehension_stay_inline(self):
        source = 'value = f({"key": g(1)}, [h(item) for item in values])\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report["remaining"], '{"key": g(1)}' in fixed,
                          '[h(item) for item in values]' in fixed), ([], True, True))

    def test_multiline_protected_expressions_collapse_without_ast_change(self):
        source = ('value = f(\n    {\n        "key": g(1)\n    },\n'
                  '    [\n        h(item) for item in values\n    ]\n)\n')
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(source)) == ast.dump(ast.parse(fixed)),
                          '{ "key": g(1) }' in fixed,
                          '[ h(item) for item in values ]' in fixed),
                         ([], [], True, True, True))

    def test_simple_comprehensions_with_nested_calls_remain_inline(self):
        sources = ('x = f([g(1, 2) for v in xs])\n',
                   'x = f({g(1, 2) for v in xs})\n',
                   'x = f({v: g(1, 2) for v in xs})\n',
                   'x = f(g(1, 2) for v in xs)\n')
        results = []
        for source in sources:
            fixed, report = self.formatted(source)
            results.append((report['remaining'], report['warnings'],
                            ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                            self.formatted(fixed)[0] == fixed))
        self.assertEqual(results, [([], [], True, True)] * len(sources))

    def test_parenthesized_binary_beside_comprehension_is_stable(self):
        source = 'x = f((g(1, 2) + h(3, 4)), [g(1, 2) for v in xs])\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          '    (g(\n' in fixed, self.formatted(fixed)[0]),
                         ([], [], True, True, fixed))

    def test_relayout_retains_parentheses_around_multiline_binary_argument(self):
        source = ('x = f(\n    (g(\n        1, 2\n    ) +\n'
                  '    h(\n        3, 4\n    )), j(5, 6)\n)\n')
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          '    (g(\n' in fixed, self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([], [], True, fixed, True))

    def test_multifield_dictionary_and_nested_containers_have_distinct_lines(self):
        source = 'value = f({"a": g(1), "b": h(2)}, [[1, 2], 3, 4], {5, 6})\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report["remaining"], report["warnings"],
                          '"a": g(1),\n' in fixed, '"b": h(2)' in fixed,
                          '1, 2\n' in fixed, '3, 4' in fixed,
                          self.formatted(fixed)[0] == fixed),
                         ([], [], True, True, True, True, True))

    def test_callable_dictionary_keys_and_values_converge(self):
        source = 'x = f({g(1): h(2), j(3): k(4)}, 0)\n'
        fixed, report = self.formatted(source)
        self.assertEqual((ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          report['remaining'], report['warnings'],
                          self.checked(fixed)['remaining'], self.formatted(fixed)[0]),
                         (True, [], [], [], fixed))

    def test_multifield_dictionary_closes_after_multiline_value(self):
        source = ('x = f(\n    {\n        "a": 1,\n        "b": g(\n'
                  '            2, 3\n        )}\n)\n')
        expected = ('x = f(\n    {\n        "a": 1,\n        "b": g(\n'
                    '            2, 3\n        )\n    }\n)\n')
        initial = self.checked(source)
        fixed, report = self.formatted(source)
        clean = self.checked(fixed)
        repeat, _ = self.formatted(fixed)
        self.assertEqual(([(item['rule'], item['line']) for item in initial['remaining']],
                          fixed, report['remaining'], clean['remaining'], repeat,
                          ast.dump(ast.parse(source)) == ast.dump(ast.parse(fixed))),
                          ([('ST009', 2)], expected, [], [], expected, True))

    def test_noncanonical_container_siblings_and_singletons_are_detected(self):
        sources = ('x = f(\n    {\n        "a": g(\n            1, 2\n        ), "b": 3\n    }\n)\n',
                   'x = f(\n    [\n        g(\n            1, 2\n        ), h(3)\n    ]\n)\n',
                   'x = f(\n    [g(1)]\n)\n', 'x = f(\n    (g(1),)\n)\n')
        results: list[tuple[bool, object, object, bool]] = []
        for source in sources:
            findings = self.checked(source)['remaining']
            fixed, report = self.formatted(source)
            results.append(('ST009' in {item['rule'] for item in findings},
                            report['remaining'], report['warnings'], self.formatted(fixed)[0] == fixed))
        self.assertEqual(results, [(True, [], [], True)] * len(sources))

    def test_clean_call_with_comment_is_positioned_violation(self):
        source = 'value = f(\n    1,  # keep\n    2\n)\n'
        self.assertEqual([(item["rule"], item["line"]) for item in self.checked(source)["remaining"]
                          if item["rule"] == 'ST008'], [('ST008', 2)])

    def test_limit_measures_expression_not_physical_line(self):
        source = 'prefix = f(' + ' ' * 70 + 'lambda x: g(x))\n'
        setting = self.root / '.quard/config.toml'
        setting.write_text(setting.read_text(encoding='utf-8')
                           + '\n[stairfmt]\ninline_limit = 16\n',
                           encoding='utf-8')
        fixed, report = self.formatted(source)
        self.assertEqual(([(item['rule'], item['line']) for item in report['remaining']],
                          'lambda x: g(x)' in fixed), ([], True))

    def test_over_limit_expression_remains_inline_with_simplification_advice(self):
        source = 'value = f(lambda x: g(x) + h(x))\n'
        self.put('src/demo/long.py', source)
        setting = self.root / '.quard/config.toml'
        setting.write_text(setting.read_text(encoding='utf-8')
                           + '\n[stairfmt]\ninline_limit = 12\n',
                           encoding='utf-8')
        code, report, _ = self.run_fix('src/demo/long.py')
        self.assertEqual((code, report['exit_code'], report['remaining'][0]['rule'],
                          'Упростите' in report['remaining'][0]['suggestion'],
                          'lambda x: g(x) + h(x)' in (self.root / 'src/demo/long.py').read_text()),
                         (1, 1, 'ST006', True, True))

    def test_second_branch_on_one_path_requires_simplification(self):
        source = 'value = f(lambda x: combine(g(a(x), b(x)), h(x)))\n'
        self.assertEqual([item['rule'] for item in self.checked(source)['remaining']
                          if item['rule'] == 'ST007'], ['ST007'])

    def test_one_branch_can_have_many_call_bearing_children(self):
        source = 'value = f(lambda x: combine(g(x), h(x), j(x), k(x)))\n'
        self.assertEqual([item['rule'] for item in self.checked(source)['remaining']
                          if item['rule'] == 'ST007'], [])

    def test_fstring_calls_do_not_count_as_inline_branches(self):
        source = 'value = f(lambda x: wrap(f"{g(x)} {h(x)}", k(x)))\n'
        self.assertEqual([item['rule'] for item in self.checked(source)['remaining']
                          if item['rule'] == 'ST007'], [])

    def test_template_argument_does_not_block_outer_call(self):
        if sys.hexversion < 0x030E0000:
            self.skipTest('Template strings require Python 3.14')
        source = 'x = f(t"{g(1)}", h(2))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          self.checked(fixed)['remaining'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([], [], [], True))

    def test_template_interpolation_does_not_count_as_inline_branch(self):
        if sys.hexversion < 0x030E0000:
            self.skipTest('Template strings require Python 3.14')
        source = 'value = f(lambda x: wrap(t"{g(x)} {h(x)}", k(x)))\n'
        self.assertEqual([item['rule'] for item in self.checked(source)['remaining']
                          if item['rule'] == 'ST007'], [])

    def test_multifield_dictionary_without_child_calls_is_formatted(self):
        source = 'value = f({"a": 1, "b": 2})\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          '"a": 1,\n' in fixed), ([], [], True))

    def test_standalone_multifield_dictionary_is_checked_and_fixed(self):
        source = 'data = {"a": 1, "b": 2}\n'
        expected = 'data = {\n    "a": 1,\n    "b": 2\n}\n'
        findings = self.checked(source)['remaining']
        fixed, report = self.formatted(source)
        self.assertEqual(([(item['rule'], item['line']) for item in findings], fixed,
                          report['remaining'], report['warnings'], self.formatted(fixed)[0]),
                         ([('ST009', 1)], expected, [], [], expected))

    def test_first_dictionary_field_needs_own_line(self):
        source = 'data = {"a": 1,\n    "b": 2\n}\n'
        expected = 'data = {\n    "a": 1,\n    "b": 2\n}\n'
        findings = self.checked(source)['remaining']
        fixed, report = self.formatted(source)
        self.assertEqual(([(item['rule'], item['line']) for item in findings], fixed,
                          report['remaining'], report['warnings']),
                         ([('ST009', 1)], expected, [], []))

    def test_standalone_dictionary_renders_child_calls_in_one_pass(self):
        source = 'data = {"a": g(1, 2), "b": h(3, 4)}\n'
        expected = ('data = {\n    "a": g(\n        1, 2\n    ),\n'
                    '    "b": h(\n        3, 4\n    )\n}\n')
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, report['remaining'], report['warnings'],
                          self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         (expected, [], [], expected, True))

    def test_standalone_parenthesized_tuple_with_calls_converges(self):
        results = []
        for source in ('x = (g(1), h(2))\n', 'x = (g(1),)\n'):
            findings = self.checked(source)['remaining']
            fixed, report = self.formatted(source)
            results.append(('ST009' in {item['rule'] for item in findings},
                            report['remaining'], report['warnings'], self.formatted(fixed)[0] == fixed,
                            ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))))
        self.assertEqual(results, [(True, [], [], True, True)] * 2)

    def test_dictionary_walrus_value_keeps_parentheses(self):
        source = 'data = {"a": (n := g(1)), "b": 2}\n'
        fixed, report = self.formatted(source)
        self.assertEqual(('"a": (n := g(1))' in fixed, report['remaining'], report['warnings'],
                          self.formatted(fixed)[0], ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         (True, [], [], fixed, True))

    def test_dictionary_union_with_nested_call_converges_in_one_pass(self):
        source = 'x = {"a": 1, "b": 2} | g(1, 2)\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'], self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([], [], fixed, True))

    def test_binary_containers_and_calls_converge_in_one_pass(self):
        sources = [
            'x = {"a": 1, "b": 2} | {"c": 3, "d": 4}\n',
            'x = {"a": g(1, 2), "b": 0} | h(1, 2)\n',
            'x = f(*(g(1, 2) + h(3, 4)))\n',
            'x = [{"a": 1, "b": 2} | g(1, 2) for i in xs]\n',
            'x = {"v": {"a": 1, "b": 2} | g(1, 2), "n": 0}\n',
            'x = f(k={"a": g(1, 2), "b": 0} | h(1, 2))\n',
        ]
        results: list[tuple[object, object, bool, bool]] = []
        for source in sources:
            fixed, report = self.formatted(source)
            results.append((report['remaining'], report['warnings'], self.formatted(fixed)[0] == fixed,
                            ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))))
        self.assertEqual(results, [([], [], True, True)] * len(sources))

    def test_multiline_single_simple_argument_is_reported_and_fixed(self):
        source = 'x = f(a[\n    0\n])\n'
        findings = self.checked(source)
        fixed, report = self.formatted(source)
        self.assertEqual(('ST003' in [item['rule'] for item in findings['remaining']],
                          'f(a[' in fixed and '\n' not in fixed.rstrip('\n'),
                          report['remaining'], report['warnings'], self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         (True, True, [], [], fixed, True))

    def test_bare_cr_multiline_index_never_leaves_silent_findings(self):
        source = 'x = f(a[\r    0\r], 1)\r'
        path = self.root / 'src/demo/working.py'
        path.write_bytes(source.encode('utf-8'))
        _, report, _ = self.run_fix('src/demo/working.py')
        self.assertEqual((report['remaining'] == [], bool(report['warnings']),
                          ast.dump(ast.parse(path.read_bytes())) == ast.dump(ast.parse(source))),
                         (True, False, True))

    def test_nested_standalone_dictionary_expands_with_container(self):
        source = 'data = [{"a": 1, "b": 2}]\n'
        expected = 'data = [\n    {\n        "a": 1,\n        "b": 2\n    }\n]\n'
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, report['remaining'], report['warnings'], self.formatted(fixed)[0]),
                         (expected, [], [], expected))

    def test_standalone_dictionary_in_comprehension_converges(self):
        source = 'data = [{"a": 1, "b": 2} for value in values]\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([], [], fixed, True))

    def test_multifield_dictionary_inside_protected_call_argument_is_reported(self):
        source = 'value = f(lambda: {"a": 1, "b": 2})\n'
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, [item['rule'] for item in report['remaining']],
                          len(report['warnings']), self.formatted(fixed)[0]),
                         (source, ['ST009'], 1, source))

    def test_standalone_dictionary_with_comment_is_left_intact(self):
        source = 'data = {"a": 1,  # keep\n        "b": 2}\n'
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, [item['rule'] for item in report['remaining']],
                          len(report['warnings'])), (source, ['ST009'], 1))

    def test_standalone_dictionary_keeps_crlf(self):
        path = self.root / 'src/demo/working.py'
        path.write_bytes(b'data = {"a": 1, "b": 2}\r\n')
        code, report, _ = self.run_fix('src/demo/working.py')
        self.assertEqual((code, report['remaining'], path.read_bytes()),
                         (0, [], b'data = {\r\n    "a": 1,\r\n    "b": 2\r\n}\r\n'))

    def test_dictionary_inside_fstring_remains_outside_scope(self):
        source = 'data = f"{ {\'a\': 1, \'b\': 2} }"\n'
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, report['remaining'], report['warnings']),
                         (source, [], []))

    def test_calls_in_list_follow_staircase_without_container_findings(self):
        source = 'value = f([g(1), h(2), 3])\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          'g(1),\n' in fixed, 'h(2),\n' in fixed),
                         ([], [], True, True))

    def test_commented_parent_skips_indentation_breaking_child(self):
        source = 'value = f(g(h(1)), k(2),  # keep\n    j(3))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((ast.dump(ast.parse(fixed)), '# keep' in fixed,
                          not any(item['rule'] == 'ST010' for item in report['remaining']),
                          self.formatted(fixed)[0] == fixed),
                         (ast.dump(ast.parse(source)), True, True, True))

    def test_successful_fix_is_clean_and_idempotent(self):
        self.put("src/demo/working.py", "x = f(g(h(1)), k(2))\n")
        code, report, _ = self.run_fix("src/demo/working.py")
        code_again, report_again, _ = self.run_fix("src/demo/working.py")
        self.assertEqual((code, report["status"], len(report["changed_files"]),
                          code_again, report_again["changed_files"], report_again["remaining"],
                          (self.root / report["report_path"]).is_file()),
                         (0, "passed", 1, 0, [], [], True))

    def test_starred_and_double_starred_calls_converge(self):
        source = 'x = f(g(1, 2), *h(3, 4), **j(5, 6))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          self.checked(fixed)['remaining'], '*h(\n' in fixed,
                          '**j(\n' in fixed, self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([], [], [], True, True, fixed, True))

    def test_benchmark_composition_converges_with_simplification_left(self):
        source = '''import json
def run(path, self, test, populations):
    _ = (path / "run.json").write_text(json.dumps({
        "detector": self.detector.__class__.__name__,
        "test_dataset": self.datasets[2].__class__.__name__,
        "test_ids": [item.name() for item in test],
        "test_labels": [item.label() for item in test],
        "test_count": len(test),
        "trajectories": {
            split: [{"id": item.name(), "path": f"trajectories/{split}/{index}"}
                    for index, item in enumerate(population)]
            for split, population in zip(("train", "validation", "test"), populations,
                                        strict=True)
        },
    }, indent=2) + "\\n", encoding="utf-8")
'''
        fixed, report = self.formatted(source)
        self.assertEqual((ast.dump(ast.parse(source)) == ast.dump(ast.parse(fixed)),
                          {item['rule'] for item in report['remaining']}, report['warnings'],
                          '        ) + "\\n", encoding="utf-8"' in fixed,
                          '                        for index, item in enumerate(population)' in fixed,
                          self.formatted(fixed)[0] == fixed,
                          {item['rule'] for item in self.checked(fixed)['remaining']}),
                         (True, {'ST007'}, [], True, True, True, {'ST007'}))

    def test_binary_operand_layout_converges_for_four_call_combinations(self):
        sources = ('x = (g(1) +\n     h(2))\n',
                   'x = f(g(1, 2) + h(3), 5)\n',
                   'x = f(g(1) + h(2, 3), 5)\n',
                   'x = (g(1, 2) + h(3, 4))\n')
        results = [self.formatted(source) for source in sources]
        self.assertEqual([(ast.dump(ast.parse(source)) == ast.dump(ast.parse(fixed)),
                           report['remaining'], report['warnings'],
                           self.formatted(fixed)[0] == fixed)
                          for source, (fixed, report) in zip(sources, results)],
                          [(True, [], [], True)] * 4)

    def test_multiline_binary_with_two_following_arguments_converges(self):
        source = 'x = f(g(1) + h(2, 3), 0, 1)\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_parenthesized_binary_operand_converges(self):
        source = 'x = (g(1, 2) + h(3, 4)) * z(5, 6)\n'
        fixed, report = self.formatted(source)
        self.assertEqual(({'ST012', 'ST001', 'ST002', 'ST004'}
                          .isdisjoint({item['rule'] for item in report['remaining']}),
                          report['warnings'], ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), (True, [], True, fixed))

    def test_parenthesized_binary_argument_converges_in_one_pass(self):
        source = 'x = f((g(1, 2) + h(3, 4)), 0)\n'
        fixed, report = self.formatted(source)
        self.assertEqual((fixed.count('('), report['remaining'], report['warnings'],
                          self.checked(fixed)['remaining'], self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         (source.count('('), [], [], [], fixed, True))

    def test_unsupported_root_fixes_all_independent_children_in_one_pass(self):
        source = 'x = f(a[g(1, 2):h(3, 4)], k(1, 2))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((self.formatted(fixed)[0], ast.dump(ast.parse(fixed)),
                          {item['rule'] for item in report['remaining']},
                          any('Subscript' in item['reason'] for item in report['warnings'])),
                         (fixed, ast.dump(ast.parse(source)), {'ST001', 'ST002', 'ST004'}, True))

    def test_explicit_continuation_preserves_source_and_reports_manual_layout(self):
        source = 'x = f(lambda: g(1) + \\\n h(2))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, self.formatted(fixed)[0],
                          {item['rule'] for item in report['remaining']},
                          any('синтаксически некорректен' in item['reason']
                              for item in report['warnings'])),
                         (source, source, {'ST006'}, True))

    def test_unsupported_parent_does_not_create_new_indentation_violation(self):
        source = 'x = f(-g(1, 2), h(3))\n'
        before = self.checked(source)['remaining']
        fixed, report = self.formatted(source)
        self.assertEqual((sum(item['rule'] == 'ST010' for item in report['remaining']),
                          self.formatted(fixed)[0], ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         (sum(item['rule'] == 'ST010' for item in before), fixed, True))

    def test_comprehension_iterable_dictionary_converges(self):
        source = 'x = f([v for v in {"a": g(1), "b": h(2)}])\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_trailing_type_ignore_stays_on_its_statement(self):
        source = 'x = f(g(1), h(2))  # type: ignore[call-arg]\n'
        fixed, report = self.formatted(source)
        self.assertEqual((ast.parse(fixed, type_comments=True).type_ignores[0].lineno,
                          fixed.splitlines()[0].endswith('# type: ignore[call-arg]'),
                          bool(report['remaining'])), (1, True, True))

    def test_trailing_pylint_directive_stays_on_its_statement(self):
        source = 'x = f(g(1), h(2))  # pylint: disable=unused-argument\n'
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, report['exit_code'], bool(report['remaining'])),
                         (source, 1, True))

    def test_trailing_comment_stays_on_its_statement(self):
        source = 'x = f(g(1), h(2))  # ordinary note\n'
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, report['exit_code']), (source, 1))

    def test_hash_inside_string_does_not_block_fix(self):
        source = 'x = f(g("#"), h(2))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'], 'g("#"),\n' in fixed),
                         ([], [], True))

    def test_unicode_before_calls_keeps_character_columns(self):
        source = 'x = f("😀", g(1), h(2))\n'
        findings = self.checked(source)['remaining']
        self.assertEqual([(item['rule'], item['column']) for item in findings],
                         [('ST002', source.index('"😀"') + 1),
                          ('ST001', source.index('g(1)') + 1),
                          ('ST001', source.index('h(2)') + 1),
                          ('ST004', source.rindex(')') + 1)])

    def test_normalized_unicode_method_keeps_position_and_fixes(self):
        source = 'x = f(g(1), obj.K(2))\n'
        checked = self.checked(source)
        fixed, report = self.formatted(source)
        self.assertEqual((checked['status'], ('ST001', source.index('obj.K') + 5) in
                          [(item['rule'], item['column']) for item in checked['remaining']],
                          'obj.K(2)' in fixed,
                          report['remaining'], report['warnings']),
                         ('violations', True, True, [], []))

    def test_normalized_unicode_keyword_keeps_original_spelling(self):
        source = 'x = f(K=1, x=2)\n'
        fixed, report = self.formatted(source)
        self.assertEqual(('K=1' in fixed, report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         (True, [], [], True))

    def test_normalized_unicode_method_chain_keeps_original_spelling(self):
        source = 'x = f(g(1), obj.K(2).𝒂(3))\n'
        fixed, report = self.formatted(source)
        self.assertEqual(('obj.K(2)\n    .𝒂(3)' in fixed, report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         (True, [], [], True))

    def test_unicode_before_container_keeps_character_column(self):
        source = 'x = f("😀", {"a": 1, "b": 2})\n'
        findings = self.checked(source)['remaining']
        self.assertEqual([item['column'] for item in findings if item['rule'] == 'ST009'],
                         [source.index('{') + 1])

    def test_unicode_before_binary_keeps_character_alignment(self):
        source = 'é = (g(1, 2) +\n     h(3, 4))\n'
        fixed, report = self.formatted(source)
        ascii_fixed, _ = self.formatted(source.replace('é', 'e'))
        self.assertEqual((fixed.replace('é', 'e'), report['remaining'],
                          ast.dump(ast.parse(source)) == ast.dump(ast.parse(fixed))),
                         (ascii_fixed, [], True))

    def test_unparenthesized_binary_converges_in_one_pass(self):
        source = 'x = g(1, 2) + h(3, 4)\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], self.formatted(fixed)[0] == fixed), ([], True))

    def test_subscripted_binary_converges_in_one_pass(self):
        source = 'x = (f(g(1, 2)[0]) + h(2))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], self.formatted(fixed)[0] == fixed), ([], True))

    def test_keyword_binary_converges_in_one_pass(self):
        source = 'x = f(key=g(1, 2) + h(3), other=0)\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], self.formatted(fixed)[0] == fixed), ([], True))

    def test_walrus_keyword_keeps_required_parentheses(self):
        source = 'x = f(k=(v := g(1, 2)), other=h(3))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          self.checked(fixed)['remaining'], 'k=(v := g(1, 2))' in fixed,
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], [], True, True, fixed))

    def test_noncanonical_binary_break_has_st012_not_sibling_st001(self):
        source = 'x = (g(1) +\n     h(2))\n'
        self.assertEqual([item['rule'] for item in self.checked(source)['remaining']], ['ST012'])

    def test_trailing_comment_does_not_report_unfixable_binary_break(self):
        source = 'x = (g(1) +\n     h(2))  # keep\n'
        self.assertEqual([item['rule'] for item in self.checked(source)['remaining']], [])

    def test_binary_break_reaches_consumer_as_positioned_violation(self):
        self.put('src/demo/working.py', 'x = (g(1) +\n     h(2))\n')
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / '.quard/reports/run'
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item['name'] == 'stairfmt')
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result['status'], result['exit_code'],
                          [(item['rule'], item['line'], item['is_infrastructure_error'])
                           for item in result['diagnostics']]),
                         ('violations', 1, [('ST012', 1, False)]))

    def test_short_binary_operands_and_original_parentheses_converge(self):
        sources = ('x = g(1) + h(2)\n',
                   'x = (g(1) +\n     h(2))\n',
                   'x = f(g(1) +\n      h(2), 3)\n',
                   'x = f((g(1) +\n       h(2)), 3)\n')
        results: list[tuple[list[object], bool, bool, bool]] = []
        for source in sources:
            fixed, report = self.formatted(source)
            results.append((report['remaining'], ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                            self.formatted(fixed)[0] == fixed,
                            fixed.count('(') == source.count('(')))
        self.assertEqual(results, [([], True, True, True)] * len(sources))

    def test_multiline_expression_limit_is_per_line(self):
        source = 'x = f((g(a) +\n       h(a)), 3)\n'
        self.assertEqual([item['rule'] for item in self.checked(source)['remaining']
                          if item['rule'] in {'ST006', 'ST007'}], [])

    def test_report_dictionary_keeps_subscript_value(self):
        source = 'x = report({"rate": state[4].get_last_lr()[0], "count": 2})\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          '"rate": state[4].get_last_lr()[0]' in fixed,
                          self.formatted(fixed)[0] == fixed), ([], [], True, True))

    def test_nested_call_under_subscript_is_fixed(self):
        source = 'x = f(g(1, 2)[0], 0)\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          self.formatted(fixed)[0] == fixed), ([], [], True))

    def test_result_of_call_can_be_called_with_nested_arguments(self):
        source = 'x = make()(g(1), h(2))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          self.checked(fixed)['remaining'], 'make()(\n' in fixed,
                          self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([], [], [], True, fixed, True))

    def test_subscripted_callable_and_nested_argument_converge(self):
        source = 'x = f(g(1, 2)[0](h(3)), 0)\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          self.checked(fixed)['remaining'], self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([], [], [], fixed, True))

    def test_called_lambda_keeps_required_parentheses(self):
        source = 'x = f((lambda y: y)(g(1, 2)), h(3))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          self.checked(fixed)['remaining'], '(lambda y: y)(' in fixed,
                          self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([], [], [], True, fixed, True))

    def test_parenthesized_callable_keeps_required_parentheses(self):
        source = 'x = (a if cond else b)(g(1), h(2))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          '(a if cond else b)(\n' in fixed,
                          self.formatted(fixed)[0],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([], [], True, fixed, True))

    def test_existing_parentheses_around_chain_are_not_duplicated(self):
        source = 'x = (a().b().c())\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], fixed.count('('),
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([], source.count('('), True))

    def test_multiline_literal_is_not_a_simple_call_argument(self):
        source = 'x = f(\n    """hello\nworld"""\n)\n'
        found = self.checked(source)
        fixed, report = self.formatted(source)
        self.assertEqual((found['remaining'], fixed, report['remaining'], report['warnings']),
                         ([], source, [], []))

    def test_protected_expression_with_multiline_literal_needs_no_simplification(self):
        source = 'x = f(lambda: """first\nsecond""")\n'
        self.assertEqual(self.checked(source)['remaining'], [])

    def test_nested_call_under_attribute_is_fixed(self):
        source = 'x = f(g(1, 2).attr, 0)\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          self.formatted(fixed)[0] == fixed), ([], [], True))

    def test_multifield_dictionary_inside_single_field_dictionary_is_fixed(self):
        source = 'x = f({"a": {"x": 1, "y": 2}})\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          self.formatted(fixed)[0] == fixed), ([], [], True))

    def test_comprehension_clauses_and_filter_follow_expanded_dictionary(self):
        source = ('x = f([{"a": item.name(), "b": 2} for group in groups '
                  'for item in group if item.active])\n')
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          '        for group in groups\n        for item in group if item.active' in fixed,
                          self.formatted(fixed)[0] == fixed), ([], [], True, True))

    def test_walrus_filter_keeps_required_parentheses(self):
        source = 'x = f([{"a": g(i, 2), "b": h(i)} for i in values if (n := p(i))], 0)\n'
        fixed, report = self.formatted(source)
        self.assertEqual(({item['rule'] for item in report['remaining']}, report['warnings'],
                          'if (n := p(i))' in fixed, '"a": g(' in fixed,
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ({'ST007'}, [], True, True, True, fixed))

    def test_multifield_dictionary_comprehension_key_deepens_clauses(self):
        source = ('x = f({{"a": item, "b": group}: value for group in groups '
                  'for item in group if item.active for value in item.values})\n')
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          '        }:' in fixed and ' for group in groups\n' in fixed,
                          '            for item in group if item.active\n' in fixed,
                          '                for value in item.values\n' in fixed,
                          self.formatted(fixed)[0] == fixed,
                          self.checked(fixed)['remaining']),
                         ([], [], True, True, True, True, True, []))

    def test_conditional_dictionaries_in_comprehension_converge(self):
        source = 'x = [v for v in ({"a": g(1), "b": h(2)} if cond else {"a": 1, "b": 2})]\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_conditional_dictionaries_inside_call_converge(self):
        source = 'x = f({"a": g(1), "b": h(2)} if cond else {"a": 1, "b": 2}, j(3))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_await_argument_converges(self):
        source = 'async def run():\n    return f(key=await g(1, 2), other=h(3))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_await_list_converges_without_dictionary_warning(self):
        source = 'async def run():\n    return [await f(g(1, 2)), h(3)]\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_yield_argument_converges(self):
        source = 'def run():\n    x = f((yield g(1, 2)), h(3))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_yield_from_argument_converges(self):
        source = 'def run():\n    x = f((yield from g(1, 2)), h(3))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_call_inside_subscript_argument_converges(self):
        source = 'x = f(a[g(1, 2)], h(3))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_interleaved_unpacking_keywords_preserves_ast(self):
        source = 'x = f(a=g(1), *h(2), b=j(3), **k(4))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_dictionary_unpacking_and_callable_field_preserve_ast(self):
        source = 'data = {**defaults, g(1): h(2), **overrides}\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_dictionary_in_generator_argument_preserves_ast(self):
        source = 'x = f(({"a": i, "b": g(i)} for i in xs), 0)\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_independent_call_fixes_beside_commented_call(self):
        source = 'x = f(g(1), h(2))\ny = f(g(3),  # keep\n    h(4))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['changed_files'],
                          [item['rule'] for item in report['remaining']],
                          'x = f(\n' in fixed and '# keep\n' in fixed,
                          self.formatted(fixed)[0]),
                         (['src/demo/working.py'], ['ST001', 'ST002', 'ST008', 'ST004'], True, fixed))

    def test_fstring_argument_does_not_hide_outer_call(self):
        source = 'x = f(f"{g(1)}", h(2))\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          'f"{g(1)}"' in fixed, self.formatted(fixed)[0]),
                         ([], [], True, True, fixed))

    def test_many_independent_calls_converge(self):
        source = ''.join(f'x{i} = f(g({i}), h({i}))\n' for i in range(30))
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], [], True, fixed))

    def test_single_field_dictionary_comprehension_key_stays_inline(self):
        source = 'x = f({{"a": item}: value for item in values})\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], '{{"a": item}: value for item in values}' in fixed),
                         ([], True))

    def test_fixable_comprehension_break_reports_st012(self):
        source = 'x = f([\n    item for item in values\n])\n'
        self.assertEqual([item['rule'] for item in self.checked(source)['remaining']], ['ST012'])

    def test_crlf_fix_and_check_converge_without_rewriting_canonical_lines(self):
        path = self.root / 'src/demo/working.py'
        source = b'\xef\xbb\xbf# keep\r\nx = f(g(1), h(2))\r\ny = f(\r\n    g(3)\r\n)\r\n'
        path.write_bytes(source)
        code, report, _ = self.run_fix('src/demo/working.py')
        fixed = path.read_bytes()
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / '.quard/reports/run'
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item['name'] == 'stairfmt')
        result = execute_job(job, self.root, directory, 30)
        again, repeat, _ = self.run_fix('src/demo/working.py')
        self.assertEqual((code, report['remaining'], report['warnings'], fixed,
                          result['status'], result['exit_code'], result['diagnostics'],
                          again, repeat['changed_files'], path.read_bytes()),
                         (0, [], [], b'\xef\xbb\xbf# keep\r\nx = f(\r\n    g(1),\r\n'
                          b'    h(2)\r\n)\r\ny = f(\r\n    g(3)\r\n)\r\n',
                          'passed', 0, [], 0, [], fixed))

    def test_mixed_endings_preserve_unedited_lines_and_local_new_lines(self):
        path = self.root / 'src/demo/working.py'
        source = (b'# LF stays\nfirst = f(g(1), h(2))\n# CRLF stays\r\n'
                  b'second = f(g(3), h(4))\r\n# LF stays too\n')
        path.write_bytes(source)
        code, report, _ = self.run_fix('src/demo/working.py')
        fixed = path.read_bytes()
        again, repeat, _ = self.run_fix('src/demo/working.py')
        self.assertEqual((code, report['remaining'], report['warnings'], fixed,
                          again, repeat['changed_files'], path.read_bytes()),
                         (0, [], [], b'# LF stays\nfirst = f(\n    g(1),\n    h(2)\n)\n'
                          b'# CRLF stays\r\nsecond = f(\r\n    g(3),\r\n'
                           b'    h(4)\r\n)\r\n# LF stays too\n', 0, [], fixed))

    def test_bare_cr_before_lf_call_does_not_change_inserted_line_endings(self):
        path = self.root / 'src/demo/working.py'
        path.write_bytes(b'before = 1\rvalue = f(g(1), h(2))\n')
        code, report, _ = self.run_fix('src/demo/working.py')
        self.assertEqual((code, report['remaining'], path.read_bytes()),
                          (0, [], b'before = 1\rvalue = f(\n    g(1),\n    h(2)\n)\n'))

    def test_bare_cr_tabbed_block_reports_indentation_and_fixes(self):
        path = self.root / 'src/demo/working.py'
        path.write_bytes(b'if True:\r\tvalue = 1\r')
        checked = self.checked('if True:\r\tvalue = 1\r')
        code, report, _ = self.run_fix('src/demo/working.py')
        self.assertEqual((checked['errors'], [(item['rule'], item['line']) for item in
                           checked['remaining']], code, report['remaining'], path.read_bytes()),
                         ([], [('ST010', 2)], 0, [], b'if True:\r    value = 1\r'))

    def test_bare_cr_protected_expression_break_is_detected_and_fixed(self):
        source = 'x = f(lambda x: (g(x)\r + h(x)))\r'
        findings = self.checked(source)
        fixed, report = self.formatted(source)
        self.assertEqual(([(item['rule'], item['line']) for item in findings['remaining']],
                          report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         ([('ST012', 1)], [], [], True))

    def test_bare_cr_inside_call_comment_blocks_fix_with_warning(self):
        path = self.root / 'src/demo/working.py'
        source = b'a=0\rx=f(1, #keep\r 2)\r'
        path.write_bytes(source)
        code, report, _ = self.run_fix('src/demo/working.py')
        self.assertEqual((code, path.read_bytes(), report['changed_files'],
                          [item['rule'] for item in report['remaining']],
                          [item['reason'] for item in report['warnings']]),
                         (1, source, [], ['ST002', 'ST008', 'ST010', 'ST004'],
                          ['пропущено: комментарий внутри вызова']))

    def test_bare_cr_type_ignore_inside_call_is_preserved(self):
        path = self.root / 'src/demo/working.py'
        source = b'a=0\rx=f(1, # type: ignore\r 2)\r'
        path.write_bytes(source)
        code, report, _ = self.run_fix('src/demo/working.py')
        self.assertEqual((code, path.read_bytes(), report['changed_files'],
                          len(report['warnings'])), (1, source, [], 1))

    def test_mixed_endings_string_split_keeps_neighboring_endings(self):
        path = self.root / 'src/demo/working.py'
        source = b'# LF stays\nvalue = "' + b'a word ' * 26 + b'"\r\n# LF remains\n'
        path.write_bytes(source)
        code, report, _ = self.run_fix('src/demo/working.py')
        fixed = path.read_bytes()
        again, repeat, _ = self.run_fix('src/demo/working.py')
        self.assertEqual((code, report['remaining'], report['warnings'],
                          fixed.startswith(b'# LF stays\nvalue = (\r\n'),
                          fixed.endswith(b')\r\n# LF remains\n'),
                          ast.dump(ast.parse(source)) == ast.dump(ast.parse(fixed)),
                          again, repeat['changed_files'], path.read_bytes()),
                         (0, [], [], True, True, True, 0, [], fixed))

    def test_rewritten_region_keeps_existing_mixed_endings(self):
        path = self.root / 'src/demo/working.py'
        source = b'x = f(\r\n    g(1),\n    h(2))\r\n# untouched\n'
        path.write_bytes(source)
        code, report, _ = self.run_fix('src/demo/working.py')
        fixed = path.read_bytes()
        check = subprocess.run([sys.executable, '-B', str(QUARD / 'stairfmt.py'), '--check',
                                '--project', str(self.root), '--json', 'src/demo/working.py'],
                               cwd=self.root, capture_output=True, text=True, check=False)
        again, repeat, _ = self.run_fix('src/demo/working.py')
        self.assertEqual((code, report['remaining'], fixed, check.returncode,
                          json.loads(check.stdout)['remaining'],
                          ast.dump(ast.parse(source)) == ast.dump(ast.parse(fixed)), again,
                          repeat['changed_files'], path.read_bytes()),
                         (0, [], b'x = f(\r\n    g(1),\n    h(2)\r\n)\r\n# untouched\n',
                          0, [], True, 0, [], fixed))

    def test_explicit_paths_obey_discovered_scope(self):
        self.put("src/demo/generated/ignored.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        code, report, _ = self.run_fix("src/demo")
        self.assertEqual((code, report["checked_files"], report["changed_files"],
                          (self.root / "src/demo/generated/ignored.py").read_text()),
                         (0, 3, ["src/demo/working.py"], "x = f(g(1), h(2))\n"))

    def test_empty_python_scope_passes_check_and_fix(self):
        for name in ('src/demo/__init__.py', 'src/demo/domain/api.py', 'tests/test_value.py'):
            (self.root / name).unlink()
        command = subprocess.run([sys.executable, '-B', str(QUARD / 'stairfmt.py'), '--check',
                                  '--project', str(self.root), '--json'], cwd=self.root,
                                 capture_output=True, text=True, check=False)
        checked = json.loads(command.stdout)
        code, fixed, _ = self.run_fix()
        self.assertEqual((command.returncode, checked['checked_files'], code,
                          fixed['checked_files'], fixed['changed_files']), (0, 0, 0, 0, []))

    def test_checkout_opt_out_and_consumer_default(self):
        self.assertEqual((read_checks(QUARD)["stairfmt"], read_checks(self.root)["stairfmt"]),
                         (False, True))

    def test_missing_switch_keeps_consumer_default(self):
        setting = self.root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "stairfmt = true\n", ""), encoding="utf-8")
        self.assertTrue(read_checks(self.root)["stairfmt"])

    def test_command_entry_point_prints_archived_json(self):
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--fix",
                                  "--project", str(self.root), "--json", "src/demo/working.py"],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        report = json.loads(command.stdout)
        self.assertEqual((command.returncode, report["exit_code"],
                          (self.root / report["report_path"]).is_file()), (0, 0, True))

    def test_archive_failure_keeps_applied_edits_and_reports_storage_error(self):
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        directory = self.root / ".quard/reports"
        directory.mkdir(parents=True)
        (directory / "stairfmt").write_bytes(b"blocked archive")
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--fix",
                                  "--project", str(self.root), "--json", "src/demo/working.py"],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        report = json.loads(command.stdout)
        self.assertEqual((command.returncode, report["exit_code"], report["status"],
                          report["changed_files"], "report_path" in report,
                          "архив" in report["errors"][-1]["message"].lower(),
                          (directory / "stairfmt").read_bytes(),
                          (self.root / "src/demo/working.py").read_bytes()),
                         (2, 2, "error", ["src/demo/working.py"], False, True,
                          b"blocked archive", b"x = f(\n    g(1),\n    h(2)\n)\n"))

    def test_latest_failure_keeps_applied_edits_and_usable_archive(self):
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        directory = self.root / ".quard/reports/stairfmt"
        (directory / "latest.json").mkdir(parents=True)
        command = subprocess.run([sys.executable, "-B", str(QUARD / "stairfmt.py"), "--fix",
                                  "--project", str(self.root), "src/demo/working.py"],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        archives = [path for path in directory.glob("*.json") if path.is_file()]
        report = json.loads(archives[0].read_text(encoding="utf-8")) if archives else {}
        self.assertEqual((command.returncode, "CHANGED: src/demo/working.py" in command.stdout,
                          "latest.json" in command.stdout, "не обновл" in command.stdout,
                          f"REPORT: {report.get('report_path')}" in command.stdout,
                          report.get("changed_files"), report.get("exit_code"),
                          len(archives), (directory / "latest.json").is_dir(),
                          (self.root / "src/demo/working.py").read_bytes()),
                         (2, True, True, True, True, ["src/demo/working.py"], 0, 1, True,
                          b"x = f(\n    g(1),\n    h(2)\n)\n"))

    def test_fix_reports_parse_failure_without_hiding_peer_edits(self):
        self.put("src/demo/working.py", "x = f(g(1), h(2))\n")
        self.put("src/demo/broken.py", "def x(:\n")
        code, report, _ = self.run_fix()
        self.assertEqual((code, report["status"], report["changed_files"],
                          report["errors"][0]["path"]),
                         (2, "error", ["src/demo/working.py"], "src/demo/broken.py"))

    def test_rejected_path_still_archives_a_fix_failure(self):
        code, report, _ = self.run_fix("src/demo/generated/ignored.py")
        self.assertEqual((code, report["status"], len(report["errors"]),
                          (self.root / report["report_path"]).is_file()),
                         (2, "error", 1, True))

    def test_corrupt_manifest_is_an_infrastructure_failure(self):
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / ".quard/reports/run"
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item["name"] == "stairfmt")
        (directory / "stairfmt-files.json").write_text("invalid", encoding="utf-8")
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          result["diagnostics"][0]["rule"]),
                         ("error", 2, "STAIRFMT_EXECUTION_ERROR"))

    def test_disabled_job_has_no_manifest(self):
        setting = self.root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "stairfmt = true", "stairfmt = false"), encoding="utf-8")
        fake, seen = fake_tools()
        with patch("quard.check_runner.native_check_runner.require_source_pin"), patch(
                "quard.check_runner.runner_utils.clone_engine_enabled", return_value=False):
            report = NativeCheckRunner().run(self.root, capture_fn=fake, versions_fn=self.versions)
        job = next(item for item in report["tools"] if item["name"] == "stairfmt")
        directory = (self.root / report["report_path"]).parent
        self.assertEqual((job["status"], job["exit_code"], "stairfmt" in seen,
                          (directory / "stairfmt-files.json").exists()),
                         ("skipped", None, False, False))

    def versions(self, root: Path) -> tuple[dict[str, str], list[dict[str, object]]]:
        return {}, []

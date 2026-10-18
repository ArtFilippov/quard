"""Check long literals and native policy on isolated Stairfmt consumers."""
from __future__ import annotations

import ast
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import tokenize
import unittest
from typing import Any, override

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from offline_fixtures import create_repo
from quard._policy import discover_files, read_scope
from quard.architecture_draft.generated_selection_utils import IDENTITY
from quard.check import execute_job, make_jobs
from quard.policy_utils import configure


class TestStairfmtStrings(unittest.TestCase):
    """Verify literal wrapping and the native diagnostics it deliberately changes.

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

    def put(self, source: str) -> None:
        (self.root / 'src/demo/working.py').write_text(source, encoding='utf-8')

    def checked(self, source: str) -> dict[str, Any]:
        self.put(source)
        command = subprocess.run([sys.executable, '-B', str(QUARD / 'stairfmt.py'), '--check',
                                  '--project', str(self.root), '--json', 'src/demo/working.py'],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        return json.loads(command.stdout)

    def formatted(self, source: str) -> tuple[str, dict[str, Any]]:
        self.put(source)
        subprocess.run([sys.executable, '-B', str(QUARD / 'stairfmt.py'), '--fix',
                        '--project', str(self.root), 'src/demo/working.py'],
                       cwd=self.root, capture_output=True, text=True, check=False)
        report = json.loads((self.root / '.quard/reports/stairfmt/latest.json').read_text(
            encoding='utf-8'))
        return (self.root / 'src/demo/working.py').read_text(encoding='utf-8'), report

    def generated(self) -> None:
        (self.root / '.quard/architecture.generated.json').write_text(
            json.dumps(IDENTITY), encoding='utf-8')
        configure(self.root)

    def test_long_literals_without_calls_and_in_multifield_dictionaries(self):
        source = ('title = "' + 'a word ' * 26 + '"\n'
                  'record = {"first": "' + 'long phrase ' * 16
                  + '", "second": "' + 'another word ' * 15 + '"}\n')
        findings = self.checked(source)['remaining']
        fixed, report = self.formatted(source)
        self.assertEqual(([(item['rule'], item['line']) for item in findings],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          [item['rule'] for item in report['remaining']],
                          self.formatted(fixed)[0] == fixed),
                         ([('ST011', 1), ('ST009', 2), ('ST011', 2), ('ST011', 2)],
                          True, [], True))

    def test_two_long_literals_in_separate_statements_are_fixed(self):
        source = 'first = "' + 'a' * 161 + '"\nsecond = "' + 'b' * 161 + '"\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['exit_code'], report['remaining'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source))),
                         (0, [], True))

    def test_enabled_job_reports_long_literal_without_calls(self):
        self.put('value = "' + 'a word ' * 26 + '"\n')
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / '.quard/reports/run'
        job = next(item for item in make_jobs(self.root, directory, files, True, [])
                   if item['name'] == 'stairfmt')
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result['status'], result['exit_code'],
                          [(item['rule'], item['line']) for item in result['diagnostics']]),
                         ('violations', 1, [('ST011', 1)]))

    def test_long_word_and_escaped_characters_keep_value_and_limit(self):
        source = 'value = "' + ('supercalifragilistic' * 12) + '\\n' + ('é' * 12) + '"\n'
        fixed, report = self.formatted(source)
        self.assertEqual((ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          report['remaining'], self.formatted(fixed)[0] == fixed,
                          all(len(token.string[1:-1]) <= 160 for token in
                              tokenize.generate_tokens(io.StringIO(fixed).readline)
                              if token.type == tokenize.STRING)),
                          (True, [], True, True))

    def test_form_feed_inside_literal_does_not_create_source_line(self):
        source = 'value = "hello\f\tworld"\nx = f(g(1), h(2))\n'
        findings = self.checked(source)
        fixed, report = self.formatted(source)
        self.assertEqual((findings['errors'],
                          {item['rule'] for item in findings['remaining']}, report['errors'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          self.formatted(fixed)[0]), ([], {'ST001', 'ST002', 'ST004'}, [], True, fixed))

    def test_escaped_long_literal_keeps_exact_spelling(self):
        source = 'value = "' + (r'one\ntwo\x20three\u0041\U0001F642 ' * 10) + '"\n'
        fixed, report = self.formatted(source)
        self.assertEqual((report['remaining'], report['warnings'],
                          ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          fixed.count(r'\x20') == 10, fixed.count(r'\u0041') == 10,
                          fixed.count(r'\U0001F642') == 10,
                          self.formatted(fixed)[0] == fixed),
                         ([], [], True, True, True, True, True))

    def test_undivisible_escape_reports_reason_without_changing_source(self):
        source = 'value = "\\N{LATIN SMALL LETTER A WITH RING ABOVE}"\n'
        setting = self.root / '.quard/config.toml'
        setting.write_text(setting.read_text(encoding='utf-8')
                           + '\n[stairfmt]\nstring_limit = 12\n', encoding='utf-8')
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, report['exit_code'], report['remaining'][0]['rule'],
                          'нет безопасной границы' in report['warnings'][0]['reason']),
                         (source, 1, 'ST011', True))

    def test_escaped_physical_newline_reports_unfixable_long_literal(self):
        source = 'value = "' + 'a' * 170 + '\\\nb"\n'
        found = self.checked(source)
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, [item['rule'] for item in found['remaining']],
                          [item['rule'] for item in report['remaining']],
                          'физический перенос' in report['warnings'][0]['reason']),
                         (source, ['ST011'], ['ST011'], True))

    def test_escaped_bare_cr_reports_unfixable_long_literal(self):
        source = 'value = "' + 'a' * 170 + '\\\rb"\r'
        path = self.root / 'src/demo/working.py'
        path.write_bytes(source.encode('utf-8'))
        checked = subprocess.run([sys.executable, '-B', str(QUARD / 'stairfmt.py'), '--check',
                                  '--project', str(self.root), '--json', 'src/demo/working.py'],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        command = subprocess.run([sys.executable, '-B', str(QUARD / 'stairfmt.py'), '--fix',
                                  '--project', str(self.root), 'src/demo/working.py'],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        report = json.loads((self.root / '.quard/reports/stairfmt/latest.json').read_text(
            encoding='utf-8'))
        self.assertEqual((json.loads(checked.stdout)['remaining'][0]['rule'], command.returncode,
                          path.read_bytes(), report['remaining'][0]['rule'],
                          'физический перенос' in report['warnings'][0]['reason']),
                         ('ST011', 1, source.encode('utf-8'), 'ST011', True))

    def test_trailing_comment_explains_why_long_literal_is_skipped(self):
        source = 'value = "' + 'a' * 170 + '"  # keep\n'
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, report['remaining'][0]['rule'],
                          'в конце строки' in report['warnings'][0]['reason'],
                          'вручную' in report['warnings'][0]['suggestion']),
                         (source, 'ST011', True, True))

    def test_bom_and_declared_encoding_are_preserved_by_both_entry_points(self):
        results: list[tuple[object, ...]] = []
        for encoding, prefix in (('utf-8-sig', b'\xef\xbb\xbf'), ('iso-8859-1', b'')):
            source = '# coding: ' + encoding + '\nvalue = "caf\xe9 ' + 'word ' * 36 + '"\n'
            path = self.root / 'src/demo/working.py'
            path.write_bytes(source.encode(encoding))
            files = discover_files(self.root, read_scope(self.root))
            directory = self.root / '.quard/reports/run'
            job = next(item for item in make_jobs(self.root, directory, files, True, [])
                       if item['name'] == 'stairfmt')
            result = execute_job(job, self.root, directory, 30)
            command = subprocess.run([sys.executable, '-B', str(QUARD / 'stairfmt.py'), '--fix',
                                      '--project', str(self.root), '--json', 'src/demo/working.py'],
                                     cwd=self.root, capture_output=True, text=True, check=False)
            fixed = path.read_bytes()
            again = subprocess.run(command.args, cwd=self.root, capture_output=True,
                                   text=True, check=False)
            results.append((result['status'], result['exit_code'], command.returncode,
                            fixed.startswith(prefix),
                            ast.dump(ast.parse(fixed.decode(encoding))) == ast.dump(ast.parse(source)),
                            json.loads(again.stdout)['changed_files']))
        self.assertEqual(results, [('violations', 1, 0, True, True, [])] * 2)

    def test_adjacent_unicode_literal_is_wrapped_as_one_value(self):
        source = 'value = u"prefix " "' + ('word ' * 35) + '"\n'
        fixed, report = self.formatted(source)
        self.assertEqual((ast.dump(ast.parse(fixed)) == ast.dump(ast.parse(source)),
                          report['remaining'], self.formatted(fixed)[0] == fixed),
                         (True, [], True))

    def test_excluded_literals_remain_unchanged(self):
        literal = 'x' * 180
        source = ('"""' + literal + '"""\n'
                  + 'value = f("' + literal + '")\n'
                  + 'raw = r"' + literal + '"\n'
                  + 'binary = b"' + literal + '"\n'
                  + 'triple = """' + literal + '"""\n'
                  + 'interpolation = f"' + literal + '{value}"\n'
                  + 'protected = ["' + literal + '" for item in values]\n'
                  + 'operator = "' + literal + '" + "a"\n'
                  + 'single = {"key": "' + literal + '"}\n')
        fixed, report = self.formatted(source)
        self.assertEqual((fixed, [item['rule'] for item in report['remaining']
                                  if item['rule'] == 'ST011']), (source, []))

    def test_template_interpolation_calls_and_long_strings_are_excluded(self):
        if sys.hexversion < 0x030E0000:
            self.skipTest('Template strings require Python 3.14')
        source = 'x = t\'{f(g("' + 'a' * 170 + '"), h(2))}\'\n'
        checked = self.checked(source)
        fixed, report = self.formatted(source)
        self.assertEqual((checked['remaining'], fixed, report['remaining'], report['warnings']),
                         ([], source, [], []))

    def test_configured_string_limit(self):
        setting = self.root / '.quard/config.toml'
        setting.write_text(setting.read_text(encoding='utf-8')
                           + '\n[stairfmt]\nstring_limit = 12\n', encoding='utf-8')
        fixed, report = self.formatted('value = "a short phrase with spaces"\n')
        self.assertEqual((report['remaining'], '"a short "' in fixed,
                          '"phrase with "' in fixed), ([], True, True))

    def test_invalid_string_limit_is_a_configuration_error(self):
        setting = self.root / '.quard/config.toml'
        setting.write_text(setting.read_text(encoding='utf-8')
                           + '\n[stairfmt]\nstring_limit = true\n', encoding='utf-8')
        report = self.checked('value = "hello"\n')
        self.assertEqual((report['exit_code'], report['errors'][0]['message'].endswith(
            'string_limit must be a positive integer.')), (2, True))

    def test_generated_pyright_allows_wrapped_strings_after_apply(self):
        self.generated()
        config = json.loads((self.root / 'pyrightconfig.json').read_text(encoding='utf-8'))
        config['reportImplicitStringConcatenation'] = 'error'
        (self.root / 'pyrightconfig.json').write_text(json.dumps(config), encoding='utf-8')
        configure(self.root)
        updated = json.loads((self.root / 'pyrightconfig.json').read_text(encoding='utf-8'))
        self.assertEqual(updated['reportImplicitStringConcatenation'], 'none')

    def test_reviewed_pyright_retains_its_implicit_concat_policy(self):
        configure(self.root)
        updated = json.loads((self.root / 'pyrightconfig.json').read_text(encoding='utf-8'))
        self.assertEqual(updated['reportImplicitStringConcatenation'], 'error')

    def test_native_pyright_retains_unrelated_diagnostics(self):
        self.generated()
        fixed, report = self.formatted('value = "' + 'hello world ' * 16 + '"\n')
        self.put(fixed + 'missing_name\n')
        command = subprocess.run([sys.executable, '-B', '-m', 'pyright', '--outputjson'],
                                 cwd=self.root, capture_output=True, text=True, check=False)
        diagnostics = json.loads(command.stdout)['generalDiagnostics']
        self.assertEqual((report['remaining'], command.returncode,
                          any(item.get('rule') == 'reportImplicitStringConcatenation'
                              for item in diagnostics),
                          any(item.get('rule') == 'reportUndefinedVariable'
                              for item in diagnostics)), ([], 1, False, True))

    def test_pylint_keeps_same_line_implicit_concat_rule(self):
        fixed, report = self.formatted('value = "' + 'hello world ' * 16 + '"\n')
        source = fixed + 'items = ["hello " "world"]\n'
        command = subprocess.run([sys.executable, '-B', '-m', 'pylint', '--rcfile=/dev/null',
                                  '--disable=all', '--enable=implicit-str-concat',
                                  '--output-format=json', '--from-stdin', 'sample.py'],
                                 input=source, capture_output=True, text=True, check=False)
        self.assertEqual((report['remaining'],
                          [item['symbol'] for item in json.loads(command.stdout)]),
                         ([], ['implicit-str-concat']))

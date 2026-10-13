"""Consolidated public, real-engine regressions for #136–#138 and #148."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import unittest

from quard._common import read_json
from rust_clone_engine_utils import prepared_native, report


def verify_clone_redesign(test: unittest.TestCase, root: Path) -> None:
    """Check native and normalized evidence for each original near miss and control."""
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"
    (root / "src/demo/domain/api.py").write_text("", encoding="utf-8")

    def check(left: str, right: str, expected: str | tuple[str, ...] | None = None,
              same_file: bool = False) -> dict[str, Any]:
        first.write_text(left + ("\n" + right if same_file else ""), encoding="utf-8")
        second.write_text("" if same_file else right, encoding="utf-8")
        result = report(root)
        test.assertEqual(result["summary"]["infrastructure_errors"], 0, result["diagnostics"])
        kinds = (expected,) if isinstance(expected, str) else (expected or ())
        test.assertEqual(result["exit_code"], 1 if kinds else 0, result["diagnostics"])
        jobs = {job["name"]: job for job in result["tools"]}
        for kind, name in (("exact", "jscpd"), ("renamed", "jscpd-renamed"),
                           ("similar", "jscpd-similar")):
            job = jobs[name]
            native = read_json(root / job["raw_output"]["native_json"])
            test.assertEqual(native["minimumUnits"], 3)
            test.assertEqual(native["pass"], kind)
            test.assertEqual(len(native["sourcePaths"]), 5)
            clones = native.get("duplicates", [])
            diagnostics = [item for item in result["diagnostics"] if item["job"] == name]
            if kind in kinds:
                test.assertEqual((job["status"], job["exit_code"]), ("violations", 1))
                test.assertTrue(clones)
                test.assertEqual({item["rule"] for item in diagnostics},
                                 {kind.upper() + "_CLONE"})
                test.assertTrue(all(item["native_details"] in clones and
                                    not item.get("is_infrastructure_error", False)
                                    for item in diagnostics))
                for clone in clones:
                    test.assertEqual(clone["kind"], kind)
                    test.assertTrue(all(units >= 3 for units in clone["matchedUnits"]))
                    a, b = clone["firstFile"], clone["secondFile"]
                    for side in (a, b):
                        source = Path(side["name"]).read_text(encoding="utf-8")
                        test.assertEqual(side["startLoc"]["line"], side["start"])
                        test.assertEqual(side["endLoc"]["line"], side["end"])
                        test.assertEqual(source[side["startLoc"]["position"]],
                                         source.splitlines()[side["start"] - 1][
                                             side["startLoc"]["column"]])
                    test.assertEqual(clone["fragment"], "\n".join(
                        Path(a["name"]).read_text(encoding="utf-8").splitlines()[
                            a["start"] - 1:a["end"]]))
                    if same_file:
                        test.assertEqual(a["name"], b["name"])
                        test.assertTrue(a["end"] < b["start"] or b["end"] < a["start"])
                    else:
                        test.assertEqual({a["name"], b["name"]}, {str(first), str(second)})
                test.assertEqual({item["path"] for item in diagnostics},
                                 {"src/demo/first.py"} if same_file else
                                 {"src/demo/first.py", "tests/second.py"})
                _ = prepared_native(test, root, result, 3, kind)
            else:
                test.assertIn((job["status"], job["exit_code"]),
                              (("passed", 0), ("skipped", None)))
                test.assertFalse(clones)
                test.assertFalse(diagnostics)
        return result

    # #136: compact declarations with distinct fields, including shifted windows
    # inside the same declaration, cannot be renamed behavioral clones.
    provenance = ("from typing import NamedTuple\nProvenance = NamedTuple('Provenance', [\n"
                  "    ('labels_source_row', float | None), ('byte_identity_status', str | None),\n"
                  "    ('source_locator_status', str | None), ('source_links_json', str | None),\n"
                  "    ('vlm_caption_pixelprose', str | None), ('generation_prompt', str | None),\n"
                  "    ('stats_evidence_id', str | None), ('stats_source', str | None),\n"
                  "    ('stats_source_row', float | None),\n])\n")
    context = ("from typing import NamedTuple\nSourceContext = NamedTuple('SourceContext', [\n"
               "    ('tag_list', tuple[str, ...] | None),\n"
               "    ('width', float | None), ('height', float | None),\n"
               "    ('topiq_nr', float | None), ('spectral_slope', float | None),\n"
               "    ('hf_energy_ratio', float | None), ('photorealism_score', float | None),\n"
               "    ('scene_complexity_score', float | None), ('brightness', float | None),\n"
               "    ('si', float | None), ('cf', float | None), ('noise_var_mean', float | None),\n"
               "    ('technical_quality_score', float | None), ('num_faces', float | None),\n"
               "    ('total_text_area_share', float | None), ('is_multipanel', float | None),\n])\n")
    check(provenance, context)
    check(context, "", same_file=True)

    # #137: one concatenated literal and one collection are single expressions;
    # adjacent lines of those values are not separate copies of behavior.
    literals = ("def make_row():\n    row = {name: None for name in (\n"
                "        'image_path_old private_variant_id original_variant_id '\n"
                "        'labels_source_row byte_identity_status source_locator_status '\n"
                "        'vlm_caption_pixelprose generation_prompt stats_evidence_id '\n"
                "        'stats_source_row tag_list src_width src_height src_topiq_nr '\n"
                "        'src_hf_energy_ratio src_photorealism_score scene_complexity_score '\n"
                "        'src_brightness src_si src_cf src_noise_var_mean '\n"
                "    ).split()}\n    assert row == {\n"
                "        'a.png',\n        'b.png',\n        'c.png',\n"
                "        'd.png',\n        'e.png',\n    }\n    return row\n")
    check(literals, "", same_file=True)

    # #138: the shared .to_pylist() suffix, ')' and partial conditions are
    # neither three complete exact nor three complete renamed constructs.
    load = ('def load(root):\n    records = (\n'
            '        arrow.dataset(root / "private_images.parquet", '
            'format="parquet").to_table().to_pylist()\n'
            '    )\n    if not records or len(records) != 7:\n'
            '        raise ValueError("private catalogue")\n')
    other_load = ('def load(samples):\n    rows = tuple(\n'
                  '        row for row in arrow.dataset(samples, format="ipc").to_table(\n'
                  '            columns=["path", "generator"]\n'
                  '        ).to_pylist()\n    )\n'
                  '    if any(row != "real" for row in rows):\n'
                  '        raise ValueError("wrong labels")\n')
    check(load, other_load)

    behavior = ('def calculate():\n    first = 1\n'
                '    second = first + 2\n    return second * 3\n')
    check(behavior, behavior, "exact")
    check(behavior + "\n" + behavior, "", "exact", same_file=True)
    renamed = ('def work(value):\n    result = make(value)\n'
               '    item = result[0]\n    return item + 17\n')
    renamed_other = renamed.replace("value", "source").replace("result", "output").replace(
        "item", "element").replace("17", "23")
    check(renamed, renamed_other, "renamed")
    nested = ('value = Outside(\n    Middle(\n        Branch(\n'
              '            Leaf(local, 17)\n        )\n    )\n)\n')
    nested_other = nested.replace("Outside", "Different").replace("local, 17", "other, 23")
    nested_report = check(nested, nested_other, "renamed")
    native = prepared_native(test, root, nested_report, 3, "renamed")
    test.assertEqual(native["candidateUnitSizes"][str(first)], [3])
    test.assertTrue(any(item["firstFile"]["start"] == 2 and
                        item["fragment"] == "\n".join(nested.splitlines()[1:6])
                        for item in native["duplicates"]))

    top = ('def merge(value):\n    alpha = value + 91\n    beta = alpha * 8\n'
           '    gamma = beta - 6\n    delta = gamma / 9\n')
    bottom = ('    left = value + 71\n    right = left * 8\n'
              '    total = right - 6\n    adjusted = total / 9\n'
              '    return adjusted\n')
    gap = check(top + '    extra = 1\n' + bottom,
                top + '    extra = 2\n' + bottom, ("exact", "similar"))
    similar = prepared_native(test, root, gap, 3, "similar")
    test.assertTrue(all(item["method"] == "gap" and
                        item["unmatchedLines"] == [1, 1]
                        for item in similar["duplicates"]))

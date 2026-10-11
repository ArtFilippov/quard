# Python and pytest tools for clean-tests

These tools illustrate roles to look for in other ecosystems: a per-test profiler,
a change-aware selector, and the project's authoritative full-test command.
Inspect the target project's runner, plugins, lockfile, and documentation before
adapting the commands. Pin chosen dev tools using its existing dependency workflow.

## Profile with `pytest-durations`

The `pytest-durations` package emits machine-readable per-test timing JSON;
pytest's built-in `--durations` displays slow tests but does not supply that JSON.
For example, from a project that already has the plugin installed:

```bash
python -m pytest tests/path/to/group -q \
  --pytest-durations=60 --pytest-durations-min=0.005 \
  --pytest-durations-json=/tmp/group-before.json
```

Repeat with `group-after.json` and the same scope. Distinguish call times from
setup/teardown and suite elapsed time; inspect subprocess timings or per-job
reports where tests run multiple tools. A timing chart can help rank outliers,
but keep temporary JSON/plots out of tracked fixtures. If pytest's cache writes
break a clean-tree test, `-p no:cacheprovider` can keep it out of the checkout
*during profiling*. Disable an unrelated pytest plugin only when it actually
breaks collection; Quard uses `-p no:tach` for that reason, while its ordinary
native Tach check remains active.

## Use `pytest-testmon` for local iteration when suitable

`pytest-testmon` uses executed Python code to select affected tests. Verify that
the project's pytest discovery covers the tests of interest. A full seeded run
can use `--testmon-noselect`; subsequent runs use `--testmon`:

```bash
python -m pytest tests -q -o cache_dir=.cache/pytest-testmon --testmon-noselect
python -m pytest tests -q -o cache_dir=.cache/pytest-testmon --testmon
```

Choose a cache path appropriate to the project and ignore the testmon database
and cache as needed. Testmon **requires** pytest's cache provider: do not combine
it with `-p no:cacheprovider`. Confirm that an unchanged run selects no tests
and that a controlled source edit selects the affected test; restore that edit.
Changes to static data, configs, or external services may need explicitly run
tests. Keep full tests and project checks as completion gates, for example a
repository may require `unittest discover` rather than pytest. Compare their
discovery counts before claiming equal coverage.

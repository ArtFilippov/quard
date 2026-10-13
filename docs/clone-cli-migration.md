# Migrate clone analysis from CLI to a pinned Git source

New Quard revisions require the source-built `quard-clones` component in a
consumer's dev environment. A consumer using an older Quard gitlink retains
that older revision's CLI behavior. After upgrading Quard, a check without
`[tool.uv.sources].quard-clones` exits **2** with
`CLONE_SOURCE_PIN_REQUIRED`; it does not silently skip the three clone jobs.

1. Make the chosen Quard commit available from its `origin` to every consumer
   machine. An unpushed local commit or developer-local `file://` source is
   not a shared pin. Update the submodule and **stage** its gitlink. The
   installer compares the staged gitlink with the selected Quard `HEAD`.
2. Provide Rust/Cargo and a working C compiler/linker on each install or
   rebuild machine. From the consumer root, reinstall with
   `bash quard/install.sh --project .` (include `--extra NAME` for each
   optional dependency on first install). The installer pins
   `quard-clones==0.1.0` in the dev group and writes
   `[tool.uv.sources].quard-clones` with `git`, `rev` and
   `subdirectory = "rust/quard-clones"`. It preserves existing uv indexes,
   other sources, optional extras, clone minimum and native settings. The
   Rust dependencies are fixed by `rust/quard-clones/Cargo.lock`; maturin is
   fixed by the source package build requirements.
3. Commit the consumer's `pyproject.toml`, `uv.lock`, staged gitlink and
   managed `.quard/` settings together. Compare the `rev` in pyproject with
   `git ls-files --stage -- quard` (use your actual submodule path), and
   verify the `quard-clones` Git source in `uv.lock` resolves that revision.
   Publish the Quard revision and consumer commit before fresh-machine sync.
4. On a fresh machine, initialize submodules and run
   `uv sync --locked --group dev` (and the same `--extra NAME` selections as
   on install). Run `uv run --locked --group dev python quard/check.py` with
   those extras for the full static check; then run relevant runtime tests.
   Confirm the three separate `jscpd`, `jscpd-renamed`, `jscpd-similar` jobs,
   their statuses and `is_infrastructure_error` diagnostics in
   `.quard/reports/latest.json`. Archived raw JSON and stdout/stderr live
   under `.quard/reports/<run-id>/`. Exit 0 means clean; exit 1 means
   unresolved violations; exit 2 means configuration, build or tool failure.
   Keep the existing positive minimum when migrating explicit `min_lines` to
   `[duplication] min_units` (reinstall performs this migration). Its numeric
   value is preserved, but its meaning changes: the CLI counted unprepared
   physical lines; Rust counts eligible AST-owned constructs, or original
   eligible declaration lines in its exact schema/contract view. Source spans
   and fragments in reports still refer to the original files. Review any
   changed clone findings rather than treating an old CLI baseline as identical
   coverage; see the [#148 verification](issue-148-clone-verification.md).

If installation fails, inspect `.quard/logs/install.log` and
`.quard/logs/install-error.json`; packaged exact, renamed, similar and clean
probes write `.quard/logs/install-probes/`. Installation rolls back managed
files on failure; the uv cache and virtual environment may need a locked
resync. Do not replace the native diagnostics or change limits to get a green
check. Pylint's duplication rule continues independently. The historical
`jscpd==5.4.0` CLI remains in Quard's **dev** group for the reproducible
[benchmark](issue-126-clone-benchmark.md), not in consumer installs.

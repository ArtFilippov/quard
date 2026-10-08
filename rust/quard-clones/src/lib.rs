use std::collections::HashMap;

use cpd_core::detect::{detect_prepared, PathFilters};
use cpd_core::models::{CpdClone, KindFilter, SourceFile};
use cpd_finder::orchestrate::{FilePreparer, RunConfig};
use cpd_finder::statistics;
use cpd_reporter::shared::extract_lines;
use cpd_tokenizer::tokenizer::Mode;
use pyo3::prelude::*;
use serde_json::{json, Value};

#[pyfunction]
fn versions() -> Vec<(&'static str, &'static str)> {
    vec![
        ("quard-clones", env!("CARGO_PKG_VERSION")),
        ("cpd-core", env!("QUARD_CPD_CORE_VERSION")),
        ("cpd-finder", env!("QUARD_CPD_FINDER_VERSION")),
        ("cpd-reporter", env!("QUARD_CPD_REPORTER_VERSION")),
        ("cpd-tokenizer", env!("QUARD_CPD_TOKENIZER_VERSION")),
    ]
}

#[pyclass]
struct SourceSession {
    texts: HashMap<String, String>,
    files: Vec<SourceFile>,
    prepared: Vec<cpd_core::detect::PreparedSource>,
    min_lines: usize,
}

#[pymethods]
impl SourceSession {
    #[new]
    fn new(sources: Vec<(String, String)>, min_lines: usize) -> PyResult<Self> {
        if sources.is_empty() || min_lines == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "nonempty sources and positive min_lines required",
            ));
        }
        let config = RunConfig {
            min_lines,
            min_tokens: 1,
            mode: Mode::Strict,
            no_gitignore: true,
            ..RunConfig::default()
        };
        let preparer = FilePreparer::new(&config);
        let mut texts = HashMap::new();
        let mut files = Vec::new();
        let mut prepared = Vec::new();
        for (path, text) in sources {
            if !path.starts_with('/') || !path.ends_with(".py") || texts.contains_key(&path) {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "unique absolute .py paths required",
                ));
            }
            if let Some((display, detection)) =
                preparer.prepare(path.clone(), String::new(), "python", &text)
            {
                if display.len() != 1 || detection.len() != 1 {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(
                        "incomplete native source preparation",
                    ));
                }
                files.extend(display);
                prepared.extend(detection);
                texts.insert(path, text);
            }
        }
        Ok(Self {
            texts,
            files,
            prepared,
            min_lines,
        })
    }

    fn exact(&self) -> PyResult<String> {
        let clones = detect_prepared(
            vec![self.prepared.clone()],
            1,
            self.min_lines,
            &PathFilters::default(),
        );
        let clones: Vec<CpdClone> = clones
            .into_iter()
            .filter(|clone| KindFilter::Exact.matches(clone))
            .collect();
        let stats = statistics::compute(&self.files, &clones);
        let duplicates: PyResult<Vec<Value>> =
            clones.iter().map(|clone| self.duplicate(clone)).collect();
        Ok(json!({"statistics": stats, "duplicates": duplicates?}).to_string())
    }

    fn source_count(&self) -> usize {
        self.files.len()
    }
}

impl SourceSession {
    fn duplicate(&self, clone: &CpdClone) -> PyResult<Value> {
        let first = &clone.fragment_a;
        let second = &clone.fragment_b;
        let mut sides = Vec::new();
        for fragment in [first, second] {
            let text = self.texts.get(&fragment.source_id).ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("native clone outside loaded scope")
            })?;
            let lines: Vec<&str> = text.lines().collect();
            let start = fragment.start.line as usize;
            let end = fragment.end.line as usize;
            if start == 0
                || end < start
                || end > lines.len()
                || fragment.start.column as usize > lines[start - 1].len()
                || fragment.end.column as usize > lines[end - 1].len()
            {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "invalid native fragment location",
                ));
            }
            sides.push(
                json!({"name": fragment.source_id, "start": start, "end": end,
                "startLoc": {"line": fragment.start.line, "column": fragment.start.column,
                             "position": fragment.start.offset},
                "endLoc": {"line": fragment.end.line, "column": fragment.end.column,
                           "position": fragment.end.offset}}),
            );
        }
        let text = &self.texts[&first.source_id];
        let fragment = extract_lines(text, first.start.line, first.end.line);
        Ok(
            json!({"format": clone.format, "lines": first.end.line - first.start.line + 1,
                  "fragment": fragment, "tokens": clone.token_count, "firstFile": sides[0],
                  "secondFile": sides[1], "isNew": clone.is_new, "kind": clone.kind.as_str()}),
        )
    }
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<SourceSession>()?;
    module.add_function(wrap_pyfunction!(versions, module)?)?;
    Ok(())
}

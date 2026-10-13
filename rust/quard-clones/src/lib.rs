use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Arc;

use cpd_core::detect::{detect_prepared, merge_gapped_clones, PathFilters};
use cpd_core::hash::token_hash;
use cpd_core::models::{CpdClone, KindFilter, SourceFile, TokenKind};
use cpd_finder::orchestrate::{FilePreparer, RunConfig};
use cpd_finder::statistics;
use cpd_reporter::shared::extract_lines;
use cpd_tokenizer::tokenizer::Mode;
use pyo3::prelude::*;
use rayon::prelude::*;
use serde_json::{json, Value};

mod preparation;

// Retain native raw hashes for Type-1/Type-2 classification. Only the hashes
// handed to detection distinguish callable identities and local alias relations.
fn rename_hashes(source: &mut cpd_core::detect::PreparedSource, unit: &preparation::Unit) {
    let mut locals: HashMap<&str, usize> = HashMap::new();
    let mut fields: HashMap<&str, usize> = HashMap::new();
    let mut keywords: HashMap<&str, usize> = HashMap::new();
    for (index, (start, end)) in source.spans.iter().enumerate() {
        let offset = start.offset as usize;
        if let Some(identity) = unit.identities.get(&offset) {
            let role = if let Some(name) = identity.strip_prefix("field:") {
                let next = fields.len();
                format!("field:{}", fields.entry(name).or_insert(next))
            } else if let Some(name) = identity.strip_prefix("keyword:") {
                let next = keywords.len();
                format!("keyword:{}", keywords.entry(name).or_insert(next))
            } else {
                identity.clone()
            };
            source.hashes[index] = token_hash(
                TokenKind::Identifier.discriminant(),
                &format!("role:{role}"),
            );
        } else if source
            .raw_hashes
            .get(index)
            .is_some_and(|raw| *raw != source.hashes[index])
        {
            let Some(token) = unit.text.get(offset..end.offset as usize) else {
                continue;
            };
            // Only normalized identifiers receive ordinal names. Native literal
            // normalization continues unchanged across the entire unit.
            if token
                .chars()
                .next()
                .is_some_and(|ch| ch == '_' || ch.is_alphabetic())
                && token.chars().all(|ch| ch == '_' || ch.is_alphanumeric())
            {
                let next = locals.len();
                let ordinal = *locals.entry(token).or_insert(next);
                source.hashes[index] = token_hash(
                    TokenKind::Identifier.discriminant(),
                    &format!("local:{ordinal}"),
                );
            }
        }
    }
}

struct SourceMap {
    path: String,
    lines: Vec<usize>,
    columns: Option<Vec<Vec<usize>>>,
    offsets: Arc<[usize]>,
    subtree: Option<(usize, usize)>,
    positions: Option<Vec<Vec<usize>>>,
}

fn original_column(map: &SourceMap, line: usize, column: usize) -> PyResult<usize> {
    if let Some(columns) = &map.columns {
        let boundaries = columns
            .get(line - 1)
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("unmapped schema line"))?;
        // The pinned tokenizer can point one byte past a final newline.
        if column > boundaries.len() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "unmapped schema column {line}:{column} ({} boundaries)",
                boundaries.len(),
            )));
        }
        return boundaries
            .get(column.min(boundaries.len() - 1))
            .copied()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("unmapped schema column"));
    }
    Ok(column)
}

// Strict Python tokenization includes newlines and indentation in whitespace
// tokens. A match of two executable lines plus only the indentation of the
// next line must not clear a three-eligible-line minimum. Keep the native
// hashes and token boundaries, but anchor such whitespace spans to their
// preceding eligible line before native detection applies its line threshold.
fn anchor_whitespace_spans(source: &mut cpd_core::detect::PreparedSource, text: &str) {
    for (start, end) in &mut source.spans {
        let whitespace = text
            .get(start.offset as usize..end.offset as usize)
            .is_some_and(|slice| slice.chars().all(char::is_whitespace));
        if !whitespace {
            continue;
        }
        if start.column == 0 && start.line > 1 {
            // Indentation alone must not be a match on the next line.
            let preceding = &text[..start.offset as usize];
            if let Some(newline) = preceding.rfind('\n') {
                let previous_start = preceding[..newline].rfind('\n').map_or(0, |i| i + 1);
                start.line -= 1;
                start.column = (newline - previous_start) as u32;
                start.offset = newline as u32;
            }
        }
        if end.line > start.line || start.offset > end.offset {
            *end = start.clone();
        }
    }
}

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
    prepared_similar: Vec<cpd_core::detect::PreparedSource>,
    prepared_renamed: Vec<cpd_core::detect::PreparedSource>,
    prepared_calls: Vec<cpd_core::detect::PreparedSource>,
    renamed_eligible_lines: HashMap<String, usize>,
    renamed_unit_lines: HashMap<String, Vec<usize>>,
    renamed_scanned_unit_lines: HashMap<String, Vec<usize>>,
    call_unit_lines: HashMap<String, Vec<usize>>,
    renamed_scannable: bool,
    maps: HashMap<String, SourceMap>,
    eligible_lines: HashMap<String, usize>,
    unit_lines: HashMap<String, Vec<usize>>,
    scanned_unit_lines: HashMap<String, Vec<usize>>,
    scannable: bool,
    exact_eligible_lines: HashMap<String, usize>,
    exact_unit_lines: HashMap<String, Vec<usize>>,
    exact_scanned_unit_lines: HashMap<String, Vec<usize>>,
    exact_scannable: bool,
    min_lines: usize,
    renamed: bool,
}

#[pymethods]
impl SourceSession {
    #[new]
    #[pyo3(signature = (sources, min_lines, renamed=false))]
    fn new(sources: Vec<(String, String)>, min_lines: usize, renamed: bool) -> PyResult<Self> {
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
            ignore_identifiers: renamed,
            ignore_literals: renamed,
            ..RunConfig::default()
        };
        let preparer = FilePreparer::new(&config);
        let normalized_config = RunConfig {
            ignore_identifiers: true,
            ignore_literals: true,
            ..config.clone()
        };
        let normalized_preparer = FilePreparer::new(&normalized_config);
        let mut texts = HashMap::new();
        let mut files = Vec::new();
        let mut prepared = Vec::new();
        let mut prepared_similar = Vec::new();
        let mut prepared_renamed = Vec::new();
        let mut prepared_calls = Vec::new();
        let mut renamed_eligible_lines = HashMap::new();
        let mut renamed_unit_lines = HashMap::new();
        let mut renamed_scanned_unit_lines = HashMap::new();
        let mut call_unit_lines = HashMap::new();
        let mut renamed_scannable = false;
        let mut maps = HashMap::new();
        let mut eligible_lines = HashMap::new();
        let mut unit_lines = HashMap::new();
        let mut scanned_unit_lines = HashMap::new();
        let mut scannable = false;
        let mut exact_eligible_lines = HashMap::new();
        let mut exact_unit_lines = HashMap::new();
        let mut exact_scanned_unit_lines = HashMap::new();
        let mut exact_scannable = false;
        let fields = preparation::discover_fields(&sources)
            .map_err(pyo3::exceptions::PyValueError::new_err)?;
        for (path, text) in sources {
            if !path.starts_with('/') || !path.ends_with(".py") || texts.contains_key(&path) {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "unique absolute .py paths required",
                ));
            }
            let views = preparation::prepare(&text, &path, min_lines, &fields)
                .map_err(pyo3::exceptions::PyValueError::new_err)?;
            renamed_eligible_lines.insert(
                path.clone(),
                views
                    .renamed_behavior
                    .iter()
                    .map(|unit| unit.lines.len())
                    .sum(),
            );
            renamed_unit_lines.insert(
                path.clone(),
                views
                    .renamed_behavior
                    .iter()
                    .map(|unit| unit.lines.len())
                    .collect(),
            );
            renamed_scanned_unit_lines.insert(path.clone(), Vec::new());
            call_unit_lines.insert(
                path.clone(),
                views.calls.iter().map(|unit| unit.lines.len()).collect(),
            );
            // Original offsets are shared by every unit from this source. Rewalking
            // all preceding lines for every display token is quadratic in file size.
            let mut offset = 0;
            let offsets: Arc<[usize]> = text
                .split_inclusive('\n')
                .map(|line| {
                    let start = offset;
                    offset += line.len();
                    start
                })
                .collect();
            eligible_lines.insert(
                path.clone(),
                views.behavior.iter().map(|unit| unit.lines.len()).sum(),
            );
            unit_lines.insert(
                path.clone(),
                views.behavior.iter().map(|unit| unit.lines.len()).collect(),
            );
            scanned_unit_lines.insert(path.clone(), Vec::new());
            exact_eligible_lines.insert(
                path.clone(),
                views
                    .behavior
                    .iter()
                    .chain(&views.schemas)
                    .map(|unit| unit.lines.len())
                    .sum(),
            );
            exact_unit_lines.insert(
                path.clone(),
                views
                    .behavior
                    .iter()
                    .chain(&views.schemas)
                    .map(|unit| unit.lines.len())
                    .collect(),
            );
            exact_scanned_unit_lines.insert(path.clone(), Vec::new());
            let file_index = files.len();
            files.push(SourceFile {
                id: path.clone(),
                format: "python".into(),
                tokens: Vec::new(),
                bytes: text.len() as u64,
            });
            for (number, (schema, unit)) in views
                .behavior
                .into_iter()
                .map(|unit| (false, unit))
                .chain(views.schemas.into_iter().map(|unit| (true, unit)))
                .enumerate()
            {
                let id = format!("{path}#quard-unit-{number}");
                let map = SourceMap {
                    path: path.clone(),
                    lines: unit.lines.clone(),
                    columns: unit.columns.clone(),
                    offsets: Arc::clone(&offsets),
                    subtree: unit.subtree,
                    positions: unit.positions,
                };
                if !schema {
                    scannable |= unit.lines.len() >= min_lines;
                }
                exact_scannable |= unit.lines.len() >= min_lines;
                if let Some((display, detection)) =
                    preparer.prepare(id.clone(), String::new(), "python", &unit.text)
                {
                    if display.len() != 1 || detection.len() != 1 {
                        return Err(pyo3::exceptions::PyRuntimeError::new_err(
                            "incomplete native source preparation",
                        ));
                    }
                    for mut token in display.into_iter().flat_map(|source| source.tokens) {
                        for loc in [&mut token.start, &mut token.end] {
                            let column =
                                original_column(&map, loc.line as usize, loc.column as usize)?;
                            let line = *unit.lines.get(loc.line as usize - 1).ok_or_else(|| {
                                pyo3::exceptions::PyRuntimeError::new_err(
                                    "unmapped native display token",
                                )
                            })?;
                            let offset = *map.offsets.get(line - 1).ok_or_else(|| {
                                pyo3::exceptions::PyRuntimeError::new_err(
                                    "unmapped native display offset",
                                )
                            })?;
                            loc.line = line as u32;
                            loc.column = column as u32;
                            loc.offset = (offset + column) as u32;
                        }
                        if !schema || !renamed {
                            files[file_index].tokens.push(token);
                        }
                    }
                    if !schema {
                        prepared_similar.extend(detection.iter().cloned().map(|mut source| {
                            anchor_whitespace_spans(&mut source, &unit.text);
                            source
                        }));
                    }
                    if !schema || !renamed {
                        prepared.extend(detection);
                    }
                    exact_scanned_unit_lines
                        .get_mut(&path)
                        .expect("discovered source")
                        .push(unit.lines.len());
                    if !schema {
                        scanned_unit_lines
                            .get_mut(&path)
                            .expect("discovered source")
                            .push(unit.lines.len());
                    }
                    maps.insert(id, map);
                } else if unit.lines.len() >= min_lines {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(
                        "eligible unit rejected by native preparation",
                    ));
                }
            }
            for (number, unit) in views.renamed_behavior.into_iter().enumerate() {
                renamed_scannable |= unit.lines.len() >= min_lines;
                let id = format!("{path}#quard-renamed-unit-{number}");
                if let Some((_, mut detection)) =
                    normalized_preparer.prepare(id.clone(), String::new(), "python", &unit.text)
                {
                    if detection.len() != 1 {
                        return Err(pyo3::exceptions::PyRuntimeError::new_err(
                            "incomplete normalized preparation",
                        ));
                    }
                    rename_hashes(&mut detection[0], &unit);
                    prepared_renamed.append(&mut detection);
                    renamed_scanned_unit_lines
                        .get_mut(&path)
                        .expect("discovered source")
                        .push(unit.lines.len());
                    maps.insert(
                        id,
                        SourceMap {
                            path: path.clone(),
                            lines: unit.lines,
                            columns: None,
                            offsets: Arc::clone(&offsets),
                            subtree: None,
                            positions: unit.positions,
                        },
                    );
                } else if unit.lines.len() >= min_lines {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(
                        "eligible normalized unit rejected by native preparation",
                    ));
                }
            }
            for (number, unit) in views.calls.into_iter().enumerate() {
                let id = format!("{path}#quard-call-{number}");
                let (_, mut detection) = normalized_preparer
                    .prepare(id.clone(), String::new(), "python", &unit.text)
                    .ok_or_else(|| {
                        pyo3::exceptions::PyRuntimeError::new_err(
                            "native preparation rejected a complete call subtree",
                        )
                    })?;
                if detection.len() != 1 {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(
                        "incomplete native call preparation",
                    ));
                }
                rename_hashes(&mut detection[0], &unit);
                prepared_calls.append(&mut detection);
                maps.insert(
                    id,
                    SourceMap {
                        path: path.clone(),
                        lines: unit.lines,
                        columns: None,
                        offsets: Arc::clone(&offsets),
                        subtree: unit.subtree,
                        positions: unit.positions,
                    },
                );
            }
            texts.insert(path, text);
        }
        Ok(Self {
            texts,
            files,
            prepared,
            prepared_similar,
            prepared_renamed,
            prepared_calls,
            renamed_eligible_lines,
            renamed_unit_lines,
            renamed_scanned_unit_lines,
            call_unit_lines,
            renamed_scannable,
            maps,
            eligible_lines,
            unit_lines,
            scanned_unit_lines,
            scannable,
            exact_eligible_lines,
            exact_unit_lines,
            exact_scanned_unit_lines,
            exact_scannable,
            min_lines,
            renamed,
        })
    }

    fn exact(&self) -> PyResult<String> {
        self.detect(KindFilter::Exact, false)
    }

    fn renamed(&self) -> PyResult<String> {
        self.detect(KindFilter::Renamed, true)
    }

    fn similar(&self) -> PyResult<String> {
        self.detect(KindFilter::Similar, false)
    }

    fn source_count(&self) -> usize {
        self.files.len()
    }

    fn eligible_lines(&self) -> HashMap<String, usize> {
        self.eligible_lines.clone()
    }

    fn unit_lines(&self) -> HashMap<String, Vec<usize>> {
        self.unit_lines.clone()
    }

    fn scanned_unit_lines(&self) -> HashMap<String, Vec<usize>> {
        self.scanned_unit_lines.clone()
    }

    fn scannable(&self) -> bool {
        self.scannable
    }

    fn exact_eligible_lines(&self) -> HashMap<String, usize> {
        self.exact_eligible_lines.clone()
    }

    fn exact_unit_lines(&self) -> HashMap<String, Vec<usize>> {
        self.exact_unit_lines.clone()
    }

    fn exact_scanned_unit_lines(&self) -> HashMap<String, Vec<usize>> {
        self.exact_scanned_unit_lines.clone()
    }

    fn exact_scannable(&self) -> bool {
        self.exact_scannable
    }

    fn renamed_eligible_lines(&self) -> HashMap<String, usize> {
        self.renamed_eligible_lines.clone()
    }

    fn renamed_unit_lines(&self) -> HashMap<String, Vec<usize>> {
        self.renamed_unit_lines.clone()
    }

    fn renamed_scanned_unit_lines(&self) -> HashMap<String, Vec<usize>> {
        self.renamed_scanned_unit_lines.clone()
    }

    fn renamed_scannable(&self) -> bool {
        self.renamed_scannable
    }

    fn call_unit_lines(&self) -> HashMap<String, Vec<usize>> {
        self.call_unit_lines.clone()
    }
}

impl SourceSession {
    fn detect(&self, kind: KindFilter, renamed: bool) -> PyResult<String> {
        if self.renamed && !renamed {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "clone session normalization does not match requested pass",
            ));
        }
        let source = if kind == KindFilter::Similar {
            &self.prepared_similar
        } else if renamed {
            &self.prepared_renamed
        } else {
            &self.prepared
        };
        // The native index retains only two secondary occurrences for a one-token
        // window. In a normalized project-wide pool, unrelated early occurrences
        // can displace a real Type-2 pair. Feed the same native detector candidate
        // unit pairs that share a three-token window (or a shorter window that
        // itself spans the minimum); each pair has its own index.
        let clones = if renamed {
            let mut windows: HashMap<Vec<u64>, Vec<usize>> = HashMap::new();
            let mut candidates = BTreeSet::new();
            for (index, unit) in source.iter().enumerate() {
                for width in 1..=3 {
                    for (start, window) in unit.hashes.windows(width).enumerate() {
                        if width < 3
                            && ((unit.spans[start + width - 1].1.line - unit.spans[start].0.line)
                                as usize)
                                < self.min_lines.saturating_sub(1)
                        {
                            continue;
                        }
                        let earlier = windows.entry(window.to_vec()).or_default();
                        for &other in earlier.iter() {
                            candidates.insert((other, index));
                        }
                        if earlier.last() != Some(&index) {
                            earlier.push(index);
                        }
                    }
                }
            }
            let mut found: Vec<CpdClone> = source
                .par_iter()
                .flat_map_iter(|unit| {
                    detect_prepared(
                        vec![vec![unit.clone()]],
                        1,
                        self.min_lines.saturating_sub(1),
                        &PathFilters::default(),
                    )
                })
                .collect();
            let pairs: Vec<_> = candidates.into_iter().collect();
            let cross: Vec<CpdClone> = pairs
                .into_par_iter()
                .flat_map_iter(|(first, second)| {
                    // Each unit's own findings were collected above. Pair scans can
                    // otherwise repeat those same native findings many times.
                    detect_prepared(
                        vec![vec![source[first].clone(), source[second].clone()]],
                        1,
                        self.min_lines.saturating_sub(1),
                        &PathFilters::default(),
                    )
                    .into_iter()
                    .filter(|clone| clone.fragment_a.source_id != clone.fragment_b.source_id)
                })
                .collect();
            found.extend(cross);
            // Compare complete, non-overlapping call subtrees before native
            // detection. Never feed overlapping windows of one expression to
            // the detector; its own exact/renamed classification remains intact.
            let mut by_tokens: HashMap<Vec<u64>, Vec<usize>> = HashMap::new();
            for (index, call) in self.prepared_calls.iter().enumerate() {
                by_tokens
                    .entry(call.hashes.clone())
                    .or_default()
                    .push(index);
            }
            let mut call_pairs = Vec::new();
            for group in by_tokens.into_values() {
                for (index, first) in group.iter().enumerate() {
                    call_pairs.extend(group[index + 1..].iter().map(|second| (*first, *second)));
                }
            }
            let call_pairs: Vec<_> = call_pairs
                .into_iter()
                .filter(|(first, second)| {
                    let left = &self.maps[&self.prepared_calls[*first].id];
                    let right = &self.maps[&self.prepared_calls[*second].id];
                    left.path != right.path || {
                        let (a, b) = left.subtree.expect("complete call");
                        let (c, d) = right.subtree.expect("complete call");
                        b <= c || d <= a
                    }
                })
                .collect();
            let nested: Vec<CpdClone> = call_pairs
                .into_par_iter()
                .flat_map_iter(|(first, second)| {
                    detect_prepared(
                        vec![vec![
                            self.prepared_calls[first].clone(),
                            self.prepared_calls[second].clone(),
                        ]],
                        1,
                        self.min_lines.saturating_sub(1),
                        &PathFilters::default(),
                    )
                    .into_iter()
                    .filter(|clone| clone.fragment_a.source_id != clone.fragment_b.source_id)
                })
                .collect();
            found.extend(nested);
            found
        } else {
            detect_prepared(
                vec![source.clone()],
                1,
                self.min_lines.saturating_sub(1),
                &PathFilters::default(),
            )
        };
        // Detection has already applied the shared eligible-line minimum to
        // each constituent. Partition before native merging so only fragments
        // from the same pair of prepared units can form a Type-3 clone. In
        // particular, two functions in one original file are separate units.
        let clones = if kind == KindFilter::Similar {
            let mut by_units: BTreeMap<(String, String, String), Vec<CpdClone>> = BTreeMap::new();
            for clone in clones {
                let key = (
                    clone.format.clone(),
                    clone.fragment_a.source_id.clone(),
                    clone.fragment_b.source_id.clone(),
                );
                by_units.entry(key).or_default().push(clone);
            }
            by_units
                .into_values()
                .flat_map(|group| merge_gapped_clones(group, 2))
                .collect()
        } else {
            clones
        };
        let mut clones: Vec<CpdClone> = clones
            .into_iter()
            .filter(|clone| kind.matches(clone))
            .collect();
        for clone in &mut clones {
            for fragment in [&mut clone.fragment_a, &mut clone.fragment_b] {
                let map = self.maps.get(&fragment.source_id).ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err("unknown prepared unit")
                })?;
                let text = &self.texts[&map.path];
                if let Some((start, end)) = map.subtree {
                    for (location, offset) in
                        [(&mut fragment.start, start), (&mut fragment.end, end)]
                    {
                        let line = map.offsets.partition_point(|begin| *begin <= offset);
                        let column = offset - map.offsets[line - 1];
                        location.line = line as u32;
                        location.column = column as u32;
                        location.offset = offset as u32;
                    }
                    fragment.source_id = map.path.clone();
                    continue;
                }
                for location in [&mut fragment.start, &mut fragment.end] {
                    if let Some(positions) = &map.positions {
                        let row = positions.get(location.line as usize - 1).ok_or_else(|| {
                            pyo3::exceptions::PyRuntimeError::new_err("unmapped folded line")
                        })?;
                        let offset = *row
                            .get((location.column as usize).min(row.len() - 1))
                            .ok_or_else(|| {
                                pyo3::exceptions::PyRuntimeError::new_err("unmapped folded column")
                            })?;
                        let line = map.offsets.partition_point(|begin| *begin <= offset);
                        location.line = line as u32;
                        location.column = (offset - map.offsets[line - 1]) as u32;
                        location.offset = offset as u32;
                        continue;
                    }
                    let column =
                        original_column(map, location.line as usize, location.column as usize)?;
                    let line = *map.lines.get(location.line as usize - 1).ok_or_else(|| {
                        pyo3::exceptions::PyRuntimeError::new_err("unmapped prepared line")
                    })?;
                    let original = *map.offsets.get(line - 1).ok_or_else(|| {
                        pyo3::exceptions::PyRuntimeError::new_err("unmapped prepared offset")
                    })?;
                    let length = text.lines().nth(line - 1).map(str::len).unwrap_or(0);
                    let column = column.min(length);
                    location.column = column as u32;
                    location.line = line as u32;
                    location.offset = (original + column) as u32;
                }
                fragment.source_id = map.path.clone();
            }
        }
        let stats = statistics::compute(&self.files, &clones);
        let duplicates: PyResult<Vec<Value>> =
            clones.iter().map(|clone| self.duplicate(clone)).collect();
        Ok(json!({"statistics": stats, "duplicates": duplicates?}).to_string())
    }

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
                return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "invalid native fragment location {} {start}:{:?} {end}:{:?}",
                    fragment.source_id, fragment.start.column, fragment.end.column
                )));
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
                  "secondFile": sides[1], "isNew": clone.is_new, "kind": clone.kind.as_str(),
                  "unmatchedLines": clone.unmatched_lines,
                  "similarity": clone.similarity_rounded(),
                  "method": clone.similarity_method.map(|method| method.as_str())}),
        )
    }
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<SourceSession>()?;
    module.add_function(wrap_pyfunction!(versions, module)?)?;
    Ok(())
}

//! Python syntax selection and reversible per-unit line maps.
use rustpython_ast::{Constant, Expr, Ranged, Stmt, Visitor};
use rustpython_parser::{ast, Parse};

pub struct Unit {
    pub text: String,
    pub lines: Vec<usize>,
}

#[derive(Default)]
struct Selection {
    keep: Vec<Vec<(usize, usize)>>,
    remove: Vec<Vec<(usize, usize)>>,
    current: usize,
    attribute_docs: bool,
    local_imports: bool,
}

impl Selection {
    fn suite(&mut self, body: Vec<Stmt>) {
        let mut previous_assignment = false;
        for (index, stmt) in body.into_iter().enumerate() {
            let doc = matches!(&stmt, Stmt::Expr(expr) if matches!(expr.value.as_ref(),
                Expr::Constant(value) if matches!(value.value, Constant::Str(_))));
            let assignment = matches!(&stmt, Stmt::Assign(_) | Stmt::AnnAssign(_));
            if doc && (index == 0 || self.attribute_docs && previous_assignment) {
                let range = stmt.range();
                self.remove[self.current].push((range.start().into(), range.end().into()));
            } else {
                self.visit_stmt(stmt);
            }
            previous_assignment = assignment;
        }
    }

    fn unit(&mut self, body: Vec<Stmt>, range: (usize, usize), attribute_docs: bool) {
        for unit in &mut self.remove {
            unit.push(range);
        }
        let parent = self.current;
        let parent_docs = self.attribute_docs;
        let parent_imports = self.local_imports;
        self.keep.push(Vec::new());
        self.remove.push(Vec::new());
        self.current = self.keep.len() - 1;
        self.attribute_docs = attribute_docs;
        self.local_imports = !attribute_docs;
        self.suite(body);
        self.current = parent;
        self.attribute_docs = parent_docs;
        self.local_imports = parent_imports;
    }
}

impl Visitor for Selection {
    fn visit_stmt(&mut self, node: Stmt) {
        let range = node.range();
        let span = (range.start().into(), range.end().into());
        match node {
            Stmt::FunctionDef(def) => self.unit(def.body, span, false),
            Stmt::AsyncFunctionDef(def) => self.unit(def.body, span, false),
            Stmt::ClassDef(def) => self.unit(def.body, span, true),
            Stmt::Import(_) | Stmt::ImportFrom(_) if !self.local_imports => {
                self.remove[self.current].push(span);
            }
            other => {
                self.keep[self.current].push(span);
                // Nested definitions in conditional suites have separate ownership.
                self.generic_visit_stmt(other);
            }
        }
    }
}

pub fn prepare(text: &str, path: &str) -> Result<Vec<Unit>, String> {
    let suite = ast::Suite::parse(text, path).map_err(|error| format!("{path}: {error}"))?;
    let mut selection = Selection {
        keep: vec![Vec::new()],
        remove: vec![Vec::new()],
        attribute_docs: true,
        ..Selection::default()
    };
    selection.suite(suite);
    let starts: Vec<usize> = std::iter::once(0)
        .chain(text.match_indices('\n').map(|(i, _)| i + 1))
        .collect();
    let mut result = Vec::new();
    for (keep, remove) in selection.keep.into_iter().zip(selection.remove) {
        let mut bytes = vec![b' '; text.len()];
        for (begin, end) in keep {
            for i in begin..end {
                bytes[i] = text.as_bytes()[i];
            }
        }
        for (begin, end) in remove {
            bytes[begin..end].fill(b' ');
        }
        let mut lines = Vec::new();
        let mut compact = String::new();
        for (index, start) in starts.iter().enumerate() {
            let end = starts.get(index + 1).copied().unwrap_or(text.len());
            let slice = &bytes[*start..end];
            if slice.iter().any(|b| !b.is_ascii_whitespace()) {
                // Keep original indentation, byte columns and text on retained lines.
                let masked = std::str::from_utf8(slice)
                    .map_err(|_| format!("{path}: masked syntax split a Unicode character"))?;
                compact.push_str(masked.trim_end_matches('\n'));
                compact.push('\n');
                lines.push(index + 1);
            }
        }
        if !lines.is_empty() {
            result.push(Unit {
                text: compact,
                lines,
            });
        }
    }
    Ok(result)
}

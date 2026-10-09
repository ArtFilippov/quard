//! Python syntax selection and reversible per-unit line maps.
use rustpython_ast::{Constant, Expr, Ranged, Stmt, Visitor};
use rustpython_parser::{ast, Parse};
use std::collections::HashSet;

pub struct Unit {
    pub text: String,
    pub lines: Vec<usize>,
    // Canonical schema columns map byte boundaries back to original columns.
    pub columns: Option<Vec<Vec<usize>>>,
}

pub struct Views {
    pub behavior: Vec<Unit>,
    pub schemas: Vec<Unit>,
}

#[derive(Default)]
struct Selection {
    keep: Vec<Vec<(usize, usize)>>,
    remove: Vec<Vec<(usize, usize)>>,
    current: usize,
    attribute_docs: bool,
    local_imports: bool,
    alias_names: HashSet<String>,
    typing_modules: HashSet<String>,
    typed_dict_names: HashSet<String>,
    typed_dict_modules: HashSet<String>,
    schemas: Vec<Vec<(usize, usize)>>,
    canonical_schemas: Vec<Unit>,
    text: String,
    error: Option<String>,
}

struct Field {
    name: String,
    name_span: (usize, usize),
    annotation: (usize, usize),
    line: usize,
}

impl Selection {
    fn placeholder(stmt: &Stmt) -> bool {
        match stmt {
            Stmt::Pass(_) => true,
            Stmt::Expr(expr) => matches!(expr.value.as_ref(), Expr::Constant(value)
                if matches!(value.value, Constant::Ellipsis)),
            Stmt::Raise(raised) if raised.cause.is_none() => match raised.exc.as_deref() {
                Some(Expr::Name(name)) => name.id.as_str() == "NotImplementedError",
                Some(Expr::Call(call)) => {
                    matches!(call.func.as_ref(), Expr::Name(name)
                            if name.id.as_str() == "NotImplementedError")
                        && call.keywords.is_empty()
                        && call.args.iter().all(|arg| matches!(arg, Expr::Constant(_)))
                }
                _ => false,
            },
            _ => false,
        }
    }

    fn is_type_alias(&self, annotation: &Expr) -> bool {
        match annotation {
            Expr::Name(name) => self.alias_names.contains(name.id.as_str()),
            Expr::Attribute(attr) => {
                matches!(attr.value.as_ref(), Expr::Name(name)
                if self.typing_modules.contains(name.id.as_str()))
                    && attr.attr.as_str() == "TypeAlias"
            }
            _ => false,
        }
    }

    fn import_bindings(&mut self, stmt: &Stmt) {
        match stmt {
            Stmt::Import(node) => {
                for alias in &node.names {
                    let module = alias.name.as_str();
                    let binding = alias.asname.as_ref().map_or_else(
                        || module.split('.').next().unwrap_or(module),
                        |name| name.as_str(),
                    );
                    self.alias_names.remove(binding);
                    self.typing_modules.remove(binding);
                    self.typed_dict_names.remove(binding);
                    self.typed_dict_modules.remove(binding);
                    if matches!(module, "typing" | "typing_extensions") {
                        self.typing_modules.insert(binding.to_owned());
                        self.typed_dict_modules.insert(binding.to_owned());
                    }
                }
            }
            Stmt::ImportFrom(node) => {
                for alias in &node.names {
                    let binding = alias.asname.as_ref().unwrap_or(&alias.name).as_str();
                    self.alias_names.remove(binding);
                    self.typing_modules.remove(binding);
                    self.typed_dict_names.remove(binding);
                    self.typed_dict_modules.remove(binding);
                    if node.level.is_none_or(|level| level.to_u32() == 0)
                        && node.module.as_ref().is_some_and(|module| {
                            matches!(module.as_str(), "typing" | "typing_extensions")
                        })
                        && alias.name.as_str() == "TypeAlias"
                    {
                        self.alias_names.insert(binding.to_owned());
                    }
                    if node.level.is_none_or(|level| level.to_u32() == 0)
                        && node.module.as_ref().is_some_and(|module| {
                            matches!(module.as_str(), "typing" | "typing_extensions")
                        })
                        && alias.name.as_str() == "TypedDict"
                    {
                        self.typed_dict_names.insert(binding.to_owned());
                    }
                }
            }
            _ => {}
        }
    }

    fn forget_target(&mut self, target: &Expr) {
        match target {
            Expr::Name(name) => {
                self.alias_names.remove(name.id.as_str());
                self.typing_modules.remove(name.id.as_str());
                self.typed_dict_names.remove(name.id.as_str());
                self.typed_dict_modules.remove(name.id.as_str());
            }
            Expr::Tuple(items) => {
                for item in &items.elts {
                    self.forget_target(item);
                }
            }
            Expr::List(items) => {
                for item in &items.elts {
                    self.forget_target(item);
                }
            }
            Expr::Starred(item) => self.forget_target(&item.value),
            _ => {}
        }
    }

    fn forget_arguments(&mut self, args: &ast::Arguments) {
        for argument in args
            .posonlyargs
            .iter()
            .chain(&args.args)
            .chain(&args.kwonlyargs)
        {
            self.alias_names.remove(argument.def.arg.as_str());
            self.typing_modules.remove(argument.def.arg.as_str());
            self.typed_dict_names.remove(argument.def.arg.as_str());
            self.typed_dict_modules.remove(argument.def.arg.as_str());
        }
        for argument in [&args.vararg, &args.kwarg].into_iter().flatten() {
            self.alias_names.remove(argument.arg.as_str());
            self.typing_modules.remove(argument.arg.as_str());
            self.typed_dict_names.remove(argument.arg.as_str());
            self.typed_dict_modules.remove(argument.arg.as_str());
        }
    }

    fn is_typed_dict(&self, base: &Expr) -> bool {
        match base {
            Expr::Name(name) => self.typed_dict_names.contains(name.id.as_str()),
            Expr::Attribute(attr) => {
                attr.attr.as_str() == "TypedDict"
                    && matches!(attr.value.as_ref(), Expr::Name(name)
                    if self.typed_dict_modules.contains(name.id.as_str()))
            }
            _ => false,
        }
    }

    fn field(
        &self,
        name: String,
        name_span: (usize, usize),
        annotation: (usize, usize),
        functional: bool,
    ) -> Option<Field> {
        let text = &self.text;
        let start = text[..name_span.0].rfind('\n').map_or(0, |i| i + 1);
        let end = text[annotation.1..]
            .find('\n')
            .map_or(text.len(), |i| annotation.1 + i);
        if annotation.1 > end
            || text[name_span.0..annotation.1].contains('\n')
            || !text[start..name_span.0].trim().is_empty()
        {
            return None;
        }
        if functional {
            let suffix = text[annotation.1..end]
                .split('#')
                .next()
                .unwrap_or("")
                .trim();
            if !matches!(suffix, "" | ",") {
                return None;
            }
        }
        Some(Field {
            name,
            name_span,
            annotation,
            line: text[..start].bytes().filter(|byte| *byte == b'\n').count() + 1,
        })
    }

    fn canonical_schema(&mut self, fields: &[Field]) {
        if fields.is_empty() || fields.windows(2).any(|pair| pair[0].line >= pair[1].line) {
            return;
        }
        let mut text = String::new();
        let mut lines = Vec::new();
        let mut columns = Vec::new();
        for field in fields {
            let original_start = self.text[..field.name_span.0]
                .rfind('\n')
                .map_or(0, |i| i + 1);
            let name_start = field.name_span.0 - original_start;
            let name_end = field.name_span.1 - original_start;
            let type_start = field.annotation.0 - original_start;
            let type_end = field.annotation.1 - original_start;
            let mut map = vec![name_start; 5]; // Four uniform indentation bytes.
            let mut canonical = format!("    {}: ", field.name);
            // Original literal keys include quotes; only mapped boundaries are
            // exposed to the reporter, never the canonical spelling.
            for offset in 1..=field.name.len() {
                map.push(if offset == field.name.len() {
                    name_end
                } else {
                    (name_start + offset).min(name_end)
                });
            }
            map.push(name_end);
            map.push(type_start);
            let annotation = &self.text[field.annotation.0..field.annotation.1];
            canonical.push_str(annotation);
            for offset in 1..=annotation.len() {
                map.push(type_start + offset);
            }
            if map.len() != canonical.len() + 1 || map.last() != Some(&type_end) {
                self.error = Some("schema field column map is incomplete".to_owned());
                return;
            }
            text.push_str(&canonical);
            text.push('\n');
            columns.push(map);
            lines.push(field.line);
        }
        self.canonical_schemas.push(Unit {
            text,
            lines,
            columns: Some(columns),
        });
    }

    fn functional_schema(&mut self, call: &ast::ExprCall) {
        if !self.is_typed_dict(&call.func)
            || call.args.is_empty()
            || !matches!(&call.args[0], Expr::Constant(value) if matches!(value.value, Constant::Str(_)))
        {
            return;
        }
        let mut fields = Vec::new();
        if call.args.len() == 2
            && call
                .keywords
                .iter()
                .all(|kw| kw.arg.as_ref().is_some_and(|n| n.as_str() == "total"))
        {
            let Expr::Dict(dictionary) = &call.args[1] else {
                return;
            };
            for (key, value) in dictionary.keys.iter().zip(&dictionary.values) {
                let Some(Expr::Constant(key)) = key else {
                    return;
                };
                let Constant::Str(name) = &key.value else {
                    return;
                };
                let key_range = key.range();
                let span = (key_range.start().into(), key_range.end().into());
                let literal = &self.text[span.0..span.1];
                if !((literal.starts_with('\'') && literal.ends_with('\''))
                    || (literal.starts_with('"') && literal.ends_with('"')))
                {
                    return;
                }
                let range = value.range();
                let annotation = (range.start().into(), range.end().into());
                let identifier = matches!(Expr::parse(name, "<schema-field>"), Ok(Expr::Name(_)));
                let canonical_name = if identifier {
                    name.clone()
                } else {
                    serde_json::to_string(name).expect("string field name")
                };
                let Some(field) = self.field(canonical_name, span, annotation, true) else {
                    return;
                };
                fields.push(field);
            }
        } else if call.args.len() == 1 {
            for kw in &call.keywords {
                let Some(name) = &kw.arg else { return };
                if name.as_str() == "total" {
                    continue;
                }
                let range = kw.range();
                let value = kw.value.range();
                let annotation = (value.start().into(), value.end().into());
                let start: usize = range.start().into();
                let span = (start, start + name.len());
                let Some(field) = self.field(name.to_string(), span, annotation, true) else {
                    return;
                };
                fields.push(field);
            }
        } else {
            return;
        }
        self.canonical_schema(&fields);
    }

    fn schema_fields(&mut self, body: &[Stmt]) {
        let mut run = Vec::new();
        let mut fields = Vec::new();
        for stmt in body {
            if let Stmt::AnnAssign(field) = stmt {
                if field.value.is_none()
                    && matches!(field.target.as_ref(), Expr::Name(_))
                    && !self.is_type_alias(&field.annotation)
                {
                    let range = stmt.range();
                    run.push((range.start().into(), range.end().into()));
                    if let Expr::Name(name) = field.target.as_ref() {
                        let name_range = name.range();
                        let annotation = field.annotation.range();
                        if let Some(item) = self.field(
                            name.id.to_string(),
                            (name_range.start().into(), name_range.end().into()),
                            (annotation.start().into(), annotation.end().into()),
                            false,
                        ) {
                            fields.push(item);
                        }
                    }
                    continue;
                }
            }
            if !run.is_empty() {
                self.finish_schema(&mut run, &mut fields);
            }
        }
        if !run.is_empty() {
            self.finish_schema(&mut run, &mut fields);
        }
    }

    fn finish_schema(&mut self, run: &mut Vec<(usize, usize)>, fields: &mut Vec<Field>) {
        if run.len() == fields.len() && fields.windows(2).all(|pair| pair[0].line < pair[1].line) {
            self.canonical_schema(fields);
        } else {
            self.schemas.push(std::mem::take(run));
        }
        run.clear();
        fields.clear();
    }

    fn suite(&mut self, body: Vec<Stmt>) {
        let mut previous_assignment = false;
        for (index, stmt) in body.into_iter().enumerate() {
            let doc = matches!(&stmt, Stmt::Expr(expr) if matches!(expr.value.as_ref(),
                Expr::Constant(value) if matches!(value.value, Constant::Str(_))));
            let assignment = matches!(&stmt, Stmt::Assign(_) | Stmt::AnnAssign(_));
            if doc && (index == 0 || self.attribute_docs && previous_assignment) {
                let range = stmt.range();
                self.remove[self.current].push((range.start().into(), range.end().into()));
            } else if Self::placeholder(&stmt) {
                let range = stmt.range();
                self.remove[self.current].push((range.start().into(), range.end().into()));
            } else {
                self.visit_stmt(stmt);
            }
            previous_assignment = assignment;
        }
    }

    fn unit(
        &mut self,
        body: Vec<Stmt>,
        range: (usize, usize),
        attribute_docs: bool,
        args: Option<&ast::Arguments>,
    ) {
        for unit in &mut self.remove {
            unit.push(range);
        }
        let parent = self.current;
        let parent_docs = self.attribute_docs;
        let parent_imports = self.local_imports;
        let parent_aliases = self.alias_names.clone();
        let parent_modules = self.typing_modules.clone();
        let parent_dict_names = self.typed_dict_names.clone();
        let parent_dict_modules = self.typed_dict_modules.clone();
        self.keep.push(Vec::new());
        self.remove.push(Vec::new());
        self.current = self.keep.len() - 1;
        self.attribute_docs = attribute_docs;
        self.local_imports = !attribute_docs;
        if let Some(args) = args {
            self.forget_arguments(args);
        }
        self.suite(body);
        self.current = parent;
        self.attribute_docs = parent_docs;
        self.local_imports = parent_imports;
        self.alias_names = parent_aliases;
        self.typing_modules = parent_modules;
        self.typed_dict_names = parent_dict_names;
        self.typed_dict_modules = parent_dict_modules;
    }
}

impl Visitor for Selection {
    fn visit_stmt(&mut self, node: Stmt) {
        let range = node.range();
        let span = (range.start().into(), range.end().into());
        match node {
            Stmt::FunctionDef(def) => {
                self.unit(def.body, span, false, Some(&def.args));
                self.alias_names.remove(def.name.as_str());
                self.typing_modules.remove(def.name.as_str());
                self.typed_dict_names.remove(def.name.as_str());
                self.typed_dict_modules.remove(def.name.as_str());
            }
            Stmt::AsyncFunctionDef(def) => {
                self.unit(def.body, span, false, Some(&def.args));
                self.alias_names.remove(def.name.as_str());
                self.typing_modules.remove(def.name.as_str());
                self.typed_dict_names.remove(def.name.as_str());
                self.typed_dict_modules.remove(def.name.as_str());
            }
            Stmt::ClassDef(def) => {
                if def.bases.iter().any(|base| self.is_typed_dict(base)) {
                    self.schema_fields(&def.body);
                }
                self.unit(def.body, span, true, None);
                self.alias_names.remove(def.name.as_str());
                self.typing_modules.remove(def.name.as_str());
                self.typed_dict_names.remove(def.name.as_str());
                self.typed_dict_modules.remove(def.name.as_str());
            }
            Stmt::Import(_) | Stmt::ImportFrom(_) => {
                self.import_bindings(&node);
                if self.local_imports {
                    self.keep[self.current].push(span);
                } else {
                    self.remove[self.current].push(span);
                }
            }
            Stmt::TypeAlias(def) => {
                self.remove[self.current].push(span);
                self.forget_target(&def.name);
            }
            Stmt::AnnAssign(def) if def.value.is_none() || self.is_type_alias(&def.annotation) => {
                self.remove[self.current].push(span);
                self.forget_target(&def.target);
            }
            Stmt::AnnAssign(def) => {
                self.keep[self.current].push(span);
                self.forget_target(&def.target);
            }
            Stmt::Assign(def) => {
                let mut schema = false;
                if def.targets.len() == 1 && matches!(&def.targets[0], Expr::Name(_)) {
                    if let Expr::Call(call) = def.value.as_ref() {
                        if self.is_typed_dict(&call.func) {
                            self.functional_schema(call);
                            schema = true;
                        }
                    }
                }
                if schema {
                    self.remove[self.current].push(span);
                } else {
                    self.keep[self.current].push(span);
                }
                for target in &def.targets {
                    self.forget_target(target);
                }
            }
            other => {
                self.keep[self.current].push(span);
                // Nested definitions in conditional suites have separate ownership.
                self.generic_visit_stmt(other);
            }
        }
    }
}

fn compact_units(
    text: &str,
    path: &str,
    starts: &[usize],
    spans: impl Iterator<Item = (Vec<(usize, usize)>, Vec<(usize, usize)>)>,
) -> Result<Vec<Unit>, String> {
    let mut result = Vec::new();
    for (keep, remove) in spans {
        let mut bytes = vec![b' '; text.len()];
        for (begin, end) in keep {
            bytes[begin..end].copy_from_slice(&text.as_bytes()[begin..end]);
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
                columns: None,
            });
        }
    }
    Ok(result)
}

pub fn prepare(text: &str, path: &str) -> Result<Views, String> {
    let suite = ast::Suite::parse(text, path).map_err(|error| format!("{path}: {error}"))?;
    let mut selection = Selection {
        keep: vec![Vec::new()],
        remove: vec![Vec::new()],
        attribute_docs: true,
        text: text.to_owned(),
        ..Selection::default()
    };
    selection.suite(suite);
    if let Some(error) = selection.error.take() {
        return Err(format!("{path}: {error}"));
    }
    let starts: Vec<usize> = std::iter::once(0)
        .chain(text.match_indices('\n').map(|(i, _)| i + 1))
        .collect();
    Ok(Views {
        behavior: compact_units(
            text,
            path,
            &starts,
            selection.keep.into_iter().zip(selection.remove),
        )?,
        schemas: compact_units(
            text,
            path,
            &starts,
            selection.schemas.into_iter().map(|run| (run, Vec::new())),
        )?
        .into_iter()
        .chain(selection.canonical_schemas)
        .collect(),
    })
}

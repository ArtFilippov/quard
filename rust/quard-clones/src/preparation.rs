//! Python syntax selection and reversible per-unit line maps.
use rustpython_ast::{Constant, Expr, Ranged, Stmt, Visitor};
use rustpython_parser::{ast, Parse};
use std::collections::HashSet;

pub struct Unit {
    pub text: String,
    pub lines: Vec<usize>,
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

    fn schema_fields(&mut self, body: &[Stmt]) {
        let mut run = Vec::new();
        for stmt in body {
            if let Stmt::AnnAssign(field) = stmt {
                if field.value.is_none()
                    && matches!(field.target.as_ref(), Expr::Name(_))
                    && !self.is_type_alias(&field.annotation)
                {
                    let range = stmt.range();
                    run.push((range.start().into(), range.end().into()));
                    continue;
                }
            }
            if !run.is_empty() {
                self.schemas.push(std::mem::take(&mut run));
            }
        }
        if !run.is_empty() {
            self.schemas.push(run);
        }
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
                self.keep[self.current].push(span);
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
        ..Selection::default()
    };
    selection.suite(suite);
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
        )?,
    })
}

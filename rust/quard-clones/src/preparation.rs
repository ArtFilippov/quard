//! Python syntax selection and reversible per-unit line maps.
use rustpython_ast::{Constant, Expr, Ranged, Stmt, Visitor};
use rustpython_parser::{ast, Parse};
use std::collections::HashSet;

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
    alias_names: HashSet<String>,
    typing_modules: HashSet<String>,
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
                    if matches!(module, "typing" | "typing_extensions") {
                        self.typing_modules.insert(binding.to_owned());
                    }
                }
            }
            Stmt::ImportFrom(node) => {
                for alias in &node.names {
                    let binding = alias.asname.as_ref().unwrap_or(&alias.name).as_str();
                    self.alias_names.remove(binding);
                    self.typing_modules.remove(binding);
                    if node.level.is_none_or(|level| level.to_u32() == 0)
                        && node.module.as_ref().is_some_and(|module| {
                            matches!(module.as_str(), "typing" | "typing_extensions")
                        })
                        && alias.name.as_str() == "TypeAlias"
                    {
                        self.alias_names.insert(binding.to_owned());
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
        }
        for argument in [&args.vararg, &args.kwarg].into_iter().flatten() {
            self.alias_names.remove(argument.arg.as_str());
            self.typing_modules.remove(argument.arg.as_str());
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
            }
            Stmt::AsyncFunctionDef(def) => {
                self.unit(def.body, span, false, Some(&def.args));
                self.alias_names.remove(def.name.as_str());
                self.typing_modules.remove(def.name.as_str());
            }
            Stmt::ClassDef(def) => {
                self.unit(def.body, span, true, None);
                self.alias_names.remove(def.name.as_str());
                self.typing_modules.remove(def.name.as_str());
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
